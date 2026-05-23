# Methodology Notes

Concise run-record, deviations from plan, and writeup-ready methodology for the OCO + Qwen3.6-27B-FP8 SWE-bench Pro 731 run. Source of truth for the eventual writeup.

## 1. What we measured

OCO 2.1.8 (CLI binary on the pod) driving Qwen3.6-27B-FP8 over the 731-task public SWE-bench Pro test set. Self-hosted vLLM 0.21.0 on a Runpod H200 SXM Community Cloud pod with MTP-4 speculative decoding. Everything (vLLM, controller, OCO subprocesses) runs on the same pod over localhost; no Cloudflare or proxy on the agent ↔ model hot path.

OCO 2.1.8 contains the changes required for headless self-hosted operation: non-streaming tool-call mode (`experimentalNonStreamingToolCalls`), empty-steps fallback so subagent results aren't lost when the AI SDK returns `steps: []`, `--wait-for-children` so the headless `oco run` doesn't dispose persistent Orchestrator sessions before they finish, glob/grep timeout guards, and safe-stdin handling for `nohup` launches.

Reference scaffold: Qwen's published model-card Qwen3.6-27B SWE-bench Pro = 53.5 (privately-corrected Pro set, Qwen's internal agent scaffold, sampling matched here). Comparison is direction-of-effect, not controlled — see plan §11.3 for full caveats (different scaffold, FP8 quantization, autocompaction policy, prompt set, privately-corrected vs public Pro set, training-data overlap).

## 2. Serving profile (locked at boot)

- Model: `Qwen/Qwen3.6-27B-FP8` served as `selfhost-qwen`, 200K max model length
- MTP-4 (`qwen3_next_mtp`, `num_speculative_tokens=4`) — won the §4.4 A/B against non-MTP and MTP-2 at c1/c4/c8/c16/c20 direct API
- FP8 KV cache, FlashInfer attention, FlashInfer GDN prefill, prefix caching, chunked prefill
- `--max-num-seqs 16`, `--max-num-batched-tokens 32768`, `--gpu-memory-utilization 0.95`
- `--reasoning-parser qwen3`, `--tool-call-parser qwen3_coder`
- Non-streaming OCO via `selfHostedNonStreamingToolCalls: true` → `providerOptions.experimentalNonStreamingToolCalls: true` (vLLM 0.21.x streaming tool-call parsing has open bugs for Qwen3-coder; non-streaming is the safer path)

## 3. Sampling (matched to Qwen's own SWE-bench scaffold)

`temperature=1.0`, `top_p=0.95`, `top_k=20`, `min_p=0.0`, `presence_penalty=0.0`, `repetition_penalty=1.0`, `enable_thinking=true`, `preserve_thinking=true`. Per-task deterministic sampling seed derived from task ID and passed to vLLM, recorded in each attempt's `normalized.json`. Sources are quoted in plan §6.5.

## 4. Deviations from plan

| Plan item | Plan position | What actually happened | Disclosure handling |
|---|---|---|---|
| Calibration batch | §9: 20-30 stress-picked tasks before paid full-run | Skipped after one Flipt single-task verification ran clean | Reported as "no formal calibration"; first-pass numbers stand on their own |
| Output token cap | §6.5: `max_tokens: 81920` | First pass used a 32k cap because the controller defaulted to OCO's internal `OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX=32000` and did not override it | Recovery wave + future runs corrected to 81,920 in both `model.limit.output` and the OCO env var; first-pass output-length stops are still counted as first-pass fails |
| Boundary proof | §5.1 + §9.2: `strace`-wrapped per-attempt | Disabled for full run after `strace -f` plus high-parallelism launch caused EBADF startup failures in OCO | Documented in `oco-subprocess.json.executed_command`; boundary is informational only for this run |
| Mac rsync backup | §3.4 + §7: every 5 min | Not configured for this run | Single-point-of-failure risk on Community Cloud H200 accepted; mitigated by per-attempt artifact durability inside the pod |
| RAM watermark | §8: 90% | Live-tweaked to 95% with a longer pause window after early `RAM_PAUSE` exits during the first pass | Pod-local hotfix; the durable equivalent is captured in the runner's wait-for-children + safe-stdin commits, not the watermark constants |

None of these change what is being measured (OCO + Qwen3.6-27B-FP8 vs the Qwen reference scaffold). They affect what counts as first-pass vs recovery and how much methodology disclosure the writeup carries.

## 5. First-pass classification (closed set)

After classifier fix at commit `e273aab` (see §6 below for the bug history), the 731 first-pass attempts decompose as:

