"""Shared fixtures for the D3 (CLI dependency manifest) tests.

The contract (context/specs/d3-cli-dependency-manifest.md, rev 2) fixes
behaviours and Python names (`lint`, `probe`, `integration_digest`,
`exec_in_running`) but NOT the module they live in, nor how `lint` is pointed
at a fixture tree. Both are ASSUMPTIONS, gathered here so that a correction is
a one-line change:

* MODULE: the module exposing ``lint``, ``probe``, ``integration_digest``,
  ``LintFinding`` and ``ProbeResult``.
* ``lint(root=<repo root>)``: an optional keyword naming the tree that plays
  the role of "the repository": ``<root>/src/multiagents/defaults/`` holds the
  shipped ``providers.yaml`` and ``providers/``. With no argument it lints the
  real shipped tree. References are repo-relative to ``root``.
"""

from __future__ import annotations

import importlib
import os
import platform
import stat
import subprocess
from pathlib import Path

import yaml

MODULE = "multiagents.manifest"          # ASSUMPTION (see above)
DEFAULTS = Path("src/multiagents/defaults")
REPO = Path(__file__).resolve().parents[2]
SHIPPED = REPO / DEFAULTS
PROVIDERS_REL = "src/multiagents/defaults/providers"


def api():
    return importlib.import_module(MODULE)


def host_platform() -> str:
    """e.g. ``linux-x86_64``, the contract's example format."""
    return f"{platform.system().lower()}-{platform.machine()}"


# ----------------------------------------------------------------- lint --

def base_manifest(provider: str = "fake", **over) -> dict:
    doc = {
        "schema": 1,
        "provider": provider,
        "binary": {"name": "fakecli", "version_command": ["--version"],
                   "version_regex": r"(\d+\.\d+\.\d+)",
                   "version_stream": "stdout"},
        "dependencies": [{
            "id": "flag.go", "kind": "flag", "value": "go", "match": "exact",
            "used_by": [f"providers.yaml#/{provider}/notes"],
        }],
        "verified": [],
    }
    doc.update(over)
    return doc


def dep(id_: str, value: str, ref, kind: str | None = None, match: str | None = None) -> dict:
    d = {"id": id_, "kind": kind or id_.split(".")[0], "value": value,
         "used_by": ref if isinstance(ref, list) else [ref]}
    if match is not None:
        d["match"] = match
    return d


def make_repo(root: Path, *, entry: dict | None = None, manifest: dict | str | None = None,
              files: dict[str, str] | None = None, provider: str = "fake") -> Path:
    """A miniature repository. The default provider entry has no leaf that the
    reverse coverage selectors care about, so only the forward check speaks."""
    defaults = root / DEFAULTS
    (defaults / "providers").mkdir(parents=True, exist_ok=True)
    entry = {"bin": "fakecli", "notes": "go"} if entry is None else entry
    (defaults / "providers.yaml").write_text(
        yaml.safe_dump({"providers": {provider: entry}}))
    if manifest is None:
        manifest = base_manifest(provider)
    text = manifest if isinstance(manifest, str) else yaml.safe_dump(manifest, sort_keys=False)
    (defaults / "providers" / f"{provider}.dependencies.yaml").write_text(text)
    for rel, body in (files or {}).items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body)
    return root


def run_lint(root: Path):
    return api().lint(root=root)


def codes(findings, provider: str | None = None) -> list[str]:
    return sorted(f.code for f in findings if provider is None or f.provider == provider)


# ---------------------------------------------------------- probe/doctor --

