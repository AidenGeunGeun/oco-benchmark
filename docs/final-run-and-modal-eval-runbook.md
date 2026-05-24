# Final Run and Modal Evaluation Runbook

This is the live checklist for turning the H200 SWE-bench Pro 731 generation artifacts into transparent, official evaluation results.

It is intentionally operational: it records the live sequence from final generation through official Modal evaluation so the final writeup can cite decisions and artifacts without relying on chat memory.

## 1. Current reporting stages

All stages use the same public SWE-bench Pro denominator: **731 tasks**. No task is filtered out of the headline denominator.

| Stage | Source roots | Evaluator-ready patches | Empty / non-evaluator-ready | Status |
|---|---|---:|---:|---|
| First pass | `swepro731-qwen-h200-mtp4-live-20260522T135306Z` | 596 | 135 | Complete |
| First pass + recovery wave | first pass + continuation + rerun roots | 692 | 39 | Complete |
| First pass + recovery + Wave3 | first pass + continuation + rerun + Wave3 root | 722 | 9 | Complete |
| Final generated bundle | first pass + continuation + rerun + Wave3 + Wave4 + Wave5 + Wave8 | 731 | 0 | Complete |

The final stopping rule is the per-task agent-loop budget, not the wave count. After Wave3, the remaining no-patch tasks were still far under budget, so additional continuation waves were applied uniformly until all 731 tasks had non-empty patches. The raw final bundle is preserved, and the primary official-eval bundle is sanitized for source-patch evaluation.

## 2. Continuation policy

Continuation waves exist because the remaining no-patch attempts were still far below the SWE-bench Pro paper's reported agent-loop budget. The goal is not to cherry-pick; it is to give the harness a full agent-loop opportunity before submitting.

Wave3 input set:

- 39 total not-clean tasks after the first recovery wave.
- 33 continued from the latest available attempt state (`worktree` + `oco-home`).
- 6 reran fresh because earlier disk cleanup removed their worktrees.

Locked rule:

- Wave count is not the methodological stopping rule; cumulative per-task agent-loop budget is.
- Continue no-patch tasks uniformly while they remain far below the comparison scaffold budget.
- Stop when a task produces a non-empty evaluator-ready patch, explicitly concludes no viable fix, times out/infra-fails, or approaches the turn/tool budget.
- Report cumulative step / tool counts for any remaining empty-patch tasks.
- Preserve first-pass and intermediate-stage numbers separately.

## 3. Known run deviations to disclose

These are not reasons to discard the run; they are part of the methodology record.

| Deviation | What happened | Reporting treatment |
|---|---|---|
| Formal calibration skipped | One Flipt single-task verification replaced the planned 20-30 task calibration | Disclose as an operational shortcut |
| Boundary proof disabled | `strace` caused EBADF startup failures under parallel launch | Boundary proof is not claimed for this run |
| First pass output cap bug | First pass used ~32k output cap instead of 81,920 | First-pass stops remain first-pass failures; recovery and Wave3 use 81,920 |
| Worktree cleanup altered recovery mode | Some bad-attempt worktrees were removed to keep disk alive | Explains 50 continuation + 78 fresh rerun, and later 33 continuation + 6 fresh Wave3 |
| RAM watermark bug | Initial code used MemFree rather than MemAvailable and stalled drivers | Pod-local fix applied; must be committed before future runs |
| Snapshot race | Wave3 parallel launch raced on config snapshot materialization | Operational workaround: prime snapshot with one driver before fan-out |
| Public uploaders blocked pod | Catbox/Litterbox rejected uploads from the pod IP / WAF path | Sanitized bundle stayed on pod; Modal credentials copied to pod instead of transferring bundle to Mac |
| Evaluator cwd gotcha | Pro evaluator resolves `dockerfiles/` relative to current working directory | Run evaluator from `vendor/SWE-bench_Pro-os`, not from repo root |

## 4. Modal setup checklist

Modal evaluation is being run from the pod because public uploaders blocked the pod and the Runpod SSH proxy did not provide a clean file-transfer path. Modal credentials were copied into `/root/.modal.toml` on the pod and verified with `python -m modal profile current`; remove this file after evaluation.

Historical Mac-side setup path, if evaluating off-pod later:

1. Create or activate a local Python environment for evaluator tooling.
2. Install Modal if missing:

   ```bash
   python3 -m pip install --upgrade modal
   ```

3. Authenticate if needed:

   ```bash
   python3 -m modal setup
   ```

4. Verify credentials:

   ```bash
   python3 -c 'import modal; print("modal", getattr(modal, "__version__", "unknown"))'
   python3 -m modal profile current
   ```

Expected: profile should be Aiden's Modal workspace. If this fails, fix Modal auth before preparing the official eval launch.

Pod-side verification used for this run:

```bash
/workspace/.venv/bin/python -m modal profile current
```

