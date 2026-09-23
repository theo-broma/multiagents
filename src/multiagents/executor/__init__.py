"""Execution backends."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .base import Executor, Handle, build_env, prepare_home, private_file
from .docker import DockerExecutor
from .local import LocalExecutor

__all__ = [
    "Executor", "Handle", "build_env", "prepare_home", "private_file",
    "LocalExecutor", "DockerExecutor", "get_executor", "executor_for",
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


def executor_for(paths, config, providers):
    """An executor per provider, honouring any per-agent pin for that provider.

    Auth differs by where the CLI runs: opencode keeps credentials on the host
    even under docker, while agy needs a login inside the container.
    """
    from ..paths import global_config_dir as _gcd

    def build(provider_name: str):
        kind = config.executor
        for spec in config.agents.values():
            if spec.provider == provider_name and spec.executor:
                kind = spec.executor
                break
        return get_executor(
            kind, config.project.get("executor", {}).get("docker", {}),
            paths=paths, providers=providers, config_dir=_gcd(),
        )
    return build
