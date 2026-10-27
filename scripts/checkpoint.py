"""Bounded, source-pinned CEF checkpoint proof for disposable GitHub Mac runners."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
import urllib.request

REPO = Path(__file__).resolve().parents[1]
GIB = 1024 ** 3
SOURCE_TIME_NS = 946684800 * 1_000_000_000


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def capture(command, cwd=None, env=None, timeout=180):
    result = subprocess.run([str(v) for v in command], cwd=cwd, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, errors="replace", timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"{shlex.join([str(v) for v in command])}\n{result.stdout[-12000:]}")
    return result.stdout.strip()


def summary(title, value):
    text = "## " + title + "\n\n" + "\n".join(
        f"- **{key}**: {json.dumps(item) if isinstance(item, (dict, list)) else item}"
        for key, item in value.items()) + "\n\n"
    print(text, flush=True)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as stream:
            stream.write(text)


def normalize_sources(src):
    """Stable file AND directory mtimes; leave cached build outputs untouched."""
    count = 0
    folders = []
    for folder, dirs, files in os.walk(src, followlinks=False):
        folder = Path(folder)
        folders.append(folder)
        dirs[:] = [v for v in dirs if v not in (".git", "__pycache__")
                   and not (v == "out" and folder == src)
                   and not (folder / v).is_symlink()]
        for name in files:
            path = folder / name
            if path.is_symlink() or path.suffix == ".pyc":
                continue
            os.utime(path, ns=(SOURCE_TIME_NS, SOURCE_TIME_NS))
            count += 1
    # GN tracks directory listings as inputs too. Set parents last so filesystem
    # traversal or changes to children cannot leave a newer manifest dependency.
    for folder in reversed(folders):
        os.utime(folder, ns=(SOURCE_TIME_NS, SOURCE_TIME_NS))
    return count


def ninja_outputs(out):
    results = {}
    for line in (out / ".ninja_log").read_text(errors="replace").splitlines():
        parts = line.split("\t")
        if len(parts) != 5 or not parts[0].isdigit():
            continue
        name = parts[3]
        path = out / name
        if path.is_file() and not path.is_symlink():
            results[name] = {"size": path.stat().st_size, "mtime_ns": path.stat().st_mtime_ns}
    return results


def samples_for(outputs, limit=100, minimum=10):
    objects = sorted(name for name in outputs if name.endswith(".o"))
    if len(objects) < minimum:
        raise RuntimeError(f"Only {len(objects)} C++ objects completed; need {minimum} for a useful proof.")
    if len(objects) <= limit:
        return objects
    return [objects[i * (len(objects) - 1) // (limit - 1)] for i in range(limit)]


def check_identity(saved, current, compatible_recipe_sha256=None):
    # GitHub may roll out multiple images behind the same runner label. The
    # actual pinned toolchain must match; image metadata alone is not an input.
    compared = dict(saved)
    # Explicit recovery of the checksum-pinned checkpoint from the previous
    # recipe. Never waive compiler, SDK, source, flags, dependency or Ninja checks.
    if compatible_recipe_sha256 and saved.get("recipe_sha256") == compatible_recipe_sha256:
        compared["recipe_sha256"] = current.get("recipe_sha256")
    changed = [k for k in set(compared) | set(current)
               if k != "runner_image" and compared.get(k) != current.get(k)]
    if changed:
        raise RuntimeError("Runner/toolchain/pin identity changed: " + ", ".join(sorted(changed)))
    return saved.get("runner_image") != current.get("runner_image")


def verify_files(root, records):
    for record in records:
        path = root / record["path"] if record["base"] == "root" else Path(record["path"])
        if not path.is_file() or path.stat().st_size != record["size"] or digest(path) != record["sha256"]:
            raise RuntimeError("Checkpoint dependency changed: " + str(path))
        if path.stat().st_mtime_ns != record["mtime_ns"]:
            raise RuntimeError("Dependency timestamp changed: " + str(path))


def verify_outputs(out, records, sdk_root=None):
    aliases = 0
    for name, expected in records.items():
        path = out / name
        resolved = path.resolve()
        if (sdk_root is not None and name.startswith("sdk/xcode_links/")
                and not resolved.is_relative_to(out.resolve())):
            if not resolved.is_relative_to(sdk_root.resolve()) or not path.is_file():
                raise RuntimeError("Invalid installed SDK alias: " + name)
            aliases += 1
            continue
        if not path.is_file() or path.stat().st_size != expected["size"] or path.stat().st_mtime_ns != expected["mtime_ns"]:
            actual = path.stat() if path.is_file() else None
            details = {"exists": actual is not None,
                       "size_matches": actual is not None and actual.st_size == expected["size"],
                       "timestamp_matches": actual is not None and actual.st_mtime_ns == expected["mtime_ns"]}
            raise RuntimeError("Output or nanosecond timestamp was not preserved: " + name + " " + json.dumps(details))
    return aliases


def restore_sdk_timestamps(root, records, sdk_root):
    """Align identical SDK inputs on a disposable runner, never waive content checks."""
    sdk_root = sdk_root.resolve()
    changes = []
    for record in records:
        if record["base"] != "external":
            continue
        path = Path(record["path"]).resolve()
        if not path.is_relative_to(sdk_root):
            continue
        if not path.is_file() or path.stat().st_size != record["size"] or digest(path) != record["sha256"]:
            raise RuntimeError("Pinned SDK dependency content changed: " + path.name)
        if path.stat().st_mtime_ns != record["mtime_ns"]:
            changes.append({**record, "path": str(path)})
    privileged = []
    for record in changes:
        try:
            os.utime(record["path"], ns=(record["mtime_ns"], record["mtime_ns"]))
        except PermissionError:
            privileged.append(record)
    if privileged:
        if sys.platform != "darwin" or os.environ.get("GITHUB_ACTIONS") != "true":
            raise RuntimeError("SDK timestamp repair is restricted to disposable Mac Actions runners.")
        request = root / "sdk-timestamps.json"
        write_json(request, privileged)
        code = "import hashlib,json,os,sys; from pathlib import Path\n" + \
               "base=Path(sys.argv[2]).resolve()\n" + \
               "for r in json.load(open(sys.argv[1])):\n" + \
               " p=Path(r['path']).resolve(); p.relative_to(base)\n" + \
               " assert p.stat().st_size==r['size'] and hashlib.sha256(p.read_bytes()).hexdigest()==r['sha256']\n" + \
               " os.utime(p,ns=(r['mtime_ns'],r['mtime_ns']))\n"
        capture(["sudo", "-n", "/usr/bin/python3", "-c", code, request, sdk_root])
    return len(changes)


def restore_angle_revision_clock(root, src, out, records=()):
    """Restore only version-generator Git clocks after checking its saved revision."""
    header = out / "gen/angle/angle_commit.h"
    if not header.is_file():
        return 0
    angle = src / "third_party/angle"
    content = header.read_text()
    saved_hash = re.search(r'^#define ANGLE_COMMIT_HASH "([0-9a-f]{12})"$', content, re.M)
    if not saved_hash:
        raise RuntimeError("Saved ANGLE version header lacks a verifiable revision.")
    revision = capture(["git", "-C", angle, "rev-parse", "HEAD"])
    if not re.fullmatch(r"[0-9a-f]{40}", revision) or not revision.startswith(saved_hash[1]):
        raise RuntimeError("Saved ANGLE revision differs from the restored checkout.")
    position = re.search(r"^#define ANGLE_COMMIT_POSITION ([0-9]+)$", content, re.M)
    if not position or position[1] != capture(["git", "-C", angle, "rev-list", "HEAD", "--count"]):
        raise RuntimeError("Saved ANGLE commit position differs from the restored checkout.")
    date = re.search(r'^#define ANGLE_COMMIT_DATE "([^"]+)"$', content, re.M)
    if not date or (date[1] != "unknown date" and date[1] != capture(["git", "-C", angle, "show", "-s", "--format=%ci", "HEAD"])):
        raise RuntimeError("Saved ANGLE commit date differs from the restored checkout.")
    head = Path(capture(["git", "-C", angle, "rev-parse", "--path-format=absolute", "--git-path", "HEAD"]))
    paths = [head]
    words = head.read_text().split()
    if words and words[0] == "ref:":
        if len(words) != 2 or not words[1].startswith("refs/"):
            raise RuntimeError("Unsupported ANGLE Git HEAD reference.")
        paths.append(Path(capture(["git", "-C", angle, "rev-parse", "--path-format=absolute", "--git-path", words[1]])))
    saved = {str((root / r["path"]).resolve()): r for r in records if r["base"] == "root"}
    changed = 0
    for path in paths:
        path = path.resolve()
        if not path.is_relative_to(root.resolve()) or not path.is_file():
            raise RuntimeError("ANGLE revision metadata is outside the dedicated build tree.")
        record = saved.get(str(path))
        target_time = SOURCE_TIME_NS
        if record:
            if path.stat().st_size != record["size"] or digest(path) != record["sha256"]:
                raise RuntimeError("Saved ANGLE Git metadata content changed.")
            target_time = record["mtime_ns"]
        if (record and path.stat().st_mtime_ns != target_time) or (not record and path.stat().st_mtime_ns > header.stat().st_mtime_ns):
            os.utime(path, ns=(target_time, target_time))
            changed += 1
    return changed


def pack_chromium_metadata(src):
    """Cache commit/tree metadata without duplicating the checked-out blob content."""
    pack = src / ".git/cef-source-metadata.pack"
    with pack.open("wb") as output:
        result = subprocess.run(["git", "-C", str(src), "pack-objects", "--stdout", "--revs", "--filter=blob:none"],
                                input=b"HEAD\n", stdout=output, stderr=subprocess.PIPE, timeout=300)
    if result.returncode:
        raise RuntimeError("Cannot prepare source Git metadata: " + result.stderr.decode(errors="replace")[-4000:])


def restore_chromium_metadata(src):
    pack = src / ".git/cef-source-metadata.pack"
    (src / ".git/objects").mkdir(parents=True, exist_ok=True)
    capture(["git", "-C", src, "config", "core.repositoryformatversion", "1"])
    capture(["git", "-C", src, "config", "extensions.partialClone", "origin"])
    capture(["git", "-C", src, "config", "remote.origin.promisor", "true"])
    capture(["git", "-C", src, "config", "remote.origin.partialclonefilter", "blob:none"])
    with pack.open("rb") as source:
        result = subprocess.run(["git", "-C", str(src), "index-pack", "--stdin", "--promisor"], stdin=source,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=300)
    if result.returncode:
        raise RuntimeError("Cannot restore source Git metadata: " + result.stdout.decode(errors="replace")[-4000:])


def require_clean_targets(ninja, out, targets, log):
    result = subprocess.run([str(ninja), "-C", str(out), "-n", "-d", "explain", *targets],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            errors="replace", timeout=300)
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(result.stdout, encoding="utf-8")
    if result.returncode or "no work to do." not in result.stdout:
        raise RuntimeError("Completed objects would rebuild. Inspect " + str(log) + "\n" + result.stdout[:7000] + "\n...\n" + result.stdout[-3000:])


def file_bytes(folder):
    return sum(p.stat().st_size for p in folder.rglob("*") if p.is_file() and not p.is_symlink())


class ArchiveTooLarge(RuntimeError):
    pass


def make_archive(root, archive, members, excludes=(), max_bytes=None, timeout=900):
    tar, zstd = shutil.which("gtar"), shutil.which("zstd")
    if not tar or not zstd:
        raise RuntimeError("GNU tar (gtar) and zstd are required to preserve Ninja timestamps.")
    with (root / "archive.log").open("w") as errors:
        options = [value for pattern in excludes for value in ("--exclude", pattern)]
        producer = subprocess.Popen([tar, "--format=posix", *options, "-C", str(root), "-cf", "-", *members],
                                    stdout=subprocess.PIPE, stderr=errors)
        consumer = subprocess.Popen([zstd, "-q", "-T2", "-6", "-f", "-o", str(archive)],
                                    stdin=producer.stdout, stderr=errors)
        producer.stdout.close()
        try:
            started = time.monotonic()
            next_report = started
            while consumer.poll() is None:
                if time.monotonic() >= next_report:
                    size = archive.stat().st_size if archive.exists() else 0
                    print(f"Compressing {archive.name}: {int(time.monotonic()-started)}s; {size/GIB:.2f} GiB written", flush=True)
                    next_report = time.monotonic() + 30
                if max_bytes is not None and archive.exists() and archive.stat().st_size > max_bytes:
                    raise ArchiveTooLarge("Archive exceeded its cache budget.")
                if time.monotonic() - started > timeout:
                    raise RuntimeError("Archive compression exceeded its time budget.")
                time.sleep(1)
            consumer_code = consumer.returncode
            producer_code = producer.wait(timeout=30)
            if max_bytes is not None and archive.stat().st_size > max_bytes:
                raise ArchiveTooLarge("Archive exceeded its cache budget.")
        finally:
            for process in (consumer, producer):
                if process.poll() is None:
                    process.kill()
                    process.wait()
        if consumer_code or producer_code:
            raise RuntimeError("Checkpoint compression failed; inspect archive.log")


def reset_output_tree(root, out):
    """Only discard fresh generated output; the verified archive stays outside it."""
    root = root.resolve()
    if out.is_symlink() or not out.resolve().is_relative_to(root) or out.resolve() == root:
        raise RuntimeError("Refusing to reset an output directory outside the dedicated build root.")
    if out.exists():
        shutil.rmtree(out)


def remaining_steps(ninja, out, log):
    env = dict(os.environ, NINJA_STATUS="[%f/%t] ")
    result = subprocess.run([str(ninja), "-C", str(out), "-n", "cefclient"], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            errors="replace", timeout=300)
    log.write_text(result.stdout, encoding="utf-8")
    if result.returncode or "Regenerating ninja files" in result.stdout:
        raise RuntimeError("Cannot count remaining build steps: " + result.stdout[-10000:])
    if "no work to do." in result.stdout:
        return 0
    totals = [int(v) for v in re.findall(r"(?m)^\[\d+/(\d+)\]", result.stdout)]
    if not totals:
        raise RuntimeError("Ninja did not report a remaining-step total.")
    return max(totals)


def extract_archive(root, archive):
    # Only a checksum-verified checkpoint produced by this workflow is accepted.
    capture(["gtar", "--use-compress-program=zstd", "-tf", archive], timeout=300)
    capture(["gtar", "--use-compress-program=zstd", "-xf", archive, "-C", root], timeout=900)


class Probe:
    def __init__(self, root):
        self.config = read_json(REPO / "cef.json")
        self.root = root.expanduser().resolve()
        if not re.fullmatch(r"/[A-Za-z0-9_./-]+", str(self.root)):
            raise RuntimeError("Use a dedicated Mac build path without spaces.")
        if self.root in (Path("/"), Path.home(), REPO):
            raise RuntimeError("Refusing to use the filesystem, home, or repository root.")
        self.src = self.root / "work/chromium/src"
        self.out = self.src / "out/Release_GN_arm64"
        self.depot = self.root / "tools/depot_tools"
        self.archive = self.root / "checkpoint.tar.zst"
        self.source_archive = Path.home() / "cef-source-cache/source.tar.zst"
        self.env = dict(os.environ)
        self.env.update(DEPOT_TOOLS_UPDATE="0", DEPOT_TOOLS_BOOTSTRAP_PYTHON3="1",
                        CEF_ENABLE_ARM64="1", PYTHONUNBUFFERED="1",
                        DEVELOPER_DIR=self.config["developer_dir"])
        self.env["PATH"] = str(self.depot) + os.pathsep + self.env["PATH"]
        self.gn = dict(self.config["gn_args"])
        self.env["GN_DEFINES"] = " ".join(f"{k}={json.dumps(v)}" for k, v in sorted(self.gn.items()))
        self.env["GN_OUT_CONFIGS"] = "Release_GN_arm64"

    def mac_only(self):
        if sys.platform != "darwin" or platform.machine() != "arm64":
            raise RuntimeError("Run this proof on a native Apple Silicon Mac runner.")
        if os.environ.get("GITHUB_ACTIONS") != "true":
            raise RuntimeError("This probe prepares disposable GitHub-hosted runners only.")

    def identity(self):
        return {
            "cef_commit": self.config["cef_commit"],
            "chromium_commit": self.config["chromium_commit"],
            "depot_tools_commit": self.config["depot_tools_commit"],
            "gn_args": self.gn,
            "root": str(self.root),
            "xcode": capture(["xcodebuild", "-version"], env=self.env),
            "sdk": capture(["xcrun", "--sdk", "macosx", "--show-sdk-version"], env=self.env),
            "sdk_build": capture(["xcrun", "--sdk", "macosx", "--show-sdk-build-version"], env=self.env),
            "sdk_path": capture(["xcrun", "--sdk", "macosx", "--show-sdk-path"], env=self.env),
            "python": platform.python_version(),
            "runner_image": os.environ.get("ImageVersion", ""),
            "recipe_sha256": digest(Path(__file__)),
        }

    def inspect(self):
        self.mac_only()
        data = self.identity()
        data.update(cpu_cores=int(capture(["sysctl", "-n", "hw.logicalcpu"])),
                    ram_gib=round(int(capture(["sysctl", "-n", "hw.memsize"])) / GIB, 2),
                    free_gib=round(shutil.disk_usage(Path.home()).free / GIB, 2),
                    volume=capture(["df", "-h", str(Path.home())]),
                    other_xcodes=[str(p) for p in Path("/Applications").glob("Xcode_*.app")
                                  if not p.is_symlink() and p.resolve() != Path(self.env["DEVELOPER_DIR"]).parents[1].resolve()],
                    gtar=shutil.which("gtar"), zstd=shutil.which("zstd"))
        summary("Mac runner inspection (no engine download)", data)
        if data["sdk"] != self.config["sdk_version"]:
            raise RuntimeError("The selected SDK differs from this CEF pin.")
        capture(["xcrun", "--sdk", "macosx", "metal", "--version"], env=self.env)
        return data

    def reclaim(self):
        self.mac_only()
        # Explicit opt-in; never run on a personal Mac or remove the selected Xcode.
        selected = Path(self.env["DEVELOPER_DIR"]).resolve().parents[1]
        for path in Path("/Applications").glob("Xcode_*.app"):
            if path.is_symlink():
                continue
            resolved = path.resolve()
            if resolved.parent != Path("/Applications") or resolved == selected:
                continue
            if not re.fullmatch(r"Xcode_[0-9.]+[.]app", resolved.name):
                continue
            print("Removing unused runner Xcode: " + str(resolved), flush=True)
            capture(["sudo", "/bin/rm", "-rf", "--", str(resolved)], timeout=300)

    def run(self, command, label, timeout=10800, compile_seconds=None):
        print("> " + shlex.join([str(v) for v in command]), flush=True)
        log = self.root / (label + ".log")
        started = time.monotonic()
        timed_out = False
        with log.open("w") as output:
            process = subprocess.Popen([str(v) for v in command], cwd=self.root, env=self.env,
                                       stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
            try:
                next_report = started
                while process.poll() is None:
                    now = time.monotonic()
                    if now >= next_report:
                        print(f"{label}: {int(now-started)}s; {shutil.disk_usage(self.root).free/GIB:.1f} GiB free", flush=True)
                        output.flush()
                        if log.stat().st_size:
                            with log.open("rb") as recent:
                                recent.seek(max(0, log.stat().st_size - 8192))
                                lines = recent.read().decode("utf-8", errors="replace").splitlines()
                            if lines:
                                print(label + " latest: " + lines[-1][-1200:], flush=True)
                        next_report = now + 30
                    if shutil.disk_usage(self.root).free < self.config["minimum_runtime_free_gib"] * GIB:
                        raise RuntimeError("Low disk space; stopping this command to retain upload space.")
                    if compile_seconds is not None and now - started >= compile_seconds:
                        timed_out = True
                        break
                    if now - started >= timeout:
                        raise RuntimeError(label + " exceeded its setup timeout")
                    time.sleep(1)
            finally:
                if process.poll() is None:
                    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGKILL):
                        try:
                            os.killpg(process.pid, sig)
                        except ProcessLookupError:
                            break
                        try:
                            process.wait(timeout=20 if sig == signal.SIGINT else 5)
                            break
                        except subprocess.TimeoutExpired:
                            pass
                process.wait()
        if timed_out and re.search(r"(?m)^FAILED:", log.read_text(errors="replace")):
            raise RuntimeError("Compiler failure before the checkpoint stop; inspect " + str(log))
        if process.returncode and not timed_out:
            raise RuntimeError(f"{label} failed ({process.returncode}):\n" + log.read_text(errors="replace")[-12000:])
        return {"elapsed_seconds": round(time.monotonic()-started, 1), "bounded_stop": timed_out}

    def bootstrap_depot(self):
        # Disabling depot_tools updates also suppresses gclient's automatic
        # bootstrap. Initialize the pinned checkout without updating its source.
        self.run(["/bin/bash", self.depot / "ensure_bootstrap"],
                 "depot-bootstrap", timeout=900)
        if not (self.depot / "python3_bin_reldir.txt").is_file():
            raise RuntimeError("depot_tools bootstrap did not initialize its Python runtime; inspect depot-bootstrap.log")
        version = capture(["/bin/bash", self.depot / "python-bin/python3", "--version"], env=self.env)
        if not version.startswith("Python 3."):
            raise RuntimeError("Unexpected depot_tools Python runtime: " + version)
        if capture(["git", "-C", self.depot, "rev-parse", "HEAD"]) != self.config["depot_tools_commit"]:
            raise RuntimeError("depot_tools revision changed during bootstrap.")
        print("Pinned depot_tools initialized: " + version, flush=True)

    def source_key(self):
        identity = self.identity()
        identity.pop("runner_image")
        identity.pop("recipe_sha256")
        encoded = json.dumps({"format": 2, "identity": identity,
                              "automate_sha256": self.config["automate_sha256"]}, sort_keys=True).encode()
        return "cef-source-v2-" + hashlib.sha256(encoded).hexdigest()[:32]

    def restore_source(self):
        if not self.source_archive.is_file():
            return False
        checksum_file = self.source_archive.with_suffix(".sha256")
        checksum = checksum_file.read_text().strip()
        if not re.fullmatch(r"[0-9a-f]{64}", checksum) or digest(self.source_archive) != checksum:
            raise RuntimeError("Prepared-source cache checksum mismatch.")
        extract_archive(self.root, self.source_archive)
        restore_chromium_metadata(self.src)
        manifest = read_json(self.root / "source-cache.json")
        if manifest["key"] != self.source_key():
            raise RuntimeError("Prepared-source cache identity mismatch.")
        print("Prepared-source cache restored; skipping Chromium download and gclient sync.", flush=True)
        return True

    def save_source(self):
        if self.source_archive.is_file():
            return
        # Report relative tree sizes so an oversized setup cache is diagnosable.
        roots = [self.root / "tools", self.root / "work/cef", self.src / ".git"]
        roots += [p for p in self.src.iterdir() if p.is_dir() and p.name not in (".git", "out", "third_party")]
        third_party = self.src / "third_party"
        roots += [p for p in third_party.iterdir() if p.is_dir()] if third_party.is_dir() else []
        sizes = []
        for line in capture(["du", "-sk", *[p for p in roots if p.exists()]], timeout=300).splitlines():
            size, path = line.split("\t", 1)
            sizes.append((int(size), Path(path).relative_to(self.root).as_posix()))
        summary("Largest prepared-source components before compression", {
            path: round(size / 1024 ** 2, 3) for size, path in sorted(sizes, reverse=True)[:15]
        })
        pack_chromium_metadata(self.src)
        write_json(self.root / "source-cache.json", {"key": self.source_key()})
        self.source_archive.parent.mkdir(parents=True, exist_ok=True)
        # Keep source+tools inside the included repository cache budget. Existing
        # compiled checkpoints are neither removed nor included in this archive.
        try:
            make_archive(self.root, self.source_archive,
                         [".cef-checkpoint-probe", "source-cache.json", "tools", "work"],
                         excludes=["work/chromium/src/out", "work/out_*", "work/chromium/src/.git/objects"],
                         max_bytes=6 * GIB, timeout=1200)
        except ArchiveTooLarge:
            self.source_archive.unlink(missing_ok=True)
            summary("Source cache size limit", {
                "result": "Prepared source exceeded the 6 GiB budget; compiled cache is untouched.",
                "action": "Measure a smaller source/tool cache before retrying cold setup again.",
            })
            return
        checksum = digest(self.source_archive)
        self.source_archive.with_suffix(".sha256").write_text(checksum + "\n")
        if os.environ.get("GITHUB_OUTPUT"):
            with open(os.environ["GITHUB_OUTPUT"], "a") as stream:
                stream.write("ready=true\n")
        summary("Reusable source cache ready", {
            "compressed_gib": round(self.source_archive.stat().st_size / GIB, 3),
            "purpose": "Skip source downloads and dependency sync on subsequent jobs.",
        })

    def prepare(self, reclaim=False):
        data = self.inspect()
        if reclaim:
            self.reclaim()
        free = shutil.disk_usage(Path.home()).free / GIB
        if free < self.config["minimum_prepare_free_gib"]:
            raise RuntimeError(f"Only {free:.1f} GiB free; this proof requires {self.config['minimum_prepare_free_gib']} GiB before source setup. Review the inspection and opt-in Xcode cleanup.")
        marker = self.root / ".cef-checkpoint-probe"
        if self.root.exists() and any(self.root.iterdir()) and not marker.exists():
            raise RuntimeError("Refusing to adopt an unmarked nonempty build folder.")
        self.root.mkdir(parents=True, exist_ok=True)
        marker.touch()
        warm_source = self.restore_source()
        tools = self.root / "tools"
        tools.mkdir(exist_ok=True)
        automate = tools / "automate-git.py"
        url = f"https://raw.githubusercontent.com/chromiumembedded/cef/{self.config['cef_commit']}/tools/automate/automate-git.py"
        if not automate.is_file():
            with urllib.request.urlopen(url, timeout=120) as source, automate.open("wb") as target:
                shutil.copyfileobj(source, target)
        if digest(automate) != self.config["automate_sha256"]:
            raise RuntimeError("CEF automation checksum mismatch.")
        if not self.depot.exists():
            self.run(["git", "init", str(self.depot)], "depot-init", timeout=60)
            self.run(["git", "-C", self.depot, "remote", "add", "origin",
                      "https://chromium.googlesource.com/chromium/tools/depot_tools.git"], "depot-remote", timeout=60)
            self.run(["git", "-C", self.depot, "fetch", "--depth=1", "origin",
                      self.config["depot_tools_commit"]], "depot-fetch", timeout=300)
            self.run(["git", "-C", self.depot, "checkout", "--detach", "FETCH_HEAD"], "depot-checkout", timeout=60)
        if capture(["git", "-C", self.depot, "rev-parse", "HEAD"]) != self.config["depot_tools_commit"]:
            raise RuntimeError("Unexpected depot_tools revision.")
        if warm_source:
            capture(["/bin/bash", self.depot / "python-bin/python3", "--version"], env=self.env)
        else:
            self.bootstrap_depot()
            self.run([sys.executable, automate, f"--download-dir={self.root / 'work'}",
                      f"--depot-tools-dir={self.depot}", f"--branch={self.config['branch']}",
                      f"--checkout={self.config['cef_commit']}", "--arm64-build", "--with-pgo-profiles",
                      "--no-debug-build", "--fast-update", "--no-depot-tools-update",
                      "--no-chromium-history", "--no-build", "--no-distrib", "--force-update"],
                     "source-setup", timeout=10800)
        for folder, expected in [(self.src, self.config["chromium_commit"]),
                                 (self.root / "work/cef", self.config["cef_commit"])]:
            if capture(["git", "-C", folder, "rev-parse", "HEAD"]) != expected:
                raise RuntimeError("Pinned source checkout mismatch: " + str(folder))
        normalize_sources(self.src)
        self.run([sys.executable, self.src / "cef/tools/gclient_hook.py"], "configure", timeout=1200)
        normalized = normalize_sources(self.src)
        args = (self.out / "args.gn").read_text()
        for key, value in self.gn.items():
            if not re.search(r"(?m)^\s*" + re.escape(key) + r"\s*=\s*" + re.escape(json.dumps(value)) + r"\s*(?:#.*)?$", args):
                raise RuntimeError("Generated GN settings differ: " + key)
        data = self.identity()
        data["clang_version"] = capture([self.src / "third_party/llvm-build/Release+Asserts/bin/clang", "--version"])
        write_json(self.root / "identity.json", data)
        summary("Pinned shallow CEF source prepared", {
            "source_files_with_stable_timestamps": normalized,
            "free_gib": round(shutil.disk_usage(self.root).free / GIB, 2),
            "gn_args": self.gn,
            "sccache": "Not used; this proof preserves actual Ninja outputs.",
        })

    def ninja(self):
        return self.src / "third_party/ninja/ninja"

    def compile(self, seconds):
        if not 60 <= seconds <= 14400:
            raise RuntimeError("Compilation windows must be between 60 and 14400 seconds.")
        deadline = os.environ.get("CEF_JOB_DEADLINE")
        if deadline:
            seconds = min(seconds, int(float(deadline) - time.time() - 1200))
            if seconds < 60:
                raise RuntimeError("Not enough job time remains for another compile window and safe checkpoint save.")
        if read_json(self.root / "identity.json")["recipe_sha256"] != digest(Path(__file__)):
            raise RuntimeError("Prepare the same recipe before compiling.")
        before = remaining_steps(self.ninja(), self.out, self.root / "remaining-before.log")
        summary("CEF compile starting", {"remaining_build_steps": before, "window_seconds": seconds})
        result = self.run([self.ninja(), "-C", self.out, "-j", str(self.config["jobs"]), "cefclient"],
                          "partial-compile", timeout=seconds + 60, compile_seconds=seconds)
        outputs = ninja_outputs(self.out)
        samples = samples_for(outputs)
        require_clean_targets(self.ninja(), self.out, samples, self.root / "baseline-dry-run.log")
        after = remaining_steps(self.ninja(), self.out, self.root / "remaining-after.log")
        result.update(remaining_build_steps=after, completed_build_steps_this_job=before-after,
                      full_build_complete=after == 0)
        write_json(self.root / "compile-result.json", result)
        if os.environ.get("GITHUB_OUTPUT"):
            with open(os.environ["GITHUB_OUTPUT"], "a") as stream:
                stream.write("complete=" + str(after == 0).lower() + "\n")
        summary("Partial CEF compilation complete", {**result, "completed_outputs": len(outputs),
                                                    "sampled_cpp_objects": len(samples)})

    def save(self):
        outputs = ninja_outputs(self.out)
        samples = samples_for(outputs)
        require_clean_targets(self.ninja(), self.out, samples, self.root / "baseline-dry-run.log")
        paths = set()
        deps = capture([self.ninja(), "-C", self.out, "-t", "deps"], timeout=300)
        for line in deps.splitlines():
            if line.startswith("    "):
                paths.add(line[4:].strip())
        paths.update(capture([self.ninja(), "-C", self.out, "-t", "inputs", *samples], timeout=300).splitlines())
        paths.update(samples)
        records = []
        for name in sorted(paths):
            path = (self.out / name).resolve()
            if not path.is_file():
                continue
            try:
                relative = path.relative_to(self.root).as_posix()
                base = "root"
            except ValueError:
                relative, base = str(path), "external"
            stat = path.stat()
            records.append({"base": base, "path": relative, "size": stat.st_size,
                            "mtime_ns": stat.st_mtime_ns, "sha256": digest(path)})
        build_state = read_json(self.root / "compile-result.json") if (self.root / "compile-result.json").is_file() else {}
        checkpoint = {"schema": 1, "build_state": build_state, "identity": read_json(self.root / "identity.json"),
                      "outputs": outputs, "samples": samples, "dependencies": records,
                      "out_relative": self.out.relative_to(self.root).as_posix()}
        write_json(self.root / "checkpoint.json", checkpoint)
        raw = file_bytes(self.out)
        make_archive(self.root, self.archive, [checkpoint["out_relative"], "checkpoint.json"])
        size = self.archive.stat().st_size
        if size > self.config["max_checkpoint_gib"] * GIB:
            raise RuntimeError(f"Checkpoint is {size/GIB:.2f} GiB; above this proof's free-cache limit.")
        checksum = digest(self.archive)
        if os.environ.get("GITHUB_OUTPUT"):
            with open(os.environ["GITHUB_OUTPUT"], "a") as stream:
                stream.write("sha256=" + checksum + "\n")
        receipt = {"key": os.environ.get("CEF_CHECKPOINT_KEY", ""), "sha256": checksum,
                   "recipe_sha256": checkpoint["identity"]["recipe_sha256"],
                   "completed_outputs": len(outputs), **build_state}
        write_json(self.root / "receipt.json", receipt)
        print("CEF_CHECKPOINT_RECEIPT " + json.dumps(receipt, sort_keys=True), flush=True)
        summary("Checkpoint ready for a fresh runner", {**build_state,
            "completed_outputs": len(outputs), "sampled_cpp_objects": len(samples),
            "hashed_dependencies": len(records), "uncompressed_gib": round(raw/GIB, 3),
            "checkpoint_gib": round(size/GIB, 3), "sha256": checksum,
            "full_build_fit": "Not established by this partial sample.",
        })

    def verify(self, expected, recovery=None):
        if recovery is not None and expected != recovery["sha256"]:
            raise RuntimeError("Recovery checksum differs from the approved checkpoint.")
        current = read_json(self.root / "identity.json")
        if not re.fullmatch(r"[0-9a-f]{64}", expected) or digest(self.archive) != expected:
            raise RuntimeError("Downloaded checkpoint checksum mismatch.")
        reset_output_tree(self.root, self.out)
        extract_archive(self.root, self.archive)
        checkpoint = read_json(self.root / "checkpoint.json")
        if checkpoint["schema"] != 1 or checkpoint["out_relative"] != self.out.relative_to(self.root).as_posix():
            raise RuntimeError("Unexpected checkpoint layout.")
        compatible_recipe = None
        if recovery is not None:
            compatible_recipe = recovery["recipe_sha256"]
            if checkpoint["identity"].get("recipe_sha256") != compatible_recipe:
                raise RuntimeError("Recovery recipe differs from the approved checkpoint.")
        image_changed = check_identity(checkpoint["identity"], current, compatible_recipe)
        sdk_root = Path(self.config["developer_dir"])
        normalized_sdk_inputs = restore_sdk_timestamps(self.root, checkpoint["dependencies"], sdk_root)
        sdk_aliases = verify_outputs(self.out, checkpoint["outputs"], sdk_root)
        angle_clocks = restore_angle_revision_clock(self.root, self.src, self.out, checkpoint["dependencies"])
        verify_files(self.root, checkpoint["dependencies"])
        require_clean_targets(self.ninja(), self.out, checkpoint["samples"], self.root / "restored-dry-run.log")
        report = {"result": "PASS", "recovered_existing_checkpoint": recovery is not None,
                  "runner_image_changed": image_changed,
                  "saved_runner_image": checkpoint["identity"].get("runner_image"),
                  "restored_runner_image": current.get("runner_image"),
                  "completed_outputs_preserved": len(checkpoint["outputs"])-sdk_aliases,
                  "sdk_alias_outputs": sdk_aliases, "identical_sdk_inputs_retimestamped": normalized_sdk_inputs,
                  "verified_angle_revision_clocks_restored": angle_clocks,
                  "sampled_cpp_objects_requiring_recompile": 0, "sampled_cpp_objects": len(checkpoint["samples"]),
                  "verified_dependencies": len(checkpoint["dependencies"]),
                  "checkpoint_gib": round(self.archive.stat().st_size/GIB, 3),
                  "scope": "Actual partial CEF compile; full checkpoint size and final linking remain untested."}
        write_json(self.root / "verification.json", report)
        summary("Fresh-runner CEF checkpoint verification", report)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("inspect", "source-key", "save-source", "prepare", "compile", "save", "verify"), nargs="?", default="inspect")
    parser.add_argument("--root", type=Path, default=Path.home() / "cef-checkpoint")
    parser.add_argument("--reclaim-unused-xcodes", action="store_true")
    parser.add_argument("--seconds", type=int, default=1200)
    parser.add_argument("--expected-sha256", default="")
    parser.add_argument("--checkpoint-manifest", type=Path,
                        help="Checksum-pinned checkpoint receipt supplied by the build workflow.")
    parser.add_argument("--recover-existing", action="store_true",
                        help="Verify the exact checkpoint approved in recovery.json; never compiles.")
    args = parser.parse_args()
    probe = Probe(args.root)
    probe.mac_only()
    if args.action == "inspect":
        probe.inspect()
    elif args.action == "source-key":
        key = probe.source_key()
        print(key)
        if os.environ.get("GITHUB_OUTPUT"):
            with open(os.environ["GITHUB_OUTPUT"], "a") as stream:
                stream.write("key=" + key + "\n")
    elif args.action == "save-source":
        probe.save_source()
    elif args.action == "prepare":
        probe.prepare(args.reclaim_unused_xcodes)
    elif args.action == "compile":
        probe.compile(args.seconds)
    elif args.action == "save":
        probe.save()
    else:
        recovery = read_json(args.checkpoint_manifest) if args.checkpoint_manifest else (
            read_json(REPO / "recovery.json") if args.recover_existing else None)
        probe.verify(args.expected_sha256, recovery)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        summary("CEF checkpoint proof stopped", {"error": str(error)})
        raise SystemExit(1)

