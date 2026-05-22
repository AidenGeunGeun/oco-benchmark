#!/usr/bin/env python3
"""Opt-in real SSH rsync integration check with a tiny synthetic artifact."""

from __future__ import annotations

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from controller.atomic import atomic_write_text  # noqa: E402
from controller.backup import BackupHook, SSHBackupTarget  # noqa: E402


def main() -> int:
    if os.environ.get("OCO_BENCHMARK_SSH_RSYNC_INTEGRATION") != "1":
        print("SKIP: set OCO_BENCHMARK_SSH_RSYNC_INTEGRATION=1 to run real SSH rsync")
        return 0
    host = os.environ.get("OCO_BENCHMARK_SSH_HOST")
    user = os.environ.get("OCO_BENCHMARK_SSH_USER")
    target_dir = os.environ.get("OCO_BENCHMARK_SSH_TARGET_DIR")
    if not (host and user and target_dir):
        print(
            "FAILED: set OCO_BENCHMARK_SSH_HOST, OCO_BENCHMARK_SSH_USER, and OCO_BENCHMARK_SSH_TARGET_DIR",
            file=sys.stderr,
        )
        return 1
    run_root = PROJECT_ROOT / "runs" / "ssh-rsync-integration"
    artifact = run_root / "attempts" / "ssh-check" / "normalized.json"
    atomic_write_text(artifact, "{}\n")
    key = os.environ.get("OCO_BENCHMARK_SSH_KEY_PATH")
    port = os.environ.get("OCO_BENCHMARK_SSH_PORT")
    target = SSHBackupTarget(
        host=host,
        user=user,
        target_dir=target_dir,
        key_path=Path(key) if key else None,
        port=int(port) if port else None,
    )
    hook = BackupHook(target)
    result = hook.mirror([artifact], source_root=run_root)
    if not result.success:
        print(f"FAILED push: {result.reason}", file=sys.stderr)
        return 1
    unreachable_host = os.environ.get(
        "OCO_BENCHMARK_SSH_UNREACHABLE_HOST", "offline.invalid"
    )
    unreachable = SSHBackupTarget(
        host=unreachable_host,
        user=user,
        target_dir=target_dir,
        key_path=Path(key) if key else None,
        port=int(port) if port else None,
        timeout_seconds=2,
    )
    unreachable_result = BackupHook(unreachable).mirror(
        [artifact], source_root=run_root
    )
    if not (
        unreachable_result.success
        and unreachable_result.retryable
        and unreachable_result.copied == []
    ):
        print(
            f"FAILED unreachable-path check: {unreachable_result.reason}",
            file=sys.stderr,
        )
        return 1
    print(f"PASS push: {result.reason}")
    print(f"PASS unreachable: {unreachable_result.reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
