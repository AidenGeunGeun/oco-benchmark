# Spec: Native Subagent Disable + Qwen-Specific Prompt Overlay + Boundary Classifier Fixes

**Date:** 2026-05-22
**Author:** PM
**Status:** ready for audit

## Intent

The first single-task benchmark verification (`onetask-flipt-20260522T114650Z`) produced a syntactically valid patch but surfaced three harness defects that must be fixed before launching the full 731-task SWE-bench Pro run:

1. **Stripped subagents leaked.** The PM agent successfully invoked `task` with `subagent_type: "explore"` and `subagent_type: "test_runner"`, both of which the benchmark intends to strip. They executed because OCO registers them as native built-ins in source before merging the materialized config; absence from config does not delete them.

2. **PM did not delegate to Orchestrator.** Normalized telemetry shows `delegation_observed: false`, `audit_observed: false`. PM did all 30 model steps directly. We have already decided (project AGENTS.md) that the benchmark measures `delegation_observed` as a strata, not a filter. However, we have also decided that for **Qwen-family models specifically** we will apply pod-only prompt hardening because Qwen 3.6 27B is a weaker open-source model that needs RFC2119 phrasing to respect the PM/Orchestrator/Auditor protocol. This mirrors how production harnesses ship per-model prompt variants. Aiden does not use Qwen daily, so this is a Qwen-targeted harness adaptation and not benchmaxxing of his daily workflow on stronger models.

3. **Boundary proof reported a mixed-signal failure.** The single real out-of-bounds write was `/tmp/test-runner-validation.log`, created by the leaked `test_runner` subagent — eliminated by fix (1). The remaining "violations" are classifier artifacts: (a) writes under `/workspace/repo-cache/` from the benchmark's own `RepoCacheManager`; (b) relative `.node-gyp` / Bun cache paths recorded as `RELATIVE:<path>` without resolving the parent fd; (c) failed `O_RDWR` probes returning `ENOENT` recorded as if they were writes.

This spec covers all three defects in one Orchestrator handoff because they share the same verification surface (re-running the Flipt single-task attempt on the pod) and because the boundary fix is mostly downstream of the native-agent fix.

## Spec Path

`/Users/aidenkim/projects/agents/OCstuff/oco-benchmark/specs/native-subagent-disable-qwen-prompts-boundary-fixes-2026-05-22.md`

## Scope

**In scope (oco-benchmark repo only):**
- `controller/materializer.py` — emit `agent.<name>.disable: true` for stripped native subagents; add Qwen-family prompt overlay path
- `controller/boundary.py` — three classifier fixes (syscall-success filter, repo-cache allowlisting via `allowed_roots`, fd-aware relative-path resolution)
- `controller/core.py` — wire `repo_cache_dir` into `BoundaryConfig.allowed_roots` at the boundary-config construction site (around `:98`)
- `prompts.qwen/pm.txt` and `prompts.qwen/orchestrator.txt` — new pod-only Qwen-targeted prompt files based on production prompts with RFC2119 hardening on the delegate/spec/audit rules
- Tests (`tests/test_materializer.py`, `tests/test_boundary.py`, others as needed) — cover new behavior
- Documentation updates in the benchmark's README and/or methodology notes that (a) describe the Qwen prompt variant policy as a per-model adaptation, and (b) explicitly record this as a documented exception to the production-fidelity rule in `oco-benchmark/AGENTS.md` (the standing rule is "no invented prompts"; the Qwen variant is a sanctioned, scoped, model-specific overlay)

**Out of scope (do NOT touch):**
- `OpenCodeOrchestra/` — no OCO source changes; this is a benchmark-side fix
- `~/.config/oco/prompts/` on the Mac — production prompts stay untouched
- The parallel-launch wrapper for 731 — separate spec
- Telemetry gap fixes (cached tokens / reasoning tokens / step wall-time) — documented as known OCO source limitations per the prior investigator finding; not in scope here
- Modal evaluator / SSH rsync paths
- `prompts/` directory of non-Qwen base prompts — copy unchanged; the variant overlay only adds Qwen-specific files

