"""Redacted cache checks; no Chromium download or diagnostic upload."""
import hashlib
import json
from pathlib import Path
import subprocess

repo = Path(__file__).resolve().parents[1]
recovery = json.loads((repo / "recovery.json").read_text())
root = Path.home() / "cef-checkpoint"
archive = root / "checkpoint.tar.zst"
h = hashlib.sha256()
with archive.open("rb") as stream:
    for block in iter(lambda: stream.read(1024*1024), b""):
        h.update(block)
assert h.hexdigest() == recovery["sha256"], "Checkpoint checksum mismatch"
subprocess.run(["gtar", "--use-compress-program=zstd", "-xf", str(archive), "-C", str(root)], check=True)
checkpoint = json.loads((root / "checkpoint.json").read_text())
out = root / checkpoint["out_relative"]
print("Saved output count:", len(checkpoint["outputs"]), flush=True)
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
