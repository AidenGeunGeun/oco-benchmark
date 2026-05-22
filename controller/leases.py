"""Per-attempt lease files and stale recovery."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

from controller.atomic import atomic_write_json


@dataclass(frozen=True)
class Lease:
    pid: int
    timestamp: float


class ActiveLeaseError(RuntimeError):
    pass


class LeaseManager:
    def __init__(self, lease_path: Path, *, stale_after_seconds: float = 300.0) -> None:
        self.lease_path = lease_path
        self.stale_after_seconds = stale_after_seconds

    def read(self) -> Lease | None:
        if not self.lease_path.exists():
            return None
        payload = json.loads(self.lease_path.read_text(encoding="utf-8"))
        return Lease(pid=int(payload["pid"]), timestamp=float(payload["timestamp"]))

    def is_stale(self, lease: Lease, *, now: float | None = None) -> bool:
        current_time = time.time() if now is None else now
        return current_time - lease.timestamp >= self.stale_after_seconds

    def recover_or_raise(self) -> bool:
        lease = self.read()
        if lease is None:
            return False
        if not self.is_stale(lease):
            raise ActiveLeaseError(
                f"attempt has an active lease from pid {lease.pid}; stale_after={self.stale_after_seconds}s"
            )
        self.release()
        return True

    def acquire(self) -> Lease:
        self.lease_path.parent.mkdir(parents=True, exist_ok=True)
        lease = Lease(pid=os.getpid(), timestamp=time.time())
        atomic_write_json(
            self.lease_path, {"pid": lease.pid, "timestamp": lease.timestamp}
        )
        return lease

    def release(self) -> None:
        self.lease_path.unlink(missing_ok=True)
