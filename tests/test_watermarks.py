from __future__ import annotations

from pathlib import Path

from controller.artifacts import AttemptPaths
from controller.atomic import atomic_write_text
from controller.core import AttemptSpec, BenchmarkController, ControllerConfig
from controller.phases import Phase
from controller.watermarks import ResourceWatermarks


def test_storage_cleanup_only_deletes_done_and_rsynced_worktrees(
    tmp_path: Path,
) -> None:
    run_root = tmp_path / "run"
    complete = AttemptPaths(run_root, "complete")
    partial = AttemptPaths(run_root, "partial")
    for paths in (complete, partial):
        paths.ensure()
        paths.worktree_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_text(paths.worktree_dir / "file.txt", "data")
    for phase in (Phase.SETUP, Phase.RUN, Phase.CAPTURE, Phase.DONE):
        complete.write_phase_marker(phase)
    complete.write_state_marker("RSYNC_DONE")
    atomic_write_text(complete.patch_path, "diff")
    atomic_write_text(complete.normalized_path, "{}\n")
    atomic_write_text(complete.phase_log_path, "{}\n")
    partial.write_phase_marker(Phase.SETUP)

    controller = BenchmarkController(
        ControllerConfig(run_root=run_root, run_id="watermark"),
        watermarks=ResourceWatermarks(
            disk_path=run_root, disk_usage_source=lambda: 0.90
        ),
    )
    cleaned = controller.cleanup_completed_worktrees()

    assert cleaned == ["complete"]
    assert not complete.worktree_dir.exists()
    assert partial.worktree_dir.exists()


def test_worktree_cleanup_pauses_when_mac_unreachable_under_disk_pressure(
    tmp_path: Path,
) -> None:
    run_root = tmp_path / "run"
    paths = AttemptPaths(run_root, "done-but-not-backed-up")
    paths.ensure()
    paths.worktree_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_text(paths.worktree_dir / "file.txt", "data")
    for phase in (Phase.SETUP, Phase.RUN, Phase.CAPTURE, Phase.DONE):
        paths.write_phase_marker(phase)
    atomic_write_text(paths.patch_path, "diff")
    atomic_write_text(paths.normalized_path, "{}\n")
    atomic_write_text(paths.phase_log_path, "{}\n")

    controller = BenchmarkController(
        ControllerConfig(run_root=run_root, run_id="mac-unreachable"),
        watermarks=ResourceWatermarks(
            disk_path=run_root, disk_usage_source=lambda: 0.95
        ),
    )
    cleaned = controller.cleanup_completed_worktrees()

    assert cleaned == []
    assert paths.worktree_dir.exists()
    assert not paths.state_marker_path("WORKTREE_CLEANED").exists()
    assert any(
        row.get("event") == "WORKTREE_CLEANUP_PAUSED" for row in paths.phase_log_rows()
    )


def test_worktree_cleanup_requires_complete_durable_artifact_set(
    tmp_path: Path,
) -> None:
    run_root = tmp_path / "run"
    paths = AttemptPaths(run_root, "missing-durable-artifact")
    paths.ensure()
    paths.worktree_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_text(paths.worktree_dir / "file.txt", "data")
    for phase in (Phase.SETUP, Phase.RUN, Phase.CAPTURE, Phase.DONE):
        paths.write_phase_marker(phase)
    paths.write_state_marker("RSYNC_DONE")
    atomic_write_text(paths.patch_path, "diff")
    atomic_write_text(paths.phase_log_path, "{}\n")

    controller = BenchmarkController(
        ControllerConfig(run_root=run_root, run_id="missing-durable"),
        watermarks=ResourceWatermarks(
            disk_path=run_root, disk_usage_source=lambda: 0.95
        ),
    )
    cleaned = controller.cleanup_completed_worktrees()

    assert cleaned == []
    assert paths.worktree_dir.exists()
    assert any(
        row.get("event") == "WORKTREE_CLEANUP_PAUSED" for row in paths.phase_log_rows()
    )


def test_ram_pressure_pauses_before_starting_attempt(tmp_path: Path) -> None:
    readings = iter([0.92, 0.40])

    def memory_source() -> float:
        return next(readings, 0.40)

    run_root = tmp_path / "run"
    controller = BenchmarkController(
        ControllerConfig(run_root=run_root, run_id="ram"),
        watermarks=ResourceWatermarks(
            disk_path=run_root, memory_usage_source=memory_source
        ),
    )
    controller.run_attempts([AttemptSpec("attempt")])

    run_log = (run_root / "run-events.jsonl").read_text(encoding="utf-8")
    assert "RAM_PAUSE" in run_log
    assert AttemptPaths(run_root, "attempt").marker_exists(Phase.DONE)
