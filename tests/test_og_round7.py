"""OG round 7 (review ag-c280ef): the runner reads a recorded provider through
the rename where it becomes a route.

A node keeps the provider name it was written under. In the consult and steer
paths that name was used raw before `_spec_of` was reached, so a standing
conversation recorded under the pre-rename name looked dropped by the roster
and was replaced, its consult bypassed the route's `max_concurrent` and queue,
and a steer was refused for a provider with no model. And
`quota_handover.reserved_instance` naming the old name pointed at the
non-routable base instead of the route.

(a)-(c) use a test-only rename declaration on invented providers; (d) uses the
shipped declaration, as the other OG-R2 config tests do. Placeholders only.
"""
from __future__ import annotations

import asyncio
import json
import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import c3_harness as h  # noqa: E402
import og_support as og  # noqa: E402
from multiagents.config import AgentSpec  # noqa: E402
from multiagents.renames import Renames  # noqa: E402
from multiagents.runner import ProviderFull  # noqa: E402
from multiagents.tree import PC_CAUSE, Node  # noqa: E402

OLD = "placeholder-old"
NEW = "placeholder-new"
SESSION = "sess-placeholder-old"
NODE = "ag-0ld7a1"

# A fake CLI that records its argv and answers once with a session of its own.
_CLI = """#!/bin/sh
probe="@PROBE@"
n=$(ls "$probe" | wc -l)
printf '%s\\n' "$@" > "$probe/.part"
mv "$probe/.part" "$probe/call-$n.argv"
printf '%s\\n' '{"type":"text","text":"answered","session":"@SESSION@"}'
exit 0
"""


def _fake_cli(tmp_path: Path, name: str) -> tuple[dict, Path]:
    probe = tmp_path / f"probe-{name}"
    probe.mkdir()
    script = tmp_path / f"{name}-cli"
    script.write_text(_CLI.replace("@PROBE@", str(probe)).replace("@SESSION@", SESSION))
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return {
        "bin": str(script),
        "spawn": {"args": ["--provider", name, "--model", "{model}", "--cwd", "{workdir}"],
                  "resume": ["--resume", "{session_id}"]},
        "stream": {"format": "ndjson", "session_id_paths": ["session"],
                   "rules": [{"match": {"type": "text"}, "as": "text",
                              "fields": {"text": "text"}}]},
    }, probe


def _calls(probe: Path) -> list[list[str]]:
    files = sorted(probe.glob("call-*.argv"), key=lambda p: int(p.stem.split("-")[1]))
    return [f.read_text().split("\n")[:-1] for f in files]


def _events(runner, kind):
    path = runner.paths.events_file
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if entry.get("kind") == kind:
            out.append(entry)
    return out


class Renamed:
    """An agent configured on the route `NEW`, and a node recorded before the
    rename under `OLD`, whose block is left behind as a non-routable CLI base."""

    def __init__(self, tmp_path, monkeypatch, *, conversational=False, limit=None):
        new, self.new_probe = _fake_cli(tmp_path, NEW)
        old, self.old_probe = _fake_cli(tmp_path, OLD)
        old["routable"] = False
        if limit is not None:
            new["max_concurrent"] = limit
        spec = AgentSpec("advisor", NEW, "model-a", conversational=conversational)
        self.runner = h.make_runner(tmp_path / "proj", monkeypatch,
                                    agents={"advisor": spec},
                                    providers={NEW: new, OLD: old})
        monkeypatch.setattr(self.runner.tree, "renames",
                            Renames(aliases={OLD: NEW}, unroutable=frozenset()))
        self.tmp = tmp_path

    def add_node(self, node_id=NODE, status="idle", **fields):
        worktree = self.runner.paths.worktree(node_id)
        worktree.mkdir(parents=True)
        self.runner.tree.add(Node(
            id=node_id, agent="advisor", provider=OLD, model="model-a", parent=None,
            depth=1, status=status, session_id=SESSION, worktree=str(worktree), **fields))
        return self.runner.tree.get(node_id)


# (a) a standing conversation recorded under the old name ---------------------

