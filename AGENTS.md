# AGENTS.md — oco-benchmark

This repo is the SWE-bench Pro benchmark harness for OCO. It is a *consumer* of OCO, not part of it.

## Hard boundary

- This repo **MUST NOT** modify or depend on OCO source code.
- OCO is invoked as an installed CLI binary on the system PATH.
- If a benchmark issue exposes a real OCO bug, file it against OCO; do not patch OCO from inside this repo.
- Code that needs to live in OCO (e.g., harness behavior changes) is tracked as a separate OCO change with its own review and release lifecycle.

## Production fidelity

The benchmark mirrors a real user's OCO setup, curated to remove personal-workflow surface area that isn't needed for SWE-bench Pro.

- Isolated per-attempt OCO config copies prompts and agent definitions for the kept subagents (`orchestrator`, `investigator`, `auditor`) from `~/.config/oco/`. `test_runner`, `web-search`, `docs`, and all skills are stripped.
- Qwen 3.6 has no OpenAI-style `reasoning.effort` knob, so OCO's `xhigh`/`high` agent variants are not used here. All agents share one Qwen-native model config (binary thinking on/off plus the model-card-recommended sampling for precise coding).
- The benchmark does not invent new prompts or agent definitions. If something is wrong with the production setup that affects the score, fix it in production first.

## Storage policy

- **Container disk**: working state — vLLM, OCO worker installs, repo cache, active worktrees, hot logs, durable artifacts (patches, telemetry, manifests).
- **Mac backup target**: passive `rsync` destination receiving small durable files from the pod every few minutes. Mac stays off the hot path; if unreachable, rsync no-ops and the run continues. If the Community Cloud pod is reclaimed *while* the Mac is also unreachable, all unsync'd work is lost — an explicit cost-vs-durability tradeoff.
- **External SSD**: long-term archive after a run finishes. Local machine internal disk is not for benchmark storage.
- During a live run, only fully-complete attempt worktrees may be deleted. Never delete partial/aborted worktrees while a controller is alive — the controller may still need them to resume a phase.

## Secrets

- All benchmark secrets live in `.env` at the repo root.
- `.env` is in `.gitignore` and is authorized for direct use by tooling inside this repo.
- Never echo secrets to chat replies, log files, or tool output. Use environment variables.

### Auth boundary (OCO data dir is off-limits)

- The materializer reads only from `~/.config/oco/` (config + prompts). It **MUST NOT** read from `~/.local/share/oco/` — that's where OCO's auth.json and secret-vault.key live.
- Benchmark runs authenticate via API keys from `.env` or pod-local env vars, never from OCO's auth store.
- The Mac rsync backup target mirrors `runs/` artifacts only (JSON, JSONL, patches, logs). Never include OCO data dir paths in any rsync include list.
- This boundary holds even if we later add support for cloud-provider models in the smoke or benchmark path.

## Communication preference

The project owner is a PM and aerospace engineer, not a software engineer. When explaining status, results, or tradeoffs:

- Lead with what the result means in plain language.
- Save paths, symbols, function names, and code for specs, handoffs, and committed artifacts — not chat replies.
- When reporting a bug: symptom first, then everyday cause, then technical detail if essential.
- Use analogies when they help. Never assume the user mentally parses internal terms on first encounter.

## Operational rules

- **No wide unscoped `glob` or `grep`.** Always start from a known directory or file. Wide scans can hang and spike CPU on the local machine.
- **Runpod vLLM template Start Command** is one line of arguments to `vllm serve`. Do not include `vllm`, `serve`, `bash -lc`, or any shell wrapping. Confirm template behavior before assuming.
- **Long or destructive commands** must be explained before they run: what will run, what can change or delete, why it's safe, and the expected result. This applies to rsync, cleanup, Docker prune/rmi, eval launches, or anything likely to run for minutes.
- **No autonomous pod restart, no autonomous serving-profile changes.** The serving profile is locked at the start of a run. If something is wrong, stop the run with a clear reason and let the user decide.
- **No autonomous benchmark restart on tokens-per-second symptoms.** Restart triggers are structural only (dead controller, no active workers while incomplete work remains, no artifact progress beyond a clear stall threshold).

## Spec workflow (meta-PM discipline, not enforced inside the benchmarked harness)

For work in this repo, the PM-and-Aiden side of the project follows this order:

1. **PM writes spec** under `specs/`. Spec is an intent contract: WHAT and WHY, not HOW.
2. **Auditor audits the spec before delegation.** The Auditor checks for scope creep into HOW, missing or unobservable acceptance criteria, internal inconsistencies, ambiguous language the Orchestrator would have to guess at, and conflicts with the plan or AGENTS.md hard boundary.
3. **Revise until the spec audit passes.** No delegation on an unaudited or flagged spec.
4. **Delegate to Orchestrator** with the audit-passed spec.
5. **Orchestrator owns HOW**, runs its own implementation-audit loop, and reports completion.

This is a meta-PM discipline for *us building the benchmark*. It is **not** added to the benchmarked OCO harness — that would be a real OCO product change and would break production fidelity. If a future OCO release adds spec-audit as a first-class harness feature, that version of OCO gets benchmarked as a separate run, not retrofitted into this one.

## Where to look first

- **Plan**: `docs/oco-pro731-benchmark-plan-2026-05-21.md`
- **Production OCO config to mirror**: `~/.config/oco/prompts/` and `~/.config/oco/opencode.jsonc`
- **External archive of prior runs**: `/Volumes/external-nvme/oco-benchmark-archive/`

## Inherited rules

This file is the *benchmark-specific* layer. General OCO setup, communication, and decision-ownership notes live in `~/.config/oco/AGENTS.md` and apply automatically.
