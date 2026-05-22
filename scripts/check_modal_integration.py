#!/usr/bin/env python3
"""Opt-in Modal/upstream evaluator integration check.

Default behavior is a clean skip. With OCO_BENCHMARK_MODAL_INTEGRATION=1,
the script runs the pinned upstream SWE-bench Pro evaluator against a developer
supplied one-row bundle. The bundle should contain patches.json and
raw_sample.jsonl for a known tiny smoke case; this keeps paid Modal work explicit.
"""

from __future__ import annotations

import os
import json
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from controller.eval_bundle import (  # noqa: E402
    to_upstream_raw_sample_row,
    validate_bundle_conformance,
)
from controller.atomic import atomic_write_json, atomic_write_text  # noqa: E402
from controller.modal_eval import (  # noqa: E402
    ModalEvalResponse,
    ModalEvaluationPipeline,
)


DEFAULT_MODAL_SMOKE_INSTANCE_ID = (
    "instance_NodeBB__NodeBB-00c70ce7b0541cfc94afe567921d7668cdc8f4ac-vnan"
)


def main() -> int:
    if os.environ.get("OCO_BENCHMARK_MODAL_INTEGRATION") != "1":
        print("SKIP: set OCO_BENCHMARK_MODAL_INTEGRATION=1 to run the real Modal check")
        return 0
    checkout = _required_path("OCO_BENCHMARK_PRO_EVALUATOR_CHECKOUT")
    dockerhub = os.environ.get("OCO_BENCH_PRO_DOCKERHUB_USERNAME")
    if checkout is None or not dockerhub:
        print(
            "FAILED: set OCO_BENCHMARK_PRO_EVALUATOR_CHECKOUT and OCO_BENCH_PRO_DOCKERHUB_USERNAME",
            file=sys.stderr,
        )
        return 1
    run_root = Path(
        os.environ.get(
            "OCO_BENCHMARK_MODAL_RUN_ROOT",
            PROJECT_ROOT / "runs" / "modal-integration-controller",
        )
    )
    bundle_dir = _required_path("OCO_BENCHMARK_MODAL_BUNDLE_DIR")
    if bundle_dir is None:
        try:
            bundle_dir = _default_smoke_bundle(run_root)
        except RuntimeError as exc:
            print(f"FAILED: {exc}", file=sys.stderr)
            return 1
    conformance = validate_bundle_conformance(
        bundle_dir=bundle_dir, upstream_checkout=checkout, require_upstream=True
    )
    if conformance["status"] != "passed":
        print(f"FAILED: bundle conformance {conformance['errors']}", file=sys.stderr)
        return 1
    output_dir = Path(
        os.environ.get(
            "OCO_BENCHMARK_MODAL_OUTPUT_DIR",
            PROJECT_ROOT / "runs" / "modal-integration-output",
        )
    )
    patches = json.loads((bundle_dir / "patches.json").read_text(encoding="utf-8"))
    raw_rows = [
        json.loads(line)
        for line in (bundle_dir / "raw_sample.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    if len(patches) != 1 or len(raw_rows) != 1:
        print("FAILED: Modal integration expects a one-row bundle", file=sys.stderr)
        return 1
    attempt_id = str(patches[0]["instance_id"])
    attempt_dir = run_root / "attempts" / attempt_id
    attempt_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(
        attempt_dir / "normalized.json",
        {
            "attempt_id": attempt_id,
            "run_id": "modal-integration",
            "queued_for_evaluation": True,
        },
    )
    atomic_write_text(attempt_dir / "patch.diff", patches[0]["patch"])
    client = UpstreamEvaluatorModalClient(
        checkout=checkout, output_root=output_dir, dockerhub_username=dockerhub
    )
    pipeline = ModalEvaluationPipeline(
        run_root=run_root,
        run_id="modal-integration",
        client=client,
        worker_count=1,
    )
    print("Running controller Modal pipeline for one-row smoke bundle...")
    first = pipeline.enqueue_attempt(attempt_dir=attempt_dir, task_row=raw_rows[0])
    first_results = pipeline.drain()
    second = pipeline.enqueue_attempt(attempt_dir=attempt_dir, task_row=raw_rows[0])
    if first.get("status") != "queued" or not first_results:
        print("FAILED: pipeline did not enqueue and collect a result", file=sys.stderr)
        return 1
    if not second.get("deduped") or len(client.calls) != 1:
        print(
            "FAILED: pipeline dedup did not suppress the second submission",
            file=sys.stderr,
        )
        return 1
    print(
        f"PASS: controller pipeline completed outcome={first_results[0].get('outcome')} and deduped resubmission; output_dir={output_dir}"
    )
    return 0


class UpstreamEvaluatorModalClient:
    def __init__(
        self, *, checkout: Path, output_root: Path, dockerhub_username: str
    ) -> None:
        self.checkout = checkout
        self.output_root = output_root
        self.dockerhub_username = dockerhub_username
        self.calls: list[str] = []

    def evaluate(self, *, submission_id: str, bundle_dir: Path) -> ModalEvalResponse:
        self.calls.append(submission_id)
        output_dir = self.output_root / submission_id
        output_dir.mkdir(parents=True, exist_ok=True)
        command = [
            sys.executable,
            str(self.checkout / "swe_bench_pro_eval.py"),
            "--raw_sample_path",
            str(bundle_dir / "raw_sample.jsonl"),
            "--patch_path",
            str(bundle_dir / "patches.json"),
            "--output_dir",
            str(output_dir),
            "--scripts_dir",
            str(self.checkout / "run_scripts"),
            "--num_workers",
            "1",
            "--dockerhub_username",
            self.dockerhub_username,
        ]
        completed = subprocess.run(command, cwd=self.checkout, check=False, text=True)
        if completed.returncode != 0:
            return ModalEvalResponse(
                "modal_infrastructure_failure",
                logs=f"upstream evaluator exited {completed.returncode}",
            )
        result_path = output_dir / "eval_results.json"
        results = json.loads(result_path.read_text(encoding="utf-8"))
        passed = next(iter(results.values()), False)
        return ModalEvalResponse(
            "pass" if passed else "fail", raw={"output_dir": str(output_dir)}
        )

    def usage_report(self) -> dict | None:
        return None


def _required_path(name: str) -> Path | None:
    value = os.environ.get(name)
    if not value:
        return None
    path = Path(value)
    return path if path.exists() else None


def _default_smoke_bundle(run_root: Path) -> Path:
    task_list = Path(
        os.environ.get(
            "OCO_BENCHMARK_MODAL_TASK_LIST",
            PROJECT_ROOT / "runs" / "pro-public-731-task-list.jsonl",
        )
    )
    if not task_list.exists():
        raise RuntimeError(
            "default Modal smoke bundle needs runs/pro-public-731-task-list.jsonl; run scripts/materialize_pro_tasks.py --public first or set OCO_BENCHMARK_MODAL_BUNDLE_DIR"
        )
    rows = [
        json.loads(line)
        for line in task_list.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    try:
        row = next(
            item
            for item in rows
            if item.get("instance_id") == DEFAULT_MODAL_SMOKE_INSTANCE_ID
        )
    except StopIteration as exc:
        raise RuntimeError(
            f"default Modal smoke task {DEFAULT_MODAL_SMOKE_INSTANCE_ID} is missing from {task_list}"
        ) from exc
    patch = str(row.get("patch") or "")
    if not patch.strip():
        raise RuntimeError(
            "default Modal smoke task does not contain the dataset gold patch"
        )
    bundle_dir = run_root / "modal-smoke-bundle"
    bundle_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(
        bundle_dir / "patches.json",
        [
            {
                "instance_id": DEFAULT_MODAL_SMOKE_INSTANCE_ID,
                "patch": patch,
                "prefix": "modal-smoke-gold",
            }
        ],
    )
    raw_row = to_upstream_raw_sample_row(row)
    atomic_write_text(
        bundle_dir / "raw_sample.jsonl",
        json.dumps(raw_row, sort_keys=True, separators=(",", ":")) + "\n",
    )
    return bundle_dir


if __name__ == "__main__":
    raise SystemExit(main())
