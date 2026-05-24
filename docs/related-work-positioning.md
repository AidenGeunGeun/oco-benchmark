# Related Work and Positioning Memo

This memo converts the deep-research findings into a practical positioning guide for the technical report. Citations still need to be converted into BibTeX before arXiv submission.

## Core positioning

The work should not claim that it invented multi-agent coding or that it proves OCO is state of the art. The credible claim is:

> A durable multi-agent orchestration harness with explicit runtime semantics produced an unusually strong SWE-bench Pro seeded-subset pilot with a small open model, and a later serving-profile change caused a generation-side collapse despite evaluator replay stability.

This places the work in **agent systems / AI for software engineering / ML systems**, not model-training research.

The positive pilot's serving profile should be described precisely from archived source evidence: Qwen3.6-27B-FP8 hosted on a Runpod B200 GPU with the `vllm/vllm-openai:latest` pod template, without speculative decoding. Earlier B200/NVFP4 experiments existed, but the archived final methodology note for the submitted 223-row positive bundle records the final reduced run as B200/FP8/non-MTP. The later H200/FP8/MTP-4 run is a separate failed serving-profile experiment.

## Related-work layers

### Benchmarks

- SWE-bench: original repository-level issue-resolution benchmark.
- SWE-bench Verified: human-filtered subset, now increasingly saturated.
- SWE-bench Pro: harder public/commercial benchmark family; this is the relevant benchmark.
- SWE-bench Live / benchmark-rigor work: important for discussing contamination, stale tasks, and evaluator artifacts.

### Harness and agent-computer interface work

- SWE-agent: canonical evidence that agent-computer interface design matters.
- mini-SWE-agent: canonical minimal-scaffold baseline.
- Agentless: counterpoint showing simpler localization/repair/validation pipelines can compete with more autonomous agents.
- OpenHands: production-grade open platform and useful comparison point for sandboxing/runtime choices.

### Multi-agent software engineering systems

- MetaGPT / ChatDev: early role-based software teams.
- MAGIS: manager/custodian/developer/QA pattern, close conceptual predecessor.
- CodeR / HyperAgent: multi-agent or role-specialized repository-level repair.
- Confucius Code Agent / Live-SWE-agent: closest modern SWE-bench Pro-era scaffold comparisons, though primarily with closed models.

### Verifiers, reviewers, and audit loops

- Self-Refine / Reflexion / critic-agent work: conceptual background for feedback loops.
- SWE-Gym / R2E-Gym / SWE-RM / SWE-PRM: evidence that verifier/reward/review components can materially affect coding-agent outcomes.

OCO's audit-loop concept should be described as a system design primitive, not as an experimentally isolated contributor, because the positive pilot did not consistently enforce or observe the audit loop.

### Serving and reproducibility sensitivity

This is the most distinctive angle for the current artifact:

- Public reproduction notes already show SWE-bench results are sensitive to scaffolds, tool formatting, post-processing, and infrastructure.
- Qwen-family reports compare models under multiple scaffolds, showing scaffold effects can be material.
- Speculative decoding and quantized serving are throughput tools, but hard software-engineering workloads may react differently than direct API token benchmarks.

The H200/FP8/MTP-4 collapse should be framed here.

## Novelty assessment

| Claim | Novelty | Strength |
|---|---|---|
| Harnesses affect SWE-bench outcomes. | Low | Already established. |
| Durable PM→Orchestrator→specialist runtime with handoff semantics. | Medium | Architecture pattern known; implementation details underreported. |
| Strong SWE-bench Pro seeded-subset result with a 27B open model. | Medium-high | Interesting empty empirical cell, but not full controlled benchmark. |
| Serving-profile collapse despite evaluator replay stability. | High as systems anecdote | Needs controlled sweep to become general claim. |

## Venue posture

Best current path:

1. GitHub artifact.
2. Technical report / arXiv preprint.
3. Blog-style engineering case study.
4. Workshop or industry/tool track if polished.

Plausible targets later:

- BoatSE / AGENT / AIware workshops.
- FSE Industry or IVR style track.
- ASE Tool Demo if the artifact and demo are polished.
- MLSys-style venue only if the serving-profile collapse is turned into a more controlled systems study.

Not ready yet:

- ICSE/FSE/ASE main research track as a causal performance paper.
- SOTA-style SWE-bench Pro leaderboard claim.

## Claims to use

Credible:

- OCO produced an unusually strong seeded-subset pilot result with Qwen3.6-27B-FP8 on a non-speculative B200 serving profile.
- The result motivates further controlled study of orchestration/runtime choices for coding agents.
- Evaluator replay indicates the later H200/FP8/MTP-4 collapse happened during generation, not evaluation.
- The project exposes reproducibility and deployment hazards in long-horizon coding-agent benchmarks.

Avoid:

- OCO is SOTA on SWE-bench Pro.
- The hierarchy caused the lift.
- The audit loop caused the lift.
- MTP/FP8/H200 are generally bad.

## Zero-budget next work

Because new GPU experiments are expensive, the no-compute plan is:

1. Freeze denominator taxonomy.
2. Publish the result summary and methodology.
3. Categorize failures from existing logs.
4. Quantify delegation/audit/full-loop strata from existing traces.
5. Compare the seeded 250 subset against the full 731 public split by repo/language/task family.
6. Polish the serving-profile collapse as a negative result with witness cases.

That is enough for a credible artifact and a serious professor conversation, even if it is not enough for a main-track paper.
