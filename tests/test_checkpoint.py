"""Portable checkpoint tests. Local fixtures never download or compile CEF."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import checkpoint as proof


class ScratchCase(unittest.TestCase):
    def setUp(self):
        scratch = Path(__file__).resolve().parents[1] / ".test-work"
        scratch.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=scratch)
        self.root = Path(self.temp.name).resolve()

    def tearDown(self):
        self.temp.cleanup()


class CheckpointTests(ScratchCase):
    def test_content_change_is_rejected_even_with_identical_timestamp(self):
        source = self.root / "header.h"
        source.write_text("one\n")
        os.utime(source, ns=(proof.SOURCE_TIME_NS, proof.SOURCE_TIME_NS))
        record = {"base": "root", "path": "header.h", "size": source.stat().st_size,
                  "mtime_ns": source.stat().st_mtime_ns, "sha256": proof.digest(source)}
        proof.verify_files(self.root, [record])
        source.write_text("two\n")
        os.utime(source, ns=(record["mtime_ns"], record["mtime_ns"]))
        with self.assertRaisesRegex(RuntimeError, "dependency changed"):
            proof.verify_files(self.root, [record])

    def test_changed_toolchain_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "sdk"):
            proof.check_identity({"sdk": "26.5", "cef_commit": "same"},
                                 {"sdk": "26.6", "cef_commit": "same"})

    def test_image_rollout_does_not_reject_identical_toolchain(self):
        identity = {"sdk": "26.5", "clang_version": "pinned clang", "cef_commit": "same",
                    "runner_image": "old-image"}
        restored = {**identity, "runner_image": "new-image"}
        self.assertTrue(proof.check_identity(identity, restored))
        self.assertFalse(proof.check_identity(identity, identity))
        self.assertEqual(identity["runner_image"], "old-image")

    def test_image_rollout_still_rejects_changed_build_inputs(self):
        identity = {"sdk": "26.5", "clang_version": "pinned clang", "cef_commit": "same",
                    "recipe_sha256": "same recipe", "gn_args": {"proprietary_codecs": True},
                    "runner_image": "old-image"}
        for key, value in [("sdk", "different sdk"), ("clang_version", "different compiler"),
                           ("cef_commit", "different source"), ("recipe_sha256", "different recipe"),
                           ("gn_args", {"proprietary_codecs": False})]:
            with self.subTest(key=key):
                restored = {**identity, "runner_image": "new-image", key: value}
                with self.assertRaisesRegex(RuntimeError, key):
                    proof.check_identity(identity, restored)

    def test_output_timestamp_loss_is_rejected(self):
        out = self.root / "out"
        out.mkdir()
        target = out / "unit.o"
        target.write_bytes(b"object")
        record = {"unit.o": {"size": target.stat().st_size, "mtime_ns": target.stat().st_mtime_ns}}
        proof.verify_outputs(out, record)
        changed = record["unit.o"]["mtime_ns"] + 1_000_000
        os.utime(target, ns=(changed, changed))
        with self.assertRaisesRegex(RuntimeError, "timestamp"):
            proof.verify_outputs(out, record)

    def test_sampling_requires_real_completed_objects(self):
        with self.assertRaisesRegex(RuntimeError, "completed"):
            proof.samples_for({"gen/header.h": {}}, minimum=1)

    @unittest.skipUnless(shutil.which("gtar") and shutil.which("zstd"), "GNU tar/zstd unavailable")
    def test_archive_roundtrip_preserves_nanosecond_timestamps(self):
        out = self.root / "out"
        out.mkdir()
        target = out / ".ninja_deps"
        target.write_bytes(b"dependency metadata")
        os.utime(target, ns=(1_700_000_000_123456789, 1_700_000_000_123456789))
        stamp = target.stat().st_mtime_ns
        archive = self.root / "checkpoint.tar.zst"
        proof.make_archive(self.root, archive, ["out"])
        target.unlink()
        proof.extract_archive(self.root, archive)
        self.assertEqual(target.read_bytes(), b"dependency metadata")
        self.assertEqual(target.stat().st_mtime_ns, stamp)


class DepotBootstrapTests(ScratchCase):
    def setUp(self):
        super().setUp()
        self.probe = proof.Probe.__new__(proof.Probe)
        self.probe.depot = self.root / "depot_tools"
        self.probe.depot.mkdir()
        self.probe.env = {"DEPOT_TOOLS_UPDATE": "0", "DEPOT_TOOLS_BOOTSTRAP_PYTHON3": "1"}
        self.probe.config = {"depot_tools_commit": "pinned-revision"}
        self.probe.run = mock.Mock()

    def initialized(self):
        (self.probe.depot / "python3_bin_reldir.txt").write_text("bootstrap/python3/bin")

    def test_missing_python_bootstrap_is_rejected_before_source_setup(self):
        with mock.patch.object(proof, "capture") as capture:
            with self.assertRaisesRegex(RuntimeError, "did not initialize its Python runtime"):
                self.probe.bootstrap_depot()
            capture.assert_not_called()

    def test_broken_bootstrapped_python_is_rejected(self):
        self.initialized()
        with mock.patch.object(proof, "capture", side_effect=RuntimeError("Python cannot start")):
            with self.assertRaisesRegex(RuntimeError, "Python cannot start"):
                self.probe.bootstrap_depot()

    def test_bootstrap_preserves_the_pinned_checkout(self):
        self.probe.run.side_effect = lambda *args, **kwargs: self.initialized()
        with mock.patch.object(proof, "capture", side_effect=["Python 3.11.9", "pinned-revision"]):
            self.probe.bootstrap_depot()
        self.probe.run.assert_called_once_with(
            ["/bin/bash", self.probe.depot / "ensure_bootstrap"], "depot-bootstrap", timeout=900)

    def test_changed_checkout_is_rejected_after_bootstrap(self):
        self.initialized()
        with mock.patch.object(proof, "capture", side_effect=["Python 3.11.9", "changed-revision"]):
            with self.assertRaisesRegex(RuntimeError, "revision changed"):
                self.probe.bootstrap_depot()


class RealNinjaTests(ScratchCase):
    def setUp(self):
        super().setUp()
        self.ninja = os.environ.get("NINJA") or shutil.which("ninja")
        if not self.ninja:
            self.skipTest("Ninja unavailable; set NINJA to run the dependency-tracking fixture")
        self.src = self.root / "src"
        self.out = self.src / "out" / "Release_GN_arm64"
        self.out.mkdir(parents=True)
        self.header = self.src / "shared.h"
        self.header.write_text("shared header\n")
        compiler = self.src / "compiler.py"
        compiler.write_text(
            "import sys\nfrom pathlib import Path\n"
            "source=Path(sys.argv[1]); output=Path(sys.argv[2]); header=source.with_name('shared.h')\n"
            "output.parent.mkdir(parents=True,exist_ok=True)\n"
            "output.write_text(source.read_text()+header.read_text())\n"
            "Path(str(output)+'.d').write_text(str(output)+': '+str(source)+' '+str(header)+'\\n')\n"
            "with source.with_name('calls.txt').open('a') as stream: stream.write(str(output)+'\\n')\n"
        )
        lines = ["rule cc",
                 '  command = "' + sys.executable.replace("\\", "/") + '" "../../compiler.py" $in $out',
                 "  depfile = $out.d", "  deps = gcc"]
        for i in range(5):
            (self.src / f"unit{i}.cc").write_text(f"unit {i}\n")
            lines.append(f"build obj/unit{i}.o: cc ../../unit{i}.cc | ../../compiler.py")
        lines.append("default " + " ".join(f"obj/unit{i}.o" for i in range(5)))
        (self.out / "build.ninja").write_text("\n".join(lines) + "\n")
        proof.normalize_sources(self.src)
        # Build four and deliberately leave the fifth unfinished.
        self.built = [f"obj/unit{i}.o" for i in range(4)]
        proof.capture([self.ninja, "-C", self.out, *self.built])

    def test_restored_work_is_clean_while_unfinished_target_remains(self):
        outputs = proof.ninja_outputs(self.out)
        self.assertEqual(len(outputs), 4)
        samples = proof.samples_for(outputs, limit=3, minimum=3)
        proof.require_clean_targets(self.ninja, self.out, samples, self.root / "baseline.log")
        calls = (self.src / "calls.txt").read_text()
        saved = self.root / "saved-output"
        # Both resolved paths remain in this test's freshly created scratch folder.
        self.assertTrue(self.out.resolve().is_relative_to(self.root))
        self.assertTrue(saved.resolve().is_relative_to(self.root))
        self.out.rename(saved)
        for source in self.src.glob("*.cc"):
            content = source.read_text()
            source.write_text(content)  # A fresh runner's identical checkout gets new mtimes.
        self.header.write_text(self.header.read_text())
        proof.normalize_sources(self.src)
        shutil.copytree(saved, self.out, copy_function=shutil.copy2)
        proof.verify_outputs(self.out, outputs)
        proof.require_clean_targets(self.ninja, self.out, samples, self.root / "restored.log")
        self.assertEqual((self.src / "calls.txt").read_text(), calls)
        remaining = proof.capture([self.ninja, "-C", self.out, "-n", "-v", "obj/unit4.o"])
        self.assertIn("unit4.cc", remaining)

    def test_fresh_input_timestamps_make_ninja_reject_reuse(self):
        source = self.src / "unit0.cc"
        future = time.time_ns() + 10_000_000_000
        os.utime(source, ns=(future, future))
        with self.assertRaisesRegex(RuntimeError, "would rebuild"):
            proof.require_clean_targets(self.ninja, self.out, ["obj/unit0.o"], self.root / "dirty.log")


if __name__ == "__main__":
    unittest.main()

