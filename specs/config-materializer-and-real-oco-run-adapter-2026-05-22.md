# Spec: Config Materializer + Real OCO RUN Adapter

**Status:** Draft v2, addressing spec-audit blockers from v1
**Date:** 2026-05-22
**Owner:** PM
**Plan reference:** `docs/oco-pro731-benchmark-plan-2026-05-21.md` §5.1, §5.5, §6 (all subsections), §11.3
**Prior spec:** `specs/controller-core-and-dry-run-gate-2026-05-22.md` (completed, auditor PASS)

---

## Intent

Replace the fixture OCO adapter from the dry-run skeleton with a real OCO subprocess and structured event-stream parsing. Materialize the isolated benchmark OCO config from the user's production setup per plan §6 — kept subagents and prompts, stripped plugins/MCP/skills, autocompaction policy from §6.8. Add per-task seed pinning (§6.5), patch-apply precheck (§5.1), and observation strata computation (§11.3) to the appropriate phases. Tighten the no-OCO-source-modification check from the prior spec's output-containment check to a real production-fidelity boundary proof.

This is step 3 of the locked plan §13. The successful exit condition is the new contracts holding under unit tests, the prior dry-run gate continuing to pass unchanged, and (when an opt-in env var is set) a real OCO invocation against a local model endpoint completing end-to-end.

## Scope

In:
- A config materializer that reads from `~/.config/oco/` and writes a materialized snapshot to the run's `oco-config-snapshot/` directory, applying the kept/stripped policy from §6.2–6.3 and the autocompaction policy from §6.8.
- A real OCO RUN adapter that invokes the installed `oco` binary, captures its structured event stream, and emits per-step records into the §5.5 schema.
- Per-task seed pinning per §6.5 — derived deterministically from the task ID, passed through to the inference server, recorded per attempt.
- Patch-apply precheck per §5.1 — verifies a generated patch applies to the task's recorded base commit before the patch is queued for evaluation.
- Observation strata computation per §11.3 — `delegation_observed`, `audit_observed`, `full_loop_observed` written per attempt.
- A `compaction_events` telemetry counter per attempt: number of times the autocompaction agent fired during the attempt.
- An OCO version/feature gate that refuses to proceed (real RUN and smoke) when the installed `oco` does not meet plan §6.1 requirements, with a clear recorded artifact.
- A production-fidelity boundary check that proves OCO source and the user's production OCO config were not modified during an attempt; runtime state written by `oco` is permitted only inside the attempt's controlled directory.
- Malformed-production-config behavior: clean failure, no partial snapshot, no writes to production config.
- An opt-in local smoke script that invokes real OCO against a developer-supplied local model endpoint when an env var is set, and produces a real `patch.diff` + telemetry. Skipped (with a clear log message) when the env var is unset.
- Unit tests covering each of the above contracts, including a positive *and* a negative fixture for the production-fidelity boundary check.

