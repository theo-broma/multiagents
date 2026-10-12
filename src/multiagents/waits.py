"""Bound tool waits below the MCP client's idle timeout."""
from __future__ import annotations

import math


MAX_WAIT_SECONDS = 1500


def bounded_timeout(timeout: float) -> tuple[float, dict]:
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout < 0:
        raise ValueError("timeout: expected non-negative seconds")
    if timeout > MAX_WAIT_SECONDS:
        return MAX_WAIT_SECONDS, {"timeout_clamped": True,
                                  "effective_timeout": MAX_WAIT_SECONDS}
    return timeout, {}
