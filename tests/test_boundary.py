from __future__ import annotations

import shutil
import subprocess
import uuid
from pathlib import Path

from controller.atomic import atomic_write_text
from controller.boundary import BoundaryConfig, BoundaryMonitor
from controller.core import AttemptSpec, BenchmarkController, ControllerConfig
from controller.modal_eval import (
    FixtureModalClient,
    ModalEvalResponse,
    ModalEvaluationPipeline,
)
from controller.repo_cache import RepoCacheManager


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _fixture_root(name: str) -> Path:
    root = PROJECT_ROOT / "runs" / f"unit-boundary-{name}-{uuid.uuid4().hex}"
    root.mkdir(parents=True)
    return root


def test_boundary_check_passes_when_only_allowed_area_changes() -> None:
    root = _fixture_root("clean")
    try:
        allowed = root / "attempt-area"
        protected = root / "synthetic-protected-sibling"
        allowed.mkdir()
        protected.mkdir()
        atomic_write_text(protected / "before.txt", "stable\n")
        monitor = BoundaryMonitor(
            BoundaryConfig(
                protected_roots=(protected,),
                allowed_roots=(allowed,),
                monitored_roots=(root,),
            )
        )
        monitor.start()

        atomic_write_text(allowed / "artifact.txt", "allowed write\n")
        proof = monitor.finish(allowed / "boundary-proof.md")

        assert proof.passed
        assert (
            (allowed / "boundary-proof.md")
            .read_text(encoding="utf-8")
            .startswith("# Production-Fidelity Boundary Proof")
        )
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_boundary_check_flags_planted_sibling_violation() -> None:
    root = _fixture_root("violation")
    try:
        allowed = root / "attempt-area"
        protected = root / "synthetic-protected-root"
        outside = root / "synthetic-outside-sibling"
        allowed.mkdir()
        protected.mkdir()
        outside.mkdir()
        atomic_write_text(protected / "before.txt", "stable\n")
        monitor = BoundaryMonitor(
            BoundaryConfig(
                protected_roots=(protected,),
                allowed_roots=(allowed,),
                monitored_roots=(root,),
            )
        )
        monitor.start()

        atomic_write_text(outside / "planted-violation.txt", "out of bounds\n")
        proof = monitor.finish(allowed / "boundary-proof.md")
        artifact = (allowed / "boundary-proof.md").read_text(encoding="utf-8")

        assert not proof.passed
        assert "Status: FAIL" in artifact
        assert "planted-violation.txt" in artifact
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_boundary_check_flags_traced_out_of_bounds_write() -> None:
    root = _fixture_root("trace")
    try:
        allowed = root / "attempt-area"
        protected = root / "synthetic-protected-root"
        allowed.mkdir()
        protected.mkdir()
        monitor = BoundaryMonitor(
            BoundaryConfig(
                protected_roots=(protected,),
                allowed_roots=(allowed,),
                monitored_roots=(root,),
                require_trace=True,
            )
        )
        monitor.start()

        atomic_write_text(
            allowed / "filesystem-trace.log",
            '123 openat(AT_FDCWD, "/tmp/oco-benchmark-outside", O_WRONLY|O_CREAT, 0600) = 3\n',
        )
        proof = monitor.finish(allowed / "boundary-proof.md")
        artifact = (allowed / "boundary-proof.md").read_text(encoding="utf-8")

        assert not proof.passed
        assert any(
            path.endswith("/tmp/oco-benchmark-outside")
            for path in proof.trace_outside_writes
        )
        assert "out-of-bounds write-like syscalls" in artifact
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_boundary_check_flags_traced_protected_and_relative_writes() -> None:
    root = _fixture_root("trace-protected")
    try:
        allowed = root / "attempt-area"
        protected = root / "synthetic-protected-root"
        allowed.mkdir()
        protected.mkdir()
        monitor = BoundaryMonitor(
            BoundaryConfig(
                protected_roots=(protected,),
                allowed_roots=(allowed,),
                monitored_roots=(root,),
                require_trace=True,
            )
        )
        monitor.start()

        atomic_write_text(
            allowed / "filesystem-trace.log",
            f'123 openat(AT_FDCWD, "{protected / "transient.txt"}", O_WRONLY|O_CREAT, 0600) = 3\n'
            '124 openat(AT_FDCWD, "relative-write.txt", O_WRONLY|O_CREAT, 0600) = 4\n',
        )
        proof = monitor.finish(allowed / "boundary-proof.md")

        assert not proof.passed
        assert any(
            path.endswith("/transient.txt") for path in proof.trace_outside_writes
        )
        assert "RELATIVE:relative-write.txt" in proof.trace_outside_writes
    finally:
        shutil.rmtree(root, ignore_errors=True)


