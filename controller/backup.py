"""Durable-artifact backup over SSH rsync with a local fallback mode."""

from __future__ import annotations

import fnmatch
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Sequence

from controller.atomic import atomic_write_bytes, atomic_write_text


Runner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]

ALLOWLIST_PATTERNS: tuple[str, ...] = (
    "attempts/*/patch.diff",
    "attempts/*/normalized.json",
    "attempts/*/phase-log.jsonl",
    "attempts/*/oco-events.ndjson",
    "attempts/*/boundary-proof.md",
    "attempts/*/filesystem-trace.log",
    "attempts/*/oco-version-gate.json",
    "attempts/*/oco-subprocess.json",
    "attempts/*/oco-stdout.log",
    "attempts/*/oco-stderr.log",
    "attempts/*/modal-result.json",
    "attempts/*/modal-eval-log.jsonl",
    "attempts/*/eval-bundle/**",
    "attempts/*/SETUP_DONE",
    "attempts/*/RUN_DONE",
    "attempts/*/CAPTURE_DONE",
    "attempts/*/DONE",
    "attempts/*/RSYNC_DONE",
    "attempts/*/WORKTREE_CLEANED",
    "oco-config-snapshot/**",
    "eval-bundle/**",
    "summary.json",
    "run-events.jsonl",
    "task-list.jsonl",
    "task-list.manifest.json",
    "*.manifest.json",
)

DENYLIST_PATTERNS: tuple[str, ...] = (
    "repo-cache/**",
    "worktrees/**",
    "attempts/*/worktree/**",
    "attempts/*/precheck-worktree/**",
    "tasks/repo-cache/**",
)

UNREACHABLE_MARKERS = (
    "could not resolve hostname",
    "connection refused",
    "permission denied",
    "operation timed out",
    "connection timed out",
    "no route to host",
    "host key verification failed",
    "connection closed",
    "auth failed",
)


@dataclass(frozen=True)
class BackupResult:
    attempted: bool
    success: bool
    copied: list[str]
    reason: str
    retryable: bool = False
    command: list[str] | None = None


@dataclass(frozen=True)
class SSHBackupTarget:
    host: str
    user: str
    target_dir: str
    key_path: Path | None = None
    port: int | None = None
    bandwidth_limit_kbps: int | None = None
    timeout_seconds: int = 30

    @property
    def destination(self) -> str:
        return f"{self.user}@{self.host}:{self.target_dir.rstrip('/')}/"


