# OpenCodeOrchestra SWE-bench Pro Pilot

This repository contains the benchmark harness, methodology notes, and report scaffold for evaluating **OpenCodeOrchestra (OCO)** on SWE-bench Pro.

OCO is a fork/extension of `opencode` focused on long-horizon coding-agent workflows: a Project Manager agent delegates to a persistent Orchestrator, which can call specialist agents such as Investigator and Auditor. The benchmark harness in this repo is a **consumer** of OCO: it invokes an installed `oco` CLI binary, isolates per-attempt runtime state, collects telemetry, prepares SWE-bench Pro evaluation bundles, and records methodology decisions.

This is an **engineering/ML-systems pilot**, not a leaderboard claim.

## Main result

The strongest positive result came from an earlier **Runpod B200** seeded-subset pilot using **Qwen3.6-27B-FP8** on a 250-task seeded subset of the SWE-bench Pro public split. The model was served with the Runpod `vllm/vllm-openai:latest` pod template. **Speculative decoding was not used** for this B200/FP8 pilot.

> Evidence note: earlier B200/NVFP4 experiments existed, and some legacy filenames still say `old-nvfp4`. The archived final methodology note for the 223-row submitted positive bundle records `Qwen/Qwen3.6-27B-FP8`, B200, non-MTP. This README follows that archived final methodology record.

| Framing | Result | Wilson 95% CI | Interpretation |
|---|---:|---:|---|
| Artifact-adjusted diagnostic | **162 / 219 = 74.0%** | **67.8%–79.3%** | Excludes only evaluator/tooling artifacts where the evaluator did not meaningfully test the patch. |
| Strict submitted-row | **162 / 223 = 72.6%** | **66.4%–78.1%** | Counts all submitted evaluator rows, including real `tests: []` non-resolutions, as non-pass. |
| Known-only diagnostic | **162 / 205 = 79.0%** | **72.9%–84.0%** | Excludes all empty-test outputs; optimistic diagnostic only. |
| Conservative seeded-subset | **162 / 250 = 64.8%** | **58.7%–70.5%** | Treats unevaluated/non-submitted seeded-subset rows as non-pass. |

The recommended public phrasing is:

> OCO with Qwen3.6-27B-FP8 on a non-speculative B200 serving profile resolved 162 tasks in a 223-row SWE-bench Pro seeded-subset pilot. Strict score over submitted evaluator rows was 72.6%; artifact-adjusted diagnostic score was 74.0% after excluding only evaluator/tooling artifacts where the evaluator did not meaningfully test the patch.

See [`docs/result-summary.md`](docs/result-summary.md) for the frozen result table, denominator policy, per-repo replay breakdown, and sanity-witness checks.

## Important caveats

- This was a **seeded 250-task subset**, not the full 731-task public SWE-bench Pro split.
- There is no matched same-model/same-serving baseline against mini-SWE-agent, SWE-agent, OpenHands, or a single-agent OCO variant.
- The audit loop was available in OCO but not consistently enforced/observed in the positive pilot, so this should not be described as an isolated audit-loop result.
- The artifact-adjusted number is a diagnostic validation-quality number, not a replacement for strict reporting.
- The result is best understood as evidence that orchestration/runtime design and serving configuration are important variables for coding-agent performance, not as proof that OCO is state of the art.

## Negative result: serving-profile collapse

A later full 731-task run using a new standalone controller with H200 FP8 + MTP-4 generated much worse patches. Official Modal evaluation on the full run produced 208 pass / 310 fail / 213 empty-test outputs over 731 tasks.

This was not primarily an evaluator problem. Replaying the old B200 patch bundle through the same current Modal/evaluator path reproduced the earlier result at roughly 70–73%. Five old-pass/current-regressed witness tasks showed ordinary code-quality regressions in the new run: undefined symbols, incomplete APIs, build failures, and semantic mismatches.

The lesson is not “MTP is bad” or “FP8 is bad.” The defensible conclusion is narrower:

> Throughput-optimized serving profiles can silently change generation quality for hard coding-agent workloads. Direct token-throughput A/B tests are not enough; serving changes need quality A/B tests on representative agent tasks.

See [`docs/h200-fp8-mtp4-collapse.md`](docs/h200-fp8-mtp4-collapse.md) and [`docs/current-vs-nvfp4-regression-forensics.md`](docs/current-vs-nvfp4-regression-forensics.md).

## Repository map

| Path | Purpose |
|---|---|
| [`controller/`](controller/) | Benchmark controller: attempt lifecycle, OCO subprocess adapter, materializer, eval bundle tooling. |
| [`scripts/`](scripts/) | CLI entry points for task materialization, benchmark launch, post-first-pass classification, eval bundle preparation. |
| [`docs/result-summary.md`](docs/result-summary.md) | Frozen positive pilot result and denominator policy. |
| [`docs/methodology-notes.md`](docs/methodology-notes.md) | Live-run methodology notes for the H200/FP8 full-run attempt. |
| [`docs/h200-fp8-mtp4-collapse.md`](docs/h200-fp8-mtp4-collapse.md) | Clean writeup of the serving-profile collapse. |
| [`docs/related-work-positioning.md`](docs/related-work-positioning.md) | Research-positioning memo and venue/claim guidance. |
| [`paper/`](paper/) | Technical-report source, compiled PDF, bibliography, and claims/threats companion note. |
| [`specs/`](specs/) | Implementation specs used while building the benchmark harness. |
| [`tests/`](tests/) | Unit tests for controller, materializer, telemetry, eval-bundle, and post-first-pass tooling. |

Large run artifacts are intentionally not committed to Git. The local long-term archive lives on external storage under:

```text
/Volumes/external-nvme/oco-benchmark-archive/oco-vs-mini-swe-agent/
```

## Current public framing

The most credible artifact today is a **technical report / systems case study**:

1. OCO implements durable multi-agent orchestration concepts for repository-level coding agents.
2. A seeded SWE-bench Pro pilot with a small open model produced an unusually strong result under transparent denominators.
3. A later full-run serving-profile change caused a generation-side collapse, demonstrating that deployment profile is a first-class experimental variable.
4. The work is not yet a controlled benchmark paper because it lacks a matched baseline and full controlled ablations.

## Requirements for rerunning the harness

- OCO 2.1.8+ installed and on `PATH`.
- Python 3.13+.
- A vLLM-compatible GPU serving a Qwen/OpenAI-compatible endpoint.
- Modal credentials for official SWE-bench Pro evaluation if using the Modal path.

See [`docs/final-run-and-modal-eval-runbook.md`](docs/final-run-and-modal-eval-runbook.md) for operational details from the completed run.

## License

TBD.
