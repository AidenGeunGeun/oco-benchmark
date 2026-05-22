# Controller Dry-Run Skeleton

This directory contains the local-only controller skeleton for the OCO SWE-bench Pro benchmark. It proves the run lifecycle and durability rules before any GPU pod, vLLM server, Modal evaluator, or real `oco` subprocess is connected.

The dry-run path uses a fixture adapter. It returns a small canned event stream and a canned patch, then the same controller machinery writes artifacts, phase markers, telemetry, leases, backup copies, and cleanup events.

## Module map

- `core.py` runs the four wrapper phases: `SETUP`, `RUN`, `CAPTURE`, `DONE`.
- `atomic.py` owns durable writes: temp file, fsync, rename.
- `artifacts.py` defines the run and attempt layout.
- `leases.py` owns per-attempt lease files and stale recovery.
- `telemetry.py` parses fixture events into the plan's per-step token records and run summary.
- `fixtures.py` is the swappable RUN-phase adapter used by dry-run tests.
- `backup.py` pushes durable files with real SSH `rsync`, while preserving a local fallback for dry runs.
- `watermarks.py` provides injectable storage and RAM pressure monitors.
- `pro_tasks.py` loads and materializes the pinned public SWE-bench Pro task list.
- `repo_cache.py` owns repo clones, per-attempt worktrees, and safe eviction.
- `eval_bundle.py` prepares the upstream SWE-bench Pro evaluator bundle and manifest.
- `modal_eval.py` owns the pipelined Modal submission, retry, dedup, result, and cost contracts.
- `cli.py` is a small helper CLI used by the dry-run gate for subprocess kill/resume drills.

## Dry-run entry point

Run from the repository root:

```bash
python scripts/dry_run_gate.py
```

The gate creates `runs/dry-run-gate-<timestamp>/gate-report.json`. The report has one entry per acceptance sub-gate, including lifecycle, atomic writes, telemetry shape, resume, force rerun, SSH-stub Mac backup restore, storage/RAM watermarks, glob/grep guard-message parsing, and the no-real-`oco` check.

To demonstrate same-run resume again, reuse the run id printed in the report path:

```bash
python scripts/dry_run_gate.py --run-id dry-run-gate-YYYYMMDDTHHMMSSZ
```

Attempts already marked `DONE` are skipped unless the gate is deliberately exercising `--force-rerun` for one target attempt.

## What is intentionally not here

The fixture adapter does not invoke `oco`; it only returns local data and remains the dry-run default. vLLM health checks, pod boot, calibration selection, and full paid-run launch remain later steps.

## SWE-bench Pro task loading and evaluation

The public task loader pins Hugging Face dataset `ScaleAI/SWE-bench_Pro`, split `test`, revision `7ab5114912baf22bb098818e604c02fe7ad2c11f`. The matching public evaluator reference is `https://github.com/scaleapi/SWE-bench_Pro-os` at commit `ca10a60a5fcae51e6948ffe1485d4153d421e6c5`. The materializer writes a canonical JSONL task list sorted by `instance_id`, plus a manifest that records the dataset source, revision, expected 731-row count, actual row count, loader source, and SHA-256 hash of the exact serialized payload.

Run the offline fixture materializer:

```bash
python scripts/materialize_pro_tasks.py --fixture tests/fixtures/pro_tasks/pro_fixture.jsonl --output runs/dev-task-list.jsonl
```

Run the pinned public loader when network access is allowed:

```bash
python scripts/materialize_pro_tasks.py --public --output runs/pro-public-731-task-list.jsonl
```

The repo cache clones each unique repository once, prepares a per-attempt worktree at the task's recorded base commit, and records clean outcomes for clone failure, transient retry exhaustion, and missing base commits. Eviction only runs when the storage watermark fires, and it refuses to delete a repo cache while any attempt for that repo is leased, active, or otherwise non-terminal.

The eval-bundle preparer consumes completed attempts and writes the upstream evaluator shape: `patches.json` as a JSON array with `instance_id`, `patch`, and `prefix`, plus `raw_sample.jsonl` with matching task rows. Inclusion is driven only by `queued_for_evaluation: true` in each attempt's `normalized.json`. Exclusions use the closed set `precheck_failed`, `no_patch`, `attempt_incomplete`, `attempt_missing`, and `artifact_inconsistent`; inconsistent artifacts fail closed instead of entering the bundle.

Prepare a bundle from a run:

