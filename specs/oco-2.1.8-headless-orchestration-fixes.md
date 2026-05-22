# OCO 2.1.8 — Headless orchestration fixes

## Intent

Ship two OCO source fixes together as version 2.1.8 so the headless `oco run --format json` command can drive a persistent orchestrator subagent to completion. Today the smoke is blocked by an interaction of two distinct bugs in OCO's CLI + non-streaming code paths:

1. The non-streaming adapter drops the orchestrator's actual model output. When the AI SDK returns a result with `steps: []` but populated top-level content/usage/finishReason (observed for subagent `generateText()` calls), the adapter iterates an empty array and yields no `finish-step`, leaving the assistant message as an empty shell with only a `step-start` part in SQLite.

2. The CLI exits before the persistent orchestrator can complete. `oco run --format json` stops as soon as the root PM session emits `session.idle`. When PM finishes its own turn before the orchestrator's model call resolves, `bootstrap` disposes the instance, which aborts every active `SessionPrompt` controller — including the still-running orchestrator. The orchestrator's processor logs `error=The operation was aborted` and its assistant message never reaches a clean step-finish.

Fix 1 is already implemented on the 2.1.8 branch and shipped to `~/.local/bin/oco` ahead of audit. Fix 2 is new and the primary subject of this spec. Both fixes need to land cleanly together in the same 2.1.8 build, with a single audit cycle covering both.

## Spec Path

`/Users/aidenkim/projects/agents/OCstuff/oco-benchmark/specs/oco-2.1.8-headless-orchestration-fixes.md`

## Scope

OCO source under `/Users/aidenkim/projects/agents/OCstuff/OpenCodeOrchestra/packages/opencode/src/`:

- `session/llm.ts` — non-streaming adapter (fix 1, already implemented; audit verification only)
- `session/llm.test.ts` — non-streaming adapter tests (fix 1, already extended; audit verification only)
- `cli/cmd/run.ts` — `oco run` lifecycle (fix 2, primary implementation site)
- Any other CLI / session / instance file the implementer needs to touch in service of fix 2

Out of scope:

- Benchmark layer (`/Users/aidenkim/projects/agents/OCstuff/oco-benchmark/`). The benchmark must remain a passive consumer; the fix lives in OCO so the same OCO build behaves correctly for any headless caller, not just the benchmark.
- OCO desktop app, electron bundle, packaging changes beyond the standard 9-package version bump.
- Streaming-path behavior. Streaming has its own separate issues (vLLM tool-call parsers on B200); this spec does not touch that surface.

## Context

**Investigator-confirmed evidence for fix 1 (empty-steps fallback):**

Failing smoke run `runs/orchestration-smoke-20260522T074626Z` had the orchestrator session's assistant message in SQLite with `tokens: {input: 0, output: 0, ...}` and exactly one `step-start` part. PM session in the same run had healthy multi-part assistant messages from identical OCO build and non-streaming path. The difference between PM and orchestrator is the AI SDK result shape returned by the underlying model call: PM's result had populated `result.steps`; the orchestrator's result had `result.steps: []` while result-level content/usage/finishReason were present. The current adapter iterates only `result.steps`, so the empty array yielded no `finish-step` event and `session.processor` had nothing to persist.

OCO source citation for the adapter location: `session/llm.ts` around the `nonStreamingResultEvents` async generator (the loop body that emits `start-step` / per-part / `finish-step` per entry in `result.steps`, then a terminal `finish`).

The shipped fix introduces a fallback: when no step is emitted from `result.steps`, synthesize one step's worth of events from `result.content` / `result.usage` / `result.finishReason` / `result.response` / `result.warnings` / `result.providerMetadata`. The fallback always emits a `finish-step` even if `result.content` is empty so the processor records usage/finishReason rather than persisting an empty step-start shell.

Tests already added in `session/llm.test.ts`:

- `falls back to result-level content when result.steps is empty` — exercises text + tool-call content in a result with `steps: []`; asserts the same event sequence as a normal one-step result.
- `synthesizes only a finish-step when both steps and content are empty` — asserts a minimal start-step → finish-step → finish sequence preserving usage and finishReason.

**Investigator-confirmed evidence for fix 2 (wait-for-children):**

Failing smoke run `runs/orchestration-smoke-20260522T081939Z` (post fix 1) shows:

