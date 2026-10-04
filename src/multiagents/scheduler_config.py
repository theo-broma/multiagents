"""Validation shared by scheduler startup, config loading and doctor."""
from __future__ import annotations

import math
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class SchedulerConfigError(ValueError):
    """A scheduler setting with its source file and line."""


def validate_setting(key, value, source=""):
    valid = True
    if key == "enabled":
        valid = isinstance(value, bool)
    elif key == "timezone":
        try:
            if not isinstance(value, str) or not value:
                raise ValueError()
            ZoneInfo(value)
        except (ValueError, ZoneInfoNotFoundError):
            valid = False
    elif key in {"starvation_after_seconds", "window_tolerance_seconds",
                 "admission_timeout_seconds", "tick_seconds"}:
        valid = (type(value) in (int, float) and math.isfinite(value) and value > 0)
    if not valid:
        raise SchedulerConfigError(f"scheduler.{key}: invalid value {value!r}" + (f" ({source})" if source else ""))


def settings(project_root):
    """Read scheduler policy using config.load's section layering."""
    from .config import load_project_section
    from .paths import ProjectPaths
    return load_project_section(ProjectPaths(project_root), "scheduler")
