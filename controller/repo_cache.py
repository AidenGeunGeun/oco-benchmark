"""Repository cache and per-attempt worktree lifecycle management."""

from __future__ import annotations

import fcntl
import json
import shutil
import subprocess
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, Protocol

from controller.atomic import atomic_write_json


TERMINAL_ATTEMPT_STATES = {
    "DONE",
    "precheck_failed",
    "no_patch",
    "repo_clone_failed",
    "repo_transient_failure",
    "repo_missing_base_commit",
}


class RepoCacheError(RuntimeError):
    """Raised when a repo cache or worktree cannot be acquired."""


class GitClient(Protocol):
    def clone_mirror(self, repo_url: str, cache_dir: Path) -> None: ...

    def fetch(self, cache_dir: Path) -> None: ...

    def has_commit(self, cache_dir: Path, commit: str) -> bool: ...

    def create_worktree(
        self, cache_dir: Path, worktree_dir: Path, commit: str
    ) -> None: ...


@dataclass(frozen=True)
class ProTaskRepo:
    instance_id: str
    repo: str
    repo_url: str
    base_commit: str

    @property
    def repo_key(self) -> str:
        return self.repo.replace("/", "__")


@dataclass(frozen=True)
class RepoAcquisitionResult:
    success: bool
    worktree_dir: Path | None
    outcome: str
    reason: str
    attempts: int


