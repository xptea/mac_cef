# mac_cef: Mac CEF checkpoint proof

A standalone repository testing whether source-built ARM64 CEF can continue
across free GitHub Actions Mac runners. This folder contains build recipes only:
no Void browser source, profiles, signing keys, or Windows binaries.

**Current state:** the latest 20-minute CEF compile produced **11,447 completed
outputs** in a **275 MiB** cached checkpoint. File/toolchain validation passed on a
fresh runner, but Ninja requested GN regeneration because recreated source
folders had newer timestamps. Preparation now normalizes directory timestamps as
well as file timestamps. Cross-runner reuse still needs a passing recovery run;
a complete engine build and final link remain unproven.

## First run: inspect

1. Push this folder as its own public GitHub repository.
2. Open **Actions > CEF checkpoint proof > Run workflow**.
3. Keep **mode = inspect** and run it.
4. Read the job summary: CPU/RAM, actual free disk, installed Xcodes, selected
   SDK and GNU tar/zstd availability.

This mode checks the recipe logic and reports resources. It downloads no CEF
source and starts no engine compilation. There are no push, pull-request or
scheduled workflow triggers, and no paid/larger runner labels.

The standard runner is **macos-26 (ARM64)**. The recipe pins Xcode 26.6 and macOS
SDK 26.5. It fails if the expected SDK is missing.

## Second run: actual CEF checkpoint

Run the workflow again with **mode = checkpoint** and **compile_seconds = 1200**
(20 minutes). Source downloads and generation add time before that compile
window; the verification job also reconstructs its own source tree. This is not
a 20-minute-total workflow.

If inspection shows insufficient space, opt into **reclaim_unused_xcodes**.
Only other Xcode version installations are removed on disposable GitHub runners;
the selected Xcode is retained. Cleanup refuses to run on a personal Mac.
The proof requires 75 GiB free before source setup and stops below 8 GiB free.

### Save job

- Fetch exact CEF, Chromium and depot_tools revisions, with shallow Chromium history.
- Bootstrap the pinned depot_tools runtime without updating its checkout; verify
  its Python interpreter starts before source setup.
- Configure the native codec-enabled, noncomponent ARM64 engine.
- Compile the actual cefclient/CEF graph with two parallel jobs for the bounded
  test window, then interrupt Ninja gracefully.
- Require at least ten completed C++ objects and check a representative sample
  is already up to date before saving.
- Record output files, nanosecond timestamps and dependency hashes.
- Compress the complete output tree, generated files, .ninja_deps and .ninja_log
  with GNU tar PAX timestamps and zstd.
- Reject checkpoints over 8 GiB; save the compressed file through Actions cache.

### Restore job

A fresh VM reconstructs the same pinned inputs at the same build path and checks:

- Python, Xcode/SDK, compiler, source pins and GN settings match;
- runner-image labels are recorded, and may differ only while all real input
  checks below still pass;
- completed files and their nanosecond timestamps survived;
- dependencies match in contents and timestamps;
- Ninja's dry-run schedules **zero rebuild commands for sampled completed C++
  objects**. Unfinished targets are expected to remain.

Success is a **PASS** summary with the preserved-output count, compressed size
and zero sampled C++ objects requiring recompilation. The second job does not
compile CEF to obtain that result.

This tests real Ninja outputs without sccache. A partial checkpoint fitting does
not prove a full checkpoint fits or the final link finishes on the free runner.
Those measurements follow the first proof.

## Recover the existing compiled checkpoint

Run the workflow with **mode = recover**. It prepares fresh pinned source, restores
only the cache approved in `recovery.json`, and runs the no-recompilation proof.
It does **not** run another compilation batch, save over, or delete the old cache.
Source reconstruction still takes time because GitHub discards completed VMs.

The recovery manifest pins the exact cache key, archive SHA-256 and older recipe
SHA-256 from the previous trusted run. Only that approved recipe is accepted for
this migration; SDK/compiler/source/GN identity, dependency content and timestamp
checks, output preservation and Ninja checks remain required. Other recipe
changes are still rejected. Normal checkpoint mode retains the current-recipe
match requirement.

A failed verification does not delete Actions caches. GitHub may evict unused
caches according to its retention/storage rules, so their presence is checked
before recovery rather than assumed permanently.

## Limits and costs

Standard hosted runner compute is free for public repositories. This recipe uses
standard Mac runners, dependency cache, logs and job summaries. It does not upload
large Actions artifacts or enable paid cache storage. Keep the repository cache
limit at the included 10 GB. Each run creates one uniquely keyed checkpoint;
GitHub may evict older caches.

Jobs have a 330-minute cap, below GitHub's six-hour limit. This is a bounded proof,
not an eleven-hour build or an automatic full-build chain. A full pipeline must
reserve checkpoint time and finish the final link in one job.

The script rejects changed toolchains and records image changes. Source file and directory timestamps are normalized
only for identical pinned inputs; dependency hashes independently check contents
before accepting reuse.

## Local checks

Python 3.12 is used in CI. Tests can run on Windows, Linux or macOS:

    python -m unittest discover -s tests -v

Set NINJA to an installed executable when Ninja is not on PATH. Local tests use
a tiny real Ninja dependency graph; that fixture is not the CEF proof.
GNU tar/zstd round-trip tests also run when those tools are available.

## References

- https://docs.github.com/en/actions/reference/limits
- https://docs.github.com/en/actions/reference/runners/github-hosted-runners
- https://docs.github.com/en/actions/reference/workflows-and-actions/dependency-caching
- https://github.com/chromiumembedded/cef/blob/master/docs/automated_build_setup.md
- https://github.com/actions/runner-images/blob/main/images/macos/macos-26-arm64-Readme.md


## Continue toward the full engine build

Use **Actions > Continue full CEF build**. Leave checkpoint fields blank for the
existing preserved checkpoint, or supply the exact key/checksum/recipe from a
later batch. This builds the real `cefclient` target until Ninja reports no work
remaining; it is not another fixed 20-minute sample.

Each job allows up to four hours of compilation, but reduces that window when
setup consumed time so a checkpoint can still be saved before the job limit.
After a successful cache save, unfinished work automatically dispatches the next
job. The automatic chain is bounded to 12 batches and stops on an error or zero
forward progress; the saved checkpoint remains available for repair and retry.
Logs now expose actual Ninja remaining-step counts and recent compiler output.

Prepared source and downloaded tools are cached separately under an exact
source/toolchain key. A warm cache skips Chromium download and dependency sync;
GN configuration and validation still run. The first cold job must reconstruct
that setup once. Source archives are capped at 6 GiB, compiled archives at 8 GiB;
source snapshot creation stops if its limit is exceeded and reports that result
instead of silently assuming the source cache fits. No paid cache limit is set.
A complete source cache and a large compiled checkpoint may compete for the
repository cache budget; actual compression and eviction must be measured.

Checkpoint restoration now clears only the generated output directory inside
the dedicated runner build root before extraction. It does not overlay archived
SDK trees on links created by fresh GN configuration, and does not remove the
preserved archive or follow links into the installed SDK.
