from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Sequence

from controller.artifacts import AttemptPaths, RunPaths
from controller.atomic import atomic_write_text
from controller.backup import (
    BackupHook,
    SSHBackupTarget,
    is_allowed_artifact,
    select_backup_files,
)
from controller.phases import Phase


def _write_attempt_artifacts(paths: AttemptPaths) -> None:
    paths.ensure()
    atomic_write_text(paths.patch_path, "diff")
    atomic_write_text(paths.normalized_path, "{}\n")
    atomic_write_text(paths.phase_log_path, "{}\n")
    atomic_write_text(paths.oco_events_path, "{}\n")
    for phase in (Phase.SETUP, Phase.RUN, Phase.CAPTURE, Phase.DONE):
        paths.write_phase_marker(phase)
    paths.worktree_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_text(paths.worktree_dir / "not-backed-up.txt", "hot worktree")


def test_backup_mirrors_only_selected_durable_files(tmp_path: Path) -> None:
    run_root = tmp_path / "run"
    paths = AttemptPaths(run_root, "attempt")
    _write_attempt_artifacts(paths)

    target = tmp_path / "mac"
    result = BackupHook(target).mirror(paths.backup_paths(), source_root=run_root)

    assert result.success
    assert "attempts/attempt/patch.diff" in result.copied
    assert (target / "attempts" / "attempt" / "patch.diff").exists()
    assert not (
        target / "attempts" / "attempt" / "worktree" / "not-backed-up.txt"
    ).exists()


def test_backup_unreachable_target_is_noop(tmp_path: Path) -> None:
    run_root = tmp_path / "run"
    paths = AttemptPaths(run_root, "attempt")
    _write_attempt_artifacts(paths)

    result = BackupHook(
        tmp_path / "missing" / "target", create_destination=False
    ).mirror(paths.backup_paths(), source_root=run_root)

    assert result.success
    assert result.retryable
    assert result.copied == []


def test_ssh_rsync_unreachable_mac_modes_are_noop_and_retryable(tmp_path: Path) -> None:
    run_root = tmp_path / "run"
    paths = AttemptPaths(run_root, "attempt")
    _write_attempt_artifacts(paths)
    messages = [
        "ssh: Could not resolve hostname mac",
        "ssh: connect to host mac port 22: Connection refused",
        "Permission denied (publickey)",
        "operation timed out",
    ]
    calls: list[list[str]] = []

    def runner(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        command_list = list(command)
        calls.append(command_list)
        message = messages[len(calls) - 1]
        return subprocess.CompletedProcess(command_list, 255, "", message)

    results = [
        BackupHook(
            SSHBackupTarget(host="mac.invalid", user="aiden", target_dir="/backup"),
            runner=runner,
        ).mirror(paths.backup_paths(), source_root=run_root)
        for _ in messages
    ]

    assert all(
        result.success and result.retryable and result.copied == []
        for result in results
    )
    assert calls and all(call[0] == "rsync" for call in calls)


def test_backup_allowlist_excludes_repo_caches_and_worktrees(tmp_path: Path) -> None:
    run_root = tmp_path / "run"
    allowed = run_root / "attempts" / "attempt" / "normalized.json"
    attempt_bundle = run_root / "attempts" / "attempt" / "eval-bundle" / "manifest.json"
    worktree = run_root / "attempts" / "attempt" / "worktree" / "secret.txt"
    repo_cache = run_root / "repo-cache" / "repo" / "HEAD"
    for path in (allowed, attempt_bundle, worktree, repo_cache):
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, "x")

    selected = select_backup_files(
        [allowed, attempt_bundle, worktree, repo_cache], source_root=run_root
    )

    assert set(selected) == {allowed, attempt_bundle}
    assert is_allowed_artifact("attempts/attempt/normalized.json")
    assert is_allowed_artifact("attempts/attempt/eval-bundle/manifest.json")
    assert not is_allowed_artifact("attempts/attempt/worktree/secret.txt")
    assert not is_allowed_artifact("repo-cache/repo/HEAD")


def test_run_level_backup_includes_summaries_manifests_config_and_eval_bundle(
    tmp_path: Path,
) -> None:
    run_root = tmp_path / "run"
    run_paths = RunPaths(run_root)
    run_paths.ensure()
    atomic_write_text(run_paths.summary_path, "{}\n")
    atomic_write_text(run_paths.run_log_path, "{}\n")
    atomic_write_text(run_root / "task-list.jsonl", "{}\n")
    atomic_write_text(run_root / "task-list.manifest.json", "{}\n")
    atomic_write_text(run_paths.config_snapshot_dir / "opencode.jsonc", "{}\n")
    atomic_write_text(run_root / "eval-bundle" / "manifest.json", "{}\n")
    atomic_write_text(run_root / "repo-cache" / "repo" / "HEAD", "not durable")
    atomic_write_text(run_root / "attempts" / "a" / "worktree" / "file.txt", "hot")

    target = tmp_path / "mac"
    result = BackupHook(target).mirror(run_paths.backup_paths(), source_root=run_root)

    assert result.success
    assert (target / "summary.json").exists()
    assert (target / "run-events.jsonl").exists()
    assert (target / "task-list.manifest.json").exists()
    assert (target / "oco-config-snapshot" / "opencode.jsonc").exists()
    assert (target / "eval-bundle" / "manifest.json").exists()
    assert not (target / "repo-cache" / "repo" / "HEAD").exists()
    assert not (target / "attempts" / "a" / "worktree" / "file.txt").exists()
