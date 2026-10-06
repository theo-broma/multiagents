"""Shared helpers for the opencode-go rename contract (context/specs/opencode-go-rename.md).

Everything goes through public surfaces: a project directory with real
`.multiagents/config/*.yaml` files, `config.load`, the CLI entry point, the MCP
tool functions of `multiagents.server`, `Tree`, and the scheduler's own store.
Hostnames and paths are placeholders under `example.invalid`.

The deprecation warning (OG-R2) has no named channel in the spec: "each load
prints one warning". `warning_text` therefore reads every channel a user could
see it on — `Config.warnings`, stdout, stderr and the `logging` records — and
`deprecations` keeps the lines that are about the alias, so a test does not care
which one the implementation chose.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import c3_harness as h  # noqa: E402

OLD = "opencode"
NEW = "opencode-go"
GO_MODEL = "opencode-go/glm-5.1"
ZEN_MODEL = "opencode/space-bunny-free"
FAKE_BIN = "/opt/example.invalid/bin/opencode"


class Project:
    """A project root whose config layer a test writes as literal text, so the
    line a warning must name is known."""

    def __init__(self, tmp: Path, name: str = "proj"):
        self.root = tmp / name
        h.make_git_repo(self.root)
        self.config = self.root / ".multiagents" / "config"
        (self.config / "agents").mkdir(parents=True)

    def write(self, rel: str, text: str) -> Path:
        path = self.config / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    @property
    def paths(self):
        from multiagents.paths import ProjectPaths
        return ProjectPaths(self.root)


def line_of(path: Path, needle: str, nth: int = 1) -> int:
    """1-based line of the nth line of `path` containing `needle`."""
    seen = 0
    for number, line in enumerate(path.read_text().splitlines(), 1):
        if needle in line:
            seen += 1
            if seen == nth:
                return number
    raise AssertionError(f"{needle!r} not in {path}")


def warning_text(capsys, caplog, config=None) -> str:
    captured = capsys.readouterr()
    parts = [captured.out, captured.err, caplog.text]
    if config is not None:
        parts.append("\n".join(str(w) for w in (getattr(config, "warnings", None) or [])))
    return "\n".join(parts)


def deprecations(text: str, about: Path | None = None) -> list[str]:
    """The lines of `text` that are deprecation warnings (optionally only those
    naming the file `about`)."""
    lines = [line for line in text.splitlines()
             if re.search(r"deprecat|renamed|no longer|alias", line, re.I)
             and NEW in line]
    if about is not None:
        lines = [line for line in lines if about.name in line]
    return lines


def named_line(warning: str, file: Path) -> int:
    match = re.search(re.escape(file.name) + r":(\d+)", warning)
    assert match, f"the warning names no {file.name}:<line>: {warning!r}"
    return int(match.group(1))


def load(project: Project, capsys, caplog):
    """`config.load` of the project, and everything it printed."""
    import logging

    from multiagents import config as config_mod
    caplog.set_level(logging.DEBUG)
    capsys.readouterr()
    cfg = config_mod.load(project.paths)
    return cfg, warning_text(capsys, caplog, cfg)


def providers_of(cfg):
    from multiagents.providers import load_providers
    return load_providers(cfg.providers)


def write_tree_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=1, sort_keys=True))
