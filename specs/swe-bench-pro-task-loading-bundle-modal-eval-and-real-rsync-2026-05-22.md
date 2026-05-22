# Spec: SWE-bench Pro Task Loading + Eval-Bundle Prep + Modal Eval Pipeline + Real SSH Rsync

**Status:** Draft, awaiting spec audit
**Date:** 2026-05-22
**Owner:** PM
**Plan reference:** `docs/oco-pro731-benchmark-plan-2026-05-21.md` §3.3, §7, §8, §10.2, §11.1, §11.2, §11.3, §11.4, §13 (step 4)
**Prior specs:** 
- `specs/controller-core-and-dry-run-gate-2026-05-22.md` (completed, auditor PASS)
- `specs/config-materializer-and-real-oco-run-adapter-2026-05-22.md` (in flight)
**Supersedes:** `specs/swe-bench-pro-task-loading-and-eval-bundle-prep-2026-05-22.md` (drafted then folded into this combined spec for delegation efficiency)

---

## Intent

Make the benchmark able to (1) load the SWE-bench Pro public 731-task test set into a canonical local form, with the dataset source pinned for reproducibility; (2) manage per-task repository state within the storage budget watermarks from plan §8 with concurrency-safe eviction; (3) prepare an eval-bundle from completed attempts in the format the upstream Pro evaluator expects to consume; (4) run that bundle through the SWE-bench Pro evaluator on Modal as a pipelined per-patch worker pool with idempotent submission deduplication; and (5) replace the prior spec's local-directory rsync mirror with a real SSH rsync to a Mac backup target that no-ops cleanly when the Mac is unreachable.

This is the full step 4 of the locked plan §13.

## Scope

In:

**Pro dataset and tasks**
- A Pro task loader that produces a canonical task list for the public 731 from whatever canonical source the dataset is currently published in. The dataset source identity (repository or dataset name), revision / commit / version, and content hash of the loaded 731-row payload are recorded so a reviewer can verify the exact dataset used.
- A controller entry point that materializes the canonical task list deterministically.
- A small fixture dataset (≤20 synthetic tasks) under the test tree that mimics the Pro schema, so unit tests run offline without network access.

**Repo cache**
- A repo cache manager: clones each unique Pro repo once into a cache; produces a per-attempt worktree at the task's recorded base commit; evicts a repo's cache only when (a) the controller's storage watermark fires or eviction is otherwise warranted, AND (b) no attempt for that repo is active, leased, or in a non-terminal state. Honors the §8 watermark trigger from the prior spec.
- Recorded behavior for repo-acquisition failure modes: clone failure, transient network blip with retry policy, and missing base commit on the upstream remote. Failure mode produces a clean recorded outcome; no partial worktree or partial task artifact is left behind.

**Eval-bundle**
- An eval-bundle preparer that produces output conforming to the SWE-bench Pro evaluator's current input format. The exact upstream evaluator reference (repository, commit/release, doc URL) used to derive the format is pinned and recorded inside the bundle.
- Inclusion / exclusion policy: inclusion is driven by the canonical handoff signal the prior spec emits in each attempt's `normalized.json`, `queued_for_evaluation: true` (defined by the prior spec as "precheck passed AND patch artifact is real"). Exclusion reasons come from a closed set anchored to the prior spec's precheck flags plus defensive fallbacks for incomplete, missing, or inconsistent attempts. See criterion 10 for the full closed set.
- A bundle manifest with: dataset reproducibility metadata; upstream evaluator reference; counts and inclusion/exclusion breakdown with closed-set reasons; per-repo breakdown; integrity hashing metadata; and an unambiguous bundle-denominator definition that does NOT collide with the plan §11.3 headline denominator.
- A non-fixture conformance verification step that validates the produced bundle against the pinned upstream evaluator reference using the upstream's own documented schema / examples / validator, runnable on demand, result recorded in the manifest.

