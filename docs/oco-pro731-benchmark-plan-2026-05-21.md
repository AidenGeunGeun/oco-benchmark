# OCO + Qwen3.6-27B on SWE-bench Pro 731 — Benchmark Plan

**Status:** Plan v4 after second auditor review (PASS-WITH-CHANGES → fully addressed).
**Date:** 2026-05-21
**Owner:** Aiden (PM) / agent

---

## 1. Premise

Test whether **OCO + Qwen3.6-27B-FP8 scores higher on SWE-bench Pro than Qwen's published Qwen3.6-27B score using Qwen's own scaffold**.

Qwen's published SWE-bench Pro score for Qwen3.6-27B is **53.5**, run with Qwen's own scaffold. We run the same benchmark with the same model — actually the *FP8 weight + FP8 KV* quantized variant, theoretically a slightly weaker setup — wrapped in OCO's PM / Orchestrator / Auditor / Investigator harness, and see whether the OCO scaffold lifts the score.

Headline:

| Configuration | Score |
|---|---|
| Qwen3.6-27B alone, Qwen's scaffold (model card) | 53.5 |
| Qwen3.6-27B-FP8 inside OCO (this run) | **TBD** |

The result shows OCO + Qwen3.6-27B-FP8 relative to Qwen's published scaffold score on the same benchmark. It does **not** isolate harness causality — different scaffold, different sampling, quantized weights all contribute. It's a direction-of-effect result, not a controlled experiment, and we report it that way.

This is **not an academic submission**. The bar is "evidence engineers and serious readers will trust," not peer-review-proof methodology.

## 2. Non-goals

- Not a Qwen vs other-model study.
- Not an OCO vs mini-swe-agent paired benchmark.
- Not a streaming-vs-non-streaming experiment.

## 3. Architecture (everything on the pod)

### 3.1 Single GPU pod runs everything

- **vLLM** serving Qwen3.6-27B-FP8 on `localhost:8000`
- **Benchmark controller** as a long-lived process on the pod
- **OCO worker subprocesses** spawned by the controller, talking to vLLM over `localhost`
- No Cloudflare, no Runpod proxy in the agent ↔ model hot path

### 3.2 Local Mac is a control terminal + passive backup target

- SSH or `runpodctl exec` for status pokes
- Receives periodic `rsync` pushes of artifacts from the pod (passive — Mac never initiates work)
- Pulls final artifacts at the end of the run
- Off the hot path; if unreachable, the controller's rsync attempts no-op and the run continues on the pod. Pushes resume when Mac comes back. See §7 for the explicit durability tradeoff when Mac is unreachable.

### 3.3 Modal for evaluation

- SWE-bench Pro evaluator runs in Modal sandboxes
- Pipelined: fired per-patch as patches land
- Mac never sees SWE-bench Pro Docker images

### 3.4 Storage layout

- **Container disk** (~200 GB): vLLM, OCO worker installs, repo cache, active worktrees, hot logs, **all run artifacts during the run** (patches, telemetry, manifests). Fast local storage, no cross-network IO.
- **Mac backup target**: every N minutes, the controller pushes only the small durable files (patches, normalized.json, phase logs, manifests, run summary) over `rsync` to a directory on the Mac. Repo caches and worktrees are not backed up — they are reproducible from task spec.
- **No Runpod network volume.** Network volumes are Secure Cloud-only on Runpod; we're on Community Cloud for cost. The Mac rsync backup replaces the volume's durability role *only when the Mac is reachable*. If the Community Cloud pod is reclaimed during an extended Mac-unreachable window, everything since the last successful rsync is lost. This is an explicit cost-vs-durability tradeoff, accepted in exchange for the ~25% hourly savings vs Secure Cloud and the assumption that the Mac is online during the run.

### 3.5 Why this beats the previous topology

| Failure class in old setup | Mitigated by |
|---|---|
| Cloudflare 524 ghost requests piling up in vLLM | localhost, no proxy |
| Non-streaming retry loops from CF timeouts | localhost — client disconnect is immediate and clean |
| Mac sleep / Docker.raw blowup / disk full / CPU churn killing driver | Mac not on the hot path |
| Lost run state on partial worktree deletion | Atomic checkpoints + periodic Mac backup |

## 4. Hardware and serving profile (locked)

### 4.1 Hardware