| Class | Count | Continuation-eligible | Notes |
|---|---|---|---|
| `clean_patch` | 596 | no | Evaluator-ready: queued AND precheck passed AND non-empty patch |
| `output_length_no_patch` | 123 | yes | 32k output-cap signal or `finish_reason=length`; recovery wave uses 81,920 |
| `subprocess_provider_infra_failure` | 6 | no | Real OCO/provider crashes (returncode ≠ 0, real provider/network error markers). Rerun-eligible, not continuation-eligible |
| `stopped_no_patch` | 3 | yes | Completed attempt without an evaluator-ready patch |
| `malformed_tool_prose_no_patch` | 2 | yes | Tool-call prose/XML emitted as text instead of an actual `task` call (vLLM qwen3_coder parser artifact) |
| `timeout` | 1 | no | Per-attempt wall-time cap exceeded |

Continuation-eligible: 128. Real first-pass failures left as-is for the headline: 7 (6 infra + 1 timeout).

## 6. Classifier regex bug and fix

The initial run of the classifier reported 60 attempts in `subprocess_provider_infra_failure`. Inspection showed every one of them had `returncode=0`, no timeout, real step/tool work, and benign phase-log content. Root cause: the `INFRA_FAILURE_PATTERN` regex contained an unbounded `5\d\d` alternative meant to catch HTTP 5xx codes, but it matched any 3-digit substring in evidence text — most commonly Unix timestamps like `1779`**`532`**`136` and token counts like `completion_tokens: 523`.

Fix (commit `e273aab`): tightened the regex to require HTTP-status context (`HTTP/x.y 5xx`, `status_code: 5xx`, `5xx Internal / Bad / Service / Gateway`, etc.). Genuine signals (`ECONNREFUSED`, `ECONNRESET`, `ETIMEDOUT`, `rate limit`, `provider error`, `vLLM error`, `upstream timeout`, etc.) still match. Two regression tests cover the benign false-positive blobs and the real provider-failure blobs; an end-to-end test reproduces the live H200 BACKUP_NOOP timestamp case.

After the fix, the 60 dropped to 6 real infra failures; the misclassified 54 returned to `output_length_no_patch` (their genuine class).

## 7. Recovery wave

The 128 continuation-eligible attempts split into two sub-waves because some first-pass worktrees were deleted during a mid-run disk-recovery pass (see §4 deviations, RAM watermark row).

| Sub-wave | Count | Mode | Prompt | Output cap | State |
|---|---|---|---|---|---|
| Continuation | 50 | `--continuation-mode --prompt-mode continuation` | `prompts.qwen/post_first_pass_continuation.txt` | 81,920 | First-pass `worktree` + `oco-home` copied into new run root |
| Fresh rerun | 78 | first-pass mode | normal first-pass prompt | 81,920 | New worktree from base commit; no carried state |

Both sub-waves run concurrently against the same vLLM endpoint, c=14 total (5 continuation + 9 rerun shards). Patches are durable per attempt; Modal evaluation does not wait for the whole wave.

Continuation prompt is evaluator-blind. It instructs the agent to continue from existing state, not ask permission, not restate the plan, use real tools rather than prose tool-call markup, inspect/resume child Orchestrator work if relevant, and stop only after producing a non-empty patch or explicitly concluding no viable fix is possible.

## 8. Reporting policy (writeup)

Pass rate is reported in three stages over the same 731-task denominator:

- **First pass only** — generation completed with original 32k cap. Empty/missing patches count as fails.
- **First pass + continuation** — adds the 50 true-continuation results.
- **First pass + continuation + fresh rerun** — adds the 78 fresh reruns. This is the headline.

No tasks are filtered out of any stage. The 7 non-eligible failures (6 infra + 1 timeout) count as fails in every stage. This mirrors how serious SWE-bench scaffolds (`scaleapi/SWE-bench_Pro-os` SWE-Agent config with `per_instance_call_limit: 150`; mini-swe-agent with `step_limit: 250` + explicit submit) treat their agent loop budget — fail at the limit, not on the first assistant stop.

Supplementary tables in the writeup (per plan §11.4): Wilson 95% binomial CI on the headline, per-repo breakdown across the 11 Pro repos, per-stratum (`delegation_observed` / `audit_observed` / `full_loop_observed`), token-economy table + prefix-cache hit rate + compaction-token share, realized cost ledger, thinking-token diagnostic, turn-count sensitivity (≤ 200 vs > 200).

## 9. Known parser and provider artifacts

- vLLM 0.21.0 `qwen3_coder` tool parser raises `ValueError: substring not found` on certain malformed tool-call shapes mid-stream. vLLM continues serving (HTTP 200, content without parsed tool call). Effect on OCO: occasional empty-patch attempts or "I will delegate" prose without a real `task` call. These land in `malformed_tool_prose_no_patch` or `output_length_no_patch` and are continuation-eligible. Not a benchmark stop condition.
- vLLM startup emits FP8 uncalibrated `q_scale` / `prob_scale` warnings on the Qwen3.6-27B-FP8 checkpoint. This is the same checkpoint Qwen used for the 53.5 reference, so the warning is symmetric to the baseline. Re-quantizing would break apples-to-apples on the model variable; we accept the small accuracy bias and document it.