def test_og_r7a_an_old_name_conversation_is_on_a_route(tmp_path, monkeypatch):
    w = Renamed(tmp_path, monkeypatch, conversational=True)
    node = w.add_node(conversation=True, turns=1)
    routed = w.runner._conversation_route(w.runner.config.agent("advisor"), node)
    assert routed is not None
    assert routed.provider == NEW


def test_og_r7a_an_old_name_conversation_resumes_on_the_route_and_is_not_replaced(
        tmp_path, monkeypatch):
    w = Renamed(tmp_path, monkeypatch, conversational=True)
    w.add_node(conversation=True, turns=1)

    result = asyncio.run(w.runner.consult("advisor", "next question", timeout=60))

    assert not result.get("error"), result
    assert "conversation_replaced" not in result, result
    assert _events(w.runner, "conversation_replaced") == []
    assert result.get("agent_id") == NODE, result
    assert not _calls(w.old_probe)
    [argv] = _calls(w.new_probe)
    assert argv[argv.index("--provider") + 1] == NEW
    assert argv[argv.index("--resume") + 1] == SESSION
    assert w.runner.tree.get(NODE).status != "cancelled"


# (b) admission of an old-name consult counts the route's holders and queue ---

def test_og_r7b_an_old_name_admission_counts_the_routes_holders(tmp_path, monkeypatch):
    w = Renamed(tmp_path, monkeypatch, limit=1)
    w.add_node("ag-h0ld01", status="running")          # holds the route's one slot
    data = w.runner.tree.read()

    full = w.runner._pc_admit(data, OLD, exclude=NODE)

    assert isinstance(full, ProviderFull)
    assert full.holders == ["ag-h0ld01"]
    assert full.limit == 1
    assert not full.gone


def test_og_r7b_an_old_name_admission_waits_behind_the_routes_queue(tmp_path, monkeypatch):
    w = Renamed(tmp_path, monkeypatch, limit=1)
    first = w.runner.tree.enqueue(NEW, {"agent": "advisor"}, PC_CAUSE)
    data = w.runner.tree.read()

    full = w.runner._pc_admit(data, OLD, exclude=NODE)

    assert isinstance(full, ProviderFull)
    assert full.ahead == 1
    # A queued entry drained for an old-name run is found, not reported gone.
    second = w.runner.tree.enqueue(NEW, {"agent": "advisor"}, PC_CAUSE)
    data = w.runner.tree.read()
    full = w.runner._pc_admit(data, OLD, exclude=NODE, queued_id=second["id"])
    assert isinstance(full, ProviderFull) and not full.gone
    assert full.ahead == 1
    assert w.runner._pc_admit(data, OLD, exclude=NODE, queued_id=first["id"]) is None


# (c) the steer pre-check ---------------------------------------------------------

def test_og_r7c_the_steer_precheck_accepts_an_old_name_node(tmp_path, monkeypatch):
    w = Renamed(tmp_path, monkeypatch)
    w.add_node(status="done")

    result = asyncio.run(w.runner.steer(NODE, "carry on"))

    assert "has no model for provider" not in str(result.get("error") or ""), result
    assert result.get("steered") is True, result
    assert not _calls(w.old_probe)


# (d) quota_handover.reserved_instance naming the old name ------------------------

def test_og_r7d_reserved_instance_old_name_is_the_route_with_one_warning(
        tmp_path, capsys, caplog):
    p = og.Project(tmp_path)
    project = p.write("project.yaml",
                      f"quota_handover:\n  reserved_instance: {og.OLD}\n")
    cfg, text = og.load(p, capsys, caplog)
    assert cfg.project["quota_handover"]["reserved_instance"] == og.NEW
    [warning] = og.deprecations(text, about=project)
    assert og.named_line(warning, project) == og.line_of(project, "reserved_instance")


def test_og_r7d_reserved_instance_new_name_is_silent(tmp_path, capsys, caplog):
    p = og.Project(tmp_path)
    project = p.write("project.yaml",
                      f"quota_handover:\n  reserved_instance: {og.NEW}\n")
    cfg, text = og.load(p, capsys, caplog)
    assert cfg.project["quota_handover"]["reserved_instance"] == og.NEW
    assert og.deprecations(text, about=project) == []

