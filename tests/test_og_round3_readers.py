"""OG round 3, readers of history (orchestrator decision, recorded on main as 60c76ea).

History stays as written: a past node's `provider` keeps `opencode`. Code that
ACTS on a recorded provider — rather than only displaying it — maps it through
the rename declaration (`renamed_from` in the shipped providers.yaml). A run
recorded under `opencode` resumes, counts and is totalled on `opencode-go`,
never on the non-routable `opencode` base.

Public paths only: `Runner._spec_of` (the place the implementer names for
resume/steer), `Runner.provider_slots`, and the monitor snapshot's
`spend_by_provider`. Placeholders only.
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
# A cap, so the per-provider slot count is observable.
_PROVIDERS = f"providers:\n  {NEW}:\n    max_concurrent: 3\n"


def _past(node_id: str, status: str, **extra) -> dict:
    return {"id": node_id, "agent": "helper", "provider": OLD, "model": GO_MODEL,
            "parent": None, "depth": 1, "status": status, "task": "placeholder task",
            "usage": {"total": 1000, "cost_usd": 0.25}, **extra}


@pytest.fixture
def world(tmp_path, monkeypatch):
    project = Project(tmp_path)
    project.write("agents.yaml", _AGENTS)
    project.write("providers.yaml", _PROVIDERS)
    og.write_tree_json(project.root / ".multiagents" / "tree.json", {
        "version": 1,
        "nodes": {
            "ag-conv01": _past("ag-conv01", "idle", session_id="sess-old",
                               conversation=True, turns=1),
            "ag-live01": _past("ag-live01", "running", started_at=1.0),
            "ag-done01": _past("ag-done01", "done"),
        },
        "provider_health": {}, "pause": {}, "deferred": [], "questions": [], "tickets": [],
    })
    h.as_root(monkeypatch)
    from multiagents import config as config_mod
    from multiagents.runner import Runner
    runner = Runner(project.paths, config_mod.load(project.paths))
    return runner


def test_og_r3_readers_a_node_recorded_under_the_old_name_keeps_it(world):
    """The fixture is a faithful pre-rename history: it is not rewritten."""
    assert world.tree.get("ag-conv01").provider == OLD


def test_og_r3_readers_resume_of_an_old_recording_resolves_to_opencode_go(world):
    node = world.tree.get("ag-conv01")
    spec, provider = world._spec_of(node)
    assert provider.name == NEW
    assert spec.provider == NEW


def test_og_r3_readers_the_old_recording_never_resolves_to_the_unroutable_base(world):
    spec, provider = world._spec_of(world.tree.get("ag-conv01"))
    assert provider.name != OLD and spec.provider != OLD


def test_og_r3_readers_runner_slot_count_reads_an_old_recording_as_opencode_go(world):
    slots = world.provider_slots()
    assert OLD not in slots
    assert slots[NEW]["in_use"] == 1                    # the one running node
    assert slots[NEW]["max_concurrent"] == 3


def test_og_r3_readers_monitor_spend_totals_an_old_recording_under_opencode_go(world):
    from multiagents.monitor import snapshot
    spend = snapshot.spend_by_provider(world.tree)
    assert OLD not in spend
    assert spend[NEW]["total"] == 3000
    assert spend[NEW]["cost_usd"] == pytest.approx(0.75)
