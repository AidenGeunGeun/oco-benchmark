# Post-First-Pass Continuation and Eval Prep

This document records the post-first-pass policy and tooling for the SWE-bench Pro 731 run. The first-pass run is treated as a read-only input; all post-first-pass actions write into new, separate run roots so the first-pass artifacts can be audited unchanged.

For the run record, deviations from plan, and writeup-ready methodology, see [`methodology-notes.md`](methodology-notes.md).

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

### Classifier regex fix (2026-05-23)

The initial run of the classifier produced 60 `subprocess_provider_infra_failure` rows on attempts that had clean returncodes, no timeouts, and real step/tool work. The `INFRA_FAILURE_PATTERN` regex contained a loose `5\d\d` alternative meant to catch HTTP 5xx codes but matched any 3-digit substring in evidence text — Unix timestamps (`1779**532**136`) and token-count fields (`completion_tokens: 523`) triggered false positives.

The regex was tightened to require HTTP-status context (`HTTP/x.y 5xx`, `status_code: 5xx`, `5xx Internal/Bad/Service/Gateway`, etc.). Genuine signals (`ECONNREFUSED`, `ECONNRESET`, `ETIMEDOUT`, `rate limit`, `provider error`, `vLLM error`, `upstream timeout`) still match. Regression tests cover both halves plus an end-to-end fixture reproducing the live H200 BACKUP_NOOP timestamp case. Fix committed at `e273aab`.

Post-fix distribution on the live 731 run: 596 `clean_patch`, 123 `output_length_no_patch`, 6 `subprocess_provider_infra_failure`, 3 `stopped_no_patch`, 2 `malformed_tool_prose_no_patch`, 1 `timeout`. Continuation-eligible: 128. Real first-pass failures: 7.

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

## Continuation vs fresh-rerun split (live run, 2026-05-23)

`prepare-continuation-run` copies the first-pass `worktree` + `oco-home` into a new continuation run root so the agent can resume from the exact state where it stopped. On the live 731 run, only 50 of the 128 eligible attempts still had a `worktree` to copy — the other 78 lost their worktrees during a first-pass disk-recovery cleanup (clean-attempt and all-completed-attempt worktrees were pruned to make room when `/workspace` hit 90% used).

The recovery wave therefore launches as two concurrent sub-waves under one c=14 budget against the same vLLM endpoint:

| Sub-wave | Count | Run mode | Prompt | Output cap | Starting state |
|---|---|---|---|---|---|
| Continuation | 50 | `--continuation-mode --prompt-mode continuation` | `prompts.qwen/post_first_pass_continuation.txt` | 81,920 | Copied first-pass worktree + oco-home |
| Fresh rerun | 78 | first-pass mode | normal first-pass prompt | 81,920 | New worktree from base commit |

Both sub-waves write to separate run roots (`...-continuation-<ts>` and `...-rerun-<ts>`) so the writeup can report continuation, rerun, and combined results independently.

## Eval Bundles

First-pass, continuation, and combined bundles stay separate:

- **First-pass clean bundle**: only first-pass clean patches, with empty/missing/non-clean attempts counted as first-pass failures in reporting.
- **Post-continuation final bundle**: continuation patches take precedence for continued attempts; otherwise first-pass clean patches are used. For attempts that fell into the fresh-rerun sub-wave, the rerun patch is treated as the post-continuation result for that attempt. The manifest records patch source counts (`first_pass` / `continuation` / `rerun`) and preserves the 731 denominator note.

Turn-count sensitivity is preserved. The classifier reports step/tool-count distributions and IDs above 200; no new hard 200-turn cap is added.

## Wave3 and later continuation waves

After the continuation + fresh-rerun recovery wave, the combined generation state was verified from disk as 692 evaluator-ready patches and 39 remaining no-patch tasks across the full 731-task set.

The 39 were split by state availability:

- 33 with usable latest state (`worktree` + `oco-home`) copied into the Wave3 run root;
- 6 without usable worktree state, rerun fresh from base commit.

Wave3 was launched because the remaining attempts were still well below the comparison scaffold's turn budget. The later stopping rule is cumulative per-task agent-loop budget, not the wave number.

Operational notes from Wave3 setup:

- Parallel drivers raced on config snapshot materialization; the safe workaround is to prime the run root with one single-ID driver, then fan out the remaining shards after `oco-config-snapshot/` exists.
- Pod-local RAM watermark code was corrected to use `/proc/meminfo` `MemAvailable` rather than `MemFree`; this must be committed before future paid runs.
- `max_ram_pause_checks` was increased to tolerate temporary memory pressure instead of exiting after a short wait.

## Later continuation waves and final source-patch bundle

After Wave3, 9 tasks still had no patch. Under the agreed agent-loop budget policy, these were still well below the comparison scaffold's turn budget, so continuation proceeded uniformly rather than stopping on an arbitrary wave count. Final generation reached 731 / 731 non-empty patches after Wave8.

The winning patch source counts were:

- first pass: 596
- continuation: 43
- fresh rerun: 53
- Wave3: 30
- Wave4: 7
- Wave5: 1
- Wave8: 1

The raw final bundle is preserved at `/workspace/runs/swepro731-final-eval-bundle-20260523T184500Z`. The primary official-evaluation bundle is the sanitized source-patch bundle at `/workspace/runs/swepro731-final-eval-bundle-sanitized-20260523T190000Z`.

Sanitization removed generated/binary/build artifacts while preserving all 731 task rows. Removed diffs are recorded in `removed-generated-artifacts.json`. The removal policy drops generated binaries named `flipt`, `tsh`, and `tctl`; `.cue-src/`; paths containing `/node_modules/`, `/dist/`, `/build/`, `/coverage/`; and files ending `.map` or `.min.js`. This reduced `patches.json` from 421,863,677 bytes to 18,460,372 bytes while keeping 731 / 731 non-empty patches.
