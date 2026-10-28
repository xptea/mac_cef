"""Redacted cache checks; no Chromium download or diagnostic upload."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import platform
import re
import struct
import plistlib


def compiler_target(version):
    match = re.search(r"^Target:\s*(\S+)", version, re.M)
    return match[1] if match else "unknown"


def executable_architectures(header):
    """Read Mach-O program headers without executing cached tools."""
    if len(header) < 16:
        return set()
    cpus = {0x01000007: "x86_64", 0x0100000C: "arm64"}
    thin = {b"\xcf\xfa\xed\xfe": "<", b"\xfe\xed\xfa\xcf": ">",
            b"\xce\xfa\xed\xfe": "<", b"\xfe\xed\xfa\xce": ">"}
    if header[:4] in thin:
        cpu, _, kind = struct.unpack_from(thin[header[:4]] + "III", header, 4)
        return {cpus.get(cpu, "unknown")} if kind == 2 else set()
    # Universal headers do not identify each slice's file type. The caller
    # treats these conservatively as programs; this cannot hide ARM-only tools.
    fat = {b"\xca\xfe\xba\xbe": (">", 20), b"\xbe\xba\xfe\xca": ("<", 20),
           b"\xca\xfe\xba\xbf": (">", 32), b"\xbf\xba\xfe\xca": ("<", 32)}
    if header[:4] not in fat:
        return set()
    endian, stride = fat[header[:4]]
    count = struct.unpack_from(endian + "I", header, 4)[0]
    if not 1 <= count <= 32 or len(header) < 8 + stride * count:
        raise ValueError("Invalid universal Mach-O header")
    return {cpus.get(struct.unpack_from(endian + "I", header, 8 + stride*i)[0], "unknown")
            for i in range(count)}


def main():
    repo = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkpoint-manifest", type=Path, default=repo / "recovery.json")
    args = parser.parse_args()
    recovery = json.loads(args.checkpoint_manifest.read_text())
    assert re.fullmatch(r"cef-(?:proof|build)-v1-[0-9]+-[0-9]+", recovery["key"]), "Invalid cache key"
    assert re.fullmatch(r"[0-9a-f]{64}", recovery["sha256"]), "Invalid cache checksum"
    assert re.fullmatch(r"[0-9a-f]{64}", recovery["recipe_sha256"]), "Invalid recipe checksum"
    root = Path.home() / "cef-checkpoint"
    archive = root / "checkpoint.tar.zst"
    h = hashlib.sha256()
    with archive.open("rb") as stream:
        for block in iter(lambda: stream.read(1024*1024), b""):
            h.update(block)
    assert h.hexdigest() == recovery["sha256"], "Checkpoint checksum mismatch"
    subprocess.run(["gtar", "--use-compress-program=zstd", "-xf", str(archive), "-C", str(root)], check=True)
    checkpoint = json.loads((root / "checkpoint.json").read_text())
    assert checkpoint["schema"] == 1, "Unsupported checkpoint schema"
    assert checkpoint["identity"]["recipe_sha256"] == recovery["recipe_sha256"], "Recipe checksum mismatch"
    out = (root / checkpoint["out_relative"]).resolve()
    assert out.is_relative_to(root.resolve()), "Output tree outside inspection root"
    print("Saved output count:", len(checkpoint["outputs"]), flush=True)
    if checkpoint.get("build_state", {}).get("full_build_complete"):
        assert checkpoint["build_state"]["remaining_build_steps"] == 0
        app = out / "cefclient.app"
        bundles = [app, *app.glob("Contents/Frameworks/*.app")]
        assert len(bundles) > 1, "Native helper app bundles missing"
        binaries = []
        for bundle in bundles:
            info = plistlib.loads((bundle / "Contents/Info.plist").read_bytes())
            binary = bundle / "Contents/MacOS" / info["CFBundleExecutable"]
            assert binary.is_file() and binary.stat().st_size > 0, "App executable missing"
            binaries.append(binary)
        frameworks = list(app.glob("Contents/Frameworks/Chromium Embedded Framework.framework"))
        assert frameworks, "CEF framework missing from app bundle"
        for framework in frameworks:
            binaries.append(framework / "Chromium Embedded Framework")
            assert (framework / "Resources").is_dir(), "Framework resources missing"
            assert any((framework / "Resources").iterdir()), "Framework resources empty"
        for binary in binaries:
            detail = subprocess.check_output(["file", str(binary)], text=True).strip()
            assert "Mach-O" in detail and "arm64" in detail, detail
            # otool-classic interprets parentheses in helper names as archive-member syntax.
            # file still validates every helper Mach-O; inspect load commands on the app/framework.
            if "(" not in str(binary):
                subprocess.run(["otool", "-L", str(binary)], check=True)
            print("PRESERVED_NATIVE_OUTPUT", json.dumps({"path": binary.relative_to(out).as_posix(),
                  "size": binary.stat().st_size, "sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
                  "file": detail}), flush=True)
        print("FINAL_NATIVE_OUTPUT_VALIDATION: PASS; app, helpers, framework and resources restored. GUI launch, signing and distribution packaging not tested.", flush=True)
    sdk_outputs = {name: record for name, record in checkpoint["outputs"].items() if name.startswith("sdk/")}
    print("SDK-shaped output count:", len(sdk_outputs), flush=True)
    by_resolved = {record["path"]: record for record in checkpoint["dependencies"] if record["base"] == "external"}
    failures = []
    for name, record in sdk_outputs.items():
        path = out / name
        stat = path.stat() if path.is_file() else None
        if stat is None or stat.st_size != record["size"] or stat.st_mtime_ns != record["mtime_ns"]:
            expected_input = by_resolved.get(str(path.resolve()))
            same_content = (stat is not None and expected_input is not None and
                            hashlib.sha256(path.read_bytes()).hexdigest() == expected_input["sha256"])
            failures.append({"file": path.name, "exists": stat is not None,
                             "size_matches": stat is not None and stat.st_size == record["size"],
                             "timestamp_matches": stat is not None and stat.st_mtime_ns == record["mtime_ns"],
                             "external_dependency_recorded": expected_input is not None,
                             "content_matches": same_content,
                             "resolves_outside_output_tree": not path.resolve().is_relative_to(out.resolve())})
    print("SDK output metadata mismatches:", len(failures), flush=True)
    for record in failures[:10]:
        print("SDK mismatch:", json.dumps(record), flush=True)
    external = [r for r in checkpoint["dependencies"] if r["base"] == "external"]
    print("External dependency count:", len(external), flush=True)
    checks = {"missing": 0, "content_changed": 0, "timestamp_changed": 0, "all_match": 0}
    for record in external:
        path = Path(record["path"])
        if not path.is_file():
            checks["missing"] += 1
            continue
        stat=path.stat()
        if stat.st_size != record["size"] or hashlib.sha256(path.read_bytes()).hexdigest() != record["sha256"]:
            checks["content_changed"] += 1
        elif stat.st_mtime_ns != record["mtime_ns"]:
            checks["timestamp_changed"] += 1
        else:
            checks["all_match"] += 1
    print("External dependency aggregate:", json.dumps(checks), flush=True)

    print("Inspector host architecture:", platform.machine(), flush=True)
    print("Saved compiler target:", compiler_target(checkpoint["identity"].get("clang_version", "")), flush=True)
    print("Saved GN target:", checkpoint["identity"]["gn_args"].get("target_cpu"), flush=True)
    native_programs = []
    for name in checkpoint["outputs"]:
        path = (out / name).resolve()
        if not path.is_relative_to(out) or not path.is_file():
            continue
        with path.open("rb") as stream:
            architectures = executable_architectures(stream.read(4096))
        if architectures:
            native_programs.append((path.name, architectures))
    intel_incompatible = [(name, arch) for name, arch in native_programs if "x86_64" not in arch]
    print("Cached Mach-O executable count:", len(native_programs), flush=True)
    print("Cached executables without an Intel slice:", len(intel_incompatible), flush=True)
    for name, architectures in intel_incompatible[:10]:
        print("Cached executable:", json.dumps({"name": name, "architectures": sorted(architectures)}), flush=True)
    print("Compatibility scope: archive inspection only; compiler execution, Ninja reuse and speed are not tested.", flush=True)


if __name__ == "__main__":
    main()