**Modal evaluation pipeline**
- A Modal-based evaluator pool that consumes the eval-bundle and runs the upstream SWE-bench Pro evaluator on each included patch in a Modal sandbox.
- Pipelined behavior: attempts that reach `CAPTURE_DONE` with `queued_for_evaluation: true` are enqueued for Modal evaluation as soon as the bundle preparer can produce a single-row sub-bundle for them; the Modal pool processes them continuously while the rest of the run is still generating.
- Idempotent submission identifiers derived deterministically from the run identifier and the task instance identifier (or equivalent). The Modal worker dedupes on this ID; a re-submission with the same ID is a no-op that returns the existing result.
- Per-patch result collection: outcomes from a closed set (see criterion 16) are written to a stable per-attempt artifact location alongside the existing CAPTURE artifacts, and aggregated into the run summary with plan §11.3 denominator discipline.
- Documented retry policy for transient Modal failures (network blip, Modal API 5xx, Modal cold-start failures). Permanent failures (e.g., evaluator reports a hard error unrelated to the patch) are recorded with the failure reason and do not infinite-retry.
- Cost recording: per-attempt Modal cost (compute time + any other line items Modal reports) is captured into the attempt's `normalized.json` if Modal exposes it, otherwise the Modal usage report at run end is captured into the run summary.
- A `prepare-pro-eval-bundle` (or equivalent intent) entry point that produces a bundle from a finalized completed run, separate from the pipelined per-attempt enqueue path. Both paths share the same bundle format and manifest contract.
- Modal worker reads only the bundle; it does not touch OCO source, the production OCO config, or the controller's internal state.

**Real SSH rsync to Mac backup target**
- The prior spec's local-directory rsync mirror is replaced by a real `rsync` over SSH to a configurable Mac target. Configurable: SSH host, user, target directory, SSH key path, optional non-default port.
- Robust-to-unreachable-Mac: when the Mac is unreachable (DNS failure, connection refused, SSH auth failure, timeout), the rsync call no-ops with a clear logged event and the run continues. The next periodic rsync attempts to push again. No durable controller state depends on the Mac being reachable.
- Durable-artifact selection: rsync pushes the same artifact set the prior spec already defined (patch.diff, normalized.json, phase logs, manifests, oco-config-snapshot, run summary). Repo caches and worktrees are not backed up.
- The prior spec's Mac backup drill (kill controller, restore from Mac snapshot, resume) continues to work end-to-end with the real rsync.
- Optional bandwidth/throttle parameters (exposed but with safe defaults) so the rsync never saturates an upload link during the run.

**Tests**
- Unit tests covering every contract above. All unit tests run offline against fixture data; no unit test requires network access to Hugging Face, GitHub, Modal, the Mac, or anywhere else.
- A separate, opt-in non-fixture integration check for Modal: runs against the developer's Modal account (gated by an explicit opt-in env var or CLI flag) with a single fixture patch and verifies the pipeline end-to-end including dedup. Skipped cleanly when the opt-in flag is not set.
- A separate, opt-in non-fixture integration check for SSH rsync: runs against a developer-supplied SSH target (gated by an explicit opt-in env var) with a synthetic small artifact set and verifies push + Mac-unreachable behavior. Skipped cleanly when unset.

Out of scope (later specs):
- Pod-side anything (vLLM, pod boot, MTP A/B).
- Calibration-batch selection per plan §9.1 (belongs with the calibration runner).
- The full Pro 731 launch itself (belongs with the calibration runner gating the launch).
- Any OCO source modification.

## Context

By the time this spec is implemented, the controller can run a real attempt against a local model endpoint end-to-end (from prior specs), produce attempt artifacts with the precheck flags, and back them up to a local directory mirror. What it can't do yet is load real Pro tasks, produce an eval-bundle the Pro evaluator can ingest, run that bundle through the actual evaluator, or push artifacts to a real remote backup target.

The Pro public test set is 731 tasks across 11 repos; the upstream dataset structure includes per-task `base_commit`, `problem_statement`, expected test outcomes, and other metadata. The upstream evaluator (`scaleapi/SWE-bench_Pro`) defines the bundle format and runs the evaluation. Modal is the recommended execution platform per the upstream README; the prior project's local-Docker path was deemed unworkable due to macOS Docker.raw disk blowup, and Modal proved successful at ~$2 for 168 patches in prior work.

Per plan §8, the storage budget allocates ~30 GB to the repo cache and triggers cleanup at 85% disk usage. The cache manager honors that trigger by evicting repos that are safe to evict. The controller's existing storage watermark monitor from the prior spec is the trigger source.

Per plan §10.2, evaluation is pipelined: each `CAPTURE_DONE` with a passing precheck enqueues the patch for Modal eval, the Modal worker pool runs continuously, dedupes on submission ID, and by the time generation finishes the eval backlog should be small.

