# mac_cef: Mac CEF checkpoint proof

A standalone repository testing whether source-built ARM64 CEF can continue
across free GitHub Actions Mac runners. This folder contains build recipes only:
no Void browser source, profiles, signing keys, or Windows binaries.

**Current state:** prepared locally. No public repository or Mac workflow run has
been created or started. Actual cross-runner CEF reuse is not yet proven.

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

- runner image, Python, Xcode/SDK, compiler, source pins and GN settings match;
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

## Limits and costs

Standard hosted runner compute is free for public repositories. This recipe uses
standard Mac runners, dependency cache, logs and job summaries. It does not upload
large Actions artifacts or enable paid cache storage. Keep the repository cache
limit at the included 10 GB. Each run creates one uniquely keyed checkpoint;
GitHub may evict older caches.

Jobs have a 330-minute cap, below GitHub's six-hour limit. This is a bounded proof,
not an eleven-hour build or an automatic full-build chain. A full pipeline must
reserve checkpoint time and finish the final link in one job.

The script rejects changed images/toolchains. Source timestamps are normalized
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

