"""Pluggable storage and RAM watermark monitors."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


RatioSource = Callable[[], float]


@dataclass(frozen=True)
class WatermarkStatus:
    name: str
    used_ratio: float
    threshold: float
    exceeded: bool


def _default_disk_ratio(path: Path) -> float:
    usage = shutil.disk_usage(path)
    return usage.used / usage.total if usage.total else 0.0


def _default_memory_ratio() -> float:
    if hasattr(os, "sysconf"):
        try:
            pages = os.sysconf("SC_PHYS_PAGES")
            available_pages = os.sysconf("SC_AVPHYS_PAGES")
            page_size = os.sysconf("SC_PAGE_SIZE")
            total = pages * page_size
            available = available_pages * page_size
            if total > 0:
                return (total - available) / total
        except (OSError, ValueError):
            pass
    return 0.0


class ResourceWatermarks:
    def __init__(
        self,
        *,
        disk_path: Path,
        disk_threshold: float = 0.85,
        ram_threshold: float = 0.90,
        disk_usage_source: RatioSource | None = None,
        memory_usage_source: RatioSource | None = None,
    ) -> None:
        self.disk_path = disk_path
        self.disk_threshold = disk_threshold
        self.ram_threshold = ram_threshold
        self.disk_usage_source = disk_usage_source or (
            lambda: _default_disk_ratio(disk_path)
        )
        self.memory_usage_source = memory_usage_source or _default_memory_ratio

    def disk_status(self) -> WatermarkStatus:
        used = float(self.disk_usage_source())
        return WatermarkStatus(
            "storage", used, self.disk_threshold, used >= self.disk_threshold
        )

    def ram_status(self) -> WatermarkStatus:
        used = float(self.memory_usage_source())
        return WatermarkStatus(
            "ram", used, self.ram_threshold, used >= self.ram_threshold
        )