- `08:19:43` PM step 0 begins
- `08:20:15` PM step 1 begins, PM calls `task` with `subagent_type: orchestrator`
- `08:20:15` Orchestrator session created, non-streaming model call begins
- `08:20:37` PM step 2 begins, PM emits final text, PM step finishes with `finish_reason: stop`, `service=session.prompt sessionID=PM exiting loop`, `service=session.prompt sessionID=PM cancel`
- `08:20:37` `service=session.processor error=The operation was aborted. ... process` — the orchestrator's session processor is aborted mid-result-handling
- `08:20:37` `service=session.prompt sessionID=ORCH cancel`

Source path that causes this:

- `tool/task.ts` persistent-orchestrator path calls `SessionPrompt.prompt(...)` without `await`. The `task` tool returns immediately with a "Orchestrator launched" message. This is by design — orchestrators are persistent and complete via `handoff_to_pm`.
- `session/orchestrator-completion.ts` implements `handoff_to_pm`: it writes a synthetic user message into the parent PM session, publishes message/part events, then calls `wakeParent(...)` to restart the PM's loop (which may run inline if PM is already idle, or wait for the parent's next idle event and start the loop then).
- `cli/cmd/run.ts` line ~206: exit condition is "root session emitted `session.idle`". The CLI does not track or wait for child sessions; it exits as soon as the root PM session goes idle, regardless of whether persistent children are still running.
- `cli/bootstrap.ts`: wraps the run callback in a `try { ... } finally { Instance.dispose() }`. Disposal cascades into `SessionPrompt`'s state disposer which aborts every active controller, including the orchestrator's.

TUI does not hit this race because `tui/worker.ts` keeps a long-lived server and only disposes on reload/shutdown. The fix scope is the headless CLI lifecycle in `cli/cmd/run.ts` (and any helper needed in the bootstrap or session layer to expose child-session readiness).

## Implementation guidance

Out of scope for the spec to dictate exact code. Implementer chooses the cleanest shape. The following are guard rails, not prescriptions:

- Default behavior for `oco run --format json` MUST wait for all persistent child sessions created during the run to reach a terminal state (idle or aborted) before allowing `bootstrap` to dispose the instance. The fix MUST NOT change the persistent-orchestrator design or the `handoff_to_pm` flow.
- "Persistent child session" means a session whose `agentID` resolves to a persistent agent (the depth-1 orchestrator path in `tool/task.ts`). Single-shot specialists already await synchronously inside the `task` tool and do not need new wait logic.
- An opt-out for legacy non-orchestrator headless flows is acceptable as a CLI flag (e.g. `--no-wait-for-children` or similar). Default MUST be wait-on. Naming is the implementer's call.
- The wait MUST be bounded by either an explicit timeout flag (default sane, e.g. tens of minutes), the outer subprocess timeout (the benchmark adapter already bounds 5400s per task), or a documented unbounded wait. Whichever path is chosen, the behavior on timeout MUST be deterministic: dispose, log the still-running child sessions, and exit with a non-zero exit code so the benchmark adapter can classify the result.
- Child session lifecycle observation SHOULD use the existing event bus (`session.status`, `session.idle`, `session.created`) rather than polling SQLite. `cli/cmd/run.ts` already subscribes to events for the permission-prompt path; extending that subscriber to also track child terminal state is the natural shape.
- A handoff-completed orchestrator should be treated as terminal even if its session has not gone idle yet. After `handoff_to_pm` fires and `wakeParent` restarts PM, the orchestrator may still be in the middle of its own loop wind-down. The implementer must decide whether to wait for full session idle or use `handoff_to_pm` completion as the terminal signal; either is acceptable as long as the chosen signal is the LAST event the orchestrator emits in the happy path.

## Acceptance Criteria

1. **Empty-steps fallback (fix 1, already implemented; audit only):** the assistant message persisted in SQLite for a non-streaming subagent call where the AI SDK returns `steps: []` MUST contain all expected parts (`text` / `reasoning` / `tool` if present in `result.content`, plus `step-finish` with usage and finishReason). The synthesized step's usage MUST match `result.usage`. The synthesized step's finishReason MUST match `result.finishReason`.

2. **Empty-empty fallback (fix 1, already implemented; audit only):** when both `result.steps` is empty AND `result.content` is empty, the adapter MUST still emit a single `finish-step` so the processor records usage and finishReason rather than leaving an empty step-start shell in SQLite.

3. **Headless wait-for-children default (fix 2, primary):** `oco run --format json <prompt>` invoked against a config that delegates PM → orchestrator via the `task` tool MUST NOT exit until the spawned orchestrator session reaches a terminal state (idle after model call completes, OR after `handoff_to_pm` fires, OR after the orchestrator session aborts naturally). The CLI MUST NOT dispose the instance while persistent child sessions are still actively generating.

