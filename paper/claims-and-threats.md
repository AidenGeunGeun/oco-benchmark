# Claims and Threats to Validity

## Claims we can make

1. OCO + Qwen3.6-27B-FP8 on a non-speculative Runpod B200 serving profile produced a strong SWE-bench Pro seeded-subset pilot result.
2. The artifact-adjusted diagnostic score is 162/219 = 74.0%, excluding only evaluator/tooling artifacts where no meaningful test occurred.
3. The strict submitted-row score is 162/223 = 72.6%.
4. Replaying the old B200 patch bundle through the current evaluator reproduced the positive result, so the later H200/FP8/MTP-4 collapse was not primarily caused by Modal or evaluator drift.
5. The H200/FP8/MTP-4 collapse is evidence that token-throughput improvements do not guarantee coding-agent quality improvements.
6. OCO's durable orchestration runtime is a useful artifact for studying harness effects.

## Claims we should avoid

1. OCO is state of the art on SWE-bench Pro.
2. OCO beats frontier closed models.
3. The hierarchy caused the score.
4. The audit loop caused the score.
5. MTP-4 or FP8 is generally harmful.
6. Any B200 quantization profile is generally better than any H200 serving profile.
7. The result generalizes to all SWE-bench Pro tasks or all coding-agent settings.

## Threats to validity

### Subset validity

The positive pilot used a seeded 250-task subset and 223 submitted evaluator rows, not the full 731-task public split. The subset may not represent the full benchmark distribution.

Mitigation: publish the task IDs, per-repo breakdown, and conservative 162/250 framing.

### Baseline validity

There is no matched same-model baseline using mini-SWE-agent, SWE-agent, OpenHands, opencode single-agent, or OCO with hierarchy disabled.

Mitigation: describe the result as a pilot and avoid causal claims.

### Evaluator artifacts

Some official evaluator outputs have `tests: []`. Most inspected empty-test rows were real non-resolutions, but a few were evaluator/tooling artifacts.

Mitigation: report strict, artifact-adjusted, known-only, and conservative denominators side by side.

### Audit-loop observability

OCO's audit loop existed but was not consistently observed/enforced in the positive pilot.

Mitigation: describe audit as a system design primitive, not as the measured cause of performance.

### Serving-profile confounding

The later full run changed multiple variables at once: model quantization/serving profile, controller, prompt overlays, and runtime details.

Mitigation: frame the collapse as a generation-profile regression and call for controlled serving-profile A/B before generalizing.

### Implementation and operator effects

The benchmark infrastructure and analysis were built through agent-mediated engineering under budget pressure.

Mitigation: publish code, specs, logs, denominator policy, and forensic notes transparently.