Per plan §11.3, the headline denominator is 731 regardless of bundle / Modal outcomes. The bundle's per-bundle denominator is a partial-run candidate count that does NOT replace the plan §11.3 headline denominator. The manifest names this field unambiguously.

Task ordering determinism is a controller-reproducibility contract: running the canonical task-list materialization twice on the same dataset on different machines produces byte-identical output.

Modal cost from prior work: ~$2 for 168 patches, ~$10 expected for the full 731. The free Modal credit covers this; cost tracking exists primarily for the writeup's economics table per §11.4.

Production fidelity (per `oco-benchmark/AGENTS.md`) remains the hard rule. The materializer reads from `~/.config/oco/` and never writes to it; Modal workers touch only the bundle they're given; SSH rsync targets a separate Mac directory and never modifies anything inside `~/.config/oco/` or OCO source.

Implementation suspicions, clearly labeled as suspicions and not requirements:
- The dataset is probably most stable loaded via the Hugging Face `datasets` library from a pinned revision, but a git-vendored copy of `scaleapi/SWE-bench_Pro-os` is also reasonable. The Orchestrator picks.
- Modal workers are probably implemented as Modal Functions calling into a vendored copy of the upstream evaluator entry point, with the bundle row passed as input and the result returned as output. Final shape is the Orchestrator's call.
- The Modal queue is probably a simple in-process queue with a Modal-side dedup table keyed on submission ID, but other shapes are acceptable.
- The rsync wrapper probably builds an explicit allowlist of artifact patterns to push (so the controller never accidentally pushes the repo cache); the Orchestrator decides.
- Cost recording is probably driven by Modal's usage API at run end; if Modal exposes per-call cost cheaply, per-attempt cost is preferred.

## Acceptance Criteria

Each criterion is observable from outside the code; an auditor verifies each by inspecting artifacts, running tests, or reading documentation.

### Dataset and tasks

1. **Dataset loading.** The task-list materializer against the public Pro test set produces a canonical task list whose row count matches the upstream 731 and whose per-task fields cover everything the controller and the bundle preparer need. The field set is documented in a reviewer-visible developer artifact.

2. **Dataset reproducibility.** The bundle manifest (and any run-level dataset artifact) records: dataset source identifier, pinned revision / commit / release, and a content hash of the loaded 731-row payload. A reviewer can use these to verify the exact dataset that was loaded. A unit test asserts that recomputing the content hash from the loaded payload matches the recorded value.

3. **Determinism.** Running the materializer twice on the same dataset source + revision produces byte-identical canonical task-list output. Determinism is established by a combination of deterministic serialization (stable key ordering, stable line endings, no embedded timestamps) AND a fixture double-run test that asserts byte-identity across two invocations. The contract is "same input → byte-identical output on any machine"; the test is the local fixture double-run.

4. **Fixture-driven unit tests.** All unit tests run offline against the small fixture dataset. No unit test requires network access to Hugging Face, GitHub, Modal, or anywhere else.

5. **Malformed-dataset row behavior.** If the loader encounters a dataset row that fails schema validation, it fails cleanly with a recorded reason; no partial canonical task list is written for that materialization invocation. A unit test exercises this against a malformed fixture row.

### Repo cache

6. **Concurrency-safe repo cache lifecycle.** The cache manager clones each unique repo once, produces a per-attempt worktree at the task's recorded base commit on demand, respects the controller's storage watermark trigger from the prior spec, and NEVER evicts a repo while any attempt for that repo is active, leased, or in a non-terminal state. A unit test exercises a synthetic scenario where the watermark fires while one attempt for a repo is still leased and asserts the unsafe repo is not evicted.

7. **Repo-acquisition failure modes.** Each of the following failure modes has a documented recorded outcome and is exercised by a unit test against synthetic fixtures (no real network calls in unit tests):
    - Clone fails outright.
    - Transient network blip during clone or checkout, with the documented retry policy applied; eventual failure if retries exhausted.
    - Base commit no longer exists in the upstream repository.
   Failure outcomes are recorded so the bundle policy's closed-set exclusion handles them; no partial worktree or partial task artifact is left behind.

