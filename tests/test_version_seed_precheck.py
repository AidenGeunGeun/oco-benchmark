from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from controller.precheck import evaluate_patch_precheck
from controller.seed import SEED_MODULUS, derive_task_seed
from controller.version_gate import REQUIRED_FEATURE_STRINGS, check_oco_binary


def _fake_oco(path: Path, version: str, *, include_features: bool = True) -> Path:
    features = (
        "\n".join(f"# {feature}" for feature in REQUIRED_FEATURE_STRINGS)
        if include_features
        else ""
    )
    path.write_text(
        f'#!/bin/sh\n{features}\nif [ "$1" = "--version" ]; then echo \'{version}\'; exit 0; fi\nexit 0\n',
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def test_oco_version_feature_gate_passes_and_fails(tmp_path: Path) -> None:
    good = _fake_oco(tmp_path / "oco-good", "2.1.7")
    old = _fake_oco(tmp_path / "oco-old", "2.1.6")
    missing = _fake_oco(tmp_path / "oco-missing", "2.1.7", include_features=False)

    assert check_oco_binary(good).passed
    old_result = check_oco_binary(old)
    assert not old_result.passed
    assert old_result.detected_version == "2.1.6"
    missing_result = check_oco_binary(missing)
    assert not missing_result.passed
    assert set(missing_result.missing_features) == set(REQUIRED_FEATURE_STRINGS)


def test_seed_derivation_is_deterministic_distinct_and_cross_process() -> None:
    task_ids = [
        "django__django-1",
        "django__django-2",
        "ansible__ansible-42",
        "teleport__teleport-7",
        "openlibrary__openlibrary-99",
    ]
    seeds = [derive_task_seed(task_id) for task_id in task_ids]
    assert seeds == [derive_task_seed(task_id) for task_id in task_ids]
    assert len(set(seeds)) == len(seeds)
    assert all(0 <= seed < SEED_MODULUS for seed in seeds)

    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "from controller.seed import derive_task_seed; print(derive_task_seed('django__django-1'))",
        ],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    assert int(completed.stdout.strip()) == seeds[0]


def _git(worktree: Path, *args: str) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        ["git", "-C", str(worktree), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed


def _repo_with_patch(tmp_path: Path) -> tuple[Path, str, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "--quiet")
    (repo / "file.txt").write_text("before\n", encoding="utf-8")
    _git(repo, "add", "file.txt")
    _git(
        repo,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "--quiet",
        "-m",
        "base",
    )
    base = _git(repo, "rev-parse", "HEAD").stdout.strip()
    (repo / "file.txt").write_text("after\n", encoding="utf-8")
    patch = _git(repo, "diff", "--binary", "--no-ext-diff").stdout
    return repo, base, patch


def test_patch_apply_precheck_success_failure_and_empty(tmp_path: Path) -> None:
    repo, base, patch = _repo_with_patch(tmp_path)

    success = evaluate_patch_precheck(
        worktree_dir=repo,
        patch_text=patch,
        base_commit=base,
        scratch_dir=tmp_path / "scratch-success",
    )
    assert success.precheck_passed
    assert success.queued_for_evaluation

    failure = evaluate_patch_precheck(
        worktree_dir=repo,
        patch_text="diff --git a/missing.txt b/missing.txt\n--- a/missing.txt\n+++ b/missing.txt\n@@ -1 +1 @@\n-a\n+b\n",
        base_commit=base,
        scratch_dir=tmp_path / "scratch-failure",
    )
    assert failure.precheck_failed
    assert not failure.queued_for_evaluation

    empty = evaluate_patch_precheck(
        worktree_dir=repo,
        patch_text="",
        base_commit=base,
        scratch_dir=tmp_path / "scratch-empty",
    )
    assert empty.no_patch
    assert not empty.precheck_failed
