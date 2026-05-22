"""Modal SWE-bench Pro evaluation pipeline contracts and fixture client."""

from __future__ import annotations

import fcntl
import hashlib
import json
import time
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Any, Iterator, Protocol

from controller.atomic import atomic_write_json
from controller.eval_bundle import (
    to_upstream_raw_sample_row,
    validate_bundle_conformance,
)


OUTCOMES: tuple[str, ...] = (
    "pass",
    "fail",
    "evaluator_timeout",
    "evaluator_hard_error",
    "modal_infrastructure_failure",
)
NON_RETRYABLE_KINDS = {"evaluator_timeout", "evaluator_hard_error"}
ACCOUNT_LEVEL_KINDS = {
    "auth_failure",
    "credit_exhausted",
    "quota_exhausted",
    "service_refusal",
}


class ModalPipelineError(RuntimeError):
    """Base Modal pipeline error."""


class ModalClientError(ModalPipelineError):
    def __init__(self, kind: str, reason: str) -> None:
        super().__init__(reason)
        self.kind = kind
        self.reason = reason


@dataclass(frozen=True)
class ModalEvalResponse:
    outcome: str
    logs: str = ""
    cost_usd: float | None = None
    raw: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.outcome not in OUTCOMES:
            raise ValueError(f"invalid Modal outcome {self.outcome!r}")


class ModalClient(Protocol):
    def evaluate(
        self, *, submission_id: str, bundle_dir: Path
    ) -> ModalEvalResponse: ...

    def usage_report(self) -> dict[str, Any] | None: ...


@dataclass(frozen=True)
class ModalRetryPolicy:
    max_attempts: int = 3
    backoff_seconds: tuple[float, ...] = (0.0, 0.01, 0.02)


