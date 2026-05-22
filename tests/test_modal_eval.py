from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from controller.atomic import atomic_write_json, atomic_write_text
from controller.modal_eval import (
    FixtureModalClient,
    ModalClientError,
    ModalEvalResponse,
    ModalEvaluationPipeline,
    ModalRetryPolicy,
    aggregate_modal_results,
    deterministic_submission_id,
)


def _attempt(run_root: Path, attempt_id: str, *, queued: bool = True) -> Path:
    attempt_dir = run_root / "attempts" / attempt_id
    attempt_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(
        attempt_dir / "normalized.json",
        {
            "attempt_id": attempt_id,
            "run_id": "modal-run",
            "queued_for_evaluation": queued,
        },
    )
    atomic_write_text(
        attempt_dir / "patch.diff",
        "diff --git a/file.py b/file.py\n--- a/file.py\n+++ b/file.py\n@@ -1 +1 @@\n-a\n+b\n",
    )
    return attempt_dir


def test_modal_submission_id_is_deterministic_across_processes(tmp_path: Path) -> None:
    expected = deterministic_submission_id("run", "task")
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "from controller.modal_eval import deterministic_submission_id; print(deterministic_submission_id('run','task'))",
        ],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    assert completed.stdout.strip() == expected


def test_modal_enqueue_dedup_and_per_attempt_cost(tmp_path: Path) -> None:
    run_root = tmp_path / "run"
    attempt_dir = _attempt(run_root, "task-1")
    client = FixtureModalClient([ModalEvalResponse("pass", cost_usd=0.02)])
    pipeline = ModalEvaluationPipeline(
        run_root=run_root, run_id="modal-run", client=client
    )

    first = pipeline.enqueue_attempt(
        attempt_dir=attempt_dir, task_row={"instance_id": "task-1"}
    )
    second = pipeline.enqueue_attempt(
        attempt_dir=attempt_dir, task_row={"instance_id": "task-1"}
    )
    results = pipeline.drain()
    normalized = json.loads(
        (attempt_dir / "normalized.json").read_text(encoding="utf-8")
    )

    assert first["status"] == "queued"
    assert second["deduped"] is True
    assert second["status"] in {"in_progress", "completed"}
    assert results[0]["outcome"] == "pass"
    assert len(client.calls) == 1
    assert normalized["modal_eval_cost_usd"] == 0.02
    assert aggregate_modal_results(run_root)["headline_pass_count"] == 1


class BlockingModalClient:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls: list[str] = []

    def evaluate(self, *, submission_id: str, bundle_dir: Path) -> ModalEvalResponse:
        del bundle_dir
        self.calls.append(submission_id)
        self.started.set()
        self.release.wait(timeout=5)
        return ModalEvalResponse("pass")

    def usage_report(self) -> dict | None:
        return None


def test_modal_enqueue_returns_while_worker_pool_is_still_evaluating(
    tmp_path: Path,
) -> None:
    run_root = tmp_path / "run"
    attempt_dir = _attempt(run_root, "pipelined")
    client = BlockingModalClient()
    pipeline = ModalEvaluationPipeline(
        run_root=run_root, run_id="modal-run", client=client, worker_count=1
    )

    result = pipeline.enqueue_attempt(attempt_dir=attempt_dir)

    assert result["status"] == "queued"
    assert client.started.wait(timeout=1)
    assert not (attempt_dir / "modal-result.json").exists()
    client.release.set()
    assert pipeline.drain()[0]["outcome"] == "pass"
    assert (attempt_dir / "modal-result.json").exists()


def test_modal_dedup_respects_existing_in_progress_submission(tmp_path: Path) -> None:
    run_root = tmp_path / "run"
    attempt_dir = _attempt(run_root, "task-in-progress")
    submission_id = deterministic_submission_id("modal-run", "task-in-progress")
    state_dir = run_root / "eval-state"
    state_dir.mkdir(parents=True)
    atomic_write_json(
        state_dir / "submissions.json",
        {
            "submissions": {
                submission_id: {
                    "status": "in_progress",
                    "submission_id": submission_id,
                    "instance_id": "task-in-progress",
                }
            }
        },
    )
    client = FixtureModalClient([ModalEvalResponse("pass")])
    pipeline = ModalEvaluationPipeline(
        run_root=run_root, run_id="modal-run", client=client
    )

    result = pipeline.enqueue_attempt(attempt_dir=attempt_dir)

    assert result["deduped"] is True
    assert result["status"] == "in_progress"
    assert client.calls == []