Out of scope (later specs):
- SWE-bench Pro task data loading.
- Modal evaluation pipeline.
- Real SSH rsync (the prior spec's local-dir rsync hook stays as-is).
- Pod-side anything (vLLM, MTP, pod boot).
- Any OCO source modification.

## Context

The prior spec built the controller skeleton, durability primitives, fixture adapter, and dry-run gate. That work is in `controller/` (importable modules), `scripts/dry_run_gate.py`, and `tests/`. The prior auditor flagged one warning: the dry-run gate's "no OCO source modification" sub-gate is an output-containment check (proves the controller only writes inside `oco-benchmark/`), not a production-fidelity boundary proof. This spec addresses that warning by requiring a stronger proof.

The fixture adapter must remain in the codebase and remain the default for the dry-run gate. The real adapter is selected by configuration. Module boundaries from the prior spec must be preserved — the controller state machine, telemetry normalizer, lease/resume behavior, backup selection, and dry-run tests do not restructure.

Plan §6.8 specifies the autocompaction policy: `compaction.auto: true`, `compaction.prune: true`, `compaction.reserved: 20000`, `agent.compaction.model` pointing at the same self-host endpoint name, `agent.compaction.prompt` carried over from production. The default self-host model name is `selfhost-qwen`; when the smoke env var supplies a different local endpoint name, the materializer respects that override.

Plan §6.1 specifies that the benchmark requires OCO 2.1.7+ with three binary feature strings present, confirmed via `strings $(which oco)`:

- `glob search timed out after`
- `grep search timed out after`
- `experimentalNonStreamingToolCalls`

The version/feature gate must check the version (≥ 2.1.7) AND all three strings.

Plan §6.6 specifies that the materialized snapshot is "the full materialized isolated benchmark config (minus secrets)." Secrets means API keys, tokens, env-var-injected values, and anything else that should not be reproducible from the snapshot. The benchmark snapshot is intended as a reproducibility artifact for reviewers, not a credential store.

**Production-fidelity boundary** for the cross-scope non-modification check. The proof must show, at the end of each attempt:

- OCO source tree (the user's `OpenCodeOrchestra/` checkout) was not modified.
- User's production OCO config directory (`~/.config/oco/`) was not modified.
- The benchmark's own controlled directories (the attempt's worktree, the run's artifact dirs under `oco-benchmark/runs/`, the materialized snapshot dir) are expected to receive writes from the controller and from the `oco` subprocess; writes inside these allowed dirs do not violate the boundary.
- Any other location written to during the attempt is a violation; the artifact records what was written, where, and the proof is marked failed for that attempt.

Production fidelity (per `oco-benchmark/AGENTS.md`) is a hard rule. The materializer reads from `~/.config/oco/` and never writes to it. The benchmark snapshot is a fresh copy in the run directory.

Implementation suspicions, clearly labeled as suspicions and not requirements:
- The cleanest layout likely adds a materializer module and a separate real-OCO adapter module sitting behind the same interface the fixture adapter already implements. Final layout is the Orchestrator's call.
- The strata computation likely lives near the telemetry parser since it's a function of the parsed event stream. The Orchestrator decides.
- The boundary check has multiple plausible mechanisms (filesystem mtimes, content hashes of a directory tree, version-control state, fanotify/audit subsystems, sandbox isolation, etc.). The Orchestrator picks one that produces a durable, reviewable artifact and survives the unit-test negative fixture below.
- The smoke env var likely supplies a URL plus an optional override of the served model name; the Orchestrator decides the exact env-var name(s), value format, and documents the contract.

## Acceptance Criteria

Each criterion is observable from outside the code; an auditor verifies each by inspecting artifacts, running tests, or reading code.

1. **Materialized snapshot is policy-correct.** A materialized `oco-config-snapshot/` produced from a fixture production config contains: PM (primary agent) plus kept subagents (Orchestrator, Auditor, Investigator) with their prompts; the compaction agent with the §6.8 model and prompt; stripped subagents absent; kept tools present in agent permissions; stripped tools absent or denied; plugin array empty; MCP block empty; skills empty; non-streaming and parallel-tool-call settings per §6.7; sampling settings per §6.5; recorded OCO version.

2. **No secrets in the snapshot.** A unit test materializes against a synthetic production-config fixture that contains planted fake secrets (API keys, tokens, env-var-injected values in any field). The resulting snapshot is scanned for the planted values; finding any of them fails the test. The set of patterns the scanner looks for is documented so a reviewer can extend it.

3. **OCO version/feature gate.** Before any real RUN or smoke invocation, the controller verifies the installed `oco` satisfies plan §6.1 (version 2.1.7+, binary contains the two named feature strings). If the gate fails, the attempt is aborted with a clear recorded artifact (a file in the attempt directory or run directory containing the failure reason, the detected version, and which features were missing). The smoke script exits non-zero in this case. The dry-run gate is unaffected because it uses the fixture adapter.

4. **Strip diff manifest** is produced alongside the snapshot, listing what was kept and what was removed. A reviewer can verify removals without re-deriving them.

5. **Real OCO RUN adapter invokes the installed `oco` binary** against the materialized snapshot, captures the structured event stream, and emits per-step records into the §5.5 schema with all eight named fields populated correctly, including `step_role: "compaction"` when the autocompaction agent fires. A per-attempt `compaction_events` counter (zero or positive integer) is written to `normalized.json`.

6. **Fixture adapter is preserved** and remains the default for the dry-run gate. Real vs fixture adapter is selected by configuration, not by code replacement.

7. **Per-task seed pinning** is implemented per §6.5:
   - Same task ID always produces the same seed (deterministic; verified by a unit test repeating the derivation across processes).
   - A set of representative distinct task IDs produces distinct seeds in a unit test (no collisions in the test fixture).
   - The seed range (the integer width and any modulus chosen by the Orchestrator) is documented in a reviewer-visible developer artifact (docstring, README section, or equivalent) so a reviewer can reason about collision probability independently.
   - The seed is passed to the inference server via the OpenAI-compatible API's sampling-seed mechanism on every model call inside the attempt.
   - The seed is recorded in `normalized.json` per attempt.

8. **Patch-apply precheck** is implemented per §5.1: in CAPTURE, before queueing a patch for Modal, the controller verifies the patch applies to the task's recorded base commit. Patches are recorded in `patch.diff` and `normalized.json` regardless. Flags written to `normalized.json`: `precheck_passed: true` if the patch applies, `precheck_failed: true` if it doesn't, `no_patch: true` if the patch is empty. Failed-precheck patches are not queued for evaluation.

9. **Observation strata** are computed per attempt and written to `normalized.json`: `delegation_observed`, `audit_observed`, `full_loop_observed`. Definitions match §11.3 exactly. Each is a boolean derived from the parsed event stream.

10. **Production-fidelity boundary proof.** At the end of each attempt, the controller produces a durable, human-readable artifact in the attempt's artifacts directory showing:
    - OCO source tree (`OpenCodeOrchestra/`) was not modified during the attempt.
    - User's production OCO config directory (`~/.config/oco/`) was not modified during the attempt.
    - Writes that landed inside the attempt's allowed area (worktree, run artifact dirs, materialized snapshot dir) are recorded but do not violate the boundary.
    - Writes outside the allowed area, if any, are recorded and the proof is marked failed for that attempt.
    The artifact format is the Orchestrator's choice; the requirement is that an auditor can read it and verify the claim without re-running the attempt.

11. **Negative fixture for the boundary check.** A unit test sets up a temporary, controlled, monitored sibling directory that is deliberately treated as "outside the allowed area" for the test (real OCO source and production config are never used in this test fixture; the test isolates a synthetic monitored tree). The test plants a write into that sibling directory mid-attempt and verifies the boundary check flags the violation with the expected failure signal. A second test exercises the clean case (no out-of-bounds writes) and verifies the check passes. The test does *not* write to real OCO source, the real production config, or anything outside `oco-benchmark/`.

12. **Opt-in local smoke**: when a documented env var is set to point at a local OpenAI-compatible endpoint, the smoke script invokes real OCO against that endpoint with a single tiny fixture task and produces a real `patch.diff`, real telemetry in `normalized.json`, and a report. When the env var is unset, the smoke skips with a clear log message and exits 0. The env var name(s), expected value format (URL vs URL+model name vs profile name), and behavior when the URL is set but unreachable are documented in `controller/README.md`.

13. **Malformed-production-config behavior.** If the production config at `~/.config/oco/` is malformed (invalid JSON/JSONC, missing required agent, etc.), the materializer fails cleanly: no partial snapshot is written, the production config is not modified, and the failure mode is reported in a recorded artifact with a clear reason. A unit test exercises this path against a malformed fixture.

14. **Unit tests** cover: the materializer (with synthetic production-config fixtures, including the secrets-exclusion test and the malformed-config test); the event-stream parser (against captured fixture event streams from prior runs or hand-crafted equivalents); the seed derivation (determinism, idempotence, distinct task IDs from a representative set produce distinct seeds, documented range); the patch-apply precheck (apply-succeeds, apply-fails, empty-patch); strata computation (synthetic event streams exercising each definition); the OCO version/feature gate (gate-passes and gate-fails fixtures); the production-fidelity boundary check (positive and negative fixtures per criterion 11); and the `compaction_events` counter logic.

15. **Implementation diff stays inside `oco-benchmark/`.** The Orchestrator's own changeset for this task (the new code, tests, and docs being added) is confined to `oco-benchmark/`. This is a separate check from the runtime production-fidelity boundary in criterion 10: criterion 10 is a per-attempt artifact produced by the controller at runtime; criterion 15 is a one-time check on the Orchestrator's own diff before reporting completion. If the diff check flags any modification outside `oco-benchmark/`, that is a hard fail to fix before reporting.

16. **Dry-run gate regression check**: `python scripts/dry_run_gate.py` still exits 0 with all twelve prior sub-gates passing. New tests do not regress the prior gate.

## Verification

From `oco-benchmark/`:

- `python -m pytest tests/ -v` exits 0 with new tests added and all prior tests still passing.
- `python scripts/dry_run_gate.py` exits 0 (regression check for the prior twelve sub-gates).
- If the smoke env var is set to a working local endpoint, the smoke script exits 0 and produces a real `patch.diff` plus telemetry. If unset, the smoke skips cleanly. If the version/feature gate fails (e.g., the installed `oco` is too old), the smoke exits non-zero with a clear recorded artifact.
- The Orchestrator runs the auditor against the finished work before reporting completion. The auditor verifies, among other things, that the negative-fixture test for the boundary check actually catches a planted violation, that the secrets-exclusion test catches planted secrets, that the version/feature gate refuses to proceed against a synthetic too-old binary fixture, and that the malformed-config test fails cleanly with no partial snapshot.

## Completion Standard

- All sixteen acceptance criteria visibly hold.
- The fixture adapter is preserved and remains the default for the dry-run gate.
- The smoke script is opt-in by env var; default behavior is no real OCO invocation in unit-test contexts.
- A short developer-facing section in `controller/README.md` describes the materializer behavior, the seed-derivation contract, the precheck contract, the version/feature gate, the boundary check, and the smoke env var contract. Plain language, ≤2 pages added. (This is a documentation artifact, not a constraint on module layout.)
- The diff produced by this work stays inside `oco-benchmark/`, and the Orchestrator's own boundary check confirms this.
- Auditor PASS.

Report back when the auditor has passed the work, summarizing in plain language what was built, how the contracts are verified, and any open items the next spec (SWE-bench Pro task loading + Modal eval pipeline) will need to address.
