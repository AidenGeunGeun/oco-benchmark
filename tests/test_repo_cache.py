from __future__ import annotations

from pathlib import Path

from controller.repo_cache import ProTaskRepo, RepoCacheManager, TransientGitError


class FakeGitClient:
    def __init__(
        self,
        *,
        commits: set[str] | None = None,
        clone_failures: list[Exception] | None = None,
    ) -> None:
        self.commits = commits or {"base"}
        self.clone_failures = list(clone_failures or [])
        self.clone_count = 0
        self.fetch_count = 0
        self.worktree_count = 0

    def clone_mirror(self, repo_url: str, cache_dir: Path) -> None:
        del repo_url
        self.clone_count += 1
        if self.clone_failures:
            raise self.clone_failures.pop(0)
        cache_dir.mkdir(parents=True)
        (cache_dir / "HEAD").write_text("fixture\n", encoding="utf-8")

    def fetch(self, cache_dir: Path) -> None:
        assert cache_dir.exists()
        self.fetch_count += 1

    def has_commit(self, cache_dir: Path, commit: str) -> bool:
        assert cache_dir.exists()
        return commit in self.commits

    def create_worktree(self, cache_dir: Path, worktree_dir: Path, commit: str) -> None:
        assert cache_dir.exists()
        assert commit in self.commits
        self.worktree_count += 1
        worktree_dir.mkdir(parents=True)
        (worktree_dir / ".git").write_text("gitdir fixture\n", encoding="utf-8")


def _task(repo: str = "example/repo", commit: str = "base") -> ProTaskRepo:
    return ProTaskRepo(
        instance_id="example__repo-1",
        repo=repo,
        repo_url=f"https://github.com/{repo}.git",
        base_commit=commit,
    )


def test_repo_cache_clones_once_and_creates_attempt_worktrees(tmp_path: Path) -> None:
    git = FakeGitClient()
    manager = RepoCacheManager(
        cache_root=tmp_path / "repo-cache",
        worktree_root=tmp_path / "worktrees",
        git_client=git,
    )

    first = manager.acquire_worktree(_task(), attempt_id="attempt-1")
    manager.release_worktree("example__repo", "attempt-1")
    second = manager.acquire_worktree(_task(), attempt_id="attempt-2")

    assert first.success and second.success
    assert git.clone_count == 1
    assert git.fetch_count == 1
    assert git.worktree_count == 2


def test_repo_cache_failure_modes_leave_no_partial_worktree(tmp_path: Path) -> None:
    clone_fail = RepoCacheManager(
        cache_root=tmp_path / "clone-cache",
        worktree_root=tmp_path / "clone-worktrees",
        git_client=FakeGitClient(clone_failures=[RuntimeError("clone exploded")]),
    )
    clone_result = clone_fail.acquire_worktree(_task(), attempt_id="clone-fail")
    assert not clone_result.success
    assert clone_result.outcome == "repo_clone_failed"
    assert not (tmp_path / "clone-worktrees" / "clone-fail").exists()

    transient = RepoCacheManager(
        cache_root=tmp_path / "transient-cache",
        worktree_root=tmp_path / "transient-worktrees",
        git_client=FakeGitClient(
            clone_failures=[
                TransientGitError("network blip"),
                TransientGitError("network blip"),
            ]
        ),
        max_clone_attempts=2,
        retry_sleep=lambda _: None,
    )
    transient_result = transient.acquire_worktree(_task(), attempt_id="transient-fail")
    assert not transient_result.success
    assert transient_result.outcome == "repo_transient_failure"
    assert transient_result.attempts == 2
    assert not (tmp_path / "transient-worktrees" / "transient-fail").exists()

    missing = RepoCacheManager(
        cache_root=tmp_path / "missing-cache",
        worktree_root=tmp_path / "missing-worktrees",
        git_client=FakeGitClient(commits={"other"}),
    )
    missing_result = missing.acquire_worktree(
        _task(commit="base"), attempt_id="missing-base"
    )
    assert not missing_result.success
    assert missing_result.outcome == "repo_missing_base_commit"
    assert not (tmp_path / "missing-worktrees" / "missing-base").exists()


def test_repo_cache_eviction_respects_active_leases_and_nonterminal_attempts(
    tmp_path: Path,
) -> None:
    manager = RepoCacheManager(
        cache_root=tmp_path / "repo-cache", worktree_root=tmp_path / "worktrees"
    )
    safe = manager.cache_dir("safe__repo")
    leased = manager.cache_dir("leased__repo")
    nonterminal = manager.cache_dir("active__repo")
    for path in (safe, leased, nonterminal):
        path.mkdir(parents=True)
        (path / "HEAD").write_text("fixture\n", encoding="utf-8")
    manager._write_lease("leased__repo", "leased-attempt")

    states = {"safe-attempt": "DONE", "active-attempt": "RUN"}
    evicted = manager.evict_for_watermark(
        watermark_exceeded=True,
        repo_attempts={
            "safe__repo": ["safe-attempt"],
            "leased__repo": ["leased-attempt"],
            "active__repo": ["active-attempt"],
        },
        attempt_state=lambda attempt_id: states.get(attempt_id, "RUN"),
    )

    assert evicted == ["safe__repo"]
    assert not safe.exists()
    assert leased.exists()
    assert nonterminal.exists()
