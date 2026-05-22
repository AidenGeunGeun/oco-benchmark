"""Attempt phase names and marker helpers."""

from __future__ import annotations

from enum import Enum
from pathlib import Path


class Phase(str, Enum):
    SETUP = "SETUP"
    RUN = "RUN"
    CAPTURE = "CAPTURE"
    DONE = "DONE"


PHASE_SEQUENCE: tuple[Phase, ...] = (Phase.SETUP, Phase.RUN, Phase.CAPTURE, Phase.DONE)
PHASE_MARKERS: dict[Phase, str] = {
    Phase.SETUP: "SETUP_DONE",
    Phase.RUN: "RUN_DONE",
    Phase.CAPTURE: "CAPTURE_DONE",
    Phase.DONE: "DONE",
}
MARKER_TO_PHASE: dict[str, Phase] = {
    marker: phase for phase, marker in PHASE_MARKERS.items()
}


def marker_path(attempt_dir: Path, phase: Phase) -> Path:
    return attempt_dir / PHASE_MARKERS[phase]


def phase_is_done(attempt_dir: Path, phase: Phase) -> bool:
    return marker_path(attempt_dir, phase).exists()


def next_phase(attempt_dir: Path) -> Phase | None:
    for phase in PHASE_SEQUENCE:
        if not phase_is_done(attempt_dir, phase):
            return phase
    return None