## Context

### Investigator findings (already source-cited; Orchestrator should re-verify but trust as starting point)

**Native subagent registration and disable mechanism:**

- OCO registers thirteen agents in `OpenCodeOrchestra/packages/opencode/src/agent/agent.ts` BEFORE merging user config: `build` (`:92`), `plan` (`:110`), `general` (`:133`), `explore` (`:149`), `compaction` (`:177`), `title` (`:193`), `summary` (`:210`), `orchestrator` (`:227`), `investigator` (`:247`), `auditor` (`:268`), `test_runner` (`:289`), `web-search` (`:311`), `docs` (`:332`).
- Disable field shape: `agent.<name>.disable: boolean` per schema at `config.ts:766`.
- Deletion path: `agent.ts:357-360` deletes `result[key]` when `value.disable` is truthy. `Agent.get(name)` at `:407-408` resolves from the cleaned state; `Agent.list()` at `:411-417` returns only surviving entries. `task` tool at `tool/task.ts:213-214` calls `Agent.get(params.subagent_type)` and throws "Unknown agent type" if the agent was deleted.
- **Subagents the benchmark should disable** (subagents only, leave primaries alone): `general`, `explore`, `test_runner`, `web-search`, `docs`.
- **Subagents the benchmark must keep**: `orchestrator`, `investigator`, `auditor`.
- Primaries (`build`, `plan`, `compaction`, `title`, `summary`) are not exposed via the `task` tool's subagent surface (`tool/task.ts:68-73` filters to non-primary agents), so they need no action.

**Boundary classifier defects:**

- Classifier lives at `controller/boundary.py:205-223` in `classify_trace_outside_writes()`. It currently scans write-like strace lines, extracts quoted paths, and classifies them WITHOUT parsing the syscall return value. Failed calls (e.g., `= -1 ENOENT`) are recorded as violations.
- Allowlist uses only `BoundaryConfig.allowed_roots` (`:208`). Default real config (`:291-305`) sets `allowed_roots=(run_root,)`. The benchmark's own `RepoCacheManager` writes to a configured `repo_cache_dir` outside `run_root`, so its setup activity reads as out-of-bounds.
- Relative paths at `:213-216` are always recorded as `RELATIVE:<path>` without resolving the directory fd from `*at` syscalls. Bun's `.bun/install/cache/...` and node-gyp's `.18b1e0bdbcef3f79-00000001.node-gyp` are real writes but live under already-allowed roots if the fd were resolved.
- Additional limitations the Orchestrator may also fix in this pass (judgment call, not required): incomplete syscall family coverage at `:197-200` (`open/openat/openat2` only — missing `mknod*`, `truncate*`, `chmod*`, `fchmodat`, xattr, fd-based writes); hard-coded ignore prefixes at `:202` (`/dev/`, `/proc/`, `/sys/`).
- `BoundaryConfig.allowed_roots` at `:14-20` already exists; no new dataclass field is required to whitelist the repo cache. The wiring needs to happen at the boundary-config construction call site in `core.py` (around `:98`).

**Prior decisions that constrain this spec:**

- We use Qwen native sampling parameters (`temperature: 1.0`, `top_p: 0.95`, `top_k: 20`, `min_p: 0.0`, thinking enabled). Per-model prompts are a similar per-model adaptation; the policy is "per-model variant directory under the materialized snapshot, selected at materialization time based on model name pattern."
- Aiden's MUST list for PM: (1) write specs so auditors have something to audit, (2) delegate to Orchestrator unless extremely simple/trivial, (3) trust autocompaction for context overflow. For Orchestrator: (4) run Auditor continuously UNTIL PASS.
- The original benchmark agent the runner invokes is `build` (the primary). The Qwen-targeted `pm.txt` corresponds to the `build` agent's prompt file mapping in OCO's config; do not rename the file mapping. Confirm in source whether the prompt-file resolution honors the agent name or a separate field.

