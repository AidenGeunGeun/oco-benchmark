from __future__ import annotations

from pathlib import Path

import pytest

from controller.atomic import atomic_write_text


def _fail(_: Path) -> None:
    raise RuntimeError("simulated failure")


def test_atomic_write_failure_leaves_no_partial_file(tmp_path: Path) -> None:
    target = tmp_path / "artifact.txt"

    with pytest.raises(RuntimeError):
        atomic_write_text(target, "partial", failure_hook=_fail)

    assert not target.exists()
    assert not list(tmp_path.glob(".*.tmp"))


def test_atomic_write_failure_preserves_prior_version(tmp_path: Path) -> None:
    target = tmp_path / "artifact.txt"
    atomic_write_text(target, "prior")

    with pytest.raises(RuntimeError):
        atomic_write_text(target, "partial", failure_hook=_fail)

    assert target.read_text(encoding="utf-8") == "prior"
    assert not list(tmp_path.glob(".*.tmp"))
