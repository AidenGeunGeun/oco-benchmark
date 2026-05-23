"""Benchmark controller state machine for the benchmark harness."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Protocol

from controller.artifacts import AttemptPaths, RunPaths
from controller.atomic import atomic_write_json, atomic_write_jsonl, atomic_write_text
from controller.backup import BackupHook, BackupResult, SSHBackupTarget
from controller.boundary import (
    BoundaryConfig,
    BoundaryMonitor,
    default_real_boundary_config,
)
from controller.constants import QWEN_OUTPUT_TOKEN_LIMIT
from controller.fixtures import FixtureOCOAdapter
from controller.leases import LeaseManager
from controller.materializer import MaterializerOptions, materialize_config
from controller.phases import Phase, next_phase
from controller.precheck import evaluate_patch_precheck
from controller.repo_cache import ProTaskRepo, RepoCacheManager
from controller.real_oco import RealOCOAdapter
from controller.seed import derive_task_seed
from controller.telemetry import aggregate_run, load_ndjson, normalize_events
from controller.version_gate import check_oco_binary, write_gate_artifact
from controller.watermarks import ResourceWatermarks


class RunAdapter(Protocol):
    def run(self, **kwargs: Any) -> Any: ...


@dataclass(frozen=True)
class AttemptSpec:
    attempt_id: str
    prompt: str | None = None
    base_commit: str | None = None
    repo: str | None = None
    repo_url: str | None = None
    task_row: dict[str, Any] | None = None


@dataclass
class ControllerConfig:
    run_root: Path
    run_id: str
    lease_stale_after_seconds: float = 300.0
    force_rerun: set[str] = field(default_factory=set)
    backup_destination: Path | None = None
    backup_create_destination: bool = True
    backup_ssh_host: str | None = None
    backup_ssh_user: str | None = None
    backup_ssh_target_dir: str | None = None
    backup_ssh_key_path: Path | None = None
    backup_ssh_port: int | None = None
    backup_bandwidth_limit_kbps: int | None = None
    backup_timeout_seconds: int = 30
    max_ram_pause_checks: int = 5
    adapter_kind: str = "fixture"
    production_config_dir: Path | None = None
    oco_binary: str | Path = "oco"
    model_name: str = "selfhost-qwen"
    endpoint_url: str | None = None
    api_key: str | None = None
    context_window: int = 200000
    output_token_limit: int = QWEN_OUTPUT_TOKEN_LIMIT
    primary_agent: str | None = None
    real_oco_timeout_seconds: float = 1800.0
    boundary_config: BoundaryConfig | None = None
    disable_boundary: bool = False
    continuation_mode: bool = False


class BenchmarkController:
    def __init__(
        self,
        config: ControllerConfig,
        *,
        adapter: RunAdapter | None = None,
        watermarks: ResourceWatermarks | None = None,
        repo_cache_manager: RepoCacheManager | None = None,
        modal_pipeline: Any | None = None,
    ) -> None:
        self.config = config
        self.run_paths = RunPaths(Path(config.run_root))
        self.adapter = adapter or self._adapter_from_config()
        self.repo_cache_manager = repo_cache_manager
        self.modal_pipeline = modal_pipeline
        project_root = Path(__file__).resolve().parents[1]
        if (
            config.boundary_config is None
            and config.adapter_kind == "real"
            and config.production_config_dir is not None
            and not config.disable_boundary
        ):
            self.boundary_config = default_real_boundary_config(
                run_root=self.run_paths.run_root,
                production_config_dir=config.production_config_dir,
                project_root=project_root,
                repo_cache_dir=(
                    repo_cache_manager.cache_root
                    if repo_cache_manager is not None
                    else None
                ),
            )
        else:
            self.boundary_config = config.boundary_config
        self.watermarks = watermarks or ResourceWatermarks(
            disk_path=self.run_paths.run_root
        )
        backup_target = self._backup_target_from_config(config)
        self.backup = BackupHook(
            backup_target,
            create_destination=config.backup_create_destination,
        )

    def _backup_target_from_config(
        self, config: ControllerConfig
    ) -> Path | SSHBackupTarget | None:
        if (
            config.backup_ssh_host
            or config.backup_ssh_user
            or config.backup_ssh_target_dir
        ):
            if not (
                config.backup_ssh_host
                and config.backup_ssh_user
                and config.backup_ssh_target_dir
            ):
                raise RuntimeError(
                    "SSH backup requires host, user, and target directory"
                )
            return SSHBackupTarget(
                host=config.backup_ssh_host,
                user=config.backup_ssh_user,
                target_dir=config.backup_ssh_target_dir,
                key_path=config.backup_ssh_key_path,
                port=config.backup_ssh_port,
                bandwidth_limit_kbps=config.backup_bandwidth_limit_kbps,
                timeout_seconds=config.backup_timeout_seconds,
            )
        return config.backup_destination

    def _adapter_from_config(self) -> RunAdapter:
        if self.config.adapter_kind == "real":
            return RealOCOAdapter(
                oco_binary=self.config.oco_binary,
                config_snapshot_dir=self.run_paths.config_snapshot_dir,
                agent=self.config.primary_agent,
                timeout_seconds=self.config.real_oco_timeout_seconds,
                filesystem_trace_enabled=not self.config.disable_boundary,
                output_token_limit=self.config.output_token_limit,
                preserve_existing_home=self.config.continuation_mode,
            )
        return FixtureOCOAdapter()

    def run_attempts(self, attempts: Iterable[AttemptSpec]) -> dict:
        self.run_paths.ensure()
        self._write_config_snapshot()
        result = {
            "completed": [],
            "skipped": [],
            "force_rerun": sorted(self.config.force_rerun),
        }

        for attempt in attempts:
            paths = self.run_paths.attempt(attempt.attempt_id)
            paths.ensure()
            if attempt.attempt_id in self.config.force_rerun:
                paths.clear_for_rerun()
                paths.append_phase_event("FORCE_RERUN")
                self.run_paths.append_run_event(
                    "FORCE_RERUN", attempt_id=attempt.attempt_id
                )

            if paths.marker_exists(Phase.DONE):
                LeaseManager(
                    paths.lease_path,
                    stale_after_seconds=self.config.lease_stale_after_seconds,
                ).release()
                self.run_paths.append_run_event(
                    "ATTEMPT_SKIPPED_DONE", attempt_id=attempt.attempt_id
                )
                result["skipped"].append(attempt.attempt_id)
                continue

            self._wait_for_ram(attempt.attempt_id)
            self._run_one_attempt(paths, attempt)
            result["completed"].append(attempt.attempt_id)
            self.cleanup_completed_worktrees()

        self._drain_modal_pipeline()
        self._mirror_completed_attempts()
        self.write_summary()
        if self.modal_pipeline is not None and hasattr(
            self.modal_pipeline, "update_run_summary"
        ):
            self.modal_pipeline.update_run_summary()
        self._mirror_run_level()
        return result

    def _drain_modal_pipeline(self) -> None:
        if self.modal_pipeline is not None and hasattr(self.modal_pipeline, "drain"):
            self.modal_pipeline.drain()

    def _mirror_completed_attempts(self) -> None:
        if not self.run_paths.attempts_dir.exists():
            return
        for attempt_dir in sorted(
            path for path in self.run_paths.attempts_dir.iterdir() if path.is_dir()
        ):
            paths = AttemptPaths(self.run_paths.run_root, attempt_dir.name)
            if paths.marker_exists(Phase.DONE):
                self._mirror_attempt(paths)

    def _write_config_snapshot(self) -> None:
        self.run_paths.ensure()
        if (
            self.config.adapter_kind == "real"
            and self.config.production_config_dir is None
        ):
            atomic_write_json(
                self.run_paths.config_snapshot_dir
                / "oco-config-materialization-error.json",
                {
                    "passed": False,
                    "reason": "real adapter requires production_config_dir for materialized snapshot",
                    "run_id": self.config.run_id,
                },
            )
            raise RuntimeError(
                "real adapter requires production_config_dir for materialized snapshot"
            )
        if self.config.production_config_dir is not None:
            materialized = self.run_paths.config_snapshot_dir / "opencode.jsonc"
            if materialized.exists():
                return
            gate_result = check_oco_binary(self.config.oco_binary)
            if not gate_result.passed:
                write_gate_artifact(
                    self.run_paths.config_snapshot_dir / "oco-version-gate.json",
                    gate_result,
                )
                raise RuntimeError(gate_result.reason)
            materialize_config(
                MaterializerOptions(
                    production_config_dir=self.config.production_config_dir,
                    output_dir=self.run_paths.config_snapshot_dir,
                    oco_version=gate_result.detected_version,
                    model_name=self.config.model_name,
                    endpoint_url=self.config.endpoint_url,
                    api_key=self.config.api_key,
                    context_window=self.config.context_window,
                    output_token_limit=self.config.output_token_limit,
                    primary_agent=self.config.primary_agent,
                )
            )
            return
        atomic_write_json(
            self.run_paths.config_snapshot_dir / "placeholder.json",
            {
                "dry_run": True,
                "note": "Fixture dry-run path; real OCO config materialization is disabled.",
                "run_id": self.config.run_id,
            },
        )

    def _wait_for_ram(self, attempt_id: str) -> None:
        checks = 0
        while True:
            status = self.watermarks.ram_status()
            if not status.exceeded:
                return
            self.run_paths.append_run_event(
                "RAM_PAUSE",
                attempt_id=attempt_id,
                used_ratio=status.used_ratio,
                threshold=status.threshold,
            )
            checks += 1
            if checks >= self.config.max_ram_pause_checks:
                raise RuntimeError(
                    "RAM watermark stayed above threshold; no new attempts spawned"
                )
            time.sleep(0.01)

    def _run_one_attempt(self, paths: AttemptPaths, attempt: AttemptSpec) -> None:
        lease_manager = LeaseManager(
            paths.lease_path, stale_after_seconds=self.config.lease_stale_after_seconds
        )
        recovered = lease_manager.recover_or_raise()
        if recovered:
            paths.append_phase_event("LEASE_RECOVERED")
        lease_manager.acquire()
        boundary = self._boundary_monitor()
        if boundary is not None:
            boundary.start()
        completed = False
        try:
            while True:
                phase = next_phase(paths.attempt_dir)
                if phase is None:
                    completed = True
                    return
                if phase == Phase.SETUP:
                    self._setup(paths, attempt)
                elif phase == Phase.RUN:
                    self._run(paths, attempt)
                elif phase == Phase.CAPTURE:
                    self._capture(paths, attempt)
                elif phase == Phase.DONE:
                    self._done(paths)
                    completed = True
                    return
        finally:
            if completed:
                self._mirror_attempt(paths)
            if completed and boundary is not None:
                proof = boundary.finish(paths.boundary_proof_path)
                paths.append_phase_event("BOUNDARY_PROOF_DONE", passed=proof.passed)
                self._mirror_attempt(paths)
            if completed and self.repo_cache_manager is not None and attempt.repo:
                self.repo_cache_manager.release_worktree(
                    attempt.repo.replace("/", "__"), attempt.attempt_id
                )
            if completed:
                lease_manager.release()

    def _boundary_monitor(self) -> BoundaryMonitor | None:
        if self.boundary_config is None:
            return None
        return BoundaryMonitor(self.boundary_config)

    def _setup(self, paths: AttemptPaths, attempt: AttemptSpec) -> None:
        paths.append_phase_event("SETUP_STARTED")
        if self.repo_cache_manager is not None and attempt.repo and attempt.repo_url:
            if not attempt.base_commit:
                raise RuntimeError("repo-cache setup requires a recorded base commit")
            result = self.repo_cache_manager.acquire_worktree(
                ProTaskRepo(
                    instance_id=attempt.attempt_id,
                    repo=attempt.repo,
                    repo_url=attempt.repo_url,
                    base_commit=attempt.base_commit,
                ),
                attempt_id=attempt.attempt_id,
                worktree_dir=paths.worktree_dir,
            )
            self.repo_cache_manager.record_acquisition_result(paths.attempt_dir, result)
            paths.append_phase_event(
                "REPO_ACQUISITION_DONE",
                outcome=result.outcome,
                success=result.success,
                reason=result.reason,
            )
            if not result.success:
                raise RuntimeError(result.reason)
        else:
            paths.worktree_dir.mkdir(parents=True, exist_ok=True)
            atomic_write_text(
                paths.worktree_dir / "README.txt",
                f"fixture worktree for {paths.attempt_id}\n",
            )
        if attempt.prompt:
            atomic_write_text(paths.attempt_dir / "task-prompt.txt", attempt.prompt)
        paths.write_phase_marker(Phase.SETUP)
        paths.append_phase_event("SETUP_DONE")

    def _run(self, paths: AttemptPaths, attempt: AttemptSpec) -> None:
        paths.append_phase_event("RUN_STARTED")
        seed = derive_task_seed(paths.attempt_id)
        result = self.adapter.run(
            attempt_id=paths.attempt_id,
            attempt_dir=paths.attempt_dir,
            worktree_dir=paths.worktree_dir,
            prompt=attempt.prompt,
            seed=seed,
            config_snapshot_dir=self.run_paths.config_snapshot_dir,
        )
        atomic_write_jsonl(paths.oco_events_path, result.events)
        paths.write_phase_marker(Phase.RUN)
        paths.append_phase_event("RUN_DONE", event_count=len(result.events), seed=seed)

    def _capture(self, paths: AttemptPaths, attempt: AttemptSpec) -> None:
        paths.append_phase_event("CAPTURE_STARTED")
        events = load_ndjson(paths.oco_events_path)
        normalized = normalize_events(
            events, attempt_id=paths.attempt_id, run_id=self.config.run_id
        )
        patch_diff = str(normalized.pop("patch_diff", ""))
        seed = derive_task_seed(paths.attempt_id)
        normalized["seed"] = seed
        precheck = evaluate_patch_precheck(
            worktree_dir=paths.worktree_dir,
            patch_text=patch_diff,
            base_commit=attempt.base_commit,
            scratch_dir=paths.attempt_dir / "precheck-worktree",
        )
        normalized.update(precheck.to_normalized_fields())
        atomic_write_text(paths.patch_path, patch_diff)
        atomic_write_json(paths.normalized_path, normalized)
        if self.modal_pipeline is not None and normalized.get("queued_for_evaluation"):
            try:
                modal_result = self.modal_pipeline.enqueue_attempt(
                    attempt_dir=paths.attempt_dir, task_row=attempt.task_row
                )
                paths.append_phase_event("MODAL_EVAL_ENQUEUED", **modal_result)
            except Exception as exc:  # noqa: BLE001 - account-level stop must be surfaced.
                paths.append_phase_event(
                    "MODAL_EVAL_STOPPED", reason=f"{type(exc).__name__}: {exc}"
                )
                raise
        paths.write_phase_marker(Phase.CAPTURE)
        paths.append_phase_event(
            "CAPTURE_DONE", patch_bytes=len(patch_diff.encode("utf-8"))
        )

    def _done(self, paths: AttemptPaths) -> None:
        paths.append_phase_event("DONE_STARTED")
        paths.write_phase_marker(Phase.DONE)
        paths.append_phase_event("DONE")

    def _mirror_attempt(self, paths: AttemptPaths) -> BackupResult:
        result = self.backup.mirror(
            paths.backup_paths(), source_root=self.run_paths.run_root
        )
        event_name = "BACKUP_DONE" if result.copied else "BACKUP_NOOP"
        paths.append_phase_event(
            event_name,
            reason=result.reason,
            copied=result.copied,
            attempted=result.attempted,
            success=result.success,
            retryable=result.retryable,
        )
        if result.copied:
            paths.write_state_marker("RSYNC_DONE")
        return result

    def _mirror_run_level(self) -> BackupResult:
        result = self.backup.mirror(
            self.run_paths.backup_paths(), source_root=self.run_paths.run_root
        )
        event_name = "RUN_BACKUP_DONE" if result.copied else "RUN_BACKUP_NOOP"
        self.run_paths.append_run_event(
            event_name,
            reason=result.reason,
            copied=result.copied,
            attempted=result.attempted,
            success=result.success,
            retryable=result.retryable,
        )
        return result

    def cleanup_completed_worktrees(self) -> list[str]:
        status = self.watermarks.disk_status()
        if not status.exceeded:
            return []
        cleaned: list[str] = []
        attempts_dir = self.run_paths.attempts_dir
        if not attempts_dir.exists():
            return []
        for attempt_dir in sorted(
            path for path in attempts_dir.iterdir() if path.is_dir()
        ):
            paths = AttemptPaths(self.run_paths.run_root, attempt_dir.name)
            if not paths.marker_exists(Phase.DONE):
                continue
            if not paths.state_marker_path("RSYNC_DONE").exists():
                if paths.worktree_dir.exists():
                    paths.append_phase_event(
                        "WORKTREE_CLEANUP_PAUSED",
                        reason="durable artifacts have not been backed up",
                        used_ratio=status.used_ratio,
                        threshold=status.threshold,
                    )
                continue
            if not all(
                artifact.exists()
                for artifact in (
                    paths.patch_path,
                    paths.normalized_path,
                    paths.phase_log_path,
                )
            ):
                paths.append_phase_event(
                    "WORKTREE_CLEANUP_PAUSED",
                    reason="durable artifact set is incomplete",
                    used_ratio=status.used_ratio,
                    threshold=status.threshold,
                )
                continue
            if not paths.worktree_dir.exists():
                continue
            for child in sorted(
                paths.worktree_dir.iterdir(),
                key=lambda path: path.as_posix(),
                reverse=True,
            ):
                if child.is_dir():
                    import shutil

                    shutil.rmtree(child)
                else:
                    child.unlink()
            paths.worktree_dir.rmdir()
            paths.append_phase_event(
                "WORKTREE_CLEANED",
                used_ratio=status.used_ratio,
                threshold=status.threshold,
            )
            paths.write_state_marker("WORKTREE_CLEANED")
            cleaned.append(paths.attempt_id)
        return cleaned

    def write_summary(self) -> dict:
        normalized_attempts: list[dict] = []
        if self.run_paths.attempts_dir.exists():
            for attempt_dir in sorted(
                path for path in self.run_paths.attempts_dir.iterdir() if path.is_dir()
            ):
                normalized_path = attempt_dir / "normalized.json"
                if normalized_path.exists():
                    normalized_attempts.append(
                        json.loads(normalized_path.read_text(encoding="utf-8"))
                    )
        summary = aggregate_run(normalized_attempts, run_id=self.config.run_id)
        atomic_write_json(self.run_paths.summary_path, summary)
        return summary
