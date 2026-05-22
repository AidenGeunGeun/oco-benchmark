"""OCO binary version and feature gate for real benchmark runs."""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from controller.atomic import atomic_write_json


REQUIRED_OCO_VERSION = (2, 1, 7)
REQUIRED_FEATURE_STRINGS: tuple[str, ...] = (
    "glob search timed out after",
    "grep search timed out after",
    "experimentalNonStreamingToolCalls",
)


@dataclass(frozen=True)
class OCOGateResult:
    passed: bool
    binary: str
    detected_version: str | None
    required_version: str
    required_features: tuple[str, ...]
    missing_features: tuple[str, ...]
    reason: str

    def to_json(self) -> dict[str, object]:
        return {
            "passed": self.passed,
            "binary": self.binary,
            "detected_version": self.detected_version,
            "required_version": self.required_version,
            "required_features": list(self.required_features),
            "missing_features": list(self.missing_features),
            "reason": self.reason,
        }


def _version_text(version: tuple[int, int, int]) -> str:
    return ".".join(str(part) for part in version)


def parse_semver(text: str) -> tuple[int, int, int] | None:
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", text)
    if match is None:
        return None
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def _resolve_binary(binary: str | Path) -> Path | None:
    candidate = Path(binary)
    if candidate.parent != Path(".") or candidate.is_absolute():
        return candidate if candidate.exists() else None
    resolved = shutil.which(str(binary))
    return Path(resolved) if resolved else None


def _read_binary_strings(binary: Path) -> str:
    try:
        completed = subprocess.run(
            ["strings", str(binary)],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        completed = None
    if completed is not None and completed.returncode == 0:
        return completed.stdout
    return binary.read_bytes().decode("utf-8", errors="ignore")


def check_oco_binary(
    binary: str | Path = "oco",
    *,
    required_version: tuple[int, int, int] = REQUIRED_OCO_VERSION,
    required_features: Iterable[str] = REQUIRED_FEATURE_STRINGS,
) -> OCOGateResult:
    resolved = _resolve_binary(binary)
    required_features_tuple = tuple(required_features)
    required_version_text = _version_text(required_version)
    if resolved is None:
        return OCOGateResult(
            passed=False,
            binary=str(binary),
            detected_version=None,
            required_version=required_version_text,
            required_features=required_features_tuple,
            missing_features=required_features_tuple,
            reason="oco binary was not found on PATH or at the configured path",
        )

    try:
        version_completed = subprocess.run(
            [str(resolved), "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        version_output = (version_completed.stdout + version_completed.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return OCOGateResult(
            passed=False,
            binary=str(resolved),
            detected_version=None,
            required_version=required_version_text,
            required_features=required_features_tuple,
            missing_features=required_features_tuple,
            reason=f"failed to read oco version: {type(exc).__name__}: {exc}",
        )

    detected = parse_semver(version_output)
    strings_output = _read_binary_strings(resolved)
    missing = tuple(
        feature for feature in required_features_tuple if feature not in strings_output
    )
    version_ok = detected is not None and detected >= required_version
    passed = version_ok and not missing
    if detected is None:
        reason = f"could not parse semantic version from: {version_output!r}"
    elif not version_ok:
        reason = f"oco {version_output} is older than required {required_version_text}"
    elif missing:
        reason = "oco binary is missing required benchmark feature strings"
    else:
        reason = "oco binary satisfies the benchmark version and feature requirements"

    return OCOGateResult(
        passed=passed,
        binary=str(resolved),
        detected_version=_version_text(detected) if detected is not None else None,
        required_version=required_version_text,
        required_features=required_features_tuple,
        missing_features=missing,
        reason=reason,
    )


def write_gate_artifact(path: Path, result: OCOGateResult) -> None:
    atomic_write_json(path, result.to_json())
