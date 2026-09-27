"""Execution backends."""

from __future__ import annotations

import sys
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any

from .base import Executor, Handle, build_env, prepare_home, private_file
from .docker import DockerExecutor
from .local import LocalExecutor

__all__ = [
    "Executor", "Handle", "build_env", "prepare_home", "private_file",
    "LocalExecutor", "DockerExecutor", "get_executor", "executor_for", "executor_at",
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


# {(cwd, provider): (project root, config fingerprint, when it was taken,
# executor)} for `executor_at`, which a transcript reader may call on every
# poll: a hit costs no git call and no config load, and re-reads the
# fingerprint at most every `_RESTAT` seconds. Least recently used first, and
# bounded: one entry per worktree ever asked about would otherwise grow for as
# long as the process runs.
_at: OrderedDict[tuple[str, str], tuple[Path, tuple, float, Any]] = OrderedDict()
_AT_LIMIT = 64
_RESTAT = 3.0
# Projects whose configuration was reported unreadable, so a reader polling
# every few seconds says so once rather than on every poll.
_unreadable: set[tuple[str, str]] = set()


def _config_stamp(root: Path) -> tuple:
    from ..paths import ProjectPaths, global_config_dir

    stamp = []
    for layer in (global_config_dir(), ProjectPaths(root).config):
        try:
            entries = sorted(layer.glob("*.yaml"))
        except OSError:
            entries = []
        for entry in entries:
            try:
                st = entry.stat()
            except OSError:
                continue
            stamp.append((str(entry), st.st_mtime_ns, st.st_size))
    return tuple(stamp)


def _project_of(cwd: Path) -> Path | None:
    from ..gitops import owning_repo
    from ..paths import find_project_root

    try:
        repo = owning_repo(cwd)
        root = find_project_root(repo) if repo is not None else None
        if root is None and cwd.is_dir():
            root = find_project_root(cwd)
    except OSError:
        return None
    return root


def executor_at(cwd: Path, provider_name: str) -> Executor | None:
    """The executor an agent of `provider_name` working in `cwd` runs under,
    or None when `cwd` belongs to no project or its configuration cannot be
    read.

    For a transcript reader that was handed no executor (SP-R2): the project
    is the one `cwd` is, or the one whose repository the worktree at `cwd`
    belongs to. Found once per `cwd`; rebuilt only when the project's
    configuration changes, which is noticed within `_RESTAT` seconds.
    """
    import yaml

    from ..config import load as load_config
    from ..paths import ProjectPaths
    from ..providers import load_providers

    cwd = Path(cwd)
    key = (str(cwd), provider_name)
    now = time.monotonic()
    cached = _at.get(key)
    if cached is not None:
        _at.move_to_end(key)
        root, stamp, checked, executor = cached
        if now - checked < _RESTAT:
            return executor
        fresh = _config_stamp(root)
        if fresh == stamp:
            _at[key] = (root, stamp, now, executor)
            return executor
        stamp = fresh
    else:
        root = _project_of(cwd)
        if root is None:
            # Not remembered: a worktree reattached later belongs to a project.
            return None
        stamp = _config_stamp(root)
    try:
        paths = ProjectPaths(root)
        config = load_config(paths, seed=False)
        executor = executor_for(paths, config, load_providers(config.providers))(provider_name)
    except (OSError, ValueError, KeyError, TypeError, yaml.YAMLError) as exc:
        # Unreadable or malformed configuration: read nothing rather than
        # raise out of a reader polling a live agent. Anything else is a bug.
        executor = None
        report = (str(root), f"{type(exc).__name__}: {exc}")
        if report not in _unreadable:
            _unreadable.add(report)
            print(f"multiagents: cannot read the configuration of {root} "
                  f"({report[1]}); reading transcripts as if on the host",
                  file=sys.stderr)
    _at[key] = (root, stamp, now, executor)
    _at.move_to_end(key)
    while len(_at) > _AT_LIMIT:
        _at.popitem(last=False)
    return executor
