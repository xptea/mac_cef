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

    def test_normalization_preserves_cached_output_and_git_timestamps(self):
        src = self.root / "src"
        templates = src / "components/policy/resources/templates"
        templates.mkdir(parents=True)
        (templates / "policy.json").write_text("{}")
        excluded = []
        for relative in ["out/Release_GN_arm64", ".git", "__pycache__"]:
            folder = src / relative
            folder.mkdir(parents=True)
            target = folder / "retained"
            target.write_text("cached metadata")
            excluded.extend([folder, target])
        before = {p: p.stat().st_mtime_ns for p in excluded}
        proof.normalize_sources(src)
        for folder in [src, templates, templates.parent, templates.parent.parent]:
            self.assertEqual(folder.stat().st_mtime_ns, proof.SOURCE_TIME_NS)
        self.assertEqual({p: p.stat().st_mtime_ns for p in excluded}, before)

    def test_approved_old_recipe_keeps_all_real_identity_checks(self):
        old = {"recipe_sha256": "approved old recipe", "sdk": "same sdk", "runner_image": "old"}
        current = {**old, "recipe_sha256": "current recipe", "runner_image": "new"}
        with self.assertRaisesRegex(RuntimeError, "recipe_sha256"):
            proof.check_identity(old, current)
        self.assertTrue(proof.check_identity(old, current, "approved old recipe"))
        with self.assertRaisesRegex(RuntimeError, "sdk"):
            proof.check_identity(old, {**current, "sdk": "changed sdk"}, "approved old recipe")
        with self.assertRaisesRegex(RuntimeError, "recipe_sha256"):
            proof.check_identity(old, current, "different old recipe")

    def test_recovery_rejects_unapproved_checksum_before_extracting(self):
        probe = proof.Probe.__new__(proof.Probe)
        with mock.patch.object(proof, "extract_archive") as extract:
            with self.assertRaisesRegex(RuntimeError, "Recovery checksum"):
                probe.verify("other checksum", {"sha256": "approved checkpoint"})
            extract.assert_not_called()

    def test_reset_output_tree_cannot_escape_build_root(self):
        outside=self.root.parent / "outside-build-output"
        with self.assertRaisesRegex(RuntimeError, "outside the dedicated build root"):
            proof.reset_output_tree(self.root, outside)
        with self.assertRaisesRegex(RuntimeError, "outside the dedicated build root"):
            proof.reset_output_tree(self.root, self.root)

    def test_clean_restore_removes_generated_sdk_tree_but_preserves_archive(self):
        out=self.root / "work/chromium/src/out/Release_GN_arm64"
        stale=out / "sdk/xcode_links/SDK"
        stale.mkdir(parents=True)
        (stale / "wrong-header.h").write_text("generated before restore")
        archive=self.root / "checkpoint.tar.zst"
        archive.write_bytes(b"preserved compiled checkpoint")
        proof.reset_output_tree(self.root, out)
        self.assertFalse(out.exists())
        self.assertEqual(archive.read_bytes(), b"preserved compiled checkpoint")

    def test_clean_restore_does_not_follow_sdk_directory_links(self):
        out=self.root / "out"
        out.mkdir()
        external=self.root / "installed-sdk"
        external.mkdir()
        header=external / "header.h"
        header.write_text("installed SDK")
        try:
            (out / "sdk").symlink_to(external, target_is_directory=True)
        except OSError as error:
            self.skipTest("Directory symlinks unavailable: " + str(error))
        proof.reset_output_tree(self.root, out)
        self.assertEqual(header.read_text(), "installed SDK")

    @unittest.skipUnless(shutil.which("gtar") and shutil.which("zstd"), "GNU tar/zstd unavailable")
    def test_source_cache_excludes_compiler_outputs(self):
        work=self.root / "work/chromium/src"
        out=work / "out/Release_GN_arm64"
        out.mkdir(parents=True)
        (out / "completed.o").write_bytes(b"compiled")
        (work / "input.cc").write_text("source")
        archive=self.root / "source.tar.zst"
        proof.make_archive(self.root,archive,["work"],excludes=["work/chromium/src/out"])
        members=proof.capture(["gtar","--use-compress-program=zstd","-tf",archive])
        self.assertIn("input.cc",members)
        self.assertNotIn("completed.o",members)

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


