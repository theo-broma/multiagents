"""OG round 4 (review ag-02feb0; spec, "Decisions after implementer ag-503821").

Code that ACTS on a recorded provider maps it through the rename declaration,
and the record stays as written. Each test here is red without its fix:

1. a charge held in `spend_pending` under the old name is on the new name's
   cap check;
2. a spend-cap crossing recorded under the old name is the new name's
   crossing: it stops an adopted run and is never claimed a second time;
3. with two declared renames, a deferred task moves to ITS route only;
4. a provider's family reads through the rename, so a node recorded under the
   old name keeps its session resume on the new one.

Placeholders only.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent / "support"))
import c3_harness as h  # noqa: E402
import og_support as og  # noqa: E402
from og_support import GO_MODEL, NEW, OLD, Project  # noqa: E402

_AGENTS = ("agents:\n  helper:\n    description: a placeholder helper\n"
           f"    provider: {NEW}\n    model: {GO_MODEL}\n")


@pytest.fixture
def world(tmp_path, monkeypatch):
    project = Project(tmp_path)
    project.write("agents.yaml", _AGENTS)
    og.write_tree_json(project.root / ".multiagents" / "tree.json", {
        "version": 1, "nodes": {},
        "provider_health": {}, "pause": {}, "deferred": [], "questions": [], "tickets": [],
        "spend_pending": [{"key": "placeholder-key", "usd": 0.5, "at": 1.0,
                           "provider": OLD, "model": GO_MODEL, "agent": "helper",
                           "node": "ag-held01"}],
    })
    h.as_root(monkeypatch)
    from multiagents import config as config_mod
    from multiagents.runner import Runner
    return Runner(project.paths, config_mod.load(project.paths))


# 1. spend_pending ---------------------------------------------------------

def test_og_r4_a_charge_held_under_the_old_name_is_pending_on_the_new(world):
    held = world._pending(provider=NEW, fresh=True)
    assert [e["key"] for e in held] == ["placeholder-key"]
    # The held record keeps the name it was written under.
    assert world.tree.read()["spend_pending"][0]["provider"] == OLD


def test_og_r4_asking_under_the_old_name_finds_the_same_charge(world):
    assert [e["key"] for e in world._pending(provider=OLD, fresh=True)] == ["placeholder-key"]


# 2. spend-cap crossings ---------------------------------------------------

def _ledger_with_old_crossing(tmp_path, monkeypatch):
    from multiagents import spendcap
    at = time.time()
    start = spendcap.period_start(at, "day")
    old_scope = f"provider:{OLD}"
    ident = spendcap.crossing_id(old_scope, start, 1.0)
    path = tmp_path / spendcap.LEDGER_NAME
    lines = [
        {"kind": "created", "ts": start},
        {"kind": "charge", "ts": at - 10, "key": "k-old", "provider": OLD,
         "model": GO_MODEL, "agent": "helper", "node": "ag-old01", "usd": 1.5},
        {"kind": "crossing", "ts": at - 10, "scope": old_scope, "id": ident,
         "provider": OLD, "model": "", "period": "day", "period_start": start,
         "until": spendcap.period_end(at, "day"), "cap": 1.0, "spend": 1.5,
         "by": "ag-old01"},
        {"kind": "announced", "id": ident},
    ]
    path.write_text("".join(json.dumps(line) + "\n" for line in lines))
    ledger = spendcap.Ledger(path)
    ledger.refresh()
    return ledger, spendcap.Cap(NEW, "", 1.0, "day"), ident, at


def test_og_r4_an_old_crossing_is_the_new_caps_crossing(tmp_path, monkeypatch):
    ledger, cap, ident, at = _ledger_with_old_crossing(tmp_path, monkeypatch)
    crossed = ledger.crossed(cap, at)
    assert crossed is not None and crossed["id"] == ident
    assert crossed["provider"] == OLD                          # record as written


def test_og_r4_an_old_crossing_stops_an_adopted_run_on_the_new_name(tmp_path, monkeypatch):
    ledger, _cap, ident, at = _ledger_with_old_crossing(tmp_path, monkeypatch)
    request = ledger.stop_request(NEW, GO_MODEL, at - 100, at)
    assert request is not None and request["id"] == ident


def test_og_r4_a_charge_on_the_new_name_claims_no_second_crossing(tmp_path, monkeypatch):
    ledger, cap, ident, at = _ledger_with_old_crossing(tmp_path, monkeypatch)
    new, binding = ledger.charge(key="k-new", provider=NEW, model=GO_MODEL,
                                 agent="helper", node="ag-new01", usd=0.25,
                                 caps=[cap], at=at)
    assert new == []
    assert [s["spend"] for s in binding] == [1.75]
    records = [json.loads(line) for line in ledger.path.read_text().splitlines()]
    assert [r["id"] for r in records if r["kind"] == "crossing"] == [ident]
    # The run is stopped by the crossing already claimed, under its own id.
    assert ledger.stops.get(ident) == ["ag-new01"]
    assert ledger.unannounced(at + 100) == []


# 3. two renames, one deferred task each -----------------------------------

def test_og_r4_a_deferred_task_moves_to_its_own_route(tmp_path):
    from multiagents import renames as renames_mod
    from multiagents.tree import Tree
    table = renames_mod.Renames(aliases={"old-first": "new-first",
                                         "old-second": "new-second"},
                                unroutable=frozenset())
    path = tmp_path / ".multiagents" / "tree.json"
    og.write_tree_json(path, {
        "version": 1, "nodes": {}, "provider_health": {}, "cooldowns": {},
        "pause": {}, "questions": [], "tickets": [],
        "deferred": [{"id": "d-1", "spec": {"provider": "old-second", "model": "m"}},
                     {"id": "d-2", "spec": {"provider": "old-first", "model": "m"}}]})
    data = Tree(path, tmp_path / ".multiagents" / "events.jsonl", renames=table).read()
    routes = {d["id"]: d["spec"]["provider"] for d in data["deferred"]}
    assert routes == {"d-1": "new-second", "d-2": "new-first"}


# 4. family through the rename ---------------------------------------------

def test_og_r4_the_old_name_is_in_the_new_names_family(world):
    assert world._family_of(OLD) == world._family_of(NEW)


def test_og_r4_an_unrelated_provider_keeps_its_own_family(world):
    assert world._family_of("placeholder-unknown") == "placeholder-unknown"
    assert world._family_of("placeholder-unknown") != world._family_of(NEW)


def test_og_r4_a_held_old_charge_is_committed_against_the_new_names_cap(tmp_path, monkeypatch):
    project = Project(tmp_path)
    project.write("agents.yaml", _AGENTS)
    project.write("providers.yaml", f"providers:\n  {NEW}:\n    spend_cap:\n"
                                    "      usd: 0.25\n      period: day\n")
    h.as_root(monkeypatch)
    from multiagents import config as config_mod
    from multiagents.runner import Runner
    runner = Runner(project.paths, config_mod.load(project.paths))
    assert runner._caps(NEW, GO_MODEL)                         # the cap is in force
    new, binding = runner._commit_charge({
        "key": "placeholder-held", "usd": 0.5, "at": time.time(), "provider": OLD,
        "model": GO_MODEL, "agent": "helper", "node": "ag-held01"})
    assert [c["provider"] for c in new] == [NEW]
    assert binding and binding[0]["reached"]
