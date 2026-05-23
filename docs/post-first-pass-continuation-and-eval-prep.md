# Post-First-Pass Continuation and Eval Prep

This document records the post-first-pass policy and local tooling for run `swepro731-qwen-h200-mtp4-live-20260522T135306Z` and future runs. The live first pass is not mutated by these tools; all pod-facing actions are explicit commands to run after the first-pass drivers are done.

## Attempt Classifier

The classifier uses this closed set:

- `clean_patch`: precheck-passing non-empty patch queued for evaluation.
- `stopped_no_patch`: completed OCO attempt stopped without a captured patch.
- `malformed_tool_prose_no_patch`: no patch and evidence of prose/XML/JSON tool-call markup instead of a real tool call.
- `delegated_or_midstream_no_patch`: no patch after delegation or a tool-call/midstream stop.
- `output_length_no_patch`: no patch with finish-reason or token evidence of output-length truncation.
- `explicit_no_fix`: no patch because the agent explicitly concluded no viable fix.
- `precheck_failed`: non-empty patch failed local apply/precheck and was not evaluator-ready.
- `timeout`: attempt timed out before producing an evaluator-ready patch.
- `subprocess_provider_infra_failure`: OCO/provider/subprocess failed before an evaluator-ready patch.
- `incomplete`: attempt artifacts are missing or phase completion is incomplete.
- `unknown`: evidence is insufficient for any other class; the row must include `unknown_reason`.

Every classification row records the decision evidence fields: patch/precheck state, step count, tool count, finish reason, output-length signal, delegation signal, subprocess return/timing state, timeout flag, and a short excerpt or artifact pointer.

Continuation eligibility is deterministic and evaluator-blind. The eligible classes are `stopped_no_patch`, `malformed_tool_prose_no_patch`, `delegated_or_midstream_no_patch`, and `output_length_no_patch`. The tooling writes one eligible ID file, one excluded ID file, and per-class excluded ID files. It never selects by repository, expected difficulty, patch quality, or evaluator result.

## Continuation Prompt

The standardized continuation prompt is checked in at `prompts.qwen/post_first_pass_continuation.txt`. It tells the agent to continue from current state, not ask permission, not restate the plan, use real tools rather than prose markup, inspect/resume Orchestrator work if relevant, and stop only with a non-empty evaluator-ready patch or an explicit no-viable-fix conclusion.

## Output-Token Cap

The first pass accidentally used the old 32k output cap. Continuation and future runs use 81,920 in both places OCO reads:

- materialized `model.limit.output` in the isolated config snapshot;
- `OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX=81920` in the OCO subprocess environment.

Existing first-pass snapshots are not rewritten. New snapshots and continuation runs use the corrected default unless a caller explicitly overrides `--output-token-limit`.

## Boundary and Background Hotfixes

When boundary proof is disabled, the real adapter does not wrap OCO in `strace`. The executed command remains visible in `oco-subprocess.json` so reviewers can verify that boundary-disabled runs are not strace-wrapped.

OCO subprocesses are launched with safe stdin (`/dev/null`) instead of inheriting the controller caller's stdin. This prevents background/nohup launches from crashing OCO when fd 0 is closed.

## Eval Bundles

First-pass and post-continuation bundles stay separate:

- First-pass clean bundle: only first-pass clean patches, with empty/missing/non-clean attempts counted as first-pass failures in reporting.
- Post-continuation final bundle: continuation patches take precedence for continued attempts; otherwise first-pass clean patches are used. The manifest records patch source counts and preserves the 731 denominator note.

Turn-count sensitivity is preserved. The classifier reports step/tool-count distributions and IDs above 200; no new hard 200-turn cap is added.
