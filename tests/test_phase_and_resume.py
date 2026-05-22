from __future__ import annotations

from pathlib import Path

from controller.artifacts import AttemptPaths
from controller.core import AttemptSpec, BenchmarkController, ControllerConfig
from controller.leases import LeaseManager
from controller.phases import PHASE_SEQUENCE, Phase, next_phase


def test_next_phase_follows_markers(tmp_path: Path) -> None:
    paths = AttemptPaths(tmp_path, "phase-order")
    paths.ensure()

    assert next_phase(paths.attempt_dir) == Phase.SETUP
    paths.write_phase_marker(Phase.SETUP)
    assert next_phase(paths.attempt_dir) == Phase.RUN
    paths.write_phase_marker(Phase.RUN)
    assert next_phase(paths.attempt_dir) == Phase.CAPTURE
    paths.write_phase_marker(Phase.CAPTURE)
    assert next_phase(paths.attempt_dir) == Phase.DONE
    paths.write_phase_marker(Phase.DONE)
    assert next_phase(paths.attempt_dir) is None


def test_stale_lease_resume_skips_completed_setup(tmp_path: Path) -> None:
    run_root = tmp_path / "run"
    paths = AttemptPaths(run_root, "resume")
    paths.ensure()
    paths.write_phase_marker(Phase.SETUP)
    LeaseManager(paths.lease_path, stale_after_seconds=0).acquire()

    controller = BenchmarkController(
        ControllerConfig(
            run_root=run_root,
            run_id="unit-resume",
            lease_stale_after_seconds=0,
        )
    )
    controller.run_attempts([AttemptSpec("resume")])

    assert all(paths.marker_exists(phase) for phase in PHASE_SEQUENCE)
    assert not paths.lease_path.exists()
    events = [row["event"] for row in paths.phase_log_rows()]
    assert "LEASE_RECOVERED" in events
    assert "SETUP_STARTED" not in events
    assert "RUN_STARTED" in events
