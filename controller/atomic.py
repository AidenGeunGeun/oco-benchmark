"""Atomic file-write helpers used for durable controller artifacts."""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any, Callable, Iterable


FailureHook = Callable[[Path], None]


def _fsync_directory(path: Path) -> None:
    """Best-effort fsync for the containing directory after a rename."""

    flags = getattr(os, "O_DIRECTORY", 0) | os.O_RDONLY
    try:
        fd = os.open(str(path), flags)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write_bytes(
    path: Path | str,
    data: bytes,
    *,
    failure_hook: FailureHook | None = None,
) -> None:
    """Write bytes using temp-file, fsync, and atomic rename.

    The optional failure hook is only for tests; if it raises after the temp file
    has been fsync'd, the destination is left as the prior version or absent.
    """

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = (
        destination.parent / f".{destination.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    )
    try:
        with tmp_path.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if failure_hook is not None:
            failure_hook(tmp_path)
        os.replace(tmp_path, destination)
        _fsync_directory(destination.parent)
    except BaseException:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def atomic_write_text(
    path: Path | str,
    text: str,
    *,
    failure_hook: FailureHook | None = None,
) -> None:
    atomic_write_bytes(path, text.encode("utf-8"), failure_hook=failure_hook)


def atomic_write_json(
    path: Path | str, payload: Any, *, failure_hook: FailureHook | None = None
) -> None:
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    atomic_write_text(path, text, failure_hook=failure_hook)


def atomic_write_jsonl(path: Path | str, rows: Iterable[dict[str, Any]]) -> None:
    text = "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows)
    atomic_write_text(path, text)


def append_jsonl_atomic(path: Path | str, row: dict[str, Any]) -> None:
    destination = Path(path)
    existing = destination.read_text(encoding="utf-8") if destination.exists() else ""
    if existing and not existing.endswith("\n"):
        existing += "\n"
    atomic_write_text(destination, existing + json.dumps(row, sort_keys=True) + "\n")