def test_modal_retry_timeout_hard_error_and_exhaustion(tmp_path: Path) -> None:
    run_root = tmp_path / "run"
    transient_attempt = _attempt(run_root, "transient")
    timeout_attempt = _attempt(run_root, "timeout")
    hard_attempt = _attempt(run_root, "hard")
    exhausted_attempt = _attempt(run_root, "exhausted")
    client = FixtureModalClient(
        [
            ModalClientError("transient", "cold start"),
            ModalEvalResponse("fail"),
            ModalClientError("evaluator_timeout", "watchdog fired"),
            ModalClientError("evaluator_hard_error", "parser failed"),
            ModalClientError("transient", "api 5xx"),
            ModalClientError("transient", "api 5xx"),
        ]
    )
    pipeline = ModalEvaluationPipeline(
        run_root=run_root,
        run_id="modal-run",
        client=client,
        retry_policy=ModalRetryPolicy(max_attempts=2, backoff_seconds=(0.0, 0.0)),
        worker_count=1,
    )

    pipeline.enqueue_attempt(attempt_dir=transient_attempt)
    pipeline.enqueue_attempt(attempt_dir=timeout_attempt)
    pipeline.enqueue_attempt(attempt_dir=hard_attempt)
    pipeline.enqueue_attempt(attempt_dir=exhausted_attempt)
    outcomes = [result["outcome"] for result in pipeline.drain()]
    assert outcomes == [
        "fail",
        "evaluator_timeout",
        "evaluator_hard_error",
        "modal_infrastructure_failure",
    ]
    assert aggregate_modal_results(run_root)["outcome_counts"] == {
        "pass": 0,
        "fail": 1,
        "evaluator_timeout": 1,
        "evaluator_hard_error": 1,
        "modal_infrastructure_failure": 1,
    }


def test_modal_account_level_failure_stops_new_dispatch(tmp_path: Path) -> None:
    run_root = tmp_path / "run"
    attempt_dir = _attempt(run_root, "account-stop")
    second_dir = _attempt(run_root, "second")
    client = FixtureModalClient([ModalClientError("auth_failure", "token expired")])
    pipeline = ModalEvaluationPipeline(
        run_root=run_root, run_id="modal-run", client=client
    )

    pipeline.enqueue_attempt(attempt_dir=attempt_dir)
    assert pipeline.drain()[0]["status"] == "account_stopped"
    stopped = pipeline.enqueue_attempt(attempt_dir=second_dir)
    summary = json.loads((run_root / "summary.json").read_text(encoding="utf-8"))

    assert stopped["reason"] == "modal_account_stopped"
    assert len(client.calls) == 1
    assert summary["modal_evaluation"]["account_level_stop"]["kind"] == "auth_failure"
    assert (
        summary["modal_evaluation"]["account_level_stop"]["dispatching_new_submissions"]
        is False
    )


def test_modal_run_level_cost_fallback(tmp_path: Path) -> None:
    run_root = tmp_path / "run"
    attempt_dir = _attempt(run_root, "cost-fallback")
    client = FixtureModalClient(
        [ModalEvalResponse("fail")], usage={"total_cost_usd": 1.23, "source": "fixture"}
    )
    pipeline = ModalEvaluationPipeline(
        run_root=run_root, run_id="modal-run", client=client
    )
    pipeline.enqueue_attempt(attempt_dir=attempt_dir)
    pipeline.drain()
    summary = json.loads((run_root / "summary.json").read_text(encoding="utf-8"))

    assert summary["modal_evaluation"]["total_cost_usd"] == 1.23
    assert summary["modal_evaluation"]["run_level_usage"]["source"] == "fixture"