class ModalEvaluationPipeline:
    def __init__(
        self,
        *,
        run_root: Path,
        run_id: str,
        client: ModalClient,
        retry_policy: ModalRetryPolicy | None = None,
        worker_count: int = 4,
    ) -> None:
        self.run_root = run_root
        self.run_id = run_id
        self.client = client
        self.retry_policy = retry_policy or ModalRetryPolicy()
        self.worker_count = worker_count
        self.state_dir = run_root / "eval-state"
        self.state_path = self.state_dir / "submissions.json"
        self.stop_path = self.state_dir / "account-stop.json"
        self._executor = ThreadPoolExecutor(
            max_workers=max(1, worker_count), thread_name_prefix="oco-modal-eval"
        )
        self._futures: dict[str, Future[dict[str, Any]]] = {}
        self._futures_lock = Lock()

    def enqueue_attempt(
        self, *, attempt_dir: Path, task_row: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        normalized_path = attempt_dir / "normalized.json"
        normalized = _load_json(normalized_path)
        if normalized.get("queued_for_evaluation") is not True:
            return {"enqueued": False, "reason": "not_queued_for_evaluation"}
        instance_id = str(normalized.get("attempt_id") or attempt_dir.name)
        submission_id = deterministic_submission_id(self.run_id, instance_id)
        bundle_dir = write_single_patch_bundle(
            attempt_dir=attempt_dir,
            run_id=self.run_id,
            instance_id=instance_id,
            task_row=task_row,
        )
        conformance = validate_bundle_conformance(bundle_dir=bundle_dir)
        if conformance["status"] != "passed":
            response = ModalEvalResponse(
                "evaluator_hard_error",
                logs="single-row bundle failed structural conformance",
                raw={"conformance": conformance},
            )
            result = self._write_result(
                attempt_dir=attempt_dir,
                normalized_path=normalized_path,
                submission_id=submission_id,
                response=response,
                attempts=0,
            )
            self._store_submission_state(submission_id, result)
            self.update_run_summary()
            return {"enqueued": True, "deduped": False, "outcome": response.outcome}

        with self._locked_state():
            state = self._load_state()
            if self.stop_path.exists():
                stop = _load_json(self.stop_path)
                return {
                    "enqueued": False,
                    "reason": "modal_account_stopped",
                    "stop": stop,
                }
            existing = state.get("submissions", {}).get(submission_id)
            if existing is not None:
                return {"enqueued": True, "deduped": True, **existing}
            state.setdefault("submissions", {})[submission_id] = {
                "status": "in_progress",
                "submission_id": submission_id,
                "instance_id": instance_id,
            }
            atomic_write_json(self.state_path, state)
        future = self._executor.submit(
            self._run_submission,
            attempt_dir=attempt_dir,
            normalized_path=normalized_path,
            submission_id=submission_id,
            bundle_dir=bundle_dir,
        )
        with self._futures_lock:
            self._futures[submission_id] = future
        return {
            "enqueued": True,
            "deduped": False,
            "status": "queued",
            "submission_id": submission_id,
        }

    def _run_submission(
        self,
        *,
        attempt_dir: Path,
        normalized_path: Path,
        submission_id: str,
        bundle_dir: Path,
    ) -> dict[str, Any]:
        try:
            response, attempts = self._evaluate_with_retry(submission_id, bundle_dir)
        except ModalClientError as exc:
            result: dict[str, Any] = {
                "status": "account_stopped",
                "submission_id": submission_id,
                "kind": exc.kind,
                "reason": exc.reason,
            }
            self._store_submission_state(submission_id, result)
            return result
        result = self._write_result(
            attempt_dir=attempt_dir,
            normalized_path=normalized_path,
            submission_id=submission_id,
            response=response,
            attempts=attempts,
        )
        self._store_submission_state(submission_id, result)
        self.update_run_summary()
        return result

    def drain(self) -> list[dict[str, Any]]:
        with self._futures_lock:
            futures = list(self._futures.items())
        results: list[dict[str, Any]] = []
        for submission_id, future in futures:
            result = future.result()
            results.append(result)
            with self._futures_lock:
                if self._futures.get(submission_id) is future:
                    self._futures.pop(submission_id, None)
        self.update_run_summary()
        return results

    def close(self) -> None:
        self.drain()
        self._executor.shutdown(wait=True)

    def _store_submission_state(
        self, submission_id: str, result: dict[str, Any]
    ) -> None:
        with self._locked_state():
            state = self._load_state()
            state.setdefault("submissions", {})[submission_id] = result
            atomic_write_json(self.state_path, state)

    def _evaluate_with_retry(
        self, submission_id: str, bundle_dir: Path
    ) -> tuple[ModalEvalResponse, int]:
        last_reason = ""
        for attempt in range(1, self.retry_policy.max_attempts + 1):
            try:
                response = self.client.evaluate(
                    submission_id=submission_id, bundle_dir=bundle_dir
                )
            except ModalClientError as exc:
                last_reason = exc.reason
                if exc.kind in ACCOUNT_LEVEL_KINDS:
                    self._record_account_stop(exc.kind, exc.reason)
                    raise
                if exc.kind in NON_RETRYABLE_KINDS:
                    return ModalEvalResponse(exc.kind, logs=exc.reason), attempt
                if attempt >= self.retry_policy.max_attempts:
                    return (
                        ModalEvalResponse(
                            "modal_infrastructure_failure",
                            logs=f"retry budget exhausted: {last_reason}",
                        ),
                        attempt,
                    )
                self._sleep_before_retry(attempt)
                continue
            if response.outcome in {"evaluator_timeout", "evaluator_hard_error"}:
                return response, attempt
            return response, attempt
        return (
            ModalEvalResponse(
                "modal_infrastructure_failure",
                logs=f"retry budget exhausted: {last_reason}",
            ),
            self.retry_policy.max_attempts,
        )

    def _sleep_before_retry(self, attempt: int) -> None:
        index = min(attempt, len(self.retry_policy.backoff_seconds) - 1)
        delay = self.retry_policy.backoff_seconds[index]
        if delay > 0:
            time.sleep(delay)

    def _write_result(
        self,
        *,
        attempt_dir: Path,
        normalized_path: Path,
        submission_id: str,
        response: ModalEvalResponse,
        attempts: int,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "status": "completed",
            "submission_id": submission_id,
            "outcome": response.outcome,
            "score_contribution": 1 if response.outcome == "pass" else 0,
            "attempts": attempts,
            "logs": response.logs,
            "cost_usd": response.cost_usd,
            "raw": response.raw or {},
        }
        atomic_write_json(attempt_dir / "modal-result.json", result)
        normalized = _load_json(normalized_path)
        normalized["modal_eval"] = {
            "submission_id": submission_id,
            "outcome": response.outcome,
            "score_contribution": result["score_contribution"],
            "cost_usd": response.cost_usd,
        }
        if response.cost_usd is not None:
            normalized["modal_eval_cost_usd"] = response.cost_usd
        atomic_write_json(normalized_path, normalized)
        return result

    def update_run_summary(self) -> dict[str, Any]:
        summary_path = self.run_root / "summary.json"
        summary: dict[str, Any] = (
            _load_json(summary_path)
            if summary_path.exists()
            else {"run_id": self.run_id}
        )
        summary["modal_evaluation"] = aggregate_modal_results(self.run_root)
        usage = self.client.usage_report()
        if usage:
            summary["modal_evaluation"]["run_level_usage"] = usage
            if "total_cost_usd" in usage:
                summary["modal_evaluation"]["total_cost_usd"] = usage["total_cost_usd"]
        if self.stop_path.exists():
            summary["modal_evaluation"]["account_level_stop"] = _load_json(
                self.stop_path
            )
        atomic_write_json(summary_path, summary)
        return summary

    def _record_account_stop(self, kind: str, reason: str) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_json(
            self.stop_path,
            {
                "event": "MODAL_ACCOUNT_LEVEL_STOP",
                "kind": kind,
                "reason": reason,
                "dispatching_new_submissions": False,
            },
        )
        self.update_run_summary()

    @contextmanager
    def _locked_state(self) -> Iterator[None]:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        lock_path = self.state_dir / ".modal-state.lock"
        with lock_path.open("w", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _load_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {"submissions": {}}
        return _load_json(self.state_path)


class FixtureModalClient:
    def __init__(
        self,
        responses: list[ModalEvalResponse | ModalClientError],
        *,
        usage: dict[str, Any] | None = None,
    ) -> None:
        self.responses = list(responses)
        self.calls: list[str] = []
        self.usage = usage
        self._lock = Lock()

    def evaluate(self, *, submission_id: str, bundle_dir: Path) -> ModalEvalResponse:
        del bundle_dir
        with self._lock:
            self.calls.append(submission_id)
            if not self.responses:
                return ModalEvalResponse("pass")
            item = self.responses.pop(0)
        if isinstance(item, ModalClientError):
            raise item
        return item

    def usage_report(self) -> dict[str, Any] | None:
        return self.usage


def deterministic_submission_id(run_id: str, instance_id: str) -> str:
    raw = f"{run_id}\0{instance_id}".encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()[:24]
    return f"oco-pro-eval-{digest}"


def write_single_patch_bundle(
    *, attempt_dir: Path, run_id: str, instance_id: str, task_row: dict[str, Any] | None
) -> Path:
    patch_text = (attempt_dir / "patch.diff").read_text(encoding="utf-8")
    bundle_dir = attempt_dir / "eval-bundle"
    bundle_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(
        bundle_dir / "patches.json",
        [{"instance_id": instance_id, "patch": patch_text, "prefix": run_id}],
    )
    raw = to_upstream_raw_sample_row(dict(task_row or {"instance_id": instance_id}))
    raw.setdefault("instance_id", instance_id)
    raw.setdefault("fail_to_pass", "[]")
    raw.setdefault("pass_to_pass", "[]")
    atomic_write_jsonl_compat(bundle_dir / "raw_sample.jsonl", [raw])
    return bundle_dir


def aggregate_modal_results(
    run_root: Path, *, headline_denominator: int = 731
) -> dict[str, Any]:
    counts = {outcome: 0 for outcome in OUTCOMES}
    costs: list[float] = []
    attempts_dir = run_root / "attempts"
    if attempts_dir.exists():
        for result_path in sorted(attempts_dir.glob("*/modal-result.json")):
            result = _load_json(result_path)
            outcome = str(result.get("outcome"))
            if outcome not in counts:
                outcome = "evaluator_hard_error"
            counts[outcome] += 1
            if isinstance(result.get("cost_usd"), (int, float)):
                costs.append(float(result["cost_usd"]))
    pass_count = counts["pass"]
    evaluated_count = sum(counts.values())
    return {
        "outcome_closed_set": list(OUTCOMES),
        "outcome_counts": counts,
        "evaluated_count": evaluated_count,
        "headline_denominator": headline_denominator,
        "headline_pass_count": pass_count,
        "headline_pass_rate": round(pass_count / headline_denominator, 6)
        if headline_denominator
        else 0.0,
        "denominator_discipline": "Only outcome 'pass' increments the score; all other Modal/evaluator outcomes are non-pass for the 731-task headline denominator.",
        "per_attempt_cost_total_usd": round(sum(costs), 6) if costs else None,
    }


def atomic_write_jsonl_compat(path: Path, rows: list[dict[str, Any]]) -> None:
    from controller.atomic import atomic_write_text

    atomic_write_text(
        path,
        "".join(
            json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n"
            for row in rows
        ),
    )


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))
