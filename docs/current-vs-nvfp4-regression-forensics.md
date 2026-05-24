# Current H200/FP8 full-run vs prior B200 positive-pilot forensics

Date: 2026-05-23

## Why this note exists

The final 731-task H200/FP8/MTP-4 run evaluated far below the earlier B200 seeded-250 positive pilot. The archived final methodology note for that 223-row submitted positive bundle records `Qwen/Qwen3.6-27B-FP8`, B200, non-MTP; earlier B200/NVFP4 experiments existed and some legacy replay names still say `old-nvfp4`, but this note follows the archived final methodology record. The current-evaluator replay of the B200 submitted bundle produced 162 pass / 43 fail / 18 empty-test outputs over 223 rows, i.e. 72.6% strict over submitted rows and 64.8% against the full seeded-250 denominator. The later H200 full run produced 208 pass / 310 fail / 213 empty-test outputs over 731 rows, i.e. 28.45% strict. The original archived B200 tally was 158 pass / 46 fail / 19 unknown over 223 rows and is retained as historical context, but the current-evaluator replay numbers are the public comparison point used by this repository.

This gap is too large to treat as ordinary sampling noise. A direct old-vs-current comparison on the same old evaluated task IDs showed that 100 old passes regressed in the current run: 49 became current fail and 51 became current empty-test output. Only 58 old passes stayed pass.

## Current interpretation

The current full-run result should not be described as "Qwen/OCO simply scored 28.45%" without caveat. The better interpretation is:

> The full H200/FP8/MTP-4 controller run regressed materially relative to the earlier B200 seeded-250 pilot, likely because the benchmark harness/model-serving/eval profile changed enough to alter model behavior.

Most likely contributors, in descending order:

1. New standalone controller and prompt/context delivery changed Qwen behavior.
2. Model serving profile changed materially: H200 FP8 + MTP-4 vs B200 FP8/non-MTP, plus controller/runtime changes.
3. Evaluation/bundle differences remain possible, but raw-vs-sanitized checks and five-witness patch inspection make this unlikely as the primary cause.

## Five regression witnesses inspected

Witnesses were selected from old-pass/current-regressed tasks, one each from Flipt, Navidrome, Vuls, OpenLibrary, and Teleport. Evidence was collected from `/workspace/runs/forensics-oldpass-regressions-5` on the pod and transferred locally as `/tmp/forensics-oldpass-regressions-5.tgz`.

### Flipt

- Instance: `instance_flipt-io__flipt-b433bd05ce405837804693bebd5f4b88d87133c8`
- Old status: pass
- Current status: empty-test output / build failure
- Old patch: 4,361 bytes, 3 files
- Current patch: 11,372 bytes, 6 files
- Current eval failure: `internal/config` build failure; tests reference undefined `OTLPTracingConfig` and missing `TracingConfig.Exporter` field.
- Interpretation: current patch changed more surface area and produced an inconsistent config/test state. This is a model/harness-quality failure, not an evaluator mismatch.

### Navidrome

- Instance: `instance_navidrome__navidrome-31799662706fedddf5bcc1a76b50409d1f91d327`
- Old status: pass
- Current status: empty-test output / build failure
- Old patch: 10,374 bytes, 9 files
- Current patch: 3,419 bytes, 4 files
- Current eval failure: `server/auth_test.go` references undefined `tokenFromHeader`.
- Interpretation: current patch appears under-scoped/incomplete relative to old patch and leaves tests uncompilable.

### Vuls

- Instance: `instance_future-architect__vuls-1832b4ee3a20177ad313d806983127cb6e53f5cf`
- Old status: pass
- Current status: empty-test output / build failure
- Old patch: 4,903 bytes, 5 files
- Current patch: 7,065 bytes, 3 files
- Current eval failure: missing macOS constants/functions such as `MacOSX`, `MacOS`, `parseSWVers`, and `macos`.
- Interpretation: old patch touched OS constants/scanner plumbing; current patch stayed mostly in detector files and missed required compatibility definitions.

### OpenLibrary

- Instance: `instance_internetarchive__openlibrary-2abe28b472ffed563a87cfe83685b161b35263b0-v13642507b4fc1f8d234172bf8129942da2c2ca26`
- Old status: pass
- Current status: fail
- Old patch: 21,802 bytes, 1 file
- Current patch: 43,472 bytes, 2 files
- Current eval failure: 5 failing tests; examples include language normalization returning `eng` where tests expect `english`, and `Languages` object serialization problems.
- Interpretation: current patch is not merely failing due to evaluator setup. It is semantically wrong for the task's expected data shape.

### Teleport

- Instance: `instance_gravitational__teleport-1330415d33a27594c948a36d9d7701f496229e9f`
- Old status: pass
- Current status: fail
- Old patch: 12,845 bytes, 2 files
- Current patch: 6,006 bytes, 2 files
- Current eval failure: `lib/utils/parse` build failure; tests reference undefined matcher helpers such as `notMatcher` and `prefixSuffixMatcher`.
- Interpretation: current patch added/changed tests around helpers that do not exist or were not implemented correctly. Again, this points to patch quality, not bundle/evaluator corruption.

## Cross-witness pattern

Across all five witnesses, the current patches are not sane-but-mis-evaluated. They fail by ordinary code-quality mechanisms:

- undefined symbols in tests or implementation,
- build failures,
- incomplete dependency/config model changes,
- semantic mismatches against expected test assertions.

This strongly weakens the hypothesis that the low H200/FP8 score is mostly caused by Modal, sanitizer, raw-sample mismatch, or official evaluator parsing. Those may still affect individual rows, but they do not explain the broad old-pass regression.

## Telemetry notes

Current witness telemetry showed missing audit/full-loop observation across the sample and mixed delegation:

- Flipt: delegation observed, no audit/full loop, 21 steps / 30 tools.
- Vuls: no delegation, no audit/full loop, 52 steps / 68 tools.
- Teleport: delegation observed, no audit/full loop, 16 steps / 17 tools.
- OpenLibrary: delegation observed, no audit/full loop, 17 steps / 27 tools.
- Navidrome: no delegation, no audit/full loop, 26 steps / 30 tools.

This supports the broader methodology caveat: the current full run exercised OCO mechanics unevenly and should be reported with delegation/audit/full-loop strata rather than as a clean measurement of the intended audit-loop harness.

## Consequence for writeup

The current 731 result is still valuable, but as a negative/diagnostic systems result, not as the primary OCO-lifts-Qwen headline. The stronger story is now:

1. The B200 seeded-250 pilot showed a surprisingly high result and deserves preservation as a pilot finding.
2. The later full-run controller profile regressed sharply.
3. Forensics show the regression is real patch-quality degradation, not just evaluator artifact.
4. Before any publication-grade full 731 claim, the benchmark needs a controlled A/B between the B200 working profile and the newer controller profile, ideally on the same first-250 slice first.

After replaying the B200 patch bundle through the current evaluator path, the accepted replay result was 162/223 = 72.6% strict over submitted rows. This is consistent with the archived result scale while using the same evaluator path as the later H200 run. It clears the current Modal/evaluator path as the main cause of the collapse. The low H200/FP8/MTP-4 full-run score is therefore a generation-profile regression, not an evaluation artifact.

For public/headline framing, use the B200 seeded-250 pilot as the positive benchmark result and present the H200/FP8/MTP-4 run as a negative deployment-profile case study. Do not present the H200/FP8/MTP-4 score as the headline OCO result unless it is explicitly framed as a failed serving-profile experiment.

The agent-perspective comparison found broadly similar task framing and top-level model/agent target, but not exact equivalence: the current standalone controller changed subagent availability, prompt overlays, and some request-option placement. Still, the old patch replay succeeding under the current evaluator means the most important remaining uncertainty is generation behavior, especially the serving/model profile and controller prompt/tool surface.

MTP note: heavy MTP should not be described as simply letting a weaker child model write the answer. In principle, draft tokens are verified by the target model before acceptance. In practice, for hard coding tasks, MTP-4 plus quantized serving can still change generation behavior enough to hurt quality: small acceptance/numerical/sampling differences compound over long code edits, tool-call formatting, and multi-step reasoning. This run is evidence that throughput optimizations that look excellent in direct token benchmarks can be unsafe for hard agentic coding without quality A/B tests.

## Recommended next forensic step

Do not spend more Modal/GPU cycles yet. First compare harness/profile variables in a small controlled rerun:

- Same first-250 subset, or a 20-task subset drawn from old-pass/current-regressed IDs.
- Hold task selection and evaluator fixed.
- Compare old-style prompts/profile vs new controller profile.
- Test H200 FP8 non-MTP or MTP-2 against MTP-4 if budget allows.
- Preserve prompt snapshots and OCO SQLite DBs for all tasks.

The key question is no longer "what is the final score?" but "which profile change destroyed patch quality?"

## Investigator patch A/B review

An Investigator reviewed the five witness bundles patch-by-patch (`old_nvfp4.patch` vs `current.patch`) and confirmed the same high-level conclusion: current failures are ordinary code-quality regressions, not primarily evaluator/sanitizer artifacts.

Key details:

- **Flipt:** old patch used the evaluator-expected config/exporter symbols; current patch used incompatible naming and left tests/build with undefined OTLP/exporter symbols.
- **Navidrome:** old patch added the expected `tokenFromHeader` path and wired JWT verification around it; current patch only adjusted header mapping and left hidden/eval tests looking for an undefined helper.
- **Vuls:** old patch implemented macOS-family support across constants, OS handling, and scanner wiring; current patch mostly renamed detector internals and missed the macOS implementation surface entirely.
- **Teleport:** old patch added the expected matcher interface and parser entry point; current patch invented a different `NewMatcher`-style API and edited tests around that, leaving evaluator-expected matchers/functions undefined.
- **OpenLibrary:** old patch handled Amazon language extraction/serialization in the expected string form; current patch converted to MARC-style codes and directly iterated a non-iterable `Languages` object, producing both assertion and type failures.

The Investigator's cross-witness conclusion was that the regression is mixed but weighted toward **serving/model-profile or prompt/context code-quality degradation**, not Modal/evaluator corruption. The only clear evaluator/reporting artifact is that some current outputs have `tests: []` even though stdout/stderr show real build/test failures; those rows are still non-resolved.

The Investigator's recommended non-generation follow-up is a **patch-only replay**: evaluate the old-passing patches and current patches for the same witness IDs through the exact same evaluator path and compare applied diffs plus stdout/stderr. This would isolate any remaining evaluator/harness drift without regenerating code.