Expected profile: `aidengeungeun`.

## 5. Final artifact and evaluation steps

After final generation shows all 731 tasks have non-empty patches, do this in order.

1. Compute final generation tally across all roots:
   - first pass;
   - continuation;
   - fresh rerun;
   - Wave3;
   - Wave4;
   - Wave5;
   - Wave8.

2. Build the final per-task source-of-truth table:
   - each of the 731 instance IDs appears exactly once;
   - highest-priority patch source wins in this order: Wave8, Wave7, Wave6, Wave5, Wave4, Wave3, rerun, continuation, first pass;
   - empty-patch rows would remain represented as empty patches, but final generation reached 0 empty patches.

3. Generate final official-eval bundle:
   - `patches.json` includes all 731 rows;
   - `raw_sample.jsonl` includes the matching 731 public Pro task rows;
   - manifest records patch source counts and exclusion/empty-patch counts.

4. Sanitize generated/binary/build artifacts from the raw generated patches for official source-patch evaluation. Preserve the raw generated bundle as audit trail.

5. Validate bundle conformance against the pinned upstream evaluator reference.

6. Run a one-task Modal smoke before launching the full 731 evaluation.

7. Launch Modal official evaluation in bounded chunks if the full launch script is not yet proven for this repo state.

## 5.1 Final bundle record

Raw generated bundle on pod: `/workspace/runs/swepro731-final-eval-bundle-20260523T184500Z`.

- 731 patch rows, 731 raw rows, 731 non-empty patches.
- `patches.json`: 421,863,677 bytes, SHA-256 `ab62a826df3179e56307f1e321708111ffa4cfb8bd3eab8a7a66d9766cac2252`.
- Raw bundle is retained for audit and reproducibility.

Primary official-eval bundle on pod: `/workspace/runs/swepro731-final-eval-bundle-sanitized-20260523T190000Z`.

- 731 patch rows, 731 raw rows, 731 non-empty source patches.
- `patches.json`: 18,460,372 bytes, SHA-256 `650efe3d5e0815b1bca5dab9281a027ced8b2324b8fe8630093d4ca2d175aa4e`.
- `raw_sample.jsonl`: 24,882,595 bytes, SHA-256 `2f6a2a5568edb0518ccd62606d24446e87c72a9eda72974d3cc5042185466843`.
- `removed-generated-artifacts.json`: 269,648 bytes, SHA-256 `2e15f50a12e2d77226db46981935b2f78669f1b6d21c93fbd0dc44b6a8bda960`.
- Removed 1,494 generated/binary/build file diffs totaling 397,048,628 bytes. The full per-file removal list is in the sanitized bundle.

The sanitized bundle should be used for official Modal evaluation because SWE-bench Pro evaluates source patches; generated binaries and vendored/build outputs are not meaningful source fixes. The raw generated bundle remains the audit trail for what the model produced.

## 5.2 Modal smoke gotcha

The pinned evaluator repository is cloned on the pod at `vendor/SWE-bench_Pro-os` and checked out to commit `ca10a60a5fcae51e6948ffe1485d4153d421e6c5`.

The evaluator resolves `dockerfiles/` relative to the current working directory. Therefore run Modal evaluation from inside the evaluator checkout:

```bash
cd /workspace/oco-benchmark/vendor/SWE-bench_Pro-os

/workspace/.venv/bin/python swe_bench_pro_eval.py \
  --raw_sample_path /workspace/runs/modal-smoke-final-sanitized-20260523T191000Z/raw_sample.jsonl \
  --patch_path /workspace/runs/modal-smoke-final-sanitized-20260523T191000Z/patches.json \
  --output_dir /workspace/runs/modal-smoke-final-sanitized-20260523T191000Z-output-cwd-fixed \
  --dockerhub_username jefzda \
  --scripts_dir run_scripts \
  --num_workers 1
```

Do not add `--use_local_docker`; Modal is the default path. If this command is run from `/workspace/oco-benchmark`, the evaluator fails immediately with missing `dockerfiles/base_dockerfile/.../Dockerfile` because the relative path resolves from the wrong directory.

## 6. Modal evaluation reporting

Official evaluator outcomes should be reported with this closed set:

- `pass`
- `fail`
- `evaluator_timeout`
- `evaluator_hard_error`
- `modal_infrastructure_failure`

Only `pass` lifts the score. Everything else is non-pass unless a clear Modal account/platform incident requires rerun under the predeclared retry policy.

Final writeup should include:

- pass / fail / non-pass counts over 731;
- strict pass rate over 731;
- Wilson 95% confidence interval;
- per-repo breakdown;
- first-pass vs recovery vs Wave3 contribution table;
- empty-patch / Not-Submitted count after Wave3;
- realized cost ledger: pod hours, Modal cost, total cost per evaluated task.
