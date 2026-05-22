# oco-benchmark

Benchmark harness for **OpenCodeOrchestra (OCO)** on the SWE-bench Pro public test set.

This project is a *consumer* of OCO. It invokes the installed `oco` CLI binary and measures its performance against the full 731-task public SWE-bench Pro test set, using a self-hosted Qwen3.6-27B-FP8 inference server.

It does **not** modify or depend on OCO source. Anyone with `oco` on their PATH and a vLLM-capable GPU can clone this repo and reproduce the benchmark.

## What this measures

- Pass rate of OCO + Qwen3.6-27B-FP8 on SWE-bench Pro public 731 tasks.
- Token usage, wall time, and inferred infrastructure cost per attempt and per run.
- Audit-observed vs audit-missing performance split (descriptive strata, not a denominator filter).

## Architecture (planned)

Everything runs on a single GPU pod, eliminating the public-internet hop between agent and model that caused most of the wasted compute in earlier ad-hoc runs.

- **GPU pod** (Runpod H200 SXM):
  - vLLM serving `Qwen/Qwen3.6-27B-FP8` on `localhost:8000`
  - Benchmark controller, a long-lived state machine driving attempt phases
  - OCO worker subprocesses talking to vLLM over localhost. The controller invokes PM only; Orchestrator, Auditor, and Investigator are subagents the PM chooses to call as needed.
- **Modal**: SWE-bench Pro official evaluator, fired per-patch as patches land
- **Local machine** (Mac): SSH terminal for status and final artifact pull. Not on the hot path during the run.

Full plan: [`docs/oco-pro731-benchmark-plan-2026-05-21.md`](docs/oco-pro731-benchmark-plan-2026-05-21.md).

## Status

Controller implementation is in progress: the dry-run lifecycle, real-OCO adapter wiring, pinned SWE-bench Pro task loader, eval-bundle preparation, Modal pipeline contracts, and SSH rsync backup path are implemented and covered by offline tests. Paid pod launch, calibration, and the full 731 run are still pending.

## Requirements

- `oco` 2.1.7+ installed and on PATH (built from the OCO source tree, contains glob/grep timeout guards and the `experimentalNonStreamingToolCalls` provider option)
- A Runpod account with H200 SXM access
- A Modal account (free tier sufficient for the full 731 eval; cost ~$10)
- Python 3.13+ on the local machine

## Project layout

```
oco-benchmark/
├── docs/         # Plan, methodology notes, post-run writeups
├── specs/        # Implementation specs (created when work is delegated)
├── controller/   # Benchmark state machine + OCO session management
├── scripts/      # CLI entry points (run, status, prepare, eval)
├── config/       # Locked vLLM serving profile, evaluator config, benchmark config
└── tests/        # Unit tests for state machine, parsers, artifact writers
```

## Prior art

This repo supersedes the older `OCstuff/benchmarks/oco-vs-mini-swe-agent/` directory, which served as a calibration ground. That work is preserved on external storage as a historical reference; this is the clean rebuild.

## License

TBD.
