"""Deterministic per-task seed derivation for benchmark attempts."""

from __future__ import annotations

import hashlib


SEED_MODULUS = 2**31 - 1
"""Seed range is 0 through 2,147,483,646 inclusive.

The controller hashes the task ID with SHA-256 and folds the first 64 bits into
this positive 31-bit range because OpenAI-compatible servers commonly accept
signed 32-bit seeds. The seed depends only on the task ID, never on run ID or
attempt order.
"""


def derive_task_seed(task_id: str) -> int:
    """Return a stable OpenAI-compatible sampling seed for a task ID."""

    digest = hashlib.sha256(task_id.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=False) % SEED_MODULUS