8. **Storage watermark behavior, not absolute footprint.** The cache manager honors the controller's 85% watermark trigger by attempting safe evictions when the watermark fires. A unit test simulates a synthetic high-disk-usage scenario and asserts: trigger fires; safe-to-evict repos are evicted; unsafe repos are NOT evicted.

   **Per-attempt worktree cleanup safety (restated from prior spec, sharpened for the SSH-rsync context).** A per-attempt worktree is deleted ONLY when (a) its attempt's CAPTURE phase is complete with durable on-disk artifacts (`patch.diff`, `normalized.json`, phase log), AND (b) those durable artifacts have been successfully rsync'd to the Mac OR a documented local-durability fallback applies (e.g., the local-directory fallback rsync mode is in use). When the Mac is unreachable for an extended period, the controller does NOT prematurely delete worktrees whose artifacts have not yet been backed up; it pauses worktree cleanup for those attempts and surfaces the pressure via the existing watermark events. A unit test exercises this constraint against a synthetic "Mac unreachable + high disk pressure" scenario and asserts the controller does not delete a worktree whose artifacts have not been confirmed durable.

### Eval-bundle

9. **Eval-bundle upstream-pinned conformance.** The bundle preparer produces output conforming to the SWE-bench Pro evaluator's current input format. The upstream evaluator reference (repository URL, pinned commit or release tag, documentation source consulted) is recorded inside the bundle manifest. A non-fixture conformance verification step validates the produced bundle against that upstream reference using the upstream's own schema / examples / validator, and the verification result is recorded in the manifest alongside the reference.

10. **Inclusion / exclusion closed set.** Inclusion is driven by the canonical handoff signal that the prior spec's CAPTURE phase emits per attempt in `normalized.json`: `queued_for_evaluation: true`. The prior spec defines that bit as "precheck passed AND the patch artifact is real," so the bundle preparer consumes the bit directly rather than recomputing its components. This avoids drift between the two specs' definitions of "ready for evaluation."

    Each excluded attempt has exactly one reason recorded in the manifest, selected from a documented closed set:
    - `precheck_failed` — attempt's `precheck_failed: true`
    - `no_patch` — attempt's `no_patch: true`
    - `attempt_incomplete` — attempt directory exists but none of the precheck flags is set (missing `normalized.json`, partial run, etc.)
    - `attempt_missing` — attempt directory is missing entirely for a task that should have been attempted
    - `artifact_inconsistent` — `queued_for_evaluation: true` BUT the on-disk patch artifact is missing or unreadable, OR any other observable inconsistency between `normalized.json` and the actual artifact set. Defensive catch-all that fails closed: an inconsistent attempt is excluded with this reason rather than silently bundled with a broken row.

    The bundle preparer never invents new states. A unit test exercises each of the five exclusion paths, including a planted artifact-inconsistency case (`queued_for_evaluation: true` with no patch file on disk).

