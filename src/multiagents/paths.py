"""Where everything lives on disk.

Three roots, deliberately separate:

* **global config** — ``~/.config/multiagents/``, the editable defaults shared by
  every project. Seeded from the package's shipped ``defaults/`` on first use.
* **project data** — ``<project>/.multiagents/``, holding this project's config
  overrides, its agent tree, and its logs. Small, inspectable, gitignored.
* **worktrees** — ``~/.multiagents/worktrees/<project-slug>/<agent-id>/``, kept
  *outside* the project so a subagent's file search cannot reach a sibling
  agent's checkout, and so worktrees never nest inside one another.

Per-agent HOMEs live beside the worktrees for the same reason.
"""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

PROJECT_DIR_NAME = ".multiagents"


def _xdg(var: str, default: str) -> Path:
    raw = os.environ.get(var)
    return Path(raw).expanduser() if raw else Path.home() / default


def global_config_dir() -> Path:
    """Editable machine-wide defaults."""
    env = os.environ.get("MULTIAGENTS_CONFIG_DIR")
    if env:
        return Path(env).expanduser()
    return _xdg("XDG_CONFIG_HOME", ".config") / "multiagents"


def state_root() -> Path:
    """Machine-wide state: worktrees and per-agent homes."""
    env = os.environ.get("MULTIAGENTS_STATE_DIR")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".multiagents"


def shipped_defaults_dir() -> Path:
    """The package's own read-only defaults, used to seed the global config."""
    return Path(__file__).parent / "defaults"


def find_project_root(start: Path | None = None) -> Path | None:
    """Walk up from `start` looking for an initialised project."""
    cur = (start or Path.cwd()).resolve()
    for candidate in [cur, *cur.parents]:
        if (candidate / PROJECT_DIR_NAME).is_dir():
            return candidate
    return None


def project_slug(project_root: Path) -> str:
    """Stable, readable, collision-resistant id for a project directory.

    Name plus a hash of the absolute path, so two checkouts of the same repo in
    different directories get their own worktree namespaces.
    """
    resolved = project_root.resolve()
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", resolved.name).strip("-") or "project"
    digest = hashlib.sha256(str(resolved).encode()).hexdigest()[:8]
    return f"{name}-{digest}"


class ProjectPaths:
    """Every path derived from one project root."""

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.slug = project_slug(self.root)

    @property
    def data(self) -> Path:
        return self.root / PROJECT_DIR_NAME

    @property
    def config(self) -> Path:
        return self.data / "config"

    @property
    def agents_config(self) -> Path:
        return self.config / "agents"

    @property
    def tree_file(self) -> Path:
        return self.data / "tree.json"

    @property
    def events_file(self) -> Path:
        return self.data / "events.jsonl"

    @property
    def runs(self) -> Path:
        return self.data / "runs"

    def run_dir(self, agent_id: str) -> Path:
        return self.runs / agent_id

    @property
    def worktrees(self) -> Path:
        return state_root() / "worktrees" / self.slug

    def worktree(self, agent_id: str) -> Path:
        return self.worktrees / agent_id

    @property
    def homes(self) -> Path:
        return state_root() / "homes" / self.slug

    def home(self, agent_id: str) -> Path:
        return self.homes / agent_id

    def ensure(self) -> None:
        """Create the project's own directories. 0700 — these hold logs and
        state that should not be world-readable."""
        for path in (self.data, self.config, self.agents_config, self.runs):
            path.mkdir(parents=True, exist_ok=True)
        for path in (self.worktrees, self.homes):
            path.mkdir(parents=True, exist_ok=True)
        try:
            self.data.chmod(0o700)
            state_root().chmod(0o700)
        except OSError:
            pass
