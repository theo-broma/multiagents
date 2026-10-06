"""OG round 5 (review ag-d5fe97): quota handover reads a recorded provider
through the rename declaration.

A node recorded under a provider's pre-rename name resumes on the route that
name became (`_spec_of`). When it hits quota there, the stop is that route's
stop: the route is not a candidate to hand over to, `_qh_usable(route)` sees
the stop, and an agent whose only route is that one has no successor. A stop
recorded under the old name by earlier code is the route's stop too.

The rename here is a test-only declaration on the C23 fixture providers, so
nothing depends on a shipped provider. Placeholders only.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent / "support"))
from test_c23_quota_handover import World  # noqa: E402

from multiagents.renames import Renames  # noqa: E402
from multiagents.tree import Node  # noqa: E402

OLD = "placeholder-alpha-old"
NEW = "alpha"


def _world(tmp_path, monkeypatch, **kw):
    w = World(tmp_path, monkeypatch, **kw)
    monkeypatch.setattr(w.r.tree, "renames",
                        Renames(aliases={OLD: NEW}, unroutable=frozenset()))
    return w


def _node(w, **fields):
    node = Node(id="ag-0ld5a1", agent="worker", provider=OLD, model="model-a",
                parent=None, depth=0, task="placeholder task", status="running", **fields)
    w.r.tree.add(node)
    return w.r.tree.get(node.id)


def _spec(w):
    return w.r._usable_spec(w.r.config.agent("worker"), NEW)


@pytest.mark.parametrize("stopped_as", [None, OLD, NEW])
def test_og_r5_the_route_a_recorded_name_became_is_not_its_own_candidate(tmp_path, monkeypatch,
                                                                         stopped_as):
    # Whether the stop was keyed by the recorded name (as earlier code wrote
    # it), by the route, or not yet written: the run's own route is never a
    # route to hand it over to.
    w = _world(tmp_path, monkeypatch)
    node = _node(w, quota_stops={stopped_as: w.clock[0] + 3600} if stopped_as else {})
    names = [name for _, name, _ in asyncio.run(w.r._qh_candidates(node, _spec(w), dict(w.readings)))]
    assert NEW not in names
    assert "beta" in names                                   # the fixture is otherwise live


def test_og_r5_a_stop_on_the_route_is_seen_by_qh_usable(tmp_path, monkeypatch):
    w = _world(tmp_path, monkeypatch)
    spec = _spec(w)
    fresh = _node(w)
    assert asyncio.run(w.r._qh_usable(NEW, spec, dict(w.readings), fresh))  # control
    w.r.tree.update(fresh.id, quota_stops={OLD: w.clock[0] + 3600})
    stopped = w.r.tree.get(fresh.id)
    assert not asyncio.run(w.r._qh_usable(NEW, spec, dict(w.readings), stopped))


def _quota_run(w, node):
    """The run `_spec_of` resumed on the route, finishing on a quota stop."""
    return SimpleNamespace(node_id=node.id, provider=w.r.providers[NEW], spec=_spec(w),
                           cap_stop=None, stop_requested=False)


def test_og_r5_a_quota_stop_is_recorded_under_the_route_and_not_handed_back(tmp_path, monkeypatch):
    w = _world(tmp_path, monkeypatch)
    w.unusable("beta", "reserve", "other")
    node = _node(w)
    run = _quota_run(w, node)
    switched = []

    async def no_switch(node, spec, target, *a, **kw):
        switched.append(target)
        return False

    monkeypatch.setattr(w.r, "_qh_switch", no_switch)
    assert asyncio.run(w.r._qh_after(run, "quota", {}, "", {"until": w.clock[0] + 600})) is False
    assert NEW not in switched
    after = w.r.tree.get(node.id)
    assert after.provider == OLD                             # the record stays as written
    assert set(after.quota_stops) == {NEW}
    assert set(after.quota_stop_readings) == {NEW}
    assert not asyncio.run(w.r._qh_usable(NEW, _spec(w), dict(w.readings), after))


def test_og_r5_the_only_route_has_no_successor(tmp_path, monkeypatch):
    w = _world(tmp_path, monkeypatch, models={})
    node = _node(w)
    run = _quota_run(w, node)

    async def no_reading(*a, **kw):
        raise AssertionError("QH-R22: no successor, so no quota reading")

    monkeypatch.setattr(w.r, "_qh_budgets", no_reading)
    assert asyncio.run(w.r._qh_after(run, "quota", {}, "", {"until": w.clock[0] + 600})) is False
    assert w.r.tree.get(node.id).quota_stops == {}
