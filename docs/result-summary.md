# Result Summary: B200/FP8 SWE-bench Pro Seeded-Subset Pilot

Date finalized: 2026-05-24

## One-sentence result

In a 223-row SWE-bench Pro seeded-subset pilot, **OpenCodeOrchestra + Qwen3.6-27B-FP8 on a non-speculative B200 serving profile resolved 162 tasks**, yielding **72.6% strict** over submitted evaluator rows and **74.0% artifact-adjusted diagnostic** after excluding only evaluator/tooling artifacts where the evaluator did not meaningfully test the patch.

The positive pilot used a **Runpod B200** pod with the `vllm/vllm-openai:latest` template. The archived final methodology note records model `Qwen/Qwen3.6-27B-FP8`, served as `selfhost-qwen`. **Speculative decoding was not enabled** for this B200/FP8 run.

Evidence note: earlier B200/NVFP4 experiments existed, and legacy replay names still include `old-nvfp4`. The archived final methodology note for the final 223-row submitted bundle supersedes those labels for the public result.

## Result table

| Denominator | Count | Rate | Wilson 95% CI | Use in writeup |
|---|---:|---:|---:|---|
| Artifact-adjusted diagnostic | 162 / 219 | **74.0%** | **67.8%–79.3%** | Preferred validation-quality number, clearly labeled diagnostic. |
| Strict submitted-row | 162 / 223 | **72.6%** | **66.4%–78.1%** | Primary strict official-style number over submitted evaluator rows. |
| Known-only diagnostic | 162 / 205 | **79.0%** | **72.9%–84.0%** | Optimistic diagnostic; excludes all empty-test rows. |
| Conservative seeded-subset | 162 / 250 | **64.8%** | **58.7%–70.5%** | Lower-bound framing if all non-submitted seeded rows are counted as non-pass. |

## Denominator policy

The report should show all four denominators side by side. The artifact-adjusted diagnostic number is fair for validation quality, but it is not a leaderboard replacement.

Definitions:

- **Pass:** official evaluator output marks the task resolved.
- **Fail:** official evaluator output runs meaningful tests and the patch does not resolve the task, or stdout/stderr show a real build/test/runtime non-resolution.
- **Real `tests: []` non-resolution:** evaluator output has `tests: []`, but logs show the patch failed by build, import, runtime, or test-collection behavior caused by the patch or repo state. These remain failures.
- **Evaluator artifact:** evaluator did not meaningfully test the patch due environment/tooling setup issues such as missing browser/toolchain/helper import problems. These are excluded only in the artifact-adjusted diagnostic denominator.

Unknown investigation on the replayed 223 rows found no hidden wins. The empty-test rows were classified as mostly real non-resolutions, with a small number of evaluator artifacts.

## Seeded 250-task subset construction

The seeded subset was not hand-picked. It was produced by the earlier benchmark runner's source-code path:

1. Load the public SWE-bench Pro rows.
2. Group rows by repository.
3. Within each repository, sort rows by `sha256(seed + ":instance:" + instance_id)`.
4. Sort repository order by `sha256(seed + ":repo:" + repo)`.
5. Select rows by round-robin across that seeded repository order until the requested count is reached.
6. For the final seeded-250 pilot, take the first 250 rows from the already materialized full-731 order created by that same seeded SHA-256 repo-balanced round-robin procedure.

The archived preparation report records the method as `first_N_from_existing_seeded_sha256_repo_round_robin_full731_order` with rationale: the full-731 order was already seeded pseudo-random within repository and round-robin across repositories, so taking the first 250 preserved the non-hand-curated order while retaining existing completed attempts.

## Current best headline wording

> OCO + Qwen3.6-27B-FP8 on a non-speculative Runpod B200 serving profile resolved 162 tasks in a 223-row SWE-bench Pro seeded-subset pilot. Strict score over submitted evaluator rows was 72.6% (Wilson 95% CI 66.4–78.1). An artifact-adjusted diagnostic score, excluding only evaluator/tooling artifacts where the evaluator did not meaningfully test the patch, was 74.0% (67.8–79.3).

Avoid saying:

- “OCO is SOTA on SWE-bench Pro.”
- “The audit loop caused the score.”
- “Small open models match frontier models in general.”
- “The result proves hierarchy caused the lift.”

## Replay validation

The old B200 positive-pilot patch bundle was replayed through the current Modal/evaluator path. Replay reproduced the positive result:

- replay root on pod: `/workspace/runs/old-nvfp4-final223-current-evaluator-modal-25w-20260524T070558Z`
- parsed result: `162 pass / 43 fail / 18 empty-test rows` over 223 submitted rows
- strict replay score: `162 / 223 = 72.6%`
- known-only replay score: `162 / 205 = 79.0%`

This clears the current Modal/evaluator stack as the main cause of the later H200/FP8/MTP-4 full-run collapse.

## Per-repo strict replay breakdown

| Repo | Pass / Total | Strict rate |
|---|---:|---:|
| NodeBB | 9 / 23 | 39.1% |
| Ansible | 15 / 18 | 83.3% |
| Element | 16 / 22 | 72.7% |
| Flipt | 15 / 21 | 71.4% |
| Vuls | 16 / 19 | 84.2% |
| Teleport | 16 / 21 | 76.2% |
| OpenLibrary | 18 / 22 | 81.8% |
| Navidrome | 16 / 21 | 76.2% |
| ProtonMail | 17 / 20 | 85.0% |
| Qutebrowser | 14 / 21 | 66.7% |
| Tutanota | 10 / 15 | 66.7% |

## Sanity witnesses

Two passing tasks were inspected against archived OCO histories to verify that the result is not merely an evaluator artifact.

### Navidrome witness

- Instance: `instance_navidrome__navidrome-3977ef6e0f287f598b6e4009876239d6f13b686d`
- Replay: passed relevant hasher tests.
- OCO history: real PM/investigator/orchestrator activity, 10 edits, 36 model calls, 41 tool calls.
- Patch behavior: fixed deterministic hashing by diagnosing zero-initialized `maphash.Hash` non-determinism and moving to a deterministic FNV-style path.

### OpenLibrary witness

- Instance: `instance_internetarchive__openlibrary-2abe28b472ffed563a87cfe83685b161b35263b0-v13642507b4fc1f8d234172bf8129942da2c2ca26`
- Replay: aggregate evaluator result true; vendor tests ran with `34 passed`.
- OCO history: real PM investigation, spec, Orchestrator delegation, four file edits, 28 model calls, 33 tool calls.
- Patch behavior: added language extraction/serialization logic in the expected data shape and updated clean-load handling.

Both witnesses still carry harness caveats: missing visible Auditor pass, missing durable handoff in some traces, and some direct PM edit detection. They validate that representative passing rows were real agentic code changes, not false-positive eval rows.

## Relationship to the failed full run

The later H200/FP8/MTP-4 full public-731 run should be reported as a separate failed serving/controller-profile experiment. It is not the headline OCO result. Its value is diagnostic: it shows the same broad setup can collapse when serving profile and controller details change.

See [`h200-fp8-mtp4-collapse.md`](h200-fp8-mtp4-collapse.md).
