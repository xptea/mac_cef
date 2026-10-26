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
    """Identical pinned inputs get stable mtimes; cached generated outputs keep theirs."""
    count = 0
    for folder, dirs, files in os.walk(src, followlinks=False):
        dirs[:] = [v for v in dirs if v not in (".git", "__pycache__")
                   and not (v == "out" and Path(folder) == src)]
        for name in files:
            path = Path(folder) / name
            if path.is_symlink() or path.suffix == ".pyc":
                continue
            os.utime(path, ns=(SOURCE_TIME_NS, SOURCE_TIME_NS))
            count += 1
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


def check_identity(saved, current):
    if saved != current:
        changed = [k for k in set(saved) | set(current) if saved.get(k) != current.get(k)]
        raise RuntimeError("Runner/toolchain/pin identity changed: " + ", ".join(sorted(changed)))


def verify_files(root, records):
    for record in records:
        path = root / record["path"] if record["base"] == "root" else Path(record["path"])
        if not path.is_file() or path.stat().st_size != record["size"] or digest(path) != record["sha256"]:
            raise RuntimeError("Checkpoint dependency changed: " + str(path))
        if path.stat().st_mtime_ns != record["mtime_ns"]:
            raise RuntimeError("Dependency timestamp changed: " + str(path))


def verify_outputs(out, records):
    for name, expected in records.items():
        path = out / name
        if not path.is_file() or path.stat().st_size != expected["size"] or path.stat().st_mtime_ns != expected["mtime_ns"]:
            raise RuntimeError("Output or nanosecond timestamp was not preserved: " + name)


def require_clean_targets(ninja, out, targets, log):
    result = subprocess.run([str(ninja), "-C", str(out), "-n", "-d", "explain", *targets],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            errors="replace", timeout=300)
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(result.stdout, encoding="utf-8")
    if result.returncode or "no work to do." not in result.stdout:
        raise RuntimeError("Completed objects would rebuild. Inspect " + str(log) + "\n" + result.stdout[-10000:])


def file_bytes(folder):
    return sum(p.stat().st_size for p in folder.rglob("*") if p.is_file() and not p.is_symlink())


def make_archive(root, archive, members):
    tar, zstd = shutil.which("gtar"), shutil.which("zstd")
    if not tar or not zstd:
        raise RuntimeError("GNU tar (gtar) and zstd are required to preserve Ninja timestamps.")
    with (root / "archive.log").open("w") as errors:
        producer = subprocess.Popen([tar, "--format=posix", "-C", str(root), "-cf", "-", *members],
                                    stdout=subprocess.PIPE, stderr=errors)
        consumer = subprocess.Popen([zstd, "-q", "-T2", "-6", "-f", "-o", str(archive)],
                                    stdin=producer.stdout, stderr=errors)
        producer.stdout.close()
        try:
            consumer_code = consumer.wait(timeout=900)
            producer_code = producer.wait(timeout=30)
        finally:
            for process in (consumer, producer):
                if process.poll() is None:
                    process.kill()
                    process.wait()
        if consumer_code or producer_code:
            raise RuntimeError("Checkpoint compression failed; inspect archive.log")


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
        if not 60 <= seconds <= 3600:
            raise RuntimeError("The first checkpoint proof is limited to 60–3600 compilation seconds.")
        if read_json(self.root / "identity.json")["recipe_sha256"] != digest(Path(__file__)):
            raise RuntimeError("Prepare the same recipe before compiling.")
        result = self.run([self.ninja(), "-C", self.out, "-j", str(self.config["jobs"]), "cefclient"],
                          "partial-compile", compile_seconds=seconds)
        outputs = ninja_outputs(self.out)
        samples = samples_for(outputs)
        require_clean_targets(self.ninja(), self.out, samples, self.root / "baseline-dry-run.log")
        write_json(self.root / "compile-result.json", result)
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
        checkpoint = {"schema": 1, "identity": read_json(self.root / "identity.json"),
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
        summary("Checkpoint ready for a fresh runner", {
            "completed_outputs": len(outputs), "sampled_cpp_objects": len(samples),
            "hashed_dependencies": len(records), "uncompressed_gib": round(raw/GIB, 3),
            "checkpoint_gib": round(size/GIB, 3), "sha256": checksum,
            "full_build_fit": "Not established by this partial sample.",
        })

    def verify(self, expected):
        current = read_json(self.root / "identity.json")
        if not re.fullmatch(r"[0-9a-f]{64}", expected) or digest(self.archive) != expected:
            raise RuntimeError("Downloaded checkpoint checksum mismatch.")
        extract_archive(self.root, self.archive)
        checkpoint = read_json(self.root / "checkpoint.json")
        if checkpoint["schema"] != 1 or checkpoint["out_relative"] != self.out.relative_to(self.root).as_posix():
            raise RuntimeError("Unexpected checkpoint layout.")
        check_identity(checkpoint["identity"], current)
        verify_outputs(self.out, checkpoint["outputs"])
        verify_files(self.root, checkpoint["dependencies"])
        require_clean_targets(self.ninja(), self.out, checkpoint["samples"], self.root / "restored-dry-run.log")
        report = {"result": "PASS", "completed_outputs_preserved": len(checkpoint["outputs"]),
                  "sampled_cpp_objects_requiring_recompile": 0, "sampled_cpp_objects": len(checkpoint["samples"]),
                  "verified_dependencies": len(checkpoint["dependencies"]),
                  "checkpoint_gib": round(self.archive.stat().st_size/GIB, 3),
                  "scope": "Actual partial CEF compile; full checkpoint size and final linking remain untested."}
        write_json(self.root / "verification.json", report)
        summary("Fresh-runner CEF checkpoint verification", report)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("inspect", "prepare", "compile", "save", "verify"), nargs="?", default="inspect")
    parser.add_argument("--root", type=Path, default=Path.home() / "cef-checkpoint")
    parser.add_argument("--reclaim-unused-xcodes", action="store_true")
    parser.add_argument("--seconds", type=int, default=1200)
    parser.add_argument("--expected-sha256", default="")
    args = parser.parse_args()
    probe = Probe(args.root)
    probe.mac_only()
    if args.action == "inspect":
        probe.inspect()
    elif args.action == "prepare":
        probe.prepare(args.reclaim_unused_xcodes)
    elif args.action == "compile":
        probe.compile(args.seconds)
    elif args.action == "save":
        probe.save()
    else:
        probe.verify(args.expected_sha256)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        summary("CEF checkpoint proof stopped", {"error": str(error)})
        raise SystemExit(1)

