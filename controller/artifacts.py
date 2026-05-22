"""Filesystem layout for run and attempt artifacts."""

from __future__ import annotations

import json
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from controller.atomic import append_jsonl_atomic, atomic_write_text
from controller.phases import PHASE_MARKERS, Phase, marker_path


ATTEMPT_ARTIFACT_FILES: tuple[str, ...] = (
    "patch.diff",
    "normalized.json",
    "phase-log.jsonl",
    "oco-events.ndjson",
)
OPTIONAL_ATTEMPT_ARTIFACT_FILES: tuple[str, ...] = (
    "boundary-proof.md",
    "oco-version-gate.json",
    "oco-subprocess.json",
    "oco-stdout.log",
    "oco-stderr.log",
    "filesystem-trace.log",
    "modal-result.json",
    "modal-eval-log.jsonl",
)
ATTEMPT_STATE_FILES: tuple[str, ...] = tuple(PHASE_MARKERS.values()) + (
    "RSYNC_DONE",
    "WORKTREE_CLEANED",
)


def utc_event(event: str, **fields: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"event": event, "timestamp": time.time()}
    payload.update(fields)
    return payload


@dataclass(frozen=True)
class RunPaths:
    run_root: Path

    @property
    def attempts_dir(self) -> Path:
        return self.run_root / "attempts"

    @property
    def summary_path(self) -> Path:
        return self.run_root / "summary.json"

    @property
    def run_log_path(self) -> Path:
        return self.run_root / "run-events.jsonl"

    @property
    def config_snapshot_dir(self) -> Path:
        return self.run_root / "oco-config-snapshot"

    def attempt(self, attempt_id: str) -> "AttemptPaths":
        return AttemptPaths(self.run_root, attempt_id)

    def ensure(self) -> None:
        self.attempts_dir.mkdir(parents=True, exist_ok=True)
        self.config_snapshot_dir.mkdir(parents=True, exist_ok=True)

    def append_run_event(self, event: str, **fields: Any) -> None:
        append_jsonl_atomic(self.run_log_path, utc_event(event, **fields))

    def backup_paths(self) -> list[Path]:
        candidates = [
            self.summary_path,
            self.run_log_path,
            self.run_root / "task-list.jsonl",
            self.run_root / "task-list.manifest.json",
        ]
        for name in ("oco-config-snapshot", "eval-bundle"):
            root = self.run_root / name
            if root.exists():
                candidates.extend(path for path in root.rglob("*") if path.is_file())
        candidates.extend(self.run_root.glob("*.manifest.json"))
        return sorted({path for path in candidates if path.exists()})


@dataclass(frozen=True)
class AttemptPaths:
    run_root: Path
    attempt_id: str

    @property
    def attempt_dir(self) -> Path:
        return self.run_root / "attempts" / self.attempt_id

    @property
    def worktree_dir(self) -> Path:
        return self.attempt_dir / "worktree"

    @property
    def lease_path(self) -> Path:
        return self.attempt_dir / "lease.json"

    @property
    def patch_path(self) -> Path:
        return self.attempt_dir / "patch.diff"

    @property
    def normalized_path(self) -> Path:
        return self.attempt_dir / "normalized.json"

    @property
    def phase_log_path(self) -> Path:
        return self.attempt_dir / "phase-log.jsonl"

    @property
    def oco_events_path(self) -> Path:
        return self.attempt_dir / "oco-events.ndjson"

    @property
    def boundary_proof_path(self) -> Path:
        return self.attempt_dir / "boundary-proof.md"

    def ensure(self) -> None:
        self.attempt_dir.mkdir(parents=True, exist_ok=True)

    def marker_path(self, phase: Phase) -> Path:
        return marker_path(self.attempt_dir, phase)

    def marker_exists(self, phase: Phase) -> bool:
        return self.marker_path(phase).exists()

    def state_marker_path(self, marker: str) -> Path:
        return self.attempt_dir / marker

    def write_phase_marker(self, phase: Phase) -> None:
        marker = self.marker_path(phase)
        atomic_write_text(
            marker, json.dumps(utc_event(f"{phase.value}_DONE"), sort_keys=True) + "\n"
        )

    def write_state_marker(self, marker: str, event: str | None = None) -> None:
        atomic_write_text(
            self.state_marker_path(marker),
            json.dumps(utc_event(event or marker), sort_keys=True) + "\n",
        )

    def append_phase_event(self, event: str, **fields: Any) -> None:
        self.ensure()
        append_jsonl_atomic(
            self.phase_log_path, utc_event(event, attempt_id=self.attempt_id, **fields)
        )

    def artifact_paths(self) -> list[Path]:
        return [self.attempt_dir / name for name in ATTEMPT_ARTIFACT_FILES]

    def backup_paths(self) -> list[Path]:
        candidates = [
            self.attempt_dir / name
            for name in ATTEMPT_ARTIFACT_FILES
            + OPTIONAL_ATTEMPT_ARTIFACT_FILES
            + ATTEMPT_STATE_FILES
        ]
        eval_bundle = self.attempt_dir / "eval-bundle"
        if eval_bundle.exists():
            candidates.extend(path for path in eval_bundle.rglob("*") if path.is_file())
        return sorted(path for path in candidates if path.exists())

    def clear_for_rerun(self) -> None:
        self.ensure()
        for name in (
            ATTEMPT_ARTIFACT_FILES
            + OPTIONAL_ATTEMPT_ARTIFACT_FILES
            + ATTEMPT_STATE_FILES
            + ("lease.json",)
        ):
            path = self.attempt_dir / name
            if path.exists():
                path.unlink()
        if self.worktree_dir.exists():
            shutil.rmtree(self.worktree_dir)

    def phase_log_rows(self) -> list[dict[str, Any]]:
        if not self.phase_log_path.exists():
            return []
        rows: list[dict[str, Any]] = []
        for line in self.phase_log_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
        return rows
