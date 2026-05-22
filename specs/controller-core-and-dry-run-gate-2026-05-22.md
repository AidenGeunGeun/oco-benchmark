# Spec: Controller Core + Dry-Run Gate

**Status:** Draft, ready for implementation
**Date:** 2026-05-22
**Owner:** PM
**Plan reference:** `docs/oco-pro731-benchmark-plan-2026-05-21.md` §5, §5.5, §7, §8, §9.2, §11, §13 (steps 2–3)

---

## Intent

Build the benchmark controller skeleton in `oco-benchmark/controller/` and the dry-run gate in `oco-benchmark/scripts/dry_run_gate.py` such that the full attempt lifecycle, durability primitives, resume semantics, rsync backup, and resource watermarks can be exercised end-to-end against local fixtures — without booting a GPU pod, without invoking a real `oco` subprocess, and without any network dependency.

This is step 2 ("Build controller") and step 3 ("Dry-run gate") of plan §13. The successful exit condition is `scripts/dry_run_gate.py` exiting 0 with a clean report on a developer's laptop.

## Scope

In:
- Controller state machine for the four wrapper phases (`SETUP`, `RUN`, `CAPTURE`, `DONE`) — wrapper phases only, not OCO-internal phases.
- Atomic write primitives (`.tmp` + `fsync` + rename) used uniformly for every durable file.
- Per-attempt artifact layout matching plan §11.1: `patch.diff`, `normalized.json`, `phase-log.jsonl`, `oco-events.ndjson`.
- Per-attempt lease file with PID + timestamp; stale-lease recovery logic.
- Phase marker files (`SETUP_DONE`, `RUN_DONE`, `CAPTURE_DONE`, `DONE`); written atomically on phase completion; checked on controller startup for resume.
- Same-`--run-id` resume rule: attempts with `DONE` marker are skipped; partial attempts resume from the appropriate phase marker; `--force-rerun <attempt-id>` overrides.
- Telemetry normalization for the per-step record shape defined in plan §5.5: parses a *fixture* event stream into `steps[]` with `prompt_tokens`, `completion_tokens`, `cached_prompt_tokens`, `reasoning_tokens`, `wall_time_ms`, `finish_reason`, `tools_called`, `step_role`, and emits the aggregations listed in §5.5 (per-attempt and per-run).
- Rsync backup hook with configurable target directory; pushes only the durable artifact files; tolerates target unreachable (no-op + retry); deterministic file selection logic.
- Storage and RAM watermark monitors per plan §8: container disk ≥85% triggers worktree cleanup; RAM ≥90% pauses new attempts. Monitors are pluggable so tests can inject synthetic pressure.
- Fixture OCO call adapter: a swappable component the controller uses during `RUN`. The dry-run fixture produces a canned event stream and a canned diff. The interface is what the *real* OCO subprocess wrapper will later implement.
- Dry-run gate script `scripts/dry_run_gate.py` exercising every behavior below and emitting a single JSON report.
- Unit tests under `tests/` covering atomic write primitives, phase marker logic, lease recovery, telemetry parser, watermark monitors, and rsync hook behavior.

Out of scope (later specs):
- Real `oco` CLI subprocess wrapping and PTY/event-stream parsing of real OCO output.
- vLLM serving, pod-side anything, real network code.
- Modal evaluation pipeline.
- Task selection / SWE-bench Pro task loading from disk (a tiny fixture task list is enough for the gate).
- Eval-bundle preparation logic.
- Real glob/grep timeout guard probe against actual filesystem fixtures larger than the test fixture below.

## Context

This repo's `AGENTS.md` defines the hard boundary: this code is a *consumer* of OCO, never modifies OCO source. The dry-run gate must not invoke a real `oco` binary; it uses the fixture adapter.

Earlier benchmark work (now archived on external SSD) had a single monolithic `benchmark.py` that grew organically and mixed concerns. That code is reference-only — do not copy wholesale. The new controller is a clean rebuild with explicit phase semantics and durability primitives, organized as importable modules.

Plan §5.5 defines per-step telemetry granularity in detail. Read it before designing the telemetry parser; the `steps[]` shape and aggregation set are fixed by the plan, not by implementation taste.

Plan §6 defines the OCO setup that the *real* RUN phase will eventually materialize from `~/.config/oco/`. The dry-run does not need to materialize anything — a stub `oco-config-snapshot/` directory containing a single `placeholder.json` is sufficient for now. The real materializer is a later spec.

Plan §7 spells out resume semantics. Lease files have a stale-after timeout (default 5 minutes); when the next controller startup sees a stale lease and a phase marker, it resumes from that marker. There is no in-flight RUN resume in the dry-run — if the fixture run was killed mid-RUN, the controller restarts RUN from a clean state inside the same attempt directory.