```bash
python scripts/prepare_pro_eval_bundle.py --run-root runs/<run-id> --run-id <run-id> --task-list runs/pro-public-731-task-list.jsonl --task-manifest runs/pro-public-731-task-list.manifest.json --output-dir runs/<run-id>/eval-bundle --record-conformance
```

The bundle manifest names its partial-run denominator as `bundle_candidate_attempt_count` so it cannot be mistaken for the final benchmark denominator. The final score still uses all 731 tasks: only Modal outcome `pass` increases the score; `fail`, `evaluator_timeout`, `evaluator_hard_error`, and `modal_infrastructure_failure` are all non-pass outcomes. Integrity metadata records SHA-256, the file ordering used for the digest, each file's digest, and the combined bundle digest so another reviewer can recompute it without reading controller code.

The conformance check is runnable on demand:

```bash
python scripts/validate_pro_eval_bundle.py --bundle-dir runs/<run-id>/eval-bundle --record
```

If a pinned upstream checkout is available, pass `--upstream-checkout /path/to/SWE-bench_Pro-os`. With the checkout present, the check executes the upstream evaluator's own loader/result path with only the heavy Modal/Docker execution function stubbed, so it proves the bundle shape is accepted by the pinned evaluator code. Without it, the check still validates the documented evaluator-facing structure offline.

## Modal pipeline

When enabled by the controller, each attempt that reaches CAPTURE with `queued_for_evaluation: true` gets a single-row bundle and a deterministic submission ID derived from the run ID and task ID. Enqueue returns immediately after writing a locked `in_progress` submission record; a worker pool processes the Modal jobs while later attempts continue generating. Re-submitting the same task is a no-op, even across processes using the same run directory.

Transient Modal failures are retried with a bounded retry policy. Evaluator timeouts and evaluator hard errors are recorded immediately without retry. Account-level failures, such as expired auth, exhausted credit, quota exhaustion, or service refusal, stop new dispatches for the run and write a clear stop event in the run summary; already-dispatched work is not silently rewritten as unknown. Cost is recorded per attempt when available, otherwise the run-level Modal usage report is stored in the summary.

The real Modal integration check is opt-in and skips by default:

```bash
python scripts/check_modal_integration.py
OCO_BENCHMARK_MODAL_INTEGRATION=1 \
  OCO_BENCHMARK_PRO_EVALUATOR_CHECKOUT=<pinned-checkout> \
  OCO_BENCH_PRO_DOCKERHUB_USERNAME=<dockerhub-user> \
  python scripts/check_modal_integration.py
```

By default the script selects the pinned public dataset row `instance_NodeBB__NodeBB-00c70ce7b0541cfc94afe567921d7668cdc8f4ac-vnan` and uses that row's dataset-provided gold patch. This pair is chosen because it is the first canonical task in the pinned 731-row task list with a non-empty gold patch, so the smoke input is deterministic and comes from the same upstream source as the benchmark. If `runs/pro-public-731-task-list.jsonl` is not present, run the public task materializer first, or provide an explicit one-row bundle with `OCO_BENCHMARK_MODAL_BUNDLE_DIR`. The script enqueues once through the controller pipeline, drains the result, then re-enqueues the same attempt and verifies the second submission is deduped.

## SSH rsync backup

Production backup uses real `rsync` over SSH to a configurable Mac target. Configure host, user, target directory, optional key path, optional port, optional bandwidth limit, and timeout through controller config or CLI flags such as `--backup-ssh-host`, `--backup-ssh-user`, and `--backup-ssh-target-dir`. The local-directory fallback remains available for small offline tests; the dry-run Mac drill uses an SSH/rsync stub to exercise restore/resume without network access.

If the Mac is unreachable because DNS fails, SSH refuses the connection, auth fails, or the connection times out, backup is a logged no-op and the run continues. The next periodic backup tries again. No controller state depends on the Mac being reachable.

Backup uses an explicit durable-artifact allowlist: patches, normalized telemetry, phase logs, OCO event logs, boundary proofs, Modal results, eval bundles, config snapshots, and run summaries. Repo caches and per-attempt worktrees are excluded. Worktree cleanup is stricter than cache eviction: a completed attempt's worktree is deleted only after its durable artifacts were backed up, or after the documented local fallback marker exists. Under high disk pressure with the Mac unreachable, cleanup pauses rather than deleting the only local copy of a completed attempt's worktree.

The real SSH check is also opt-in and skips by default:

