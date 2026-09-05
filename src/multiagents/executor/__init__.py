"""Execution backends."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .base import Executor, Handle, build_env, prepare_home
from .docker import DockerExecutor
from .local import LocalExecutor

__all__ = [
    "Executor", "Handle", "build_env", "prepare_home",
    "LocalExecutor", "DockerExecutor", "get_executor",
]


def get_executor(
    kind: str,
    config: dict | None = None,
    paths: Any = None,
    providers: dict[str, Any] | None = None,
    config_dir: Path | None = None,
) -> Executor:
    """Build an executor. The docker backend needs project context for its
    bind mounts; the local one ignores everything but `kind`."""
    if kind == "local":
        return LocalExecutor()
    if kind == "docker":
        return DockerExecutor(config or {}, paths, providers, config_dir)
    raise ValueError(f"Unknown executor kind {kind!r} (expected 'local' or 'docker')")
