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

import hashlib
import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .paths import ProjectPaths, global_config_dir, shipped_defaults_dir

CONFIG_FILES = ("project.yaml", "providers.yaml", "agents.yaml", "models.yaml")
# Kept for installs that still carry a top-level orchestrator.md from before it
# became a normal agent brief under agents/.
STANDALONE_FILES = ()


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



# Copies of the shipped defaults are pinned in the global and project layers so
# they can be edited. That has a cost: a pinned copy overrides the newer shipped
# file for every key, including ones the user never touched, so improvements to
# the defaults silently never reach an existing install.
#
# The fix is to know which copies were edited. A manifest records the hash of
# each file as written; a copy still matching its hash was never touched and can
# be refreshed safely, while an edited one is left alone and merely reported.

MANIFEST = ".seeded.json"


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_manifest(target: Path) -> dict:
    path = target / MANIFEST
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _write_manifest(target: Path, data: dict) -> None:
    (target / MANIFEST).write_text(json.dumps(data, indent=2, sort_keys=True))


def layer_files(source: Path, scope: str = "global") -> list[str]:
    """Relative names a layer should carry.

    The project layer deliberately carries less. Auth scripts and the
    orchestrator prompt are machine-level, and a stale per-project copy of
    either would be a liability rather than a convenience; they still resolve
    through the global layer, and a project may add its own if it wants.
    """
    names = [n for n in CONFIG_FILES if (source / n).is_file()]
    names += [f"agents/{p.name}" for p in sorted((source / "agents").glob("*.md"))]
    if scope == "global":
        names += [n for n in STANDALONE_FILES if (source / n).is_file()]
        names += [f"providers/{p.name}" for p in sorted((source / "providers").glob("*"))
                  if p.is_file()]
    return names


def sync_layer(source: Path, target: Path, force: bool = False,
               dry_run: bool = False, scope: str = "global") -> dict[str, list[str]]:
    """Copy shipped files into a layer, refreshing only untouched copies."""
    report: dict[str, list[str]] = {"added": [], "updated": [], "customised": []}
    manifest = _read_manifest(target)

    for name in layer_files(source, scope):
        src, dst = source / name, target / name
        shipped = _digest(src)
        if not dst.is_file():
            report["added"].append(name)
            if not dry_run:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                manifest[name] = shipped
            continue
        if _digest(dst) == shipped:
            manifest.setdefault(name, shipped)
            continue                                  # already current
        untouched = manifest.get(name) == _digest(dst)
        if untouched or force:
            report["updated"].append(name)
            if not dry_run:
                # Forcing over an EDITED file destroys work the user did by
                # hand. Keep a copy — the first --force in this codebase silently
                # reverted a project from the docker executor back to local.
                if not untouched:
                    backup = dst.with_suffix(dst.suffix + ".bak")
                    shutil.copy2(dst, backup)
                    report.setdefault("backed_up", []).append(str(backup))
                shutil.copy2(src, dst)
                manifest[name] = shipped
        else:
            report["customised"].append(name)

    if not dry_run:
        _write_manifest(target, manifest)
    return report


def seed_global(force: bool = False) -> Path:
    """Copy the package's shipped defaults into the global config dir."""
    target = global_config_dir()
    for sub in ("", "agents", "providers"):
        (target / sub).mkdir(parents=True, exist_ok=True)
    sync_layer(shipped_defaults_dir(), target, force=force)
    return target


def seed_project(paths: ProjectPaths, force: bool = False) -> Path:
    """Copy the global config into a project so it can be edited locally."""
    seed_global()
    target = paths.config
    for sub in ("", "agents"):
        (target / sub).mkdir(parents=True, exist_ok=True)
    sync_layer(global_config_dir(), target, force=force, scope="project")
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
    max_steps: int = 0                # 0 = use limits.max_steps
    writes: bool = True               # False -> branch dropped if it stays empty
    conversational: bool = False      # talked to via consult(), keeps context
    executor: str = ""                # "" = project default; else local | docker
    # LAUNCHED as an MCP client rather than spawned as a subagent. Real fields
    # rather than `extra` keys, because extras are coerced into command options
    # and `isinstance(True, int)` is True.
    # Per-provider fallback models: {provider: model_id}. A model id belongs to
    # its provider's namespace, so failing over without one would run
    # `agy --model opencode-go/glm-5.3-flash`. Named here, an agent can move to
    # another provider when its own is exhausted; without one it waits instead.
    # {provider: model_id} or {provider: {model: ..., effort: ...}}. The second
    # form exists because a model id is not the only thing that belongs to a
    # provider's namespace: `effort` does too. An agent pinned effort:high
    # failed over to another vendor, kept its effort, and the CLI refused the
    # combination in eight seconds — "--effort is not supported for model
    # claude-opus-4-6-thinking". The move was right and the options came with
    # it uninvited.
    models: dict[str, Any] = field(default_factory=dict)
    launch: bool = False
    # Which command launches it: "orchestrator" for `run`, "initializer" for
    # `init-agent`. Both are launch: true; the role says which door they use.
    role: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def fallback_for(self, provider: str) -> tuple[str, dict[str, Any]]:
        """What this agent becomes on another provider: `(model, overrides)`.

        A bare string names the model and says nothing about options, so the
        options it was configured with travel unchanged — deliberately, because
        silently dropping `effort: high` from an agent that exists BECAUSE it
        reasons deeply is a slow, quiet failure, and a refused flag is a loud
        one that takes eight seconds. Say `{model: ..., effort: ""}` to mean
        something else.
        """
        entry = (self.models or {}).get(provider)
        if isinstance(entry, dict):
            model = str(entry.get("model") or entry.get("id") or "")
            fields = set(AgentSpec.__dataclass_fields__)
            overrides = {k: v for k, v in entry.items()
                         if k in fields and k not in ("name", "model", "models")}
            return model, overrides
        return str(entry or ""), {}

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
        # The mandatory briefs are named with a leading underscore so they are
        # visibly not yours to delete. A config written before that convention
        # names them without it, and an install can carry both files at once
        # mid-upgrade — so the exact name always wins, and the other spelling
        # is only a fallback.
        names = [spec.instructions]
        stem = spec.instructions
        names.append(stem[1:] if stem.startswith("_") else "_" + stem)
        for name in names:
            for base in self.instruction_dirs:
                path = base / name
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