class BackupHook:
    """Push selected durable files to a Mac backup target.

    Passing a plain path keeps the fixture/local fallback used by the dry-run gate.
    Passing an SSHBackupTarget uses real rsync over SSH. Both modes enforce the same
    durable-artifact allowlist so repo caches and worktrees are never backed up.
    """

    def __init__(
        self,
        destination: Path | str | SSHBackupTarget | None,
        *,
        create_destination: bool = True,
        runner: Runner | None = None,
    ) -> None:
        self.ssh_target = (
            destination if isinstance(destination, SSHBackupTarget) else None
        )
        self.destination = (
            Path(destination)
            if destination is not None and not isinstance(destination, SSHBackupTarget)
            else None
        )
        self.create_destination = create_destination
        self.runner = runner or _run_command

    def mirror(self, files: Iterable[Path], *, source_root: Path) -> BackupResult:
        selected = select_backup_files(files, source_root=source_root)
        if self.ssh_target is not None:
            return self._mirror_ssh(selected, source_root=source_root)
        return self._mirror_local(selected, source_root=source_root)

    def _mirror_local(self, selected: list[Path], *, source_root: Path) -> BackupResult:
        if self.destination is None:
            return BackupResult(
                False, True, [], "backup target not configured", retryable=True
            )
        if self.destination.exists() and not self.destination.is_dir():
            return BackupResult(
                True,
                True,
                [],
                "backup target is not a directory; no-op",
                retryable=True,
            )
        if not self.destination.exists():
            if not self.create_destination:
                return BackupResult(
                    True, True, [], "backup target unreachable; no-op", retryable=True
                )
            self.destination.mkdir(parents=True, exist_ok=True)

        copied: list[str] = []
        for source in selected:
            relative = source.relative_to(source_root)
            target = self.destination / relative
            atomic_write_bytes(target, source.read_bytes())
            copied.append(relative.as_posix())
        return BackupResult(True, True, copied, "mirrored durable artifacts")

    def _mirror_ssh(self, selected: list[Path], *, source_root: Path) -> BackupResult:
        if not selected:
            return BackupResult(True, True, [], "no durable artifacts selected")
        target = self.ssh_target
        assert target is not None
        file_list = source_root / f".rsync-files.{os.getpid()}.txt"
        rels = [path.relative_to(source_root).as_posix() for path in selected]
        atomic_write_text(file_list, "".join(f"{relative}\n" for relative in rels))
        command = build_rsync_command(
            target, source_root=source_root, files_from=file_list
        )
        try:
            completed = self.runner(command)
        finally:
            try:
                file_list.unlink()
            except OSError:
                pass
        if completed.returncode == 0:
            return BackupResult(
                True, True, rels, "rsync over SSH completed", command=command
            )
        reason = classify_rsync_failure(completed)
        if reason == "mac_unreachable":
            return BackupResult(
                True,
                True,
                [],
                "Mac backup target unreachable; rsync no-op",
                retryable=True,
                command=command,
            )
        return BackupResult(
            True,
            False,
            [],
            f"rsync failed with exit code {completed.returncode}",
            retryable=True,
            command=command,
        )


def select_backup_files(files: Iterable[Path], *, source_root: Path) -> list[Path]:
    selected: list[Path] = []
    resolved_root = source_root.resolve()
    for source in sorted(
        {Path(path) for path in files}, key=lambda path: path.as_posix()
    ):
        if not source.exists() or not source.is_file():
            continue
        try:
            relative = source.resolve().relative_to(resolved_root).as_posix()
        except ValueError:
            continue
        if is_allowed_artifact(relative):
            selected.append(source)
    return selected


def is_allowed_artifact(relative_path: str) -> bool:
    normalized = relative_path.strip("/")
    if any(fnmatch.fnmatch(normalized, pattern) for pattern in DENYLIST_PATTERNS):
        return False
    return any(_match_allowlist(normalized, pattern) for pattern in ALLOWLIST_PATTERNS)


def _match_allowlist(path: str, pattern: str) -> bool:
    if pattern.endswith("/**"):
        return fnmatch.fnmatch(path, pattern[:-3]) or fnmatch.fnmatch(path, pattern)
    return fnmatch.fnmatch(path, pattern)


def build_rsync_command(
    target: SSHBackupTarget, *, source_root: Path, files_from: Path
) -> list[str]:
    ssh_parts = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        f"ConnectTimeout={target.timeout_seconds}",
    ]
    if target.port is not None:
        ssh_parts.extend(["-p", str(target.port)])
    if target.key_path is not None:
        ssh_parts.extend(["-i", str(target.key_path)])

    command = [
        "rsync",
        "-az",
        "--delete-missing-args",
        "--files-from",
        str(files_from),
        "--timeout",
        str(target.timeout_seconds),
    ]
    if target.bandwidth_limit_kbps is not None:
        command.extend(["--bwlimit", str(target.bandwidth_limit_kbps)])
    command.extend(["-e", " ".join(ssh_parts), f"{source_root}/", target.destination])
    return command


def classify_rsync_failure(completed: subprocess.CompletedProcess[str]) -> str:
    text = f"{completed.stderr}\n{completed.stdout}".lower()
    if any(marker in text for marker in UNREACHABLE_MARKERS):
        return "mac_unreachable"
    return "rsync_error"


def _run_command(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command), check=False, capture_output=True, text=True, timeout=None
    )