def write_cli(directory: Path, name: str = "fakecli", body: str | None = None) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text("#!/bin/sh\n" + (body if body is not None else
                                     'echo "fakecli 1.2.3"\n'))
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def make_project(tmp_path: Path, monkeypatch, *, provider: str = "fakecli",
                 bin_path: Path | None = None, enabled: bool = True,
                 project_yaml: dict | None = None, agents: dict | None = None,
                 extra_entry: dict | None = None, providers_extra: dict | None = None):
    """A real git project with a project-layer providers.yaml defining one
    provider whose binary is `bin_path`. Global config is redirected too."""
    from multiagents.paths import ProjectPaths
    root = tmp_path / "project"
    root.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e.invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e.invalid"}
    for args in (["init"], ["commit", "--allow-empty", "-m", "init"]):
        subprocess.run(["git", "-C", str(root), *args], capture_output=True,
                       env=env, check=True)
    gdir = tmp_path / "global"
    gdir.mkdir(exist_ok=True)
    monkeypatch.setenv("MULTIAGENTS_CONFIG_DIR", str(gdir))
    monkeypatch.chdir(root)
    paths = ProjectPaths(root)
    paths.ensure()
    cfg = paths.config
    (cfg / "providers").mkdir(parents=True, exist_ok=True)
    entry = {"bin": str(bin_path) if bin_path else "no-such-cli-d3",
             "spawn": {"args": ["go"]}, "enabled": enabled}
    entry.update(extra_entry or {})
    providers = {provider: entry, **(providers_extra or {})}
    (cfg / "providers.yaml").write_text(yaml.safe_dump({"providers": providers}))
    _restrict_config(monkeypatch, {provider, *(providers_extra or {})})
    if project_yaml:
        (cfg / "project.yaml").write_text(yaml.safe_dump(project_yaml))
    if agents:
        (cfg / "agents.yaml").write_text(yaml.safe_dump({"agents": agents}))
    return paths, gdir


def _restrict_config(monkeypatch, keep: set[str]) -> None:
    """Hermetic doctor/probe: only the test's providers exist, so the real
    machine's claude/codex/opencode/agy are never probed or authenticated."""
    import multiagents.cli as cli
    import multiagents.config as config_mod
    real = config_mod.load

    def load(paths=None, *a, **kw):
        cfg = real(paths, *a, **kw)
        cfg.providers = {k: v for k, v in cfg.providers.items() if k in keep}
        cfg.agents = {k: v for k, v in cfg.agents.items() if v.provider in keep}
        return cfg
    monkeypatch.setattr(config_mod, "load", load)
    monkeypatch.setattr(cli, "load_config", load)


def project_manifest_path(paths, provider: str = "fakecli") -> Path:
    return paths.config / "providers" / f"{provider}.dependencies.yaml"


def global_manifest_path(gdir: Path, provider: str = "fakecli") -> Path:
    (gdir / "providers").mkdir(parents=True, exist_ok=True)
    return gdir / "providers" / f"{provider}.dependencies.yaml"


def runtime_manifest(provider: str = "fakecli", **over) -> dict:
    doc = base_manifest(provider)
    doc["dependencies"] = [
        dep("flag.go", "go", f"providers.yaml#/{provider}/spawn/args/0"),
        dep("flag.stop", "stop", f"providers.yaml#/{provider}/spawn/args/0"),
    ]
    doc.update(over)
    return doc


def write_yaml(path: Path, doc) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(doc if isinstance(doc, str) else yaml.safe_dump(doc, sort_keys=False))
    return path


def verified_entry(digests: dict, **over) -> dict:
    entry = {"version": "1.2.3", "date": "2026-09-30", "platform": host_platform(),
             "executor": "local", "evidence": "real run", "scope": "all",
             "integration_digest": digests["integration_digest"],
             "dependencies_digest": digests["dependencies_digest"]}
    entry.update(over)
    return entry


def paths_of(tmp_path):
    from multiagents.paths import ProjectPaths
    return ProjectPaths(tmp_path / "project")


def doctor(paths_root: Path, capsys):
    import argparse

    import multiagents.cli as cli
    rc = cli.cmd_doctor(argparse.Namespace(path=str(paths_root), clear=None, force=False))
    return rc, capsys.readouterr().out


def section(output: str, title: str) -> list[str]:
    """Lines of one top-level doctor section (header excluded)."""
    lines = output.splitlines()
    if title not in lines:
        return []
    out = []
    for line in lines[lines.index(title) + 1:]:
        if line and not line[0].isspace():
            break
        out.append(line)
    return out