```bash
python scripts/check_ssh_rsync_integration.py
OCO_BENCHMARK_SSH_RSYNC_INTEGRATION=1 \
  OCO_BENCHMARK_SSH_HOST=<host> \
  OCO_BENCHMARK_SSH_USER=<user> \
  OCO_BENCHMARK_SSH_TARGET_DIR=<target-dir> \
  python scripts/check_ssh_rsync_integration.py
```

When opted in, the SSH check first pushes a tiny allowlisted artifact to the supplied target, then deliberately points a second rsync at `offline.invalid` (or `OCO_BENCHMARK_SSH_UNREACHABLE_HOST`) with a short timeout to prove the unreachable-Mac path is a retryable no-op.

## Real OCO adapter contracts

The fixture adapter is still the default. A real run is selected explicitly with controller configuration (`adapter_kind="real"`) or the CLI's `--adapter-kind real`; the dry-run gate keeps using the fixture path and does not need an `oco` binary.

### Config materializer

For real runs, the controller reads the production OCO config directory, writes a fresh `oco-config-snapshot/` under the run directory, and points the subprocess at an isolated per-attempt home. It copies the primary agent plus `orchestrator`, `investigator`, `auditor`, and `compaction`; it strips `test_runner`, `web-search`, `docs`, plugins, MCP servers, and skills. The snapshot enables autocompaction with `auto=true`, `prune=true`, `reserved=20000`, and the compaction agent model set to the benchmark self-host model name.

The materializer writes `strip-diff-manifest.json` beside the snapshot so reviewers can see what was kept, removed, and redacted. The secret scanner redacts fields whose keys look like API keys, tokens, secrets, passwords, credentials, authorization, or bearer values, plus values matching common `sk-*`, GitHub `gh*_*`, Slack `xox*`, AWS `AKIA*`, PEM private-key, and secret-looking env-placeholder patterns. If production config is malformed, the materializer removes any partial snapshot and writes `oco-config-materialization-error.json` with the reason.

### Seed pinning

Each attempt derives its sampling seed from the task ID only: SHA-256 of the task ID, first 64 bits, modulo `2^31-1`. The resulting range is `0..2147483646`, which stays inside common signed 32-bit seed limits. The seed is recorded in `normalized.json` and injected into the per-attempt isolated config before `oco run` starts.

### Patch precheck

During CAPTURE, the controller writes `patch.diff` regardless of quality, then checks whether the patch applies to the recorded base commit in a temporary clone. `normalized.json` records `precheck_passed`, `precheck_failed`, `no_patch`, and `queued_for_evaluation`. Only patches that apply are marked queueable for the later Modal pipeline.

### Version and boundary gates

Before materialization or a real subprocess run, the controller checks `oco --version` for `2.1.7+` and scans the binary for the required feature strings: `glob search timed out after`, `grep search timed out after`, and `experimentalNonStreamingToolCalls`. Failures write `oco-version-gate.json` and abort the real path. Fixture dry runs are unaffected.

Real runs enable the production-fidelity boundary monitor. It snapshots protected roots, normally the OCO source checkout and production OCO config, plus monitored parent roots used to catch writes outside the allowed run directory. On systems with `strace`, the real adapter also records write-like filesystem syscalls in `filesystem-trace.log`; real-run proofs require that trace so unexpected writes outside the monitored roots are still visible. At attempt end it writes `boundary-proof.md`; protected-root changes, monitored writes outside allowed/protected roots, missing required trace, or traced out-of-bounds writes mark the proof failed, while run-directory changes are listed as allowed.

### Opt-in smoke

Run `python scripts/smoke_real_oco.py` with no environment and it skips cleanly. To invoke real OCO locally, set `OCO_BENCHMARK_SMOKE_OPENAI_BASE_URL` to a local OpenAI-compatible base URL such as `http://127.0.0.1:8000/v1`. Optional variables: `OCO_BENCHMARK_SMOKE_MODEL` (default `selfhost-qwen`), `OCO_BENCHMARK_SMOKE_API_KEY` (default `sk-local-smoke`), `OCO_BENCHMARK_SMOKE_OCO_BINARY` (default `oco`), `OCO_BENCHMARK_SMOKE_PRODUCTION_CONFIG_DIR` (default `~/.config/oco`), and `OCO_BENCHMARK_SMOKE_RUN_ROOT` for a custom artifact directory. If the URL is set but unreachable, the smoke exits non-zero with a report instead of falling back to fixtures.