class _LocalMirrorGitClient:
    def __init__(self, source_repo: Path) -> None:
        self.source_repo = source_repo

    def clone_mirror(self, repo_url: str, cache_dir: Path) -> None:
        subprocess.run(
            ["git", "clone", "--mirror", repo_url, str(cache_dir)],
            check=True,
            capture_output=True,
            text=True,
        )

    def fetch(self, cache_dir: Path) -> None:
        subprocess.run(
            ["git", "--git-dir", str(cache_dir), "remote", "update", "--prune"],
            check=True,
            capture_output=True,
            text=True,
        )

    def has_commit(self, cache_dir: Path, commit: str) -> bool:
        return (
            subprocess.run(
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
            ).returncode
            == 0
        )

    def create_worktree(self, cache_dir: Path, worktree_dir: Path, commit: str) -> None:
        subprocess.run(
            [
                "git",
                "--git-dir",
                str(cache_dir),
                "worktree",
                "add",
                "--detach",
                str(worktree_dir),
                commit,
            ],
            check=True,
            capture_output=True,
            text=True,
        )


def _git_repo_with_base(root: Path) -> tuple[Path, str]:
    repo = root / "source-repo"
    repo.mkdir()
    subprocess.run(["git", "init", "--quiet"], cwd=repo, check=True)
    atomic_write_text(repo / "README.md", "fixture\n")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "--quiet",
            "-m",
            "base",
        ],
        cwd=repo,
        check=True,
    )
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return repo, base


def test_boundary_proof_still_covers_repo_cache_modal_and_backup_paths(
    tmp_path: Path,
) -> None:
    source_repo, base = _git_repo_with_base(tmp_path)
    run_root = tmp_path / "run"
    protected = tmp_path / "protected"
    protected.mkdir()
    atomic_write_text(protected / "stable.txt", "stable\n")
    repo_cache = RepoCacheManager(
        cache_root=run_root / "repo-cache",
        worktree_root=run_root / "worktrees",
        git_client=_LocalMirrorGitClient(source_repo),
    )
    modal = ModalEvaluationPipeline(
        run_root=run_root,
        run_id="boundary-flow",
        client=FixtureModalClient([ModalEvalResponse("pass", cost_usd=0.01)]),
    )
    controller = BenchmarkController(
        ControllerConfig(
            run_root=run_root,
            run_id="boundary-flow",
            backup_destination=run_root / "mac-backup-local",
            boundary_config=BoundaryConfig(
                protected_roots=(protected,),
                allowed_roots=(run_root,),
                monitored_roots=(tmp_path,),
            ),
        ),
        repo_cache_manager=repo_cache,
        modal_pipeline=modal,
    )

    controller.run_attempts(
        [
            AttemptSpec(
                "boundary-task",
                base_commit=base,
                repo="example/repo",
                repo_url=str(source_repo),
                task_row={"instance_id": "boundary-task", "repo": "example/repo"},
            )
        ]
    )
    proof = run_root / "attempts" / "boundary-task" / "boundary-proof.md"

    assert proof.exists()
    assert "Status: PASS" in proof.read_text(encoding="utf-8")
    assert (run_root / "attempts" / "boundary-task" / "modal-result.json").exists()
    assert (
        run_root / "mac-backup-local" / "attempts" / "boundary-task" / "patch.diff"
    ).exists()
