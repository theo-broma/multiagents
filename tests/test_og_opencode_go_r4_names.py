"""Black-box contract for OG-R4 of context/specs/opencode-go-rename.md.

OG-R4  names shown to the user say `opencode-go`: `doctor`, `budget_status`,
       `list_agents` (and the monitor, routing reasons and errors).

Covered here: the output of `multiagents doctor`, the MCP tools `budget_status`
and `list_agents`, and, for a project that still holds PRE-RENAME state, what
`budget_status` says once that state is loaded (OG-R3 seen from the user's side).
Not covered, see the run result: the monitor page and routing-reason text, which
the spec names without saying what triggers them.

`budget_status` runs the providers' budget readers; the test narrows them to the
opencode family (a wrapper around the real reader, not a replacement) so the
claude/agy readers never reach a real home directory.
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent / "support"))
import og_support as og  # noqa: E402
from og_support import GO_MODEL, NEW, OLD, Project  # noqa: E402

FUTURE = time.time() + 7200


def _hermetic_path(tmp_path: Path, monkeypatch) -> None:
    """A PATH holding only a fake `opencode` and the system tools: doctor and
    the budget readers must not run the developer's real agent CLIs."""
    import stat
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    fake = bindir / "opencode"
    fake.write_text('#!/bin/sh\necho "0 credentials"\n')
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PATH", f"{bindir}:/usr/bin:/bin")


def _agents(provider: str = NEW) -> str:
    return ("agents:\n  helper:\n    description: a placeholder helper\n"
            f"    provider: {provider}\n    model: {GO_MODEL}\n")


def _tree(project: Project, provider: str) -> None:
    og.write_tree_json(project.root / ".multiagents" / "tree.json", {
        "version": 1,
        "nodes": {"n1": {"id": "n1", "agent": "helper", "provider": provider,
                         "model": GO_MODEL, "parent": None, "depth": 0, "status": "done",
                         "task": "placeholder", "usage": {"total": 1200, "cost_usd": 0.4}}},
        "provider_health": {}, "pause": {}, "deferred": [], "questions": [], "tickets": [],
        "cooldowns": {provider: {"until": FUTURE, "reason": "quota window full",
                                 "cause": "quota"}},
    })


def _budget_status(project: Project, monkeypatch) -> dict:
    from multiagents import server
    real = server.budget_mod.read_all

    def only_opencode(providers, *args, **kwargs):
        return real({n: p for n, p in providers.items() if n.startswith("opencode")},
                    *args, **kwargs)

    monkeypatch.setattr(server.budget_mod, "read_all", only_opencode)
    monkeypatch.setenv("MULTIAGENTS_PROJECT", str(project.root))
    monkeypatch.chdir(project.root)
    server._reset()
    try:
        status = server.budget_status()
    finally:
        server._reset()
    return json.loads(status) if isinstance(status, str) else status


# ===========================================================================
# budget_status
# ===========================================================================

def test_og_r4_budget_status_reports_the_go_subscription_as_opencode_go(tmp_path, monkeypatch):
    _hermetic_path(tmp_path, monkeypatch)
    p = Project(tmp_path)
    p.write("project.yaml", 'team: ""\n')
    p.write("agents.yaml", _agents())
    _tree(p, NEW)
    status = _budget_status(p, monkeypatch)
    assert NEW in status["providers"], sorted(status["providers"])
    assert OLD not in status["providers"], (
        "the CLI base is not a route and has no budget row")
    rows = [r for r in status["by_model"] if r["model"] == GO_MODEL]
    assert [r["provider"] for r in rows] == [NEW], rows
    assert rows[0]["cost_usd"] == pytest.approx(0.4)
    assert any(f"{NEW} is cooling down" in line for line in status["advice"]), status["advice"]


def test_og_r4_budget_status_of_a_pre_rename_project_speaks_of_opencode_go_only(
        tmp_path, monkeypatch):
    _hermetic_path(tmp_path, monkeypatch)
    p = Project(tmp_path)
    p.write("project.yaml", 'team: ""\n')
    p.write("agents.yaml", _agents())
    _tree(p, OLD)                                  # spend and a cooldown filed under the old name
    status = _budget_status(p, monkeypatch)
    rows = [r for r in status["by_model"] if r["model"] == GO_MODEL]
    assert [r["provider"] for r in rows] == [NEW], rows
    assert rows[0]["cost_usd"] == pytest.approx(0.4) and rows[0]["runs"] == 1
    assert any(f"{NEW} is cooling down" in line for line in status["advice"]), status["advice"]
    assert not any(re.search(rf"\b{OLD} is\b", line) for line in status["advice"])
    assert OLD not in status["providers"]


