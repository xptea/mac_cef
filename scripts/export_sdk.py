"""Package the completed, checksum-pinned ARM checkpoint without rebuilding CEF."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import checkpoint


def main():
    repo = Path(__file__).resolve().parents[1]
    receipt = json.loads((repo / "checkpoint-inspection.json").read_text())
    probe = checkpoint.Probe(Path.home() / "cef-checkpoint")
    assert checkpoint.digest(probe.archive) == receipt["sha256"], "Checkpoint checksum mismatch"
    subprocess.run(["gtar", "--use-compress-program=zstd", "-xf", str(probe.archive),
                    "-C", str(probe.root), "checkpoint.json"], check=True)
    saved = checkpoint.read_json(probe.root / "checkpoint.json")
    assert saved["identity"]["recipe_sha256"] == receipt["recipe_sha256"], "Recipe mismatch"
    assert saved["build_state"]["full_build_complete"] and saved["build_state"]["remaining_build_steps"] == 0
    # Prepare an empty source tree; the saved outputs are restored only after GN configuration.
    (probe.root / ".cef-checkpoint-probe").touch()
    probe.prepare()
    probe.verify(receipt["sha256"], receipt)
    assert checkpoint.remaining_steps(probe.ninja(), probe.out, probe.root / "final-sdk-dry-run.log") == 0
    destination = probe.root / "sdk"
    subprocess.run([sys.executable, str(probe.src / "cef/tools/make_distrib.py"),
                    "--output-dir=" + str(destination), "--ninja-build", "--arm64-build", "--minimal",
                    "--allow-partial", "--no-symbols", "--no-docs", "--no-archive", "--no-format"],
                   cwd=probe.src / "cef", env=probe.env, check=True)
    version = probe.config["cef_version"]
    name = "cef_binary_" + version + "_macosarm64_minimal"
    sdk = destination / name
    required = ["include/cef_version.h", "include/cef_sandbox_mac.h", "cmake/FindCEF.cmake",
                "libcef_dll/CMakeLists.txt", "LICENSE.txt", "CREDITS.html",
                "Release/Chromium Embedded Framework.framework/Chromium Embedded Framework",
                "Release/Chromium Embedded Framework.framework/Libraries/libcef_sandbox.dylib",
                "Release/Chromium Embedded Framework.framework/Resources/icudtl.dat"]
    for item in required:
        assert (sdk / item).is_file(), "Missing SDK file: " + item
    assert '#define CEF_VERSION "' + version + '"' in (sdk / "include/cef_version.h").read_text()
    # Compile the SDK wrapper against the packaged headers/framework as a consumer would.
    build = probe.root / "sdk-wrapper-check"
    subprocess.run(["cmake", "-S", str(sdk), "-B", str(build), "-DPROJECT_ARCH=arm64",
                    "-DCMAKE_BUILD_TYPE=Release", "-DCMAKE_OSX_ARCHITECTURES=arm64"], check=True)
    subprocess.run(["cmake", "--build", str(build), "--target", "libcef_dll_wrapper", "-j", "2"], check=True)
    export = repo / "sdk-export"
    export.mkdir(exist_ok=True)
    archive = export / (name + ".tar.bz2")
    with tarfile.open(archive, "w:bz2") as bundle:
        bundle.add(sdk, arcname=name)
    assert archive.stat().st_size < 400 * 1024**2, "SDK export exceeds bounded free artifact allocation"
    result = {"version": version, "directory": name, "archive": archive.name,
              "sha256": checkpoint.digest(archive), "size": archive.stat().st_size,
              "cef_commit": probe.config["cef_commit"], "chromium_commit": probe.config["chromium_commit"],
              "checkpoint": receipt, "gn_args": probe.config["gn_args"],
              "validation": "Zero Ninja work; native ARM framework restored; packaged C++ wrapper compiled",
              "limitations": "Void Mac backend, GUI launch, codec playback, signing and notarization not tested"}
    (export / "cef-sdk-artifact.json").write_text(json.dumps(result, indent=2) + "\n")
    print("CEF_SDK_EXPORT " + json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
