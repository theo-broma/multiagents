"""Execution backends."""

from __future__ import annotations

from .base import Executor, Handle, build_env, prepare_home
from .docker import DockerExecutor
from .local import LocalExecutor

__all__ = [
    "Executor", "Handle", "build_env", "prepare_home",
    "LocalExecutor", "DockerExecutor", "get_executor",
]


def get_executor(kind: str, config: dict | None = None) -> Executor:
    if kind == "local":
        return LocalExecutor()
    if kind == "docker":
        return DockerExecutor(config or {})
    raise ValueError(f"Unknown executor kind {kind!r} (expected 'local' or 'docker')")
