"""PC-R1 — per-provider concurrency limit, configuration.
Contract: `context/specs/provider-concurrency.md`.

Surfaces: `providers.load_providers` on raw maps (the validation matrix),
`config.load` on layers on disk, the shipped defaults, and `start_agent` over a
project whose `providers.yaml` the test edits.

Assumptions where the contract is silent (deliberately loose):
- A rejected value raises `ValueError` (as every other provider-key check in
  `providers.py` does) whose message names `max_concurrent`.
- "Admitted" on start is: an `agent_id` and the CLI spawned. "Queued" is a
  `deferred: true` result whose reason names `provider_concurrency` (PC-R3).
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import pc_harness as pc  # noqa: E402
import sc_harness as sc  # noqa: E402
from multiagents import config as config_mod  # noqa: E402
from multiagents.paths import ProjectPaths  # noqa: E402
from multiagents.providers import load_providers  # noqa: E402


def raw(value, **extra):
    return {"acme": {"bin": "acme", "models_include": ["acme/*"],
                     "max_concurrent": value, **extra}}


VALID = [pytest.param(1, id="one"), pytest.param(2, id="two"),
         pytest.param(64, id="large"), pytest.param(None, id="null")]


@pytest.mark.parametrize("value", VALID)
def test_r1_valid_values_load(value):
    load_providers(raw(value))


def test_r1_an_absent_key_loads():
    load_providers({"acme": {"bin": "acme", "models_include": ["acme/*"]}})


INVALID = [
    pytest.param(0, id="zero"), pytest.param(-1, id="negative"),
    pytest.param(-100, id="very-negative"),
    pytest.param(1.5, id="float"), pytest.param(2.0, id="whole-float"),
    pytest.param(0.5, id="fraction"),
    pytest.param(True, id="true"), pytest.param(False, id="false"),
    pytest.param("2", id="numeric-string"), pytest.param("two", id="string"),
    pytest.param("", id="empty-string"),
    pytest.param([2], id="list"), pytest.param({"n": 2}, id="mapping"),
    pytest.param(float("nan"), id="nan"), pytest.param(float("inf"), id="inf"),
]


@pytest.mark.parametrize("value", INVALID)
def test_r1_invalid_values_are_a_load_error_naming_the_key(value):
    with pytest.raises(ValueError) as caught:
        load_providers(raw(value))
    assert "max_concurrent" in str(caught.value), str(caught.value)


def test_r1_the_error_names_the_offending_provider_among_several():
    data = {"good": {"bin": "g", "models_include": ["g/*"], "max_concurrent": 2},
            "bad": {"bin": "b", "models_include": ["b/*"], "max_concurrent": 0}}
    with pytest.raises(ValueError) as caught:
        load_providers(data)
    assert "max_concurrent" in str(caught.value) and "bad" in str(caught.value)


def test_r1_an_invalid_value_in_a_config_layer_fails_config_load(tmp_path, monkeypatch):
    sc.h.as_root(monkeypatch)
    proj = sc.Project(tmp_path)
    proj.add_provider("acme", models_include=["acme/*"], max_concurrent=0)
    proj.add_agent("worker", "acme", "acme/m1")
    proj.write()
    with pytest.raises(ValueError) as caught:
        config_mod.load(ProjectPaths(proj.root), seed=False)
    assert "max_concurrent" in str(caught.value)


def test_r1_no_shipped_provider_sets_a_limit():
    shipped = yaml.safe_load(
        (sc.shipped_defaults_dir() / "providers.yaml").read_text())["providers"]
    limited = [n for n, e in shipped.items() if "max_concurrent" in (e or {})
               and (e or {}).get("max_concurrent") is not None]
    assert limited == []


# ------------------------------------------------------- what the key does --

@pytest.fixture
def w(tmp_path, monkeypatch):
    world = sc.World(tmp_path, monkeypatch)
    yield world
    world.down()


def one_worker(w, limit, name="acme"):
    g = pc.gated(w, name, max_concurrent=limit)
    w.agent("worker", name, f"{name}/m1")
    g.close()
    w.up()
    return g


@pytest.mark.parametrize("limit", [None, "null"], ids=["absent", "explicit-null"])
def test_r1_with_no_limit_nothing_is_queued_whatever_the_load(w, limit):
    g = one_worker(w, limit)

    async def go():
        rs = [await w.start("worker", f"t{i}") for i in range(3)]
        await pc.await_until(lambda: g.spawns() >= 3)
        g.open()
        await w.settle()
        return rs
    rs = asyncio.run(go())
    assert all(r.get("agent_id") and not r.get("deferred") for r in rs), rs
    assert g.spawns() == 3


def test_r1_a_limit_of_one_admits_one_and_queues_the_next(w):
    g = one_worker(w, 1)

    async def go():
        a = await w.start("worker", "first")
        await pc.await_until(lambda: g.spawns() == 1)
        b = await w.start("worker", "second")
        g.open()
        await w.settle()
        return a, b
    a, b = asyncio.run(go())
    assert a.get("agent_id"), a
    assert pc.deferred_for_pc(b), b


def test_r1_a_limit_of_two_admits_two_and_queues_the_third(w):
    g = one_worker(w, 2)

    async def go():
        rs = [await w.start("worker", f"t{i}") for i in range(3)]
        await pc.await_until(lambda: g.spawns() == 2)
        spawned = g.spawns()
        g.open()
        await w.settle()
        return rs, spawned
    rs, spawned = asyncio.run(go())
    assert rs[0].get("agent_id") and rs[1].get("agent_id"), rs
    assert pc.deferred_for_pc(rs[2]), rs[2]
    assert spawned == 2


def test_r1_the_limit_is_re_read_at_the_next_admission_without_a_restart(w):
    g = one_worker(w, 1)

    async def go():
        await w.start("worker", "first")
        await pc.await_until(lambda: g.spawns() == 1)
        blocked = await w.start("worker", "second")
        pc.set_limit(w, "acme", 3)
        admitted = await w.start("worker", "third")
        await pc.await_until(lambda: g.spawns() >= 2)
        g.open()
        await w.settle()
        return blocked, admitted
    blocked, admitted = asyncio.run(go())
    assert pc.deferred_for_pc(blocked), blocked
    assert admitted.get("agent_id") and not admitted.get("deferred"), admitted


def test_r1_a_limit_added_to_a_provider_that_had_none_applies_to_the_next_start(w):
    g = one_worker(w, None)

    async def go():
        await w.start("worker", "first")
        await pc.await_until(lambda: g.spawns() == 1)
        pc.set_limit(w, "acme", 1)
        r = await w.start("worker", "second")
        g.open()
        await w.settle()
        return r
    assert pc.deferred_for_pc(asyncio.run(go()))


def _extends_world(w, parent_limit, child_limit):
    parent = pc.gated(w, "acme", max_concurrent=parent_limit)
    child = pc.gated(w, "acme2", max_concurrent=child_limit, extends="acme")
    w.agent("on-parent", "acme", "acme/m1")
    w.agent("on-child", "acme2", "acme2/m1")
    parent.close()
    child.close()
    w.up()
    return parent, child


def test_r1_a_limit_is_not_inherited_through_extends(w):
    parent, child = _extends_world(w, 1, None)

    async def go():
        rs = [await w.start("on-child", f"t{i}") for i in range(3)]
        await pc.await_until(lambda: child.spawns() == 3)
        spawned = child.spawns()
        parent.open()
        child.open()
        await w.settle()
        return rs, spawned
    rs, spawned = asyncio.run(go())
    assert all(r.get("agent_id") and not r.get("deferred") for r in rs), rs
    assert spawned == 3


def test_r1_a_childs_own_limit_does_not_limit_its_parent(w):
    parent, child = _extends_world(w, None, 1)

    async def go():
        rs = [await w.start("on-parent", f"t{i}") for i in range(3)]
        await pc.await_until(lambda: parent.spawns() == 3)
        parent.open()
        child.open()
        await w.settle()
        return rs
    rs = asyncio.run(go())
    assert all(r.get("agent_id") and not r.get("deferred") for r in rs), rs


def test_r1_a_parent_and_its_extender_have_separate_counts(w):
    parent, child = _extends_world(w, 1, 1)

    async def go():
        a = await w.start("on-parent", "p")
        b = await w.start("on-child", "c")
        await pc.await_until(lambda: parent.spawns() == 1 and child.spawns() == 1)
        parent.open()
        child.open()
        await w.settle()
        return a, b
    a, b = asyncio.run(go())
    assert a.get("agent_id") and b.get("agent_id")
    assert not a.get("deferred") and not b.get("deferred"), (a, b)
