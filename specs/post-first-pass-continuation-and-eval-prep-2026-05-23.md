# Post-First-Pass Continuation and Eval Prep

## Intent

Prepare the benchmark controller for the moment the live 731-task first pass finishes, without disturbing the running pod workload.

The current first pass is a real diagnostic pass, but it is not the final generation policy because many non-clean attempts are model/harness stops before a submitted patch, not completed SWE-bench-style agent loops. Serious SWE-bench Pro scaffolds keep interacting until submit, give-up, timeout, or turn limit. This work gives us a deterministic, evaluator-blind way to classify the first pass, continue eligible stopped attempts once, and produce clear evaluation bundles and methodology artifacts.

## Scope

In scope:

- `oco-benchmark/` only.
- Local controller/scripts/docs/tests that can be developed on the Mac while the pod continues running.
- Post-first-pass analysis and continuation tooling for run `swepro731-qwen-h200-mtp4-live-20260522T135306Z` and future runs.
- Controller safety fixes already proven on the pod but not yet represented cleanly in the repo: boundary-disabled runs must not invoke `strace`, and background/nohup runs must not crash OCO because stdin is closed.
- Output-cap correction for future continuation/future runs: Qwen-style 81,920 output tokens must be represented in the materialized model limit and in the OCO process environment.

Out of scope:

- Any OCO source change.
- Any change to the live first-pass processes, active attempt directories, vLLM serving profile, pod lifecycle, or current first-pass materialized config.
- Any new hard 200-turn cap.
- Any selective human nudging of individual tasks.
- Any Modal evaluation launch before the first pass and continuation wave artifacts are intentionally bundled.

## Context

The live run currently uses H200 + Qwen3.6-27B-FP8 + MTP-4 on pod `uoj16ihxwsdnef`, with launch-level concurrency 14 and run id `swepro731-qwen-h200-mtp4-live-20260522T135306Z`.

Important facts already established:

- Boundary proof via `strace` is disabled methodologically for the full run because parallel `strace` wrapping caused EBADF startup failures. The current pod has a local hotfix that bypasses `strace`; the repo must make that behavior explicit and tested.
- Background/nohup launch also exposed an OCO stdin EBADF crash. The current pod has a local hotfix using a safe stdin for subprocesses; the repo must make that behavior explicit and tested.
- The first-pass output cap is unintentionally 32k because OCO caps output by both model limit and `OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX`. The continuation/future-run path must set both to 81,920. The live first pass must not be mutated mid-run.
- Bad-attempt samples so far show no systemic patch extractor/provider crash issue. Most no-patch attempts are stopped-before-edit, permission/confirmation asks, malformed tool-call prose/XML, delegated/midstream no-patch, or output-length stops.
- SWE-bench Pro itself does not define no-patch continuation policy, but the paper and reproduction scaffolds use iterative agent loops with turn limits and submit/review mechanics. A single assistant stop after planning is not equivalent to a completed agent-loop attempt.

## Desired Outcome

After implementation, we can safely do the following once the first pass finishes:

1. Classify every non-clean completed attempt into a small, documented taxonomy.
2. Pull deeper diagnostics only for delegated/midstream no-patch attempts, without pulling worktrees or traces.
3. Produce deterministic continuation ID files and a single standardized continuation prompt for all eligible attempts.
4. Resume only eligible attempts under the corrected 81,920 output-token cap, with `strace` disabled and safe subprocess stdin.
5. Generate first-pass and post-continuation eval bundles with denominator notes that make first-pass, continuation, and final scoring clearly separable.
6. Preserve the methodology rationale in docs so the final writeup does not look like post-hoc marketing.

## Acceptance Criteria

1. **No live-run mutation:** implementation and tests do not read from or write to the live pod. Any pod-facing output is a pasteable command block for Aiden to run later.
2. **Boundary-disabled means no `strace`:** when boundary proof is disabled, the OCO subprocess command is not wrapped by `strace`; the executed command artifact makes this observable.
3. **Background subprocess stdin is safe:** OCO subprocesses launched by the real adapter do not inherit a potentially closed caller stdin; a regression test covers the nohup/background failure class.
4. **Output cap fixed for future runs:** controller configuration and CLI expose an 81,920 output-token setting for continuation/future runs; the materialized model limit reflects it; the OCO subprocess environment includes the matching `OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX` value. Existing first-pass artifacts are not rewritten.
5. **Classifier closed set:** completed non-clean attempts are classified into a documented closed set that includes at least clean patch, stopped/no-patch, malformed-tool-prose/no-patch, delegated-or-midstream/no-patch, output-length/no-patch, explicit-no-fix, precheck-failed, timeout, subprocess/provider/infra failure, incomplete, and unknown. Unknown is allowed only when evidence is insufficient and must include a reason.
6. **Classifier evidence:** classifier output records the fields used for each decision, including patch/precheck state, step count, tool count, finish reason, output-length signal, delegation signal, subprocess status, timeout flag, and a short evidence excerpt or artifact pointer where available.
7. **Delegated diagnostics bundle:** a script or command can package only delegated/midstream no-patch attempts' metadata, full OCO event stream, OCO SQLite DB/WAL/SHM, stdout/stderr tails, OCO log tails, normalized/phase/subprocess/patch artifacts, and excludes worktrees, repo caches, `filesystem-trace.log`, and broad logs.
8. **Continuation prompt is standardized:** one evaluator-blind continuation prompt is checked into the repo or emitted by the tooling. It tells the agent to continue from existing state, not ask permission, not restate the plan, use real tools rather than prose markup, inspect/resume Orchestrator work if relevant, and stop only with a non-empty evaluator-ready patch or an explicit no-viable-fix conclusion.
9. **Continuation selection is deterministic:** tooling emits IDs files for the eligible continuation set and excluded sets, with counts by class. It does not choose tasks based on patch quality, repo, expected difficulty, or any evaluator result.
10. **First-pass and final bundles stay separate:** eval bundle prep can produce a first-pass clean bundle and a post-continuation final bundle with manifest notes that preserve the 731 denominator and explain excluded/non-clean classes.
11. **Turn-count sensitivity preserved:** no hard 200-turn cap is added; tooling/reporting records step/tool-count distributions and can identify attempts over 200 action/model cycles for writeup sensitivity analysis.
12. **Methodology docs updated:** docs explicitly record the continuation rationale, first-pass 32k cap deviation, 81,920 correction for continuation/future runs, disabled boundary proof due parallel `strace` EBADF, and first-pass vs after-continuation reporting separation.
13. **Tests pass:** relevant targeted tests plus the existing test suite pass locally. Dry-run gate remains 12/12.
14. **Scope containment:** implementation diff stays inside `oco-benchmark/`; no OCO source, production prompts, pod files, or external archive files are modified.

## Verification

- Unit tests for real-adapter no-strace and safe-stdin behavior.
- Unit tests or fixture tests for classifier closed-set behavior and continuation eligibility.
- Unit tests or fixture tests for output-cap materialization and OCO subprocess environment.
- Fixture bundle generation for first-pass vs continuation-final manifests.
- Dry-run gate.
- Auditor review after implementation.

## Completion Standard

Done means the repo contains audited tooling and docs for the post-first-pass workflow, and Aiden has concise pasteable pod commands for:

1. Read-only final first-pass status.
2. Classification/export after drivers are done.
3. Optional delegated/midstream diagnostic bundle upload.
4. Continuation shard launch with 81,920 output-token cap and boundary disabled.
5. First-pass and post-continuation eval bundle generation.