class PreparedSourceTests(ScratchCase):
    def test_source_key_tracks_build_inputs_but_not_image_or_recipe_labels(self):
        probe=proof.Probe.__new__(proof.Probe)
        probe.config={"automate_sha256":"pinned automation"}
        identity={"cef_commit":"same", "gn_args":{"target_cpu":"arm64"},
                  "sdk":"same sdk", "runner_image":"old", "recipe_sha256":"old"}
        probe.identity=mock.Mock(return_value=identity.copy())
        key=probe.source_key()
        probe.identity.return_value={**identity,"runner_image":"new","recipe_sha256":"new"}
        self.assertEqual(probe.source_key(),key)
        probe.identity.return_value={**identity,"cef_commit":"different source"}
        self.assertNotEqual(probe.source_key(),key)

    def test_bad_source_archive_checksum_is_rejected_before_extracting(self):
        probe=proof.Probe.__new__(proof.Probe)
        probe.source_archive=self.root / "source.tar.zst"
        probe.source_archive.write_bytes(b"damaged source archive")
        probe.source_archive.with_suffix(".sha256").write_text("0"*64)
        with mock.patch.object(proof,"extract_archive") as extract:
            with self.assertRaisesRegex(RuntimeError,"source cache checksum"):
                probe.restore_source()
            extract.assert_not_called()

    def test_warm_source_preparation_does_not_run_download_or_bootstrap(self):
        probe=proof.Probe.__new__(proof.Probe)
        probe.root=self.root
        (self.root / ".cef-checkpoint-probe").touch()
        probe.config=proof.read_json(proof.REPO / "cef.json")
        probe.src=self.root / "work/chromium/src"
        probe.out=probe.src / "out/Release_GN_arm64"
        probe.depot=self.root / "tools/depot_tools"
        probe.depot.mkdir(parents=True)
        probe.out.mkdir(parents=True)
        automate=self.root / "tools/automate-git.py"
        automate.write_text("cached verified automation")
        probe.config["automate_sha256"]=proof.digest(automate)
        probe.config["minimum_prepare_free_gib"]=0
        probe.gn={"target_cpu":"arm64"}
        (probe.out / "args.gn").write_text('target_cpu = "arm64"\n')
        probe.env={}
        probe.inspect=mock.Mock(return_value={})
        probe.restore_source=mock.Mock(return_value=True)
        probe.bootstrap_depot=mock.Mock()
        probe.run=mock.Mock()
        probe.identity=mock.Mock(return_value={"test":"pinned identity"})
        def capture(command,**kwargs):
            if str(command[0]) == "git":
                if command[2] == probe.depot:
                    return probe.config["depot_tools_commit"]
                if command[2] == probe.src:
                    return probe.config["chromium_commit"]
                return probe.config["cef_commit"]
            return "Python/compiler version"
        with mock.patch.object(proof,"capture",side_effect=capture), \
             mock.patch.object(proof.urllib.request,"urlopen",side_effect=AssertionError("download requested")):
            probe.prepare()
        probe.bootstrap_depot.assert_not_called()
        self.assertEqual([call.args[1] for call in probe.run.call_args_list],["configure"])


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
        lines.append("build cefclient: phony " + " ".join(f"obj/unit{i}.o" for i in range(5)))
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

    def test_fresh_directory_timestamp_requests_regeneration_then_fix_preserves_objects(self):
        templates = self.src / "components/policy/resources/templates"
        templates.mkdir(parents=True)
        (templates / "policy.json").write_text("{}")
        manifest = self.out / "build.ninja"
        manifest.write_text(manifest.read_text() +
                            "\nrule regen\n"
                            '  command = "' + sys.executable.replace("\\", "/") + '" -c "pass"\n'
                            "  description = Regenerating ninja files\n"
                            "  generator = 1\n"
                            "build build.ninja.stamp: regen ../../components/policy/resources/templates\n"
                            "build build.ninja: phony build.ninja.stamp\n")
        (self.out / "build.ninja.stamp").write_text("saved GN generation")
        outputs = proof.ninja_outputs(self.out)
        calls = (self.src / "calls.txt").read_text()
        future = time.time_ns() + 10_000_000_000
        os.utime(templates, ns=(future, future))
        with self.assertRaisesRegex(RuntimeError, "Regenerating ninja files"):
            proof.require_clean_targets(self.ninja, self.out, self.built, self.root / "directory-dirty.log")
        proof.normalize_sources(self.src)
        proof.require_clean_targets(self.ninja, self.out, self.built, self.root / "directory-restored.log")
        proof.verify_outputs(self.out, outputs)
        self.assertEqual((self.src / "calls.txt").read_text(), calls)

    def test_remaining_build_steps_counts_actions_instead_of_output_files(self):
        self.assertEqual(proof.remaining_steps(self.ninja,self.out,self.root / "remaining.log"), 1)
        proof.capture([self.ninja,"-C",self.out,"obj/unit4.o"])
        self.assertEqual(proof.remaining_steps(self.ninja,self.out,self.root / "done.log"), 0)

    def test_fresh_input_timestamps_make_ninja_reject_reuse(self):
        source = self.src / "unit0.cc"
        future = time.time_ns() + 10_000_000_000
        os.utime(source, ns=(future, future))
        with self.assertRaisesRegex(RuntimeError, "would rebuild"):
            proof.require_clean_targets(self.ninja, self.out, ["obj/unit0.o"], self.root / "dirty.log")


if __name__ == "__main__":
    unittest.main()