Implementation suspicions, clearly labeled as suspicions:
- The cleanest layout is probably a single Python package `oco_benchmark` under `controller/`, with submodules for `phases`, `artifacts`, `telemetry`, `resume`, `backup`, `watermarks`, `fixtures`. Use whatever layout is actually cleanest at implementation time.
- The dry-run gate likely runs 3–5 fixture attempts with deliberately varied behavior: a clean attempt, an attempt that crashes mid-RUN to exercise resume, an attempt with a no-patch outcome, an attempt that triggers the watermark monitor, and an attempt that targets an offline rsync destination.

## Acceptance Criteria

Observable from outside the code; a reviewer or auditor verifies each.

1. **Lifecycle**: a single fixture attempt completes `SETUP → RUN → CAPTURE → DONE`, leaving all four phase markers and the four artifact files in the attempt directory.

2. **Atomic writes**: there is no code path that writes a durable file without going through the atomic primitive. A test asserts that interrupting a write mid-flight (simulated via injected failure) leaves no partial file on disk — only either the prior version or nothing.

3. **Telemetry shape**: `normalized.json` matches plan §5.5 exactly. `steps[]` is present, each step has all eight named fields, and the per-attempt aggregations (`step_count`, `tool_call_count`, `tokens_in_total`, `tokens_out_total`, `cached_tokens_total`, `reasoning_tokens_total`, `prefix_cache_hit_rate`, `prefix_cache_hit_rate_excluding_first_step`, per-step distribution stats) are computed and present. A `summary.json` at the run level aggregates the same numbers across all attempts in the run.

4. **Resume drill**: the gate script runs an attempt, kills the controller process during `RUN`, restarts the controller with the same `--run-id`, and the controller resumes the same attempt without re-running the prior `DONE` attempts and without orphan lease files. A test asserts no attempt directory ends up with duplicated work or missing markers after restart.

5. **Force rerun**: `--force-rerun <attempt-id>` clears that one attempt's markers + artifacts and re-runs only that attempt, leaving other `DONE` attempts untouched.

6. **Mac backup drill**: rsync hook is exercised with the destination set to a local directory acting as the Mac target. After a fixture attempt reaches `DONE`, the durable artifact set has been mirrored to that directory. With the destination set to an unreachable path, the hook logs a no-op event and the run continues without raising.

7. **Storage watermark**: the watermark monitor is invoked with a synthetic disk-usage source returning 90% (above the 85% threshold). The controller triggers worktree cleanup on completed-and-rsync'd attempts and writes a cleanup event. Active partial attempts are not touched.

8. **RAM watermark**: with synthetic memory pressure at 92% (above the 90% threshold), the controller pauses spawning new attempts. When pressure drops, spawning resumes.

9. **Glob/grep safety guard verification**: a synthetic bounded fixture directory of <1000 files is created in `tests/fixtures/`. A controlled probe simulates the OCO subprocess emitting the timeout-guard message (the actual guard lives in the OCO 2.1.7 binary; the controller just needs to recognize and record it correctly in the event stream parser).

10. **Single-command exit**: from a fresh checkout of `oco-benchmark/`, the developer runs `python scripts/dry_run_gate.py` and gets exit 0 with a final JSON report listing every sub-gate above and a `pass: true` for each. A failing sub-gate produces a clear failure reason in the report.

11. **No OCO source modification**: the diff produced by this work touches only `oco-benchmark/`. No files under `OpenCodeOrchestra/` or any other repo are modified.

12. **No real OCO invocation**: the dry-run gate completes without a real `oco` binary existing on PATH. The fixture adapter must be the only RUN-phase code exercised.

## Verification

Run from `oco-benchmark/`:

```
python -m pytest tests/ -v
python scripts/dry_run_gate.py
```

Both exit 0. The gate script writes its report to `runs/dry-run-gate-<timestamp>/gate-report.json` and prints the headline pass/fail per sub-gate.

A second invocation of the same gate script with the same `--run-id` (if it accepts one) demonstrates the resume rule: the second invocation completes in noticeably less wall time because `DONE` attempts are skipped. This is the no-paid-pod proxy for the run-resume behavior.

## Completion Standard

- All twelve acceptance criteria visibly hold.
- Code is organized as importable modules — a future spec wires the real `oco` subprocess into the same phase machinery without restructuring.
- No `oco` subprocess invocation occurs anywhere in the dry-run code path.
- Tests under `tests/` are runnable in isolation and document the durability primitives' contracts.
- A short developer-facing README under `oco-benchmark/controller/README.md` (≤2 pages) describes the module layout and the dry-run entry point. Plain English, not API reference; reviewers should be able to read this and understand what runs where.
- Diff stays inside `oco-benchmark/`.