class SubprocessGitClient:
    def clone_mirror(self, repo_url: str, cache_dir: Path) -> None:
        _git(["clone", "--mirror", repo_url, str(cache_dir)], cwd=None)

    def fetch(self, cache_dir: Path) -> None:
        _git(["remote", "update", "--prune"], cwd=cache_dir)

    def has_commit(self, cache_dir: Path, commit: str) -> bool:
        completed = subprocess.run(
            [
                "git",
                "--git-dir",
                str(cache_dir),
                "cat-file",
                "-e",
                f"{commit}^{{commit}}",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
        return completed.returncode == 0

    def create_worktree(self, cache_dir: Path, worktree_dir: Path, commit: str) -> None:
        _git(
            [
                "--git-dir",
                str(cache_dir),
                "worktree",
                "add",
                "--detach",
                str(worktree_dir),
                commit,
            ],
            cwd=None,
        )


class RepoCacheManager:
    def __init__(
        self,
        *,
        cache_root: Path,
        worktree_root: Path,
        git_client: GitClient | None = None,
        max_clone_attempts: int = 3,
        retry_sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.cache_root = cache_root
        self.worktree_root = worktree_root
        self.git = git_client or SubprocessGitClient()
        self.max_clone_attempts = max_clone_attempts
        self.retry_sleep = retry_sleep

    def acquire_worktree(
        self,
        task: ProTaskRepo,
        *,
        attempt_id: str,
        worktree_dir: Path | None = None,
    ) -> RepoAcquisitionResult:
        self.cache_root.mkdir(parents=True, exist_ok=True)
        self.worktree_root.mkdir(parents=True, exist_ok=True)
        cache_dir = self.cache_dir(task.repo_key)
        worktree_dir = worktree_dir or self.worktree_dir(attempt_id)
        with self._locked():
            self._write_lease(task.repo_key, attempt_id)
            try:
                clone_result = self._ensure_cache(task, cache_dir)
                if clone_result is not None:
                    self._remove_lease(task.repo_key, attempt_id)
                    return clone_result
                if not self.git.has_commit(cache_dir, task.base_commit):
                    self._remove_partial_worktree(worktree_dir)
                    self._remove_lease(task.repo_key, attempt_id)
                    return RepoAcquisitionResult(
                        False,
                        None,
                        "repo_missing_base_commit",
                        "recorded base commit is missing from upstream cache",
                        1,
                    )
                self._remove_partial_worktree(worktree_dir)
                self._retry_transient(
                    lambda: self.git.create_worktree(
                        cache_dir, worktree_dir, task.base_commit
                    )
                )
                return RepoAcquisitionResult(
                    True, worktree_dir, "repo_ready", "worktree prepared", 1
                )
            except TransientGitError as exc:
                self._remove_partial_worktree(worktree_dir)
                self._remove_lease(task.repo_key, attempt_id)
                return RepoAcquisitionResult(
                    False,
                    None,
                    "repo_transient_failure",
                    str(exc),
                    self.max_clone_attempts,
                )
            except Exception as exc:  # noqa: BLE001 - failure outcome must be recorded.
                self._remove_partial_worktree(worktree_dir)
                self._remove_lease(task.repo_key, attempt_id)
                return RepoAcquisitionResult(
                    False, None, "repo_clone_failed", str(exc), 1
                )

    def release_worktree(self, repo_key: str, attempt_id: str) -> None:
        with self._locked():
            self._remove_lease(repo_key, attempt_id)

    def evict_for_watermark(
        self,
        *,
        watermark_exceeded: bool,
        repo_attempts: dict[str, list[str]],
        attempt_state: Callable[[str], str],
    ) -> list[str]:
        if not watermark_exceeded:
            return []
        evicted: list[str] = []
        with self._locked():
            for cache_dir in sorted(
                self.cache_root.iterdir() if self.cache_root.exists() else []
            ):
                if not cache_dir.is_dir() or cache_dir.name == "leases":
                    continue
                repo_key = cache_dir.name
                if self._repo_has_active_lease(repo_key):
                    continue
                states = [
                    attempt_state(attempt_id)
                    for attempt_id in repo_attempts.get(repo_key, [])
                ]
                if any(state not in TERMINAL_ATTEMPT_STATES for state in states):
                    continue
                shutil.rmtree(cache_dir)
                evicted.append(repo_key)
        return evicted

    def cache_dir(self, repo_key: str) -> Path:
        return self.cache_root / repo_key

    def worktree_dir(self, attempt_id: str) -> Path:
        return self.worktree_root / attempt_id

    def record_acquisition_result(
        self, attempt_dir: Path, result: RepoAcquisitionResult
    ) -> None:
        atomic_write_json(
            attempt_dir / "repo-acquisition.json",
            {
                "success": result.success,
                "worktree_dir": str(result.worktree_dir)
                if result.worktree_dir
                else None,
                "outcome": result.outcome,
                "reason": result.reason,
                "attempts": result.attempts,
            },
        )

    def _ensure_cache(
        self, task: ProTaskRepo, cache_dir: Path
    ) -> RepoAcquisitionResult | None:
        if cache_dir.exists():
            self._retry_transient(lambda: self.git.fetch(cache_dir))
            return None
        last_error: Exception | None = None
        for attempt in range(1, self.max_clone_attempts + 1):
            try:
                self.git.clone_mirror(task.repo_url, cache_dir)
                return None
            except TransientGitError as exc:
                last_error = exc
                if cache_dir.exists():
                    shutil.rmtree(cache_dir, ignore_errors=True)
                if attempt < self.max_clone_attempts:
                    self.retry_sleep(0.01 * attempt)
                    continue
                raise
            except Exception as exc:  # noqa: BLE001 - caller records clean failure.
                last_error = exc
                if cache_dir.exists():
                    shutil.rmtree(cache_dir, ignore_errors=True)
                return RepoAcquisitionResult(
                    False, None, "repo_clone_failed", str(last_error), attempt
                )
        raise RepoCacheError(str(last_error or "clone failed"))

    def _retry_transient(self, operation: Callable[[], None]) -> None:
        for attempt in range(1, self.max_clone_attempts + 1):
            try:
                operation()
                return
            except TransientGitError:
                if attempt >= self.max_clone_attempts:
                    raise
                self.retry_sleep(0.01 * attempt)

    def _write_lease(self, repo_key: str, attempt_id: str) -> None:
        lease_dir = self.cache_root / "leases" / repo_key
        lease_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_json(lease_dir / f"{attempt_id}.json", {"attempt_id": attempt_id})

    def _remove_lease(self, repo_key: str, attempt_id: str) -> None:
        try:
            (self.cache_root / "leases" / repo_key / f"{attempt_id}.json").unlink()
        except OSError:
            pass

    def _repo_has_active_lease(self, repo_key: str) -> bool:
        lease_dir = self.cache_root / "leases" / repo_key
        return lease_dir.exists() and any(lease_dir.iterdir())

    def _remove_partial_worktree(self, worktree_dir: Path) -> None:
        if worktree_dir.exists():
            shutil.rmtree(worktree_dir, ignore_errors=True)

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self.cache_root.mkdir(parents=True, exist_ok=True)
        lock_path = self.cache_root / ".repo-cache.lock"
        with lock_path.open("w", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class TransientGitError(RuntimeError):
    """Synthetic/real git transient failure marker used by retry policy."""


def _git(args: list[str], *, cwd: Path | None) -> None:
    completed = subprocess.run(
        ["git", *args],
        check=False,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=300,
    )
    if completed.returncode != 0:
        text = f"{completed.stderr}\n{completed.stdout}".lower()
        if any(
            marker in text for marker in ("timeout", "temporarily", "connection", "5xx")
        ):
            raise TransientGitError(
                completed.stderr.strip() or completed.stdout.strip()
            )
        raise RepoCacheError(completed.stderr.strip() or completed.stdout.strip())