# ===========================================================================
# list_agents
# ===========================================================================

def test_og_r4_list_agents_shows_opencode_go(tmp_path, monkeypatch):
    from multiagents import server
    _hermetic_path(tmp_path, monkeypatch)
    p = Project(tmp_path)
    p.write("project.yaml", 'team: ""\n')
    p.write("agents.yaml", _agents())
    monkeypatch.setenv("MULTIAGENTS_PROJECT", str(p.root))
    monkeypatch.chdir(p.root)
    server._reset()
    try:
        reply = server.list_agents()
    finally:
        server._reset()
    reply = json.loads(reply) if isinstance(reply, str) else reply
    helper = [a for a in reply["agents"] if a["name"] == "helper"]
    assert helper and helper[0]["provider"] == NEW, reply["agents"]
    assert not [a for a in reply["agents"] if a.get("provider") == OLD], reply["agents"]


# ===========================================================================
# doctor
# ===========================================================================

def _doctor(project: Project, capsys, monkeypatch) -> str:
    from multiagents import cli
    monkeypatch.chdir(project.root)
    capsys.readouterr()
    cli.main(["--path", str(project.root), "doctor"])
    captured = capsys.readouterr()
    return captured.out + captured.err


def _section(text: str, title: str) -> list[str]:
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == title)
    out = []
    for line in lines[start + 1:]:
        if line and not line.startswith(" "):
            break
        out.append(line)
    return out


def test_og_r4_doctor_lists_opencode_go_as_a_provider_and_in_auth(tmp_path, capsys, monkeypatch):
    _hermetic_path(tmp_path, monkeypatch)
    p = Project(tmp_path)
    p.write("project.yaml", 'team: ""\n')
    p.write("agents.yaml", _agents())
    text = _doctor(p, capsys, monkeypatch)
    providers = [line.split()[0] for line in _section(text, "providers")
                 if line.strip() and not line.lstrip().startswith("!")]
    assert NEW in providers, text
    auth = [line.split()[0] for line in _section(text, "auth") if line.strip()
            and not line.startswith("    ")]
    assert NEW in auth or NEW in [line.split()[1] for line in _section(text, "auth")
                                  if len(line.split()) > 1 and line.split()[0] == "!"], text


def test_og_r4_doctor_roster_shows_the_route_as_opencode_go(tmp_path, capsys, monkeypatch):
    _hermetic_path(tmp_path, monkeypatch)
    p = Project(tmp_path)
    p.write("project.yaml", 'team: ""\n')
    p.write("agents.yaml", _agents())
    text = _doctor(p, capsys, monkeypatch)
    rows = [line for line in _section(text, "agents") if " helper " in f" {line} "]
    assert len(rows) == 1, text
    assert f"{NEW}/{GO_MODEL}" in rows[0], rows[0]
    stale = [line for line in _section(text, "agents") if f"{OLD}/{OLD}-go/" in line]
    assert stale == [], f"the roster still shows the old route name: {stale[:3]}"


def test_og_r4_doctor_has_no_budget_row_for_the_cli_base(tmp_path, capsys, monkeypatch):
    _hermetic_path(tmp_path, monkeypatch)
    p = Project(tmp_path)
    p.write("project.yaml", 'team: ""\n')
    p.write("agents.yaml", _agents())
    text = _doctor(p, capsys, monkeypatch)
    rows = [line.split()[0] for line in _section(text, "budget") if line.strip()]
    assert OLD not in rows, f"`{OLD}` is not a route: {rows}"


def test_og_r4_doctor_of_an_old_config_shows_opencode_go_and_the_deprecation(
        tmp_path, capsys, monkeypatch):
    _hermetic_path(tmp_path, monkeypatch)
    p = Project(tmp_path)
    p.write("project.yaml", 'team: ""\n')
    agents = p.write("agents.yaml", _agents(OLD))
    text = _doctor(p, capsys, monkeypatch)
    assert f"{NEW}/{GO_MODEL}" in "\n".join(_section(text, "agents")), text
    found = og.deprecations(text, about=agents)
    assert len(found) == 1, text
    assert og.named_line(found[0], agents) == og.line_of(agents, f"provider: {OLD}")
