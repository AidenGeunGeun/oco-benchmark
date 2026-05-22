"""Dry-run fixture adapter that stands in for the future real OCO runner."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class FixtureRunResult:
    events: list[dict]


class FixtureOCOAdapter:
    """Swappable RUN-phase adapter for dry-run tests and the gate.

    It never invokes an installed OCO binary. The future real adapter can return
    the same event list shape after wrapping the actual subprocess.
    """

    def __init__(
        self,
        *,
        slow_attempt_ids: set[str] | None = None,
        slow_seconds: float = 0.0,
        guard_attempt_ids: set[str] | None = None,
        no_patch_attempt_ids: set[str] | None = None,
    ) -> None:
        self.slow_attempt_ids = slow_attempt_ids or set()
        self.slow_seconds = slow_seconds
        self.guard_attempt_ids = guard_attempt_ids or set()
        self.no_patch_attempt_ids = no_patch_attempt_ids or set()

    def run(self, *, attempt_id: str, attempt_dir: Path, **_: Any) -> FixtureRunResult:
        if attempt_id in self.slow_attempt_ids and self.slow_seconds > 0:
            time.sleep(self.slow_seconds)
        events = fixture_events(
            attempt_id,
            include_guard=attempt_id in self.guard_attempt_ids,
            no_patch=attempt_id in self.no_patch_attempt_ids,
        )
        return FixtureRunResult(events=events)


def fixture_events(
    attempt_id: str, *, include_guard: bool = False, no_patch: bool = False
) -> list[dict]:
    events: list[dict] = [
        {
            "type": "model_step",
            "step_role": "PM",
            "usage": {
                "prompt_tokens": 1200,
                "completion_tokens": 240,
                "prompt_tokens_details": {"cached_tokens": 0},
                "completion_tokens_details": {"reasoning_tokens": 90},
            },
            "wall_time_ms": 1500,
            "finish_reason": "tool_calls",
            "tools_called": [{"name": "task", "subagent": "orchestrator"}],
        },
        {
            "type": "model_step",
            "step_role": "Orchestrator",
            "usage": {
                "prompt_tokens": 1800,
                "completion_tokens": 520,
                "prompt_tokens_details": {"cached_tokens": 1250},
                "completion_tokens_details": {"reasoning_tokens": 220},
            },
            "wall_time_ms": 2800,
            "finish_reason": "tool_calls",
            "tools_called": [
                {"name": "read"},
                {"name": "bash"},
                {"name": "task", "subagent": "auditor"},
            ],
        },
        {
            "type": "model_step",
            "step_role": "Auditor",
            "usage": {
                "prompt_tokens": 2100,
                "completion_tokens": 180,
                "prompt_tokens_details": {"cached_tokens": 1700},
                "completion_tokens_details": {"reasoning_tokens": 70},
            },
            "wall_time_ms": 1200,
            "finish_reason": "stop",
            "tools_called": [],
        },
    ]
    if include_guard:
        events.extend(
            [
                {
                    "type": "tool_message",
                    "message": "glob search timed out after 30s; narrow the pattern or pass a larger timeout",
                },
                {
                    "type": "tool_message",
                    "message": "grep search timed out after 30s; narrow the path or include filter",
                },
            ]
        )
    diff = "" if no_patch else _fixture_diff(attempt_id)
    events.append({"type": "fixture_diff", "diff": diff})
    return events


def _fixture_diff(attempt_id: str) -> str:
    safe_id = attempt_id.replace(" ", "-")
    return (
        "diff --git a/fixture.txt b/fixture.txt\n"
        "new file mode 100644\n"
        "index 0000000..1111111\n"
        "--- /dev/null\n"
        "+++ b/fixture.txt\n"
        f"@@ -0,0 +1 @@\n+fixture patch for {safe_id}\n"
    )