4. **Orchestrator output survives (fix 1 + fix 2 together):** after the smoke 2 run with the new build, the orchestrator's SQLite assistant message MUST contain at least one of `text` / `tool` parts (in addition to `step-start` / `step-finish`), AND its `tokens.input` MUST be non-zero, AND its `step-finish` part MUST carry a non-default `finishReason`. Empty assistant messages from orchestrator sessions are unacceptable.

5. **`handoff_to_pm` flow preserved:** PM continues to receive the synthetic user message and restart its loop after the orchestrator handoffs. PM's final response (if any) MUST be persisted as a normal assistant message with its own step-start / text / step-finish parts.

6. **Smoke 1 (PM-direct) regression-free:** the PM-direct smoke (`scripts/smoke_real_oco.py`) MUST still pass with the same shape it did under 2.1.7 and the current 2.1.8 build (file present, content matches, returncode 0).

7. **Timeout determinism:** if a persistent orchestrator does not reach terminal state within the chosen bound (timeout flag, outer subprocess timeout, or implementer-documented limit), `oco run` MUST exit with a non-zero exit code and log the unfinished child session IDs so the benchmark adapter can classify the outcome. Indefinite hangs are unacceptable.

8. **Tests:**
   - `bun test src/session/llm.test.ts` continues to pass with the two added empty-steps tests.
   - New tests added under `src/cli/cmd/run.test.ts` (or a sibling test file the implementer chooses) covering:
     - PM run that spawns a persistent child waits for the child to reach terminal state before exit.
     - PM run with no children exits as soon as root goes idle (no regression to existing behavior).
     - PM run with a child that times out exits non-zero with the child ID surfaced.
   - The full OCO test suite invoked the way the implementer normally invokes it MUST pass with no new failures attributable to these changes. Pre-existing failures unrelated to this work MAY be left untouched and noted.

9. **Build artifact:**
   - All nine package.json files remain at version 2.1.8 (NO bump to 2.1.9; the in-flight 2.1.8 is being completed, not superseded).
   - `bun run --cwd packages/opencode build --single --skip-install` succeeds.
   - The built binary at `packages/opencode/dist/@skybluejacket/oco-darwin-arm64/bin/oco` contains both fixes' identifying strings (verifiable via `strings`).

10. **Scope containment:** the diff MUST stay inside `OpenCodeOrchestra/packages/opencode/src/` (excluding test snapshots/fixtures it may need to touch). No changes to `oco-benchmark/`, `apps/`, `sdks/vscode/`, electron packaging, prompts, or AGENTS.md files as part of this implementation.

## Verification

For the auditor and implementer:

- `bun test src/session/llm.test.ts` from `/Users/aidenkim/projects/agents/OCstuff/OpenCodeOrchestra/packages/opencode` — all tests pass.
- `bun test src/cli/cmd/run.test.ts` (or wherever the new wait-for-children tests live) — all pass.
- `bun run typecheck` from the same directory — passes, OR if it surfaces only the pre-existing `tool/glob.ts` `any[]` warnings inherited from the 2.1.7 baseline, those are tolerated.
- `bun run --cwd packages/opencode build --single --skip-install` — succeeds.
- After installation by the PM (the build → install → codesign workflow is PM-owned, not Orchestrator-owned), the user re-runs the two smokes manually:
  - `scripts/smoke_real_oco.py` (smoke 1) — verdict `pass`, file present, content matches.
  - `scripts/smoke_orchestration_real_oco.py` (smoke 2) — orchestrator session's SQLite assistant message has at least one `text` or `tool` part beyond `step-start`, non-zero input tokens, and a real `finishReason`. The smoke's own verdict may still report `partial` (model didn't follow exact instructions on the weak 35B-A3B-4bit local) or `pass`, but the SQLite-level evidence of orchestrator completion is the actual gate.

## Completion Standard

Auditor PASS on a single review covering both fixes:

- Fix 1's already-shipped code in `session/llm.ts` and `session/llm.test.ts` is correct, on-spec, and has no regressions.
- Fix 2's new code in `cli/cmd/run.ts` (and any necessary helpers) implements the wait-for-children semantics, all listed acceptance criteria pass, and the diff is contained to OCO source.

PM then takes the audited build through `install -m 755` → ad-hoc codesign → version verification → user-driven smoke re-runs.