### Reproduction artifacts

Local extracted run dir from the Flipt verification: `/tmp/onetask-flipt/`. Inspect `runs/onetask-flipt-20260522T114650Z/attempts/.../normalized.json`, `boundary-proof.md`, `oco-events.ndjson` (steps 0 and 25 show the leaked subagent task invocations), and the materialized snapshot at `runs/onetask-flipt-20260522T114650Z/oco-config-snapshot/` to confirm current behavior before changing it.

### Why Qwen-specific prompts and not just always-on hardening

The production prompts on the Mac are already RFC2119-aware and work well on Claude / GPT-5.5. They use `MUST`/`MUST NOT` for hard invariants and softer language for taste. Qwen 3.6 27B is observed to ignore the softer language and follow only the literal `MUST` markers; the smoke that initially failed (PM emitted delegation prose without calling `task`) passed once we re-phrased the smoke prompt with explicit `MUST` markers. The Qwen variant codifies the same lesson into the persistent prompt: anywhere the production prompt says "should" or "prefer" or implies a behavior, the Qwen variant says `MUST` if the rule is actually a hard rule for benchmark execution.

This is a Qwen-targeted prompt adaptation, not a content rewrite. The semantic intent of every rule is preserved.

## Acceptance Criteria

Observable outcomes that prove the work is done. Each is verifiable from the materialized snapshot, the controller code, or a re-run of the Flipt single-task attempt.

1. **Native subagent disable shape in the materialized snapshot.** For every benchmark run, the materialized `opencode.jsonc` contains explicit `agent.<name>.disable: true` entries for at minimum `general`, `explore`, `test_runner`, `web-search`, `docs`. The materializer test suite asserts this. `strip-diff-manifest.json` records the disabled list.

2. **Stripped subagents unreachable from a real OCO process.** After materialization, `oco debug config` on the snapshot succeeds (existing programmatic test in the materializer suite). A fixture-level test against an isolated HOME confirms that the disabled subagents do not appear in OCO's resolved agent listing and that a `task` call referencing one of them would fail with "Unknown agent type". The Orchestrator chooses the most reliable mechanism to assert this (debug subcommand, JSON-formatted listing, in-process inspection of the materialized config + a synthetic `Agent.get` check via test harness, etc.) — but the assertion must be against a real OCO binary or a real OCO-source-backed test runner, not against the materializer's own data structures alone.

3. **Qwen-targeted prompt overlay applied conditionally.** When the materializer is configured for a Qwen-family model (e.g., model name contains "qwen" case-insensitive, or a configurable regex), the snapshot's `prompts/` directory contains the Qwen-hardened `pm.txt` and `orchestrator.txt` instead of the production files. The manifest records which agents had Qwen-overlay applied and the source path. When the materializer is configured for a non-Qwen model, the snapshot's `prompts/` is byte-equal to the production source.

4. **Qwen prompt hardening covers Aiden's MUST list.** The Qwen `pm.txt` MUST include unambiguous RFC2119 statements that the PM:
   - MUST write a spec file under a `specs/` subdirectory of the current working directory before delegating non-trivial work
   - MUST delegate non-trivial work to the Orchestrator via the `task` tool; doing the work directly is permitted ONLY when the task is verifiably trivial (single-file fix with proven cause, obvious typo, small config tweak)
   - MUST trust the autocompaction system to handle context overflow and MUST NOT attempt to manually trim or rewrite context
   The Qwen `orchestrator.txt` MUST include an unambiguous RFC2119 statement that the Orchestrator MUST run an Auditor on substantive changes and MUST iterate fix-and-re-audit until the Auditor returns PASS before calling `handoff_to_pm`.
   The Qwen variants MUST preserve all other rules and structure of the production prompts. Diff is additive/strengthening, not semantic rewrite.

