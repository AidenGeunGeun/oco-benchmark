# H200 FP8 + MTP-4 Full-Run Collapse

Date finalized: 2026-05-24

## Summary

After the successful **Runpod B200 / Qwen3.6-27B-FP8** seeded-subset pilot, a cleaner standalone benchmark controller was used for a full 731-task SWE-bench Pro run on an H200 serving `Qwen/Qwen3.6-27B-FP8` with MTP-4 speculative decoding. The B200/FP8 pilot used the Runpod `vllm/vllm-openai:latest` pod template and did **not** use speculative decoding. The H200 run generated non-empty patches for all 731 tasks, but official evaluation collapsed to a much lower score.

Evidence note: earlier B200/NVFP4 experiments existed, but the archived final methodology note for the submitted 223-row positive bundle records the final reduced run as B200/FP8/non-MTP. This note follows that archived final record.

This should be treated as a **failed serving/controller-profile experiment**, not as the primary OCO result.

## What happened

The full H200/FP8/MTP-4 run produced:

- `208` pass
- `310` fail
- `213` empty-test outputs
- strict score: `208 / 731 = 28.45%`

The same current Modal/evaluator path was then used to replay the old B200 positive-pilot patch bundle. Replay reproduced the old result at roughly 70–73% strict over submitted rows.

Therefore, the low H200/FP8/MTP-4 score is not primarily explained by:

- Modal evaluator drift,
- raw-sample mismatch,
- patch sanitizer corruption,
- or the official evaluator path itself.

The regression happened during generation.

## Evidence that the current patches were genuinely worse

Five old-pass/current-regressed witnesses were inspected patch-by-patch:

| Repo | Old patch | Current patch | Current failure pattern |
|---|---|---|---|
| Flipt | Used evaluator-expected config/exporter symbols. | Invented incompatible config/exporter names. | Undefined `OTLPTracingConfig` / `Exporter`. |
| Navidrome | Added expected `tokenFromHeader` helper and JWT path. | Tweaked header mapping only. | Hidden/eval tests looked for undefined helper. |
| Vuls | Added macOS constants, EOL, and scanner wiring. | Stayed mostly in detector internals. | Missing macOS implementation surface. |
| Teleport | Added expected matcher interface and parser entry point. | Invented a different `NewMatcher` API and edited local tests around it. | Evaluator-expected helpers undefined. |
| OpenLibrary | Preserved expected language serialization shape. | Converted values to MARC-style codes and iterated a non-iterable object. | Assertion and type failures. |

These are ordinary code-quality regressions: undefined symbols, missing APIs, build failures, and semantic mismatches. They are not sane patches being mis-scored by the evaluator.

## Likely contributors

The run changed several variables at once, so this is not a clean causal experiment. Likely contributors include:

1. **Serving profile changed:** B200 FP8/non-MTP pilot vs H200 FP8/MTP-4 full run, plus controller/runtime changes.
2. **Controller changed:** standalone controller, different config materialization, different prompt overlays, different retry/continuation behavior.
3. **Prompt/tool surface changed:** stripped native subagents, Qwen-specific RFC2119 prompt overlay, different request-option placement.
4. **MTP-4 changed generation behavior:** in principle MTP verifies draft tokens with the target model, but in practice hard coding tasks can be sensitive to small decoding/numerical/tool-call differences over long trajectories.

The current evidence supports a generation-profile regression, not a precise one-variable diagnosis.

## What this does not prove

Do not claim:

- FP8 is generally bad for coding agents.
- MTP is generally bad for coding agents.
- H200 is worse than B200 for coding agents.
- Any particular B200 quantization path is superior in general.

The defensible claim is narrower:

> Token-throughput wins do not guarantee coding-agent quality. Serving-profile changes need task-level quality A/B tests, especially for long-horizon repository-level coding agents.

## Why it matters

The H200 run is still valuable because it documents a common systems failure mode: optimizing the inference server for throughput can silently degrade multi-step agent performance. Direct API benchmarks showed MTP-4 was much faster, but the full agent workload produced worse patches.

This is a useful negative result for ML systems and agent-infrastructure work.

## Follow-up if compute becomes available

A controlled serving-profile study should use a fixed small slice and compare:

- B200 FP8 non-MTP,
- H200 FP8 non-MTP,
- H200 FP8 MTP-2,
- H200 FP8 MTP-4,
- same prompts/controller/evaluator across all cells.

Until then, keep this as a diagnostic case study rather than a general claim about any one hardware or quantization path.