11. **Manifest with unambiguous denominator naming.** The bundle manifest contains: dataset reproducibility metadata (criterion 2); evaluator reference (criterion 9); the count of attempts considered for this bundle (named in a way that cannot be confused with plan §11.3's headline 731-denominator, with a brief note pointing the reader at §11.3 for the final benchmark denominator); the count included; per-exclusion-reason counts; per-repo included/excluded breakdown; included instance identifiers; excluded instance identifiers with reasons; integrity hash metadata (criterion 12).

12. **Integrity hashing — algorithm and canonical input recorded.** The manifest records the hash algorithm, canonical input ordering applied before hashing, and the resulting digest. An external auditor can recompute the hash from the bundle files without reading implementation internals. A unit test exercises hash determinism and recomputation.

13. **Per-repo breakdown.** The manifest's per-repo breakdown lists, for each of the 11 Pro repos, how many tasks were considered, how many were included, and how many were excluded with each reason. Makes the supplementary per-repo analysis in plan §11.4 feasible without re-derivation.

### Modal evaluation pipeline

14. **Pipelined Modal evaluation.** Attempts that reach `CAPTURE_DONE` with `precheck_passed: true` are enqueued for Modal evaluation as part of the per-attempt lifecycle. The Modal pool processes them continuously while the rest of the run is still generating. A unit test exercises the enqueue path against a fixture queue (no real Modal calls) and asserts each precheck-passed attempt produces exactly one submission.

15. **Idempotent submission identifiers.** Each submission has a deterministic identifier derived from the run identifier and the task instance identifier (or an equivalently stable input). Resubmitting with the same ID is a no-op that returns the existing result. A unit test verifies determinism across processes and dedup behavior against a fixture Modal stub.

16. **Per-patch result artifact and outcome categorization.** For each attempt evaluated, the Modal outcome plus relevant logs are written to a stable per-attempt artifact location alongside the existing CAPTURE artifacts. The outcome is one of a closed set:
    - `pass` — the upstream Pro evaluator reported the patch passes the task's required tests.
    - `fail` — the upstream Pro evaluator reported the patch fails the task's required tests.
    - `evaluator_timeout` — the upstream Pro evaluator hit its own internal timeout. Categorized as a NON-pass for the final pass-rate denominator (so it counts against the score, never for it).
    - `evaluator_hard_error` — the upstream Pro evaluator returned a hard error unrelated to the patch (e.g., infrastructure-level failure inside the evaluator). Categorized as NON-pass for the denominator. Recorded with the evaluator's error reason so the writeup can disclose any non-trivial hard-error rate.
    - `modal_infrastructure_failure` — all Modal retries (criterion 17) were exhausted for transient infrastructure causes that are not the evaluator's own behavior. Categorized as NON-pass for the denominator.
    Plan §11.3 denominator discipline: ONLY `pass` lifts the score; all other outcomes count against it. The aggregator updates the run summary with per-outcome running counts.

17. **Modal retry policy and account-level failure stop.** Modal failures are classified and handled:
    - Transient evaluator-side or Modal-side glitches (network blip, Modal API 5xx, cold-start failures) are retried per a documented bounded policy (max retry count, backoff schedule). Exhausting the retry budget produces `modal_infrastructure_failure` (criterion 16).
    - Evaluator internal timeouts (the upstream evaluator's own watchdog firing) are NOT retried; they produce `evaluator_timeout` immediately.
    - Permanent evaluator hard errors are NOT retried; they produce `evaluator_hard_error` immediately.
    - **Account-level failures** — Modal API authentication failure, account credit / quota exhausted, organization-level service refusal — are recognized as run-stopping conditions. The controller stops dispatching new Modal submissions, records the account-level failure reason in the run summary with a clear stop event, and surfaces the condition for the operator. Already-dispatched submissions are allowed to complete or fail naturally. The controller does NOT silently re-queue or mark all remaining attempts `unknown`.
    A unit test exercises each failure class against a fixture Modal stub, including the account-level stop class.

18. **Modal cost recording with verification test.** Per-attempt or run-level Modal cost is captured. Per-attempt is preferred if Modal exposes it cheaply; run-level Modal usage is acceptable as a fallback. The cost record is part of the run summary and is referenced from `normalized.json` per attempt when available. A unit test exercises both paths (per-attempt cost available vs run-level fallback only) against fixture Modal usage data and asserts the resulting run summary contains the expected cost field.

19. **Opt-in non-fixture Modal integration check.** A documented opt-in mechanism (env var or CLI flag) enables a one-shot integration check that runs against the developer's actual Modal account using a known-valid tiny patch + task pair (one that the upstream Pro evaluator is known to evaluate cleanly, e.g., a small previously-confirmed-passing or previously-confirmed-failing case from the public dataset, or an equivalently stable fixture the Orchestrator selects and documents). The check verifies the pipeline end-to-end including dedup. Skipped cleanly with a clear log message when not opted in. Documented in the controller README, including which fixture patch + task pair the check uses and why it was chosen.

### Real SSH rsync to Mac

20. **Real SSH rsync replaces local-directory mirror.** The prior spec's local-directory rsync hook is replaced by a real `rsync` over SSH to a configurable Mac target. The previous local-directory mode remains available as a documented fallback for the dry-run gate, but the production rsync path uses real SSH against a configured target.

21. **Robust-to-unreachable-Mac.** When the Mac target is unreachable, the rsync call no-ops with a clear logged event and the run continues. The next periodic rsync attempts to push again. No durable controller state depends on the Mac being reachable. A unit test exercises each documented unreachability mode against a fixture (no real SSH) and asserts the run continues.

22. **Durable-artifact allowlist.** The rsync push uses an explicit allowlist of artifact patterns (patches, normalized.json, phase logs, manifests, oco-config-snapshot, run summary, eval-bundle artifacts, Modal result artifacts). Repo caches and per-attempt worktrees are NOT pushed. A unit test verifies the allowlist is enforced against a fixture artifact tree.

23. **Mac backup drill still passes.** The prior spec's Mac backup drill (kill controller, restore from Mac snapshot, resume) continues to work end-to-end with the real-SSH rsync replacing the local-directory mirror. A test exercises this against a local SSH-stub or against a real SSH target gated by the opt-in integration check.

24. **Opt-in non-fixture SSH integration check.** A documented opt-in mechanism enables a one-shot integration check against a developer-supplied SSH target with a synthetic small artifact set; verifies push + Mac-unreachable behavior. Skipped cleanly when not opted in. Documented in the controller README.

### Cross-cutting

25. **Runtime production-fidelity boundary still holds.** The controller's runtime production-fidelity boundary check from the prior spec continues to produce its expected proof artifact during attempts that touch repo caches, Modal pipeline, and the SSH rsync path. No file outside the controller's allowed area is modified by any of these operations. A unit test asserts this for a fixture flow.

26. **Implementation diff stays inside `oco-benchmark/`.** The Orchestrator's own changeset is confined to `oco-benchmark/`. The Orchestrator runs the same diff-containment check the prior spec required and reports the result before completion. If the check flags any modification outside `oco-benchmark/`, that is a hard fail to fix before reporting.

27. **No regression.** Prior unit tests, the dry-run gate, and the prior spec's contracts continue to pass unchanged.

## Verification

From `oco-benchmark/`:

- `python -m pytest tests/ -v` exits 0 with new tests added and all prior tests still passing.
- `python scripts/dry_run_gate.py` exits 0 (regression check for the prior twelve sub-gates).
- The materializer entry point runs against the public Pro test set and produces a canonical task list with the expected row count; running it twice produces byte-identical output.
- The eval-bundle preparer runs against a fixture completed run (a small set of synthetic attempts with mixed precheck statuses, plus one `attempt_incomplete` case and one `attempt_missing` case) and produces a bundle + manifest matching the closed-set inclusion policy.
- The non-fixture upstream conformance step runs against the pinned Pro evaluator reference and records its result inside the manifest.
- The Modal pipeline runs in unit tests against a fixture Modal stub for all enqueue, dedup, retry, and result-collection paths.
- The SSH rsync runs in unit tests against a fixture SSH stub for the unreachable-Mac paths and the allowlist enforcement.
- If the opt-in Modal integration env var/flag is set, the one-shot Modal integration check passes end-to-end against the developer's Modal account.
- If the opt-in SSH integration env var/flag is set, the one-shot SSH integration check passes against the developer's SSH target.
- The Orchestrator runs the auditor against the finished work before reporting completion. The auditor verifies, among other things: malformed-dataset rejection; each of the five closed-set exclusion reasons (`precheck_failed`, `no_patch`, `attempt_incomplete`, `attempt_missing`, `artifact_inconsistent`); the concurrency-safe eviction rule; the per-attempt worktree cleanup safety constraint under Mac-unreachable + high disk pressure; integrity-hash recomputability; dataset reproducibility metadata presence + self-consistency; idempotent Modal submission ID dedup; Modal outcome closed set with plan §11.3 denominator discipline; Modal transient retry, permanent-failure-no-retry, and account-level run-stop handling; Modal cost recording (both per-attempt and run-level fallback paths); the SSH rsync unreachable-Mac path; and the durable-artifact allowlist.

## Completion Standard

- All twenty-seven acceptance criteria visibly hold.
- Module boundaries from prior specs are preserved.
- All unit tests run offline against fixture data.
- The non-fixture conformance step (criterion 9), the opt-in Modal integration (criterion 19), and the opt-in SSH integration (criterion 24) are runnable on demand and documented.
- A short developer-facing section is added to `controller/README.md` (or an adjacent docs file) describing: the loader, dataset reproducibility metadata, cache lifecycle, bundle format, exclusion policy, integrity hashing contract, Modal pipeline (enqueue / dedup / retry / cost), Modal opt-in integration, real SSH rsync configuration, the unreachable-Mac behavior, and the SSH opt-in integration. Plain language. (Documentation artifact, not a constraint on module layout.)
- The runtime production-fidelity boundary check (criterion 25) holds for all fixture flows.
- The implementation diff stays inside `oco-benchmark/` (criterion 26).
- Auditor PASS.

Report back when the auditor has passed the work, summarizing in plain language what was built, how the contracts are verified, and any open items the next spec (pod boot + MTP A/B harness, gated by Aiden's paid-resource green light) will need to address.
