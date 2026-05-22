#!/usr/bin/env python3
"""Dry-run gate for the OCO SWE-bench Pro controller skeleton."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from controller.artifacts import AttemptPaths, RunPaths  # noqa: E402
from controller.atomic import atomic_write_json, atomic_write_text  # noqa: E402
from controller.core import AttemptSpec, BenchmarkController, ControllerConfig  # noqa: E402
from controller.fixtures import FixtureOCOAdapter, fixture_events  # noqa: E402
from controller.phases import PHASE_SEQUENCE, Phase  # noqa: E402
from controller.telemetry import (
    ATTEMPT_AGGREGATION_FIELDS,
    STEP_FIELDS,
    normalize_events,
)  # noqa: E402
from controller.watermarks import ResourceWatermarks  # noqa: E402


GateFunc = Callable[[Path, str], dict[str, Any]]


def _timestamp_run_id() -> str:
    return "dry-run-gate-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _controller(
    run_root: Path,
    run_id: str,
    *,
    force_rerun: set[str] | None = None,
    backup_destination: Path | None = None,
    backup_create_destination: bool = True,
    adapter: FixtureOCOAdapter | None = None,
    watermarks: ResourceWatermarks | None = None,
) -> BenchmarkController:
    config = ControllerConfig(
        run_root=run_root,
        run_id=run_id,
        lease_stale_after_seconds=0.0,
        force_rerun=force_rerun or set(),
        backup_destination=backup_destination,
        backup_create_destination=backup_create_destination,
    )
    return BenchmarkController(config, adapter=adapter, watermarks=watermarks)


def _run_controller(
    run_root: Path,
    run_id: str,
    attempt_ids: list[str],
    **kwargs: Any,
) -> dict[str, Any]:
    controller = _controller(run_root, run_id, **kwargs)
    return controller.run_attempts(
        [AttemptSpec(attempt_id) for attempt_id in attempt_ids]
    )


def _all_phase_markers(paths: AttemptPaths) -> bool:
    return all(paths.marker_exists(phase) for phase in PHASE_SEQUENCE)


def _all_artifacts(paths: AttemptPaths) -> bool:
    return all(path.exists() for path in paths.artifact_paths())


def _non_backup_events(rows: list[dict[str, Any]]) -> list[str]:
    return [
        str(row.get("event"))
        for row in rows
        if row.get("event") not in {"BACKUP_DONE", "BACKUP_NOOP"}
    ]


def gate_lifecycle(run_root: Path, run_id: str) -> dict[str, Any]:
    attempt_id = "lifecycle"
    _run_controller(run_root, run_id, [attempt_id])
    paths = AttemptPaths(run_root, attempt_id)
    markers_ok = _all_phase_markers(paths)
    artifacts_ok = _all_artifacts(paths)
    return {
        "pass": markers_ok and artifacts_ok,
        "reason": "fixture attempt completed SETUP -> RUN -> CAPTURE -> DONE",
        "evidence": {
            "attempt_id": attempt_id,
            "markers": [
                phase.value for phase in PHASE_SEQUENCE if paths.marker_exists(phase)
            ],
            "artifacts": [
                path.name for path in paths.artifact_paths() if path.exists()
            ],
        },
    }


def gate_atomic_writes(run_root: Path, run_id: str) -> dict[str, Any]:
    del run_id
    check_dir = run_root / "gate-checks" / "atomic"
    empty_target = check_dir / "empty.txt"
    prior_target = check_dir / "prior.txt"
    atomic_write_text(prior_target, "prior\n")

    def fail(_: Path) -> None:
        raise RuntimeError("simulated interrupted write")

    empty_failed = False
    prior_failed = False
    try:
        atomic_write_text(empty_target, "partial\n", failure_hook=fail)
    except RuntimeError:
        empty_failed = True
    try:
        atomic_write_text(prior_target, "partial\n", failure_hook=fail)
    except RuntimeError:
        prior_failed = True
    temp_leftovers = list(check_dir.glob(".*.tmp")) if check_dir.exists() else []
    passed = (
        empty_failed
        and prior_failed
        and not empty_target.exists()
        and prior_target.read_text() == "prior\n"
        and not temp_leftovers
    )
    return {
        "pass": passed,
        "reason": "simulated interruption left either the prior version or no file",
        "evidence": {
            "empty_target_exists": empty_target.exists(),
            "prior_target_text": prior_target.read_text(encoding="utf-8"),
            "temp_leftovers": [path.name for path in temp_leftovers],
        },
    }


def gate_telemetry_shape(run_root: Path, run_id: str) -> dict[str, Any]:
    paths = AttemptPaths(run_root, "lifecycle")
    if not paths.normalized_path.exists():
        _run_controller(run_root, run_id, ["lifecycle"])
    normalized = _load_json(paths.normalized_path)
    summary = _load_json(RunPaths(run_root).summary_path)
    steps = normalized.get("steps", [])
    step_field_set = set(STEP_FIELDS)
    step_fields_ok = bool(steps) and all(
        set(step.keys()) == step_field_set and len(step) == len(STEP_FIELDS)
        for step in steps
    )
    aggregations_ok = all(field in normalized for field in ATTEMPT_AGGREGATION_FIELDS)
    summary_ok = all(
        field in summary
        for field in (
            "step_count",
            "tool_call_count",
            "tokens_in_total",
            "tokens_out_total",
            "cached_tokens_total",
            "reasoning_tokens_total",
            "prefix_cache_hit_rate",
            "attempt_distribution_stats",
        )
    )
    return {
        "pass": step_fields_ok and aggregations_ok and summary_ok,
        "reason": "normalized.json and summary.json expose the plan 5.5 token-accounting shape",
        "evidence": {
            "step_count": normalized.get("step_count"),
            "step_fields": list(steps[0].keys()) if steps else [],
            "summary_attempt_count": summary.get("attempt_count"),
        },
    }


def _controller_command(
    run_root: Path, run_id: str, attempts: list[str], *extra: str
) -> list[str]:
    return [
        sys.executable,
        "-m",
        "controller.cli",
        "--run-root",
        str(run_root),
        "--run-id",
        run_id,
        "--lease-stale-after",
        "0",
        "--attempts",
        *attempts,
        *extra,
    ]


def _pathless_env(run_root: Path) -> dict[str, str]:
    env = os.environ.copy()
    empty_path = run_root / "gate-checks" / "empty-path"
    empty_path.mkdir(parents=True, exist_ok=True)
    env["PATH"] = str(empty_path)
    env["PYTHONPATH"] = str(PROJECT_ROOT)
    return env


def _wait_for_event(path: Path, event: str, timeout_seconds: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if any(row.get("event") == event for row in _load_jsonl(path)):
            return True
        time.sleep(0.05)
    return False


def gate_resume_drill(run_root: Path, run_id: str) -> dict[str, Any]:
    done_before = AttemptPaths(run_root, "resume-done-before")
    crash_paths = AttemptPaths(run_root, "resume-crash")
    if not crash_paths.marker_exists(Phase.DONE):
        command = _controller_command(
            run_root,
            run_id,
            ["resume-done-before", "resume-crash"],
            "--slow-attempt",
            "resume-crash",
            "--slow-seconds",
            "30",
        )
        process = subprocess.Popen(
            command,
            cwd=PROJECT_ROOT,
            env=_pathless_env(run_root),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        saw_run = _wait_for_event(crash_paths.phase_log_path, "RUN_STARTED")
        if not saw_run:
            stdout, stderr = process.communicate(timeout=5)
            return {
                "pass": False,
                "reason": "controller subprocess did not enter RUN before timeout",
                "evidence": {
                    "returncode": process.returncode,
                    "stdout": stdout[-500:],
                    "stderr": stderr[-500:],
                },
            }
        before_rows = done_before.phase_log_rows()
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        restart = subprocess.run(
            _controller_command(
                run_root, run_id, ["resume-done-before", "resume-crash"]
            ),
            cwd=PROJECT_ROOT,
            env=_pathless_env(run_root),
            capture_output=True,
            text=True,
            timeout=30,
        )
        if restart.returncode != 0:
            return {
                "pass": False,
                "reason": "controller restart failed after simulated mid-RUN death",
                "evidence": {
                    "returncode": restart.returncode,
                    "stdout": restart.stdout[-500:],
                    "stderr": restart.stderr[-500:],
                },
            }
    else:
        before_rows = done_before.phase_log_rows()
        restart = subprocess.run(
            _controller_command(
                run_root, run_id, ["resume-done-before", "resume-crash"]
            ),
            cwd=PROJECT_ROOT,
            env=_pathless_env(run_root),
            capture_output=True,
            text=True,
            timeout=30,
        )

    after_rows = done_before.phase_log_rows()
    lease_files = [path for path in (run_root / "attempts").glob("*/lease.json")]
    passed = (
        restart.returncode == 0
        and _all_phase_markers(crash_paths)
        and _all_artifacts(crash_paths)
        and _non_backup_events(before_rows) == _non_backup_events(after_rows)
        and not lease_files
    )
    return {
        "pass": passed,
        "reason": "same run id resumed the partial attempt and skipped the prior DONE attempt",
        "evidence": {
            "restart_returncode": restart.returncode,
            "done_before_non_backup_events_before": len(
                _non_backup_events(before_rows)
            ),
            "done_before_non_backup_events_after": len(_non_backup_events(after_rows)),
            "crash_markers_complete": _all_phase_markers(crash_paths),
            "lease_files": [str(path.relative_to(run_root)) for path in lease_files],
        },
    }


def gate_force_rerun(run_root: Path, run_id: str) -> dict[str, Any]:
    _run_controller(run_root, run_id, ["force-keep", "force-target"])
    keep = AttemptPaths(run_root, "force-keep")
    target = AttemptPaths(run_root, "force-target")
    keep_rows_before = keep.phase_log_rows()
    _run_controller(
        run_root, run_id, ["force-keep", "force-target"], force_rerun={"force-target"}
    )
    keep_rows_after = keep.phase_log_rows()
    target_rows = target.phase_log_rows()
    target_events = [row.get("event") for row in target_rows]
    passed = (
        _non_backup_events(keep_rows_before) == _non_backup_events(keep_rows_after)
        and "FORCE_RERUN" in target_events
        and _all_phase_markers(target)
    )
    return {
        "pass": passed,
        "reason": "force rerun cleared and rebuilt only the requested attempt",
        "evidence": {
            "keep_non_backup_events_before": len(_non_backup_events(keep_rows_before)),
            "keep_non_backup_events_after": len(_non_backup_events(keep_rows_after)),
            "target_events": target_events,
        },
    }


def gate_mac_backup(run_root: Path, run_id: str) -> dict[str, Any]:
    snapshot = run_root.parent / f"{run_id}-ssh-stub-snapshot"
    stub_bin = run_root.parent / f"{run_id}-ssh-stub-bin"
    shutil.rmtree(snapshot, ignore_errors=True)
    shutil.rmtree(stub_bin, ignore_errors=True)
    stub_bin.mkdir(parents=True, exist_ok=True)
    _write_rsync_stub(stub_bin / "rsync")
    env = os.environ.copy()
    env["PATH"] = f"{stub_bin}{os.pathsep}" + env.get("PATH", "")
    env["PYTHONPATH"] = str(PROJECT_ROOT)
    env["OCO_BENCHMARK_RSYNC_STUB_TARGET"] = str(snapshot)

    done_attempt = "backup-restore-done"
    crash_attempt = "backup-restore-crash"
    initial = subprocess.run(
        _controller_command(
            run_root,
            run_id,
            [done_attempt],
            "--backup-ssh-host",
            "mac.stub",
            "--backup-ssh-user",
            "aiden",
            "--backup-ssh-target-dir",
            "/snapshot",
        ),
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if initial.returncode != 0:
        return {
            "pass": False,
            "reason": "SSH-stub backup initial run failed",
            "evidence": {"stderr": initial.stderr[-500:]},
        }

    process = subprocess.Popen(
        _controller_command(
            run_root,
            run_id,
            [done_attempt, crash_attempt],
            "--slow-attempt",
            crash_attempt,
            "--slow-seconds",
            "30",
            "--backup-ssh-host",
            "mac.stub",
            "--backup-ssh-user",
            "aiden",
            "--backup-ssh-target-dir",
            "/snapshot",
        ),
        cwd=PROJECT_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    saw_run = _wait_for_event(
        AttemptPaths(run_root, crash_attempt).phase_log_path, "RUN_STARTED"
    )
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)

    shutil.rmtree(run_root, ignore_errors=True)
    shutil.copytree(snapshot, run_root)
    rows_after_restore = AttemptPaths(run_root, done_attempt).phase_log_rows()
    restart = subprocess.run(
        _controller_command(
            run_root,
            run_id,
            [done_attempt, crash_attempt],
            "--backup-ssh-host",
            "mac.stub",
            "--backup-ssh-user",
            "aiden",
            "--backup-ssh-target-dir",
            "/snapshot",
        ),
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    offline_attempt = "backup-offline"
    offline = subprocess.run(
        _controller_command(
            run_root,
            run_id,
            [offline_attempt],
            "--force-rerun",
            offline_attempt,
            "--backup-ssh-host",
            "offline.invalid",
            "--backup-ssh-user",
            "aiden",
            "--backup-ssh-target-dir",
            "/snapshot",
        ),
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    offline_rows = AttemptPaths(run_root, offline_attempt).phase_log_rows()
    passed = (
        saw_run
        and restart.returncode == 0
        and offline.returncode == 0
        and AttemptPaths(run_root, done_attempt).marker_exists(Phase.DONE)
        and AttemptPaths(run_root, crash_attempt).marker_exists(Phase.DONE)
        and _non_backup_events(rows_after_restore)
        == _non_backup_events(AttemptPaths(run_root, done_attempt).phase_log_rows())
        and (snapshot / "attempts" / done_attempt / "patch.diff").exists()
        and any(row.get("event") == "BACKUP_NOOP" for row in offline_rows)
    )
    return {
        "pass": passed,
        "reason": "SSH-stub rsync snapshot survives a killed controller, restores locally, resumes, and unreachable Mac logs a no-op",
        "evidence": {
            "saw_crash_run_started": saw_run,
            "restart_returncode": restart.returncode,
            "offline_returncode": offline.returncode,
            "snapshot_patch_exists": (
                snapshot / "attempts" / done_attempt / "patch.diff"
            ).exists(),
            "done_attempt_non_backup_events_stable_after_restore": _non_backup_events(
                rows_after_restore
            )
            == _non_backup_events(
                AttemptPaths(run_root, done_attempt).phase_log_rows()
            ),
            "offline_done": AttemptPaths(run_root, offline_attempt).marker_exists(
                Phase.DONE
            ),
        },
    }


def _write_rsync_stub(path: Path) -> None:
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import os, pathlib, shutil, sys\n"
        "args=sys.argv[1:]\n"
        "dest=args[-1] if args else ''\n"
        "if 'offline.invalid' in dest:\n"
        "    sys.stderr.write('ssh: connect to host offline.invalid port 22: Connection refused\\n')\n"
        "    raise SystemExit(255)\n"
        "files_from=pathlib.Path(args[args.index('--files-from')+1])\n"
        "source=pathlib.Path(args[-2])\n"
        "target=pathlib.Path(os.environ['OCO_BENCHMARK_RSYNC_STUB_TARGET'])\n"
        "for line in files_from.read_text().splitlines():\n"
        "    if not line.strip():\n"
        "        continue\n"
        "    src=source / line\n"
        "    dst=target / line\n"
        "    dst.parent.mkdir(parents=True, exist_ok=True)\n"
        "    shutil.copy2(src, dst)\n"
        "raise SystemExit(0)\n",
        encoding="utf-8",
    )
    path.chmod(0o755)


def gate_storage_watermark(run_root: Path, run_id: str) -> dict[str, Any]:
    backup_target = run_root / "storage-backup"
    _run_controller(
        run_root, run_id, ["storage-complete"], backup_destination=backup_target
    )
    partial = AttemptPaths(run_root, "storage-partial")
    partial.ensure()
    partial.worktree_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_text(
        partial.worktree_dir / "active.txt", "active worktree must survive cleanup\n"
    )
    partial.write_phase_marker(Phase.SETUP)
    watermarks = ResourceWatermarks(disk_path=run_root, disk_usage_source=lambda: 0.90)
    controller = _controller(run_root, run_id, watermarks=watermarks)
    cleaned = controller.cleanup_completed_worktrees()
    complete = AttemptPaths(run_root, "storage-complete")
    cleanup_recorded = complete.state_marker_path("WORKTREE_CLEANED").exists()
    passed = (
        (
            "storage-complete" in cleaned
            or (cleanup_recorded and not complete.worktree_dir.exists())
        )
        and not complete.worktree_dir.exists()
        and partial.worktree_dir.exists()
    )
    return {
        "pass": passed,
        "reason": "90% synthetic disk usage cleans completed-and-backed-up worktrees only",
        "evidence": {
            "cleaned": cleaned,
            "cleanup_recorded": cleanup_recorded,
            "complete_worktree_exists": complete.worktree_dir.exists(),
            "partial_worktree_exists": partial.worktree_dir.exists(),
        },
    }


def gate_ram_watermark(run_root: Path, run_id: str) -> dict[str, Any]:
    readings = iter([0.92, 0.40])

    def memory_source() -> float:
        return next(readings, 0.40)

    attempt_id = "ram-pause"
    watermarks = ResourceWatermarks(
        disk_path=run_root, memory_usage_source=memory_source
    )
    _run_controller(
        run_root, run_id, [attempt_id], force_rerun={attempt_id}, watermarks=watermarks
    )
    run_events = _load_jsonl(RunPaths(run_root).run_log_path)
    passed = any(
        row.get("event") == "RAM_PAUSE" and row.get("attempt_id") == attempt_id
        for row in run_events
    )
    return {
        "pass": passed and AttemptPaths(run_root, attempt_id).marker_exists(Phase.DONE),
        "reason": "92% synthetic RAM pauses spawning; lower pressure lets the attempt start",
        "evidence": {
            "ram_pause_events": [
                row
                for row in run_events
                if row.get("event") == "RAM_PAUSE"
                and row.get("attempt_id") == attempt_id
            ],
            "attempt_done": AttemptPaths(run_root, attempt_id).marker_exists(
                Phase.DONE
            ),
        },
    }


def gate_glob_grep_guard(run_root: Path, run_id: str) -> dict[str, Any]:
    fixture_dir = PROJECT_ROOT / "tests" / "fixtures" / "glob_guard"
    fixture_count = sum(1 for path in fixture_dir.iterdir() if path.is_file())
    attempt_id = "guard-probe"
    adapter = FixtureOCOAdapter(guard_attempt_ids={attempt_id})
    _run_controller(
        run_root, run_id, [attempt_id], force_rerun={attempt_id}, adapter=adapter
    )
    normalized = _load_json(AttemptPaths(run_root, attempt_id).normalized_path)
    tools = {message.get("tool") for message in normalized.get("guard_messages", [])}
    direct_probe = normalize_events(
        fixture_events("direct-guard", include_guard=True),
        attempt_id="direct-guard",
        run_id=run_id,
    )
    passed = (
        fixture_count < 1000
        and {"glob", "grep"}.issubset(tools)
        and len(direct_probe["guard_messages"]) == 2
    )
    return {
        "pass": passed,
        "reason": "bounded fixture records simulated OCO glob/grep timeout-guard messages",
        "evidence": {
            "fixture_file_count": fixture_count,
            "recorded_tools": sorted(tools),
            "direct_probe_guard_count": len(direct_probe["guard_messages"]),
        },
    }


def gate_no_oco_source_modification(run_root: Path, run_id: str) -> dict[str, Any]:
    del run_id
    project_root = PROJECT_ROOT.resolve()
    output_root = run_root.resolve()
    passed = output_root.is_relative_to(project_root)
    return {
        "pass": passed,
        "reason": "dry-run output root is contained inside oco-benchmark; this task writes no OCO source paths",
        "evidence": {
            "project_root": str(project_root),
            "output_root": str(output_root),
        },
    }


def gate_no_real_oco_invocation(run_root: Path, run_id: str) -> dict[str, Any]:
    attempt_id = "no-oco-path"
    command = _controller_command(
        run_root, run_id, [attempt_id], "--force-rerun", attempt_id
    )
    completed = subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        env=_pathless_env(run_root),
        capture_output=True,
        text=True,
        timeout=30,
    )
    passed = completed.returncode == 0 and AttemptPaths(
        run_root, attempt_id
    ).marker_exists(Phase.DONE)
    return {
        "pass": passed,
        "reason": "controller completes with PATH set to an empty directory, proving the fixture path needs no oco binary",
        "evidence": {
            "returncode": completed.returncode,
            "attempt_done": AttemptPaths(run_root, attempt_id).marker_exists(
                Phase.DONE
            ),
            "stderr_tail": completed.stderr[-500:],
        },
    }


def gate_single_command_exit(
    run_root: Path, run_id: str, entries: list[dict[str, Any]]
) -> dict[str, Any]:
    report_path = run_root / "gate-report.json"
    prior_pass = all(entry["pass"] for entry in entries)
    return {
        "pass": prior_pass,
        "reason": "python scripts/dry_run_gate.py can emit one JSON report with every sub-gate passing",
        "evidence": {
            "run_id": run_id,
            "report_path": str(report_path),
            "prior_sub_gates_pass": prior_pass,
        },
    }


GATES: list[tuple[int, str, GateFunc]] = [
    (1, "Lifecycle", gate_lifecycle),
    (2, "Atomic writes", gate_atomic_writes),
    (3, "Telemetry shape", gate_telemetry_shape),
    (4, "Resume drill", gate_resume_drill),
    (5, "Force rerun", gate_force_rerun),
    (6, "Mac backup drill", gate_mac_backup),
    (7, "Storage watermark", gate_storage_watermark),
    (8, "RAM watermark", gate_ram_watermark),
    (9, "Glob/grep safety guard verification", gate_glob_grep_guard),
    (11, "No OCO source modification", gate_no_oco_source_modification),
    (12, "No real OCO invocation", gate_no_real_oco_invocation),
]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the local dry-run gate for the benchmark controller."
    )
    parser.add_argument(
        "--run-id",
        default=None,
        help="Reuse a dry-run gate run id to exercise same-run resume.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    run_id = args.run_id or _timestamp_run_id()
    run_root = PROJECT_ROOT / "runs" / run_id
    run_root.mkdir(parents=True, exist_ok=True)

    entries: list[dict[str, Any]] = []
    for gate_id, name, func in GATES:
        try:
            result = func(run_root, run_id)
        except Exception as exc:  # noqa: BLE001 - gate report must capture all failures.
            result = {
                "pass": False,
                "reason": f"{type(exc).__name__}: {exc}",
                "evidence": {},
            }
        entry = {"id": gate_id, "name": name, **result}
        entries.append(entry)
        status = "PASS" if entry["pass"] else "FAIL"
        print(f"[{status}] {gate_id}. {name}: {entry['reason']}")

    single_command = gate_single_command_exit(run_root, run_id, entries)
    single_entry = {"id": 10, "name": "Single-command exit", **single_command}
    entries.insert(9, single_entry)
    print(
        f"[{'PASS' if single_entry['pass'] else 'FAIL'}] 10. Single-command exit: {single_entry['reason']}"
    )

    report = {
        "run_id": run_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "pass": all(entry["pass"] for entry in entries),
        "sub_gates": sorted(entries, key=lambda entry: entry["id"]),
    }
    atomic_write_json(run_root / "gate-report.json", report)
    print(f"Report: {run_root / 'gate-report.json'}")
    return 0 if report["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
