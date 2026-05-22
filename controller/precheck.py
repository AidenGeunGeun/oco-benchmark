"""Patch-apply precheck before evaluation queueing."""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class PatchPrecheckResult:
    precheck_passed: bool
    precheck_failed: bool
    no_patch: bool
    queued_for_evaluation: bool
    reason: str

    def to_normalized_fields(self) -> dict[str, object]:
        return {
            "precheck_passed": self.precheck_passed,
            "precheck_failed": self.precheck_failed,
            "no_patch": self.no_patch,
            "queued_for_evaluation": self.queued_for_evaluation,
            "precheck_reason": self.reason,
        }


def evaluate_patch_precheck(
    *,
    worktree_dir: Path,
    patch_text: str,
    base_commit: str | None,
    scratch_dir: Path,
) -> PatchPrecheckResult:
    if not patch_text.strip():
        return PatchPrecheckResult(
            precheck_passed=False,
            precheck_failed=False,
            no_patch=True,
            queued_for_evaluation=False,
            reason="empty patch",
        )
    if not base_commit:
        return PatchPrecheckResult(
            precheck_passed=False,
            precheck_failed=True,
            no_patch=False,
            queued_for_evaluation=False,
            reason="missing recorded base commit",
        )
    git_dir = worktree_dir / ".git"
    if not git_dir.exists():
        return PatchPrecheckResult(
            precheck_passed=False,
            precheck_failed=True,
            no_patch=False,
            queued_for_evaluation=False,
            reason="worktree is not a git repository",
        )

    scratch_parent = scratch_dir.parent
    scratch_parent.mkdir(parents=True, exist_ok=True)
    if scratch_dir.exists():
        shutil.rmtree(scratch_dir)
    try:
        clone = subprocess.run(
            [
                "git",
                "clone",
                "--quiet",
                "--no-checkout",
                str(worktree_dir),
                str(scratch_dir),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if clone.returncode != 0:
            return _failed("could not clone worktree for precheck", clone)
        checkout = subprocess.run(
            [
                "git",
                "-C",
                str(scratch_dir),
                "checkout",
                "--quiet",
                "--detach",
                base_commit,
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if checkout.returncode != 0:
            return _failed("could not check out recorded base commit", checkout)
        apply = subprocess.run(
            ["git", "-C", str(scratch_dir), "apply", "--check", "-"],
            input=patch_text,
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if apply.returncode == 0:
            return PatchPrecheckResult(
                precheck_passed=True,
                precheck_failed=False,
                no_patch=False,
                queued_for_evaluation=True,
                reason="patch applies to recorded base commit",
            )
        return _failed("patch does not apply to recorded base commit", apply)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return PatchPrecheckResult(
            precheck_passed=False,
            precheck_failed=True,
            no_patch=False,
            queued_for_evaluation=False,
            reason=f"precheck execution failed: {type(exc).__name__}: {exc}",
        )
    finally:
        if scratch_dir.exists():
            shutil.rmtree(scratch_dir, ignore_errors=True)


def _failed(
    prefix: str, completed: subprocess.CompletedProcess[str]
) -> PatchPrecheckResult:
    details = (completed.stderr or completed.stdout or "").strip().splitlines()
    suffix = details[-1] if details else f"exit code {completed.returncode}"
    return PatchPrecheckResult(
        precheck_passed=False,
        precheck_failed=True,
        no_patch=False,
        queued_for_evaluation=False,
        reason=f"{prefix}: {suffix}",
    )