5. **Boundary classifier requires syscall success.** Failed write-like syscalls (return value matching `= -1` followed by an error name) are not recorded as violations. A test fixture with synthetic strace lines proves this.

6. **Boundary classifier honors repo-cache allowlist.** The default real boundary config wires the configured `repo_cache_dir` into `BoundaryConfig.allowed_roots`. Writes under that directory by the benchmark's own `RepoCacheManager` during SETUP are not recorded as violations. A test proves this with the actual default config.

7. **Boundary classifier resolves relative paths against the parent fd when the `*at` syscall provides it.** When a relative path's parent fd corresponds to an already-allowed root, the classifier does not emit a `RELATIVE:<path>` violation. A test fixture with synthetic `openat`-style lines proves this. The Orchestrator MAY choose to instead bypass relative-path classification entirely when the strace line lacks fd resolution detail, provided the resulting behavior is documented and the test fixture covers it.

8. **Optional additional classifier hardening (Orchestrator judgment).** If the Orchestrator extends the syscall family list (mknod, truncate, chmod, xattr, etc.) or makes the ignore-prefix list config-driven, those changes MUST be covered by tests and called out in the audit handoff. If the Orchestrator declines to expand syscall families, that is acceptable; document the decision.

9. **No OCO source modifications.** `git status` and `git diff` on the OpenCodeOrchestra repo show no changes. All edits are inside `oco-benchmark/`.

10. **Existing test suite still passes.** All tests under `oco-benchmark/tests/` pass after changes. The dry-run gate (12 sub-gates) still passes. Scoped diff containment proven: no files modified outside `oco-benchmark/`.

11. **Auditor PASS on the full changeset.** Orchestrator runs the audit-fix-re-audit loop until a fresh Auditor returns PASS on the complete diff against this spec.

### Post-handoff validation (PM/Aiden, after pod upload — not the Orchestrator's responsibility)

After Orchestrator hands back, PM uploads the changed benchmark code to the pod, Aiden re-runs the Flipt single-task attempt, and PM inspects the new attempt's artifacts. The pod-side success bar (recorded here so the Orchestrator can include matching debug/log capture in the implementation, not so the Orchestrator must wait for it): the new attempt's `normalized.json` shows `tools_called` contains no `task:explore` or `task:test_runner` entries, `delegation_observed: true`, `audit_observed: true`; the new attempt's `boundary-proof.md` reports `passed: true` with zero violations. If the pod re-run fails any of these, PM dispatches a follow-up investigation or spec — it does not retroactively invalidate the Orchestrator's handoff.

## Verification

The Orchestrator runs all Mac-side verification (tests, builds, audits, dry-run gate, scoped diff check). Pod-side verification happens after PM uploads the changes to the pod and Aiden runs the Flipt re-attempt, as described in the post-handoff section above; that step is PM-driven and is not part of the Orchestrator's completion contract.

Recommended verification flow:
- Targeted unit tests for the materializer (disable shape, overlay selection, manifest fields)
- Targeted unit tests for the boundary classifier (success filter, allowed_roots respect, fd-aware relative resolution)
- The existing `oco debug config` programmatic validity test against the new materialized snapshot
- The dry-run gate end-to-end
- Scoped diff check `git status --short` outside `oco-benchmark/` returns empty
- Auditor pass

## Completion Standard

Done means: all 11 Orchestrator-owned acceptance criteria pass, the audit loop has terminated with PASS on a fresh Auditor against the full changeset, the implementation diff is contained inside `oco-benchmark/`, and the Orchestrator's `handoff_to_pm` summary includes per-criterion evidence (test names, test counts, file diffs at a glance, audit verdict) sufficient for PM to brief Aiden and stage the pod upload without reopening the work. The post-handoff pod re-run is PM/Aiden's responsibility and is not a gate on Orchestrator completion.
