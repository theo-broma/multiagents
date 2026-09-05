"""Three-layer configuration.

``shipped defaults`` (in the package)  →  ``~/.config/multiagents/``  →  ``<project>/.multiagents/config/``

Each layer overrides the one before it, key by key. Maps deep-merge, so a
project can retune one agent's model without restating the roster; lists and
scalars replace wholesale, because a partially-overridden list is never what
anyone means.

The global layer is seeded from the package on first use and the project layer
from the global one at ``multiagents init``, so both are real editable files
rather than invisible built-ins.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .paths import ProjectPaths, global_config_dir, shipped_defaults_dir

CONFIG_FILES = ("project.yaml", "providers.yaml", "agents.yaml", "models.yaml")


def deep_merge(base: dict, override: dict) -> dict:
    """Merge `override` onto `base`. Maps recurse; everything else replaces."""
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _read_yaml(path: Path) -> dict:
    if not path.is_file():
        return {}
    with path.open() as handle:
        return yaml.safe_load(handle) or {}


def seed_global(force: bool = False) -> Path:
    """Copy the package's shipped defaults into the global config dir."""
    target = global_config_dir()
    target.mkdir(parents=True, exist_ok=True)
    (target / "agents").mkdir(exist_ok=True)
    source = shipped_defaults_dir()

    for name in CONFIG_FILES:
        src = source / name
        if src.is_file() and (force or not (target / name).is_file()):
            shutil.copy2(src, target / name)
    for src in sorted((source / "agents").glob("*.md")):
        dst = target / "agents" / src.name
        if force or not dst.is_file():
            shutil.copy2(src, dst)
    return target


def seed_project(paths: ProjectPaths, force: bool = False) -> Path:
    """Copy the global config into a project so it can be edited locally."""
    seed_global()
    source, target = global_config_dir(), paths.config
    target.mkdir(parents=True, exist_ok=True)
    (target / "agents").mkdir(exist_ok=True)

    for name in CONFIG_FILES:
        src = source / name
        if src.is_file() and (force or not (target / name).is_file()):
            shutil.copy2(src, target / name)
    for src in sorted((source / "agents").glob("*.md")):
        dst = target / "agents" / src.name
        if force or not dst.is_file():
            shutil.copy2(src, dst)
    return target


@dataclass
class AgentSpec:
    """One entry from ``agents.yaml``."""

    name: str
    provider: str
    model: str
    instructions: str = ""
    description: str = ""
    effort: str | None = None
    permission: str = "full"          # full | sandbox | readonly
    can_spawn: bool = False
    max_children: int = 2
    timeout: int = 900                # wall-clock seconds
    silence_timeout: int = 180        # seconds with no stream event
    max_steps: int = 120
    writes: bool = True               # False -> no worktree, runs read-only in project
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, name: str, data: dict) -> AgentSpec:
        known = {f for f in cls.__dataclass_fields__ if f != "extra"}
        kwargs = {k: v for k, v in data.items() if k in known}
        extra = {k: v for k, v in data.items() if k not in known}
        return cls(name=name, extra=extra, **{k: v for k, v in kwargs.items() if k != "name"})


@dataclass
class Config:
    project: dict[str, Any]
    providers: dict[str, Any]
    agents: dict[str, AgentSpec]
    models: dict[str, Any]
    instruction_dirs: list[Path]

    # --- convenience accessors, all with defaults so a sparse config works ---

    @property
    def remote(self) -> str:
        return self.project.get("git", {}).get("remote", "") or ""

    @property
    def base_branch(self) -> str:
        return self.project.get("git", {}).get("base_branch", "") or ""

    @property
    def branch_prefix(self) -> str:
        return self.project.get("git", {}).get("branch_prefix", "agents") or "agents"

    @property
    def push_agent_branches(self) -> bool:
        return bool(self.project.get("git", {}).get("push_agent_branches", False))

    @property
    def executor(self) -> str:
        return self.project.get("executor", {}).get("kind", "local")

    @property
    def env_passthrough(self) -> list[str]:
        return list(self.project.get("security", {}).get("env_passthrough", []))

    @property
    def env_block(self) -> list[str]:
        return list(self.project.get("security", {}).get("env_block", []))

    @property
    def home_policy(self) -> str:
        return self.project.get("security", {}).get("home_policy", "per-agent")

    @property
    def limits(self) -> dict[str, Any]:
        return self.project.get("limits", {})

    def agent(self, name: str) -> AgentSpec:
        if name not in self.agents:
            raise KeyError(f"No agent named {name!r}. Configured: {sorted(self.agents)}")
        return self.agents[name]

    def instructions_for(self, spec: AgentSpec) -> str:
        """Resolve an agent's ``.md`` file across the config layers.

        Project instructions win over global ones, so you can rewrite a shipped
        agent's brief without touching the machine-wide copy.
        """
        if not spec.instructions:
            return ""
        candidate = Path(spec.instructions).expanduser()
        if candidate.is_absolute() and candidate.is_file():
            return candidate.read_text()
        for base in self.instruction_dirs:
            path = base / spec.instructions
            if path.is_file():
                return path.read_text()
        return ""


def load(paths: ProjectPaths | None) -> Config:
    """Load the merged configuration for a project (or the global one alone)."""
    seed_global()
    layers: list[Path] = [shipped_defaults_dir(), global_config_dir()]
    if paths is not None and paths.config.is_dir():
        layers.append(paths.config)

    merged: dict[str, dict] = {name: {} for name in CONFIG_FILES}
    for layer in layers:
        for name in CONFIG_FILES:
            merged[name] = deep_merge(merged[name], _read_yaml(layer / name))

    agents_raw = merged["agents.yaml"].get("agents", {}) or {}
    agents = {
        name: AgentSpec.from_dict(name, data or {})
        for name, data in agents_raw.items()
        if not (data or {}).get("disabled")
    }

    return Config(
        project=merged["project.yaml"],
        providers=merged["providers.yaml"].get("providers", {}) or {},
        agents=agents,
        models=merged["models.yaml"].get("models", {}) or {},
        instruction_dirs=[layer / "agents" for layer in reversed(layers)],
    )