- **1x H200 SXM, Runpod Community Cloud, on-demand: $3.59/hr** (per web search, low stock — verify availability at pod-creation time)
- 16 vCPU, 251 GB RAM
- 200 GB container disk
- No network volume (Community Cloud doesn't support them; Mac rsync backup is the durability strategy)
- Cost breakdown by run-length scenario:
  - **Base** (calibration-matched p50, ~13h): $47 GPU + $10 Modal = **~$57**
  - **Expected** (calibration-matched p95, ~15-18h): $54-65 GPU + $10 Modal = **~$64-75**
  - **Ceiling** (24h hard cap, includes any debug/restart overhead): $86 GPU + $10 Modal = **~$96**

### 4.2 vLLM serving profile (locked at boot, not tuned during the run)

```
--host 0.0.0.0
--port 8000
--model Qwen/Qwen3.6-27B-FP8
--served-model-name selfhost-qwen
--api-key sk-<pod-id>
--max-model-len 200000
--language-model-only
--enable-prefix-caching
--enable-chunked-prefill
--kv-cache-dtype fp8
--gpu-memory-utilization 0.95
--max-num-seqs 16
--max-num-batched-tokens 32768
--reasoning-parser qwen3
--enable-auto-tool-choice
--tool-call-parser qwen3_coder
```

- 200K max context matches Qwen's own SWE-bench scaffold setting per model card.
- **MTP**: default off in the baseline above. **Tentative** — re-evaluated at pod boot per §4.4 A/B. If MTP-2 wins on H200 FP8 at c16, we switch to MTP-2 before calibration.
- No NVFP4. Non-streaming OCO.
- `qwen3_coder` tool parser per Qwen model card recommendation.

### 4.3 Server-side request hard cap

A per-request server-side timeout so stuck requests can't sit forever, with verified cancellation that actually releases vLLM scheduler state. Calibration validates this.

### 4.4 MTP decision at pod boot (A/B before locking)

Prior MTP data we have on this model family:

- H200 FP8, direct API benchmark only: MTP-2 = +9%, MTP-4 = +18% over non-MTP at low concurrency. *No agent-workload data on H200.*
- B200 NVFP4, agent workload: MTP-2 was *slower* than non-MTP at c10+ — but B200 NVFP4 has its own scheduler / Mamba-alignment interactions that don't necessarily apply to H200 FP8.

The B200-NVFP4 result does **not** generalize to H200 FP8. Before locking the serving profile for the calibration batch, we run a quick A/B:

1. Boot H200 with the §4.2 baseline (no MTP). Direct API benchmark at c1, c4, c8, c16, c20 — record completion tok/s, prompt tok/s, prefix cache hit rate, queue depth.
2. Restart with MTP-2 added (`--speculative-config '{"method":"qwen3_next_mtp","num_speculative_tokens":2}'`).
3. Run the same benchmark.
4. Optional third pass with MTP-4 if MTP-2 wins cleanly.

Decision rule:

- If MTP-2 (or MTP-4) **wins on completion tok/s at c16 with no waiting-queue growth and no KV-pressure increase**, lock that profile for the full run.
- If MTP variants tie or lose at c16, lock the baseline (no MTP).
- Cost of A/B: ~1-2 pod hours (~$4-8). Trivial against the run budget.

This A/B is *server-only* — the controller and OCO setup are unchanged regardless of outcome. The locked profile (with or without MTP) becomes part of the `oco-config-snapshot/` for the run.

## 5. Controller — attempt lifecycle, not a fixed agent pipeline

The controller's job is to **run OCO on each task and record what happened**. It does not impose an internal sequence on how OCO's agents behave. The agent (PM) gets the curated tool set defined in §6, and decides which subagents to invoke. Whether the Auditor fires is observability, not a gate.

### 5.1 Attempt lifecycle phases

```
SETUP    → worktree prepared, isolated OCO config materialized from production snapshot,
           per-task seed derived from task ID (see §6.5)
RUN      → `oco run` invoked; subprocess runs to completion (or timeout)
CAPTURE  → diff extracted (tracked + untracked), patch-apply precheck against task base commit,
           telemetry normalized, observation strata computed (see §11.3), classification computed
DONE     → artifacts atomically committed, queued for Mac rsync, queued for Modal eval (only
           if patch-apply precheck passed; failed-precheck patches are still recorded but flagged
           as `precheck_failed` so Modal compute isn't spent on patches that can't apply)
```

These are wrapper phases, not OCO phases. What happens *inside* RUN is up to the agent.

### 5.2 Inside RUN — what we observe (not enforce)

From OCO's structured event stream we capture, per attempt:

- Whether PM delegated to Orchestrator (`task` tool call observed)
- Whether Orchestrator invoked Auditor
- Whether Orchestrator invoked Investigator
- Per-step token / wall / cache telemetry (see §5.5)
- Any provider errors, parser glitches, retry events
- Final patch (if any)

Anything the agent chose to do or skip is recorded as telemetry. Not a pass/fail input.

### 5.3 Retry policy

| Class | Treatment |
|---|---|
| Provider transient (5xx, network blip, parser stream glitch) inside RUN | Retry within phase, capped |
| Phase timeout | Mark attempt phase-timeout, archive, classified infrastructure if pre-RUN, harness if post-RUN |
| OCO subprocess crash inside RUN | Restart subprocess, resume using OCO's session resume |
| Patch extraction failure | Re-extract once including untracked files; if still empty, mark no-patch |
| Model produced a bad patch or one that fails tests | Real model outcome — not retried |

### 5.4 No autonomous restart of the GPU pod

The controller does not restart vLLM or the pod. If vLLM goes sideways, the controller stops the run and writes a clear stop reason. Aiden decides whether to restart.

### 5.5 Telemetry granularity — per-step token accounting

OCO calls the model in **steps**. One step = one model API request: send the conversation, receive a response. A step ends either with a final text answer or with one or more tool calls. When a step ends with tool calls, OCO executes the tools, appends their results to the conversation, and starts a **new step** with the updated context. A single SWE-bench Pro attempt typically contains dozens of steps — initial PM step, Orchestrator delegation step, many tool-call cycles, audit calls, etc.

The controller records, **per step**, from vLLM's OpenAI-compatible `usage` field:

- `prompt_tokens` — input tokens for this step (accumulated history + most recent tool result)
- `completion_tokens` — output tokens generated this step (reasoning tokens + final text + tool-call args, combined)
- `cached_prompt_tokens` — prefix cache hits from `usage.prompt_tokens_details.cached_tokens`. Post-tool-call continuations should show high cached ratios because the prior conversation prefix is unchanged.
- `reasoning_tokens` — thinking-block tokens separated from final-answer tokens, from `usage.completion_tokens_details.reasoning_tokens` when vLLM reports it
- `wall_time_ms` — duration of the step
- `finish_reason` — `stop`, `tool_calls`, `length`, or error
- `tools_called` — list of tool names, plus subagent name when the tool is `task`
- `step_role` — which OCO session emitted the step. Valid values: `pm`, `orchestrator`, `auditor`, `investigator`, `compaction`. The `compaction` role is the autocompaction agent firing when context approaches its limit (see §6.8); its tokens are tracked separately so they don't get confused with primary-agent tokens.

Per-attempt aggregation (in `normalized.json`):

- `steps` — array of the per-step records above
- `step_count`, `tool_call_count`
- `tokens_in_total`, `tokens_out_total`, `cached_tokens_total`, `reasoning_tokens_total`
- `prefix_cache_hit_rate` — `cached_tokens_total / tokens_in_total`, plus an "excluding first step" variant
- Per-step distribution stats: median + p95 of `prompt_tokens`, `completion_tokens`, `wall_time_ms`

Per-run aggregation (in `summary.json`):

- Aggregate sums across all attempts
- Distribution stats across attempts (median + p95 of step_count, tokens_in_total, tokens_out_total, wall_time)
- Whole-run prefix cache hit rate

This granularity lets us answer questions like "how much of the output budget went to thinking vs final response?" and "did the prefix cache actually save work on post-tool-call continuations?" without having to re-derive anything from raw logs after the fact.

## 6. OCO setup — production fidelity, curated

### 6.1 OCO version requirement

This benchmark requires **OCO 2.1.7+** with the following features in the binary:

- Glob and grep tool calls with explicit timeouts (default 30s, max 300s) — prevents wide unscoped searches from hanging indefinitely during agent work
- `experimentalNonStreamingToolCalls` opt-in for self-hosted Qwen via vLLM (works around streaming tool-call parser bugs in vLLM 0.21.x)

The local Mac binary was built and installed on 2026-05-21 from the source tree in `OCstuff/OpenCodeOrchestra/`. Verification:

```
oco --version                                                    # expects: 2.1.7
strings $(which oco) | grep 'glob search timed out after'        # expects a hit
strings $(which oco) | grep 'grep search timed out after'        # expects a hit
strings $(which oco) | grep 'experimentalNonStreamingToolCalls'  # expects a hit (provider-option name in OCO source)
```

Note on naming: `experimentalNonStreamingToolCalls` is the *provider option* read by OCO's LLM session code. The benchmark controller's config has a higher-level toggle named `selfHostedNonStreamingToolCalls` which the controller translates into setting `providerOptions.experimentalNonStreamingToolCalls: true` on the model. Both names appear in the materialized config snapshot at different layers.

### 6.2 What the benchmark mirrors from production

The wrapper copies these from `~/.config/oco/` at startup into the isolated benchmark config:

- Prompts for kept agents: PM, Orchestrator, Auditor, Investigator
- Agent definitions for kept agents (model binding, tool list, behavior)
- The compaction agent definition (see §6.8)
- `oco.jsonc` model section (model entries, sampling/extra-body defaults)
- OCO version (`oco --version` captured into snapshot)

The wrapper **does not** copy or carry over:

- The `plugin[]` array — the materialized snapshot has `plugin: []`. The user's local `opencode-context-compress` plugin provides the `compress` and `compress_map` tools, which are stripped per §6.3, so the plugin itself has nothing to do in a headless run.
- The `mcp{}` block — the materialized snapshot has `mcp: {}`. All four MCP servers in production (`context7`, `deepwiki`, `perplexity`, `playwright`) are either personal-workflow accelerators or unrelated to local code tasks.

Both of these strips are recorded in the materialized snapshot diff so a reviewer can verify what was removed.

### 6.3 Kept vs stripped — the minimum benchmark surface

OCO's full production install includes a lot of personal-workflow and external-service surface area that has nothing to do with SWE-bench Pro. The benchmark uses a curated subset.

**Kept subagents** (the agent can invoke these):

- `orchestrator`
- `investigator`
- `auditor`

**Stripped subagents:**

- `test_runner` — the agent can run tests directly via `bash`; a dedicated subagent isn't required for SWE-bench Pro tasks
- `web-search` — depends on runtime-injected env vars; not relevant to local code tasks
- `docs` — no documentation work in SWE-bench Pro tasks

**Kept tools:**

- `read`, `write`, `edit`, `bash`, `glob`, `grep`, `task`, `todowrite`, `todoread`

**Stripped tools:**

- `webfetch` — no internet needed for SWE-bench Pro tasks
- `compress` and `compress_map` — PM-only context-management primitives, not relevant in headless autonomous runs
- All MCP servers and Playwright tools

**Stripped plugins:** every entry in the production `plugin[]` array. The only entry currently is the local-dev build of `opencode-context-compress`, which provides the `compress`/`compress_map` tools that are also stripped above. Removing the plugin entry from the snapshot ensures the plugin loader doesn't even register the unused tools.

**Stripped MCP servers:** every entry in the production `mcp{}` block. Even MCP servers that are currently `enabled: false` in production are removed from the snapshot entirely so the materialized config can be diffed against a known-empty baseline.

**Stripped skills:** every skill in `~/.config/oco/skills/`. These are personal-workflow accelerators (recall, omlx, oco-dev, todoist-cli, notion-cli, runpod, image-gen suite, design suites, brandkit, etc.) and would only introduce noise into the benchmark.

The resulting OCO setup is "PM with three core subagents and a basic file/bash toolset." Same harness shape as production, stripped of personal-workflow surface area.

### 6.4 Model variants — Qwen 3.6 doesn't have a reasoning-effort knob

Qwen 3.6 does **not** expose an OpenAI-style `reasoning.effort: low | medium | high | xhigh` parameter. It exposes binary thinking on/off via `extra_body.chat_template_kwargs.enable_thinking` (and `preserve_thinking` for multi-turn).

Implication for the benchmark: **all OCO agents use the same model config.** No variant split. The "xhigh / high" production setup doesn't translate to Qwen 3.6 — it's an artifact of OpenAI-compatible reasoning APIs.

### 6.5 Sampling and request shape

Matched to Qwen's own SWE-bench Series evaluation scaffold so the comparison is apples-to-apples on sampling:

- `temperature: 1.0`
- `top_p: 0.95`
- `top_k: 20` (via `extra_body`)
- `min_p: 0.0`
- `presence_penalty: 0.0`
- `repetition_penalty: 1.0`
- `max_tokens: 81920`
- `extra_body.chat_template_kwargs.enable_thinking: true`
- `extra_body.chat_template_kwargs.preserve_thinking: true`

Sources, both from `https://huggingface.co/Qwen/Qwen3.6-27B`:

- "SWE-Bench Series: Internal agent scaffold (bash + file-edit tools); temp=1.0, top_p=0.95, 200K context window."
- "For benchmarking on highly complex problems… we suggest setting the max output length to 81,920 tokens."
- "Thinking mode for general tasks: temperature=1.0, top_p=0.95, top_k=20, min_p=0.0, presence_penalty=0.0, repetition_penalty=1.0."

These are the same parameters Qwen used to produce the 53.5 baseline we're comparing against. Apples-to-apples on sampling; the only intentional difference is FP8 quantization on weights + KV cache (theoretically a slightly weaker setup than Qwen's reported run), and the harness scaffold itself (OCO vs Qwen's internal agent scaffold) — which is the variable we want to measure.

**Per-task seed pinning.** With `temperature=1.0`, identical inputs produce different outputs across runs unless a sampling seed is fixed. To make individual task outcomes reproducible (so any reader rerunning a single task gets the same generation), the controller derives a stable per-task seed from the task ID and passes it to the inference server on every model call inside that task's attempt.

Requirements:

- The seed is a deterministic function of the task ID alone — same task ID always yields the same seed, across runs and across machines.
- The seed is passed to the inference server through whatever sampling-seed mechanism the OpenAI-compatible API exposes for vLLM.
- The seed is recorded in `normalized.json` for that attempt so any reviewer can read it and reproduce the generation.

The Orchestrator picks the actual derivation (hash function, byte width, fold) when implementing the materializer.

This does *not* reduce noise across the full 731 (we still get one sample per task); it just makes the specific sample reproducible. We do not seed-vary the run itself — a multi-seed sweep across the full 731 would multiply pod cost by 3x or 5x for diminishing methodology value at this stage. Variance is addressed in the writeup via Wilson confidence intervals on the headline (see §11.4).

### 6.6 Snapshot as a run artifact

The full materialized isolated benchmark config (minus secrets) is written to `runs/<run-id>/oco-config-snapshot/` at the start of the run and queued for rsync to the Mac backup target. This is the reproducibility artifact: future reviewers can see *exactly* what OCO setup produced these numbers.

### 6.7 Non-streaming and self-host settings

- `selfHostedNonStreamingToolCalls: true` — controller-level flag
- `selfHostedParallelToolCalls: false`

The controller translates the first flag into `providerOptions.experimentalNonStreamingToolCalls: true` on the model, which is the option name OCO's session/LLM code reads. Kept because Qwen + vLLM 0.21.x streaming tool-call parsing is documented as having open bugs. Non-streaming is the safer path. Documented in the snapshot, not hidden.

### 6.8 Autocompaction — enabled for the benchmark

Production has `compaction.auto: false` because the user prefers deterministic manual `/compress manage` control. In a headless autonomous 731-task run there is no human to trigger compression, and a task that overflows context just dies — that "ran out of room" failure looks identical to "model gave up" in the final score, which conflates infrastructure with model quality.

The benchmark therefore turns autocompaction **on** with the following materialized policy:

| Field | Value | Why |
|---|---|---|
| `compaction.auto` | `true` | Automatic overflow handling in headless runs |
| `compaction.prune` | `true` | Cheap background drop of stale tool outputs; no methodology cost |
| `compaction.reserved` | `20000` (~10% of 200K) | OCO default; gives the compaction agent enough headroom to write the summary |
| `agent.compaction.model` | `selfhost-qwen` | Same Qwen3.6-27B-FP8 on localhost vLLM. Keeps the "everything on the pod" architecture; no external API call mid-attempt |
| `agent.compaction.prompt` | `prompts/compaction.txt` from production | Production fidelity — same compaction prompt the user has been iterating on |

Compaction calls fire as their own OCO session and are visible in the event stream as steps with `step_role: "compaction"` (see §5.5). Their token cost is recorded separately so:

- Total token spend per attempt includes compaction overhead, honestly disclosed.
- Per-attempt `compaction_events` count is surfaced as telemetry (zero on most attempts; non-zero on long ones).
- Aggregate compaction-token share is reportable in the writeup.

Qwen's own SWE-bench Pro scaffold for the 53.5 baseline almost certainly handled overflow somehow (truncation, summarization, or scaffold-imposed turn limits); the Qwen model card doesn't disclose the policy. By picking a documented compaction policy and reporting its cost, we make our policy auditable rather than implicit. This is recorded as a methodology note in the writeup.

## 7. Resume safety

Concrete semantics:

- **Atomic JSON/JSONL writes**: write to `.tmp`, `fsync`, rename. Never half-written files.
- **Per-attempt lease file** with timestamp and controller PID. Stale lease → attempt is recoverable by next controller startup.
- **Phase markers**: `SETUP_DONE`, `RUN_DONE`, `CAPTURE_DONE` files written atomically as each phase completes. Controller restart skips any phase already marked done.
- **Idempotent eval queue**: each attempt has a deterministic `eval-submission-id`. Modal worker dedupes on this ID.
- **Run-level resume rule**: same `--run-id` skips attempts with `DONE` artifacts present. `--force-rerun <id>` overrides for a specific attempt.
- **Mac backup**: every N minutes (default 5 min), the controller pushes durable artifacts to the Mac via `rsync` over SSH. Mac doesn't need to be on; rsync just no-ops when Mac is unreachable and resumes when it is.

If the controller process dies mid-RUN:
- Lease goes stale after timeout
- Next controller startup detects stale lease, examines phase markers, resumes from `SETUP_DONE` (re-runs RUN) or `RUN_DONE` (re-runs CAPTURE)
- Orchestrator/PM OCO sessions are resumed via OCO's session resume where possible; otherwise the attempt restarts cleanly from SETUP

If the pod is reclaimed by Community Cloud:
- Container disk is lost
- Latest Mac backup snapshot (if any) is restored to a freshly created pod
- Attempts marked `DONE` in that snapshot are preserved; in-flight attempts restart from SETUP
- **Worst-case data loss**: everything generated since the last successful Mac rsync. If Mac was reachable throughout, that's a few minutes. If Mac was unreachable for hours, that's hours of work. We accept this tradeoff for Community Cloud pricing; mitigated by ensuring Mac is online for the calibration drill and reachable during the full run.

## 8. Storage and resource budget

| Resource | Budget | Watermark trigger |
|---|---|---|
| Container disk (200 GB) | vLLM + model cache ~80 GB, repo cache ~30 GB, c16 worktrees ~40 GB, logs ~10 GB, artifacts ~5 GB → ~165 GB used | At 85% used, controller starts aggressive worktree cleanup |
| RAM (251 GB) | vLLM ~140 GB, OCO at c16 ~30-40 GB peak, controller and OS ~10 GB. Headroom ~60 GB | At 90% used, controller pauses new attempts |
| GPU memory (141 GB) | vLLM owns it via `gpu-memory-utilization 0.95`. Out of controller's hands | vLLM exits on OOM; controller stops run with clear reason |
| Mac backup | Small JSON files only. Realistic ~100 MB across 731 attempts | Mac disk free space checked at controller startup; warning only |

Cleanup policy:
- Worktree deleted after its attempt's `CAPTURE_DONE` marker exists AND that marker has been rsync'd to Mac (so the patch is durable elsewhere)
- Hot logs older than 1 hour rotated to compressed format
- Repo cache cap: keep only repos for tasks not yet attempted; evict completed-repo caches

## 9. Calibration batch (must pass before full 731 launch)

### 9.1 Selection — 20-30 tasks

- 5-8 long-context (large repos: ProtonMail, Element, OpenLibrary)
- 5-8 tool-heavy (Vuls, Ansible, Teleport)
- 5-8 previously-failed tasks from the last run (the `agent_failure` and `generation_infrastructure_artifact` set)
- A few "average" tasks for baseline behavior

### 9.2 Pass criteria

All of:

- Zero generation infrastructure artifacts. No `step_start` loops, no clog, no Cloudflare-like timeouts (there shouldn't be any — we're on localhost — but verify).
- Patches captured for every successful attempt, including untracked-file changes.
- Token, wall-time, and per-phase telemetry written for every attempt.
- vLLM steady-state: prefix cache hit ≥60%, no sustained queue, KV stable.
- **Resume drill**: kill the controller mid-run, restart, verify it resumes correctly with no duplicate work and no lost attempts. Mandatory.
- **Mac backup drill**: kill the controller, wipe the pod's container disk, restore from Mac snapshot, verify resume continues correctly. Mandatory.
- **Disk/RAM drill**: peak container disk usage ≤85%, peak RAM ≤90%, no eviction storms.
- **Glob/grep guard verification**: a synthetic calibration probe (not a real task) runs a deliberately broad glob pattern against a bounded fixture directory of <1000 files, confirming the timeout guard fires within its 30s default and the OCO subprocess emits the recovery message. Controlled drill, not a free-roam test on a real task.
- Eval pipeline: Modal eval consumes patches within reasonable lag, no duplicate submissions.

### 9.3 What "fail" means

If any criterion breaks, we fix before paid full-run launch. No mid-run patching of the controller during the 731.

## 10. Full 731 run

### 10.1 Parameters

- **Concurrency:** c16 (revisit after calibration; revise if calibration says lower).
- **Per-attempt timeout:** set from calibration data, not in advance. Take the p95 wall time of *successful* calibration attempts and multiply by **1.5** to set the per-task timeout for the full run. Floor of 30 minutes; soft ceiling of 120 minutes (hard ceiling at 180 minutes). Locking the timeout from calibration data avoids two failure modes:
  - Setting it too low up front and truncating legitimately long-running tasks (especially in large repos like ProtonMail/Element/OpenLibrary), which would show up as artificial timeouts conflated with model quality.
  - Setting it too high up front and burning pod hours on stuck-attempt edge cases.
  The calibration p95 also informs the §10.3 pod-runtime ceiling.
- **Resume-safe:** same run ID picks up where it left off.

### 10.2 Pipelined evaluation

- Each `CAPTURE_DONE` enqueues the patch for Modal eval
- Modal worker pool runs continuously, dedupes on submission ID
- By the time generation finishes, eval backlog should be ≤20 tasks

### 10.3 Budget guard

- Pod runtime cap: derived from calibration (target ~15-18h, with a +6h ceiling)
- Idle auto-stop: triggers only if **no unfinished attempts remain AND no eval backlog AND no controller heartbeat in 30 minutes**. Cannot be fooled by transient stalls.
- Per-hour cost ledger written to container disk and rsync'd to Mac

## 11. Outputs

### 11.1 Per-attempt artifacts (container disk, mirrored to Mac via rsync)

- `patch.diff`
- `normalized.json` — per-attempt summary plus a `steps[]` array of per-step records (see §5.5): each step's `prompt_tokens`, `completion_tokens`, `cached_prompt_tokens`, `reasoning_tokens`, `wall_time_ms`, `finish_reason`, `tools_called`, `step_role`
- `phase-log.jsonl` — controller phase transitions for this attempt
- `oco-events.ndjson` — raw OCO subprocess events (compressed)

### 11.2 Run-level artifacts

- `summary.json` — counts, pass rate, token totals, wall, cost ledger
- `oco-config-snapshot/` — exact OCO setup used (no secrets)
- `eval-bundle/` — Pro evaluator-format patches.json + raw_sample.jsonl
- `report.md` — writeup with headline scores, methodology, what changed from prior runs

### 11.3 Final headline (primary metric)

- **Pass rate on full 731 Pro: X.Y%** — denominator is all 731 tasks regardless of audit observation, patch quality, or generation outcome. Empty/missing patches count as fails.

- **Observation strata** (descriptive, not denominator filters). All three are computed identically across all 731 attempts from the same telemetry source. The strata definitions are published in the methodology section of the writeup so a critic can verify they aren't post-hoc tuned:

  | Stratum | Definition |
  |---|---|
  | `delegation_observed` | At least one `task` tool call targeting the `orchestrator` subagent appears in the PM session's event stream. |
  | `audit_observed` | At least one `task` tool call targeting the `auditor` subagent appears anywhere in the attempt's event tree (PM or Orchestrator). |
  | `full_loop_observed` | Both `delegation_observed` AND `audit_observed` hold; i.e., PM delegated to Orchestrator AND an Auditor was invoked somewhere in the attempt. |

  The stratum membership flags are written to `normalized.json` per attempt. A supplementary table in the writeup reports pass rate for each stratum vs its complement, so a reader can see *where in the harness the lift actually happens* — not just whether the audit loop was nominally engaged.

- **Comparison to Qwen 53.5 baseline** as context, with these explicit caveats disclosed:
  - Different scaffold: OCO's PM/Orchestrator/Auditor/Investigator harness vs Qwen's "internal agent scaffold (bash + file-edit tools)." This is the variable we *want* to measure.
  - Quantized weights and FP8 KV cache (theoretically a slightly weaker setup than Qwen's reported run).
  - Autocompaction policy disclosed (see §6.8); Qwen's overflow policy is not disclosed.
  - Our prompts (which are the OCO production prompts, snapshotted as a run artifact).
  - **Qwen evaluated against a privately-corrected version of the public Pro set** — model card quote: *"We correct some problematic tasks in the public set of SWE-bench Pro and evaluate all baselines on the refined benchmark."* Those corrections have not been released. We evaluate against the unmodified public 731-task set. Direction of skew unknown.
  - **Training-data overlap** between Qwen3.6's pretraining cutoff and the dates of SWE-bench Pro task PRs is plausible and unmeasurable on our side. Same caveat applies symmetrically to Qwen's own 53.5.

  The point is *direction of effect* with the asymmetries disclosed, not a controlled comparison.

### 11.4 Writeup-time supplementary analyses

These don't gate the run; they're produced from the final telemetry and added to the writeup as supplementary tables/figures:

- **Wilson 95% confidence interval** on the headline pass rate, computed as a binomial-proportion CI over 731. Lets a reader judge whether the overlap with 53.5 is meaningful.
- **Per-repo pass rate** broken out across the 11 Pro repos. Catches systematic per-repo failure modes and prevents headline distortion from uneven repo task counts.
- **Per-stratum pass rate** for each of the three observation strata above (and their complements). Plus a small note on stratum sample sizes — `audit_observed` may be small.
- **Token economy table**: mean and p95 of `tokens_in_total`, `tokens_out_total`, `reasoning_tokens_total`, `cached_tokens_total`, plus aggregate prefix cache hit rate and the share of total tokens consumed by the compaction agent.
- **Realized cost ledger**: pod hours × hourly rate, Modal compute, total $/task. Honest economics for any reader.
- **Thinking-token diagnostic**: ratio of `reasoning_tokens` to `completion_tokens` per step, distribution over the run. If the realized ratio is consistently very high (>70%) and `finish_reason: length` is non-trivial, that's evidence the thinking budget is starving the final answer and `max_tokens` should be raised in any future run.

## 12. Risk register

| Risk | Mitigation |
|---|---|
| vLLM scheduler clog at c16 | In-pod removes ghost requests; locked profile; calibration validates |
| Controller dies mid-phase | Atomic checkpoints + lease + phase markers + OCO session resume |
| Pod reclaimed by Community Cloud mid-run | Mac rsync backup; restore on new pod; worst case = since last successful rsync (see §7) |
| Mac unreachable during run | rsync no-ops, run continues, backup resumes when Mac returns; data loss bound widens until Mac is back |
| Audit fires rarely (model just doesn't invoke it) | Audit observation reported as strata; not in primary denominator |
| Production config drifts between snapshot and run | Snapshot committed at startup, written to artifacts, rsync'd to Mac |
| Modal evaluator changes or duplicate submissions | Eval submission ID is deterministic; worker dedupes |
| Disk/RAM pressure under c16 | Storage budget table + cleanup watermarks; calibration disk/RAM drill |
| Budget guard kills a healthy run | Threshold derived from calibration data; idle-stop checks unfinished + backlog + heartbeat |
| Wide unscoped glob/grep hangs OCO subprocess | OCO 2.1.7 has source-tree timeout guards (verified in installed binary) |
| Modal cost overrun | Trivial historically (~$2 for 168 tasks); free credit covers full 731 |

## 13. Sequencing

0. **OCO 2.1.7 build & install** (✅ completed 2026-05-21). Source-tree glob/grep timeout guards and `experimentalNonStreamingToolCalls` flag confirmed in the installed binary. Backup of 2.1.6 preserved at `~/.local/bin/oco.pre-2.1.7-20260521T182149Z`.
1. **Local cleanup** (✅ completed 2026-05-22). Archived the old `OCstuff/benchmarks/oco-vs-mini-swe-agent/` to external SSD at `/Volumes/external-nvme/oco-benchmark-archive/`; local copy deleted; ~11 GB recovered.
2. **Build controller skeleton + dry-run gate** under this repo's `controller/`, `scripts/`, and `tests/` (✅ completed 2026-05-22). Twelve sub-gates verified; 11 unit tests passing; auditor PASS; fixture-only RUN adapter; no real `oco` invocation in the dry-run path.
3. **Config materializer + real OCO RUN adapter wiring** (next). Replace the fixture adapter with the real OCO subprocess wrapper. Materialize the isolated benchmark config from `~/.config/oco/` per §6 (strip plugins/MCP, apply §6.8 autocompaction policy, point `agent.compaction.model` at `selfhost-qwen`). Add per-task seed pinning (§6.5), patch-apply precheck (§5.1), and observation strata computation (§11.3). Add a stricter cross-repo scope check than the dry-run gate's output-containment check. Locally testable; no pod required.
4. **SWE-bench Pro task loading + Modal eval pipeline.** Public 731 dataset loader, eval-bundle preparation, Modal worker pool with idempotent submission IDs, real rsync over SSH to the Mac backup target.
4.5 **Local-machine real-OCO smoke (required before any paid step).** Run `scripts/smoke_real_oco.py` against a local OpenAI-compatible endpoint (e.g., oMLX on `localhost:8000/v1`) with the documented opt-in env vars. Verifies the installed `oco` binary, the materialized config snapshot, the version/feature gate, the real OCO subprocess wrapper, telemetry parsing, patch precheck, and boundary proof all work end-to-end against a real model before we spend pod hours. A clean smoke is the gate to step 5.
5. **Boot H200 Community Cloud pod**, verify direct API health, prefix caching, and request-cancel behavior. Confirm `strace` is available in the pod image so the real-run boundary proof can write its filesystem trace; if not, install it before any benchmark attempt runs. Then run the **MTP A/B per §4.4** — baseline vs MTP-2 (and optionally MTP-4) at c1/c4/c8/c16/c20. Lock the winner.
6. **Calibration batch** (20-30 stress-picked tasks) against the locked profile. Must pass all pass criteria including resume drill, Mac backup drill, and disk/RAM drill. Calibration p95 sets the §10.1 per-task timeout.
7. **Full 731 run** with pipelined Modal eval.
8. **Writeup** with headline, strata, methodology, cost — including the §11.4 supplementary analyses (Wilson CI, per-repo, decontamination, thinking-token diagnostic).

---

*End of plan.*
