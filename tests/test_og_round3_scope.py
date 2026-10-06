"""OG round 3, spend-cap scope (spec, "Decisions after implementer ag-503821").

Code that ACTS on a recorded provider maps it through the rename declaration,
spend-cap scoping included: a running node recorded under `opencode` draws on
an `opencode-go` cap and is stopped by its crossing. Placeholders only.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent / "support"))
import c3_harness as h  # noqa: E402
import og_support as og  # noqa: E402
from og_support import GO_MODEL, NEW, OLD, Project  # noqa: E402

_AGENTS = ("agents:\n  helper:\n    description: a placeholder helper\n"
           f"    provider: {NEW}\n    model: {GO_MODEL}\n")


def _past(node_id: str, status: str) -> dict:
    return {"id": node_id, "agent": "helper", "provider": OLD, "model": GO_MODEL,
            "parent": None, "depth": 1, "status": status, "task": "placeholder task",
            "started_at": 1.0, "usage": {"total": 1000, "cost_usd": 0.25}}


@pytest.fixture
def world(tmp_path, monkeypatch):
    project = Project(tmp_path)
    project.write("agents.yaml", _AGENTS)
    og.write_tree_json(project.root / ".multiagents" / "tree.json", {
        "version": 1,
        "nodes": {"ag-live01": _past("ag-live01", "running"),
                  "ag-done01": _past("ag-done01", "done")},
        "provider_health": {}, "pause": {}, "deferred": [], "questions": [], "tickets": [],
    })
    h.as_root(monkeypatch)
    from multiagents import config as config_mod
    from multiagents.runner import Runner
    return Runner(project.paths, config_mod.load(project.paths))


def test_og_r3_scope_a_running_old_recording_is_in_scope_for_the_new_cap(world):
    assert world.tree.get("ag-live01").provider == OLD          # history unrewritten
    assert world._scope_agents(NEW, "") == ["ag-live01"]


def test_og_r3_scope_a_model_cap_on_the_new_name_reaches_the_old_recording(world):
    assert world._scope_agents(NEW, GO_MODEL) == ["ag-live01"]
    assert world._scope_agents(NEW, "some-other-model") == []
