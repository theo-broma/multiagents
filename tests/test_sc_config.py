"""SC-R1 — spend-cap configuration. Contract: `context/specs/spend-caps.md`.

Surfaces: `providers.load_providers` on raw provider maps (the validation
matrix), `config.load` on config layers on disk (load-time errors, layering),
and — for what a cap *does* — the server's `start_agent` over a project whose
`providers.yaml` the test edits. `usd: 0` is the probe: it is valid, it admits
nothing, and so it shows a cap is in force without spending a cent.

Assumptions where the contract is silent (each deliberately loose):
- A rejected cap raises `ValueError` (every other provider-key validation in
  `providers.py` does) whose message names `spend_cap`, and the key at fault:
  `usd`, `period`, or the offending `models` key.
- "Refused" on start is observed as: no `agent_id`, and the CLI not spawned.
  A deferral carries `spend_cap` in its reason (SC-R3).
"""
from __future__ import annotations

import asyncio
import math
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import sc_harness as sc  # noqa: E402
import os  # noqa: E402
from multiagents import config as config_mod  # noqa: E402
from multiagents import server  # noqa: E402
from multiagents.paths import ProjectPaths  # noqa: E402
from multiagents.providers import load_providers  # noqa: E402


def raw(cap, **extra):
    return {"acme": {"bin": "acme", "models_include": ["acme/*"], "spend_cap": cap, **extra}}


# --------------------------------------------------------------- the matrix --

VALID = [
    pytest.param({"usd": 5.0}, id="float"),
    pytest.param({"usd": 5}, id="int"),
    pytest.param({"usd": 0}, id="zero-int"),
    pytest.param({"usd": 0.0}, id="zero-float"),
    pytest.param({"usd": 0.01}, id="one-cent"),
    pytest.param({"usd": None}, id="null"),
    pytest.param({}, id="empty"),
    pytest.param({"period": "day"}, id="period-only-day"),
    pytest.param({"usd": 1, "period": "week"}, id="week"),
    pytest.param({"usd": 1, "period": "month"}, id="month"),
    pytest.param({"models": {"acme/big": {"usd": 2.0}}}, id="model-cap-alone"),
    pytest.param({"usd": 5, "models": {"acme/big": {"usd": None, "period": "month"}}},
                 id="model-null-with-period"),
    pytest.param({"models": {"acme/org/deep": {"usd": 0}}}, id="two-slash-model"),
]


@pytest.mark.parametrize("cap", VALID)
def test_r1_valid_caps_load(cap):
    load_providers(raw(cap))


# (cap, the key the message must name)
INVALID = [
    pytest.param({"usd": -1}, "usd", id="negative"),
    pytest.param({"usd": -0.01}, "usd", id="negative-cent"),
    pytest.param({"usd": "five"}, "usd", id="string"),
    pytest.param({"usd": True}, "usd", id="true"),
    pytest.param({"usd": False}, "usd", id="false"),
    pytest.param({"usd": float("nan")}, "usd", id="nan"),
    pytest.param({"usd": float("inf")}, "usd", id="inf"),
    pytest.param({"usd": float("-inf")}, "usd", id="minus-inf"),
    pytest.param({"usd": [5]}, "usd", id="list"),
    pytest.param({"usd": {"a": 1}}, "usd", id="mapping"),
    pytest.param({"usd": 1, "period": "hour"}, "period", id="unknown-period"),
    pytest.param({"usd": 1, "period": "weeks"}, "period", id="plural-period"),
    pytest.param({"usd": 1, "period": ""}, "period", id="empty-period"),
    pytest.param({"usd": 1, "period": 7}, "period", id="numeric-period"),
    pytest.param({"models": {"other/x": {"usd": 1}}}, "other/x", id="model-matching-no-pattern"),
    pytest.param({"usd": 1, "models": {"acme/big": {"usd": -1}}}, "usd", id="model-negative"),
    pytest.param({"models": {"acme/big": {"usd": True}}}, "usd", id="model-bool"),
    pytest.param({"models": {"acme/big": {"usd": float("nan")}}}, "usd", id="model-nan"),
    pytest.param({"models": {"acme/big": {"usd": "x"}}}, "usd", id="model-string"),
    pytest.param({"models": {"acme/big": {"usd": 1, "period": "hour"}}}, "period",
                 id="model-unknown-period"),
    pytest.param({"models": ["acme/big"]}, "models", id="models-not-a-mapping"),
]


@pytest.mark.parametrize("cap,key", INVALID)
def test_r1_invalid_caps_are_a_load_error_naming_the_key(cap, key):
    with pytest.raises(ValueError) as caught:
        load_providers(raw(cap))
    message = str(caught.value)
    assert "spend_cap" in message, message
    assert key in message, message


def test_r1_a_spend_cap_that_is_not_a_mapping_is_a_load_error():
    with pytest.raises(ValueError) as caught:
        load_providers(raw(5))
    assert "spend_cap" in str(caught.value)


def test_r1_one_valid_and_one_invalid_model_cap_is_still_an_error():
    cap = {"models": {"acme/ok": {"usd": 1}, "other/bad": {"usd": 1}}}
    with pytest.raises(ValueError) as caught:
        load_providers(raw(cap))
    assert "other/bad" in str(caught.value)


def test_r1_an_invalid_cap_in_a_config_layer_fails_config_load_not_silently(tmp_path, monkeypatch):
    sc.h.as_root(monkeypatch)
    proj = sc.Project(tmp_path)
    proj.add_provider("acme", models_include=["acme/*"], spend_cap={"usd": -5})
    proj.add_agent("worker", "acme", "acme/m1")
    proj.write()
    with pytest.raises(ValueError) as caught:
        config_mod.load(ProjectPaths(proj.root), seed=False)
    assert "spend_cap" in str(caught.value) and "usd" in str(caught.value)


def test_r1_no_shipped_provider_has_a_cap():
    shipped = yaml.safe_load(
        (sc.shipped_defaults_dir() / "providers.yaml").read_text())["providers"]
    capped = [name for name, entry in shipped.items() if "spend_cap" in (entry or {})]
    assert capped == []


# ------------------------------------------------------ what a cap does --

@pytest.fixture
def w(tmp_path, monkeypatch):
    world = sc.World(tmp_path, monkeypatch)
    yield world
    world.down()


def probe(w, cap, *, models=("m1",), **extra):
    """acme with `cap`, one agent per model: `worker`, `worker2`, ..."""
    acme = w.provider("acme", spend_cap=cap, **extra)
    acme.costs(0.01)
    for i, model in enumerate(models):
        w.agent("worker" if i == 0 else f"worker{i + 1}", "acme", f"acme/{model}")
    w.up()
    return acme


def refused(result) -> bool:
    return not result.get("agent_id")


def test_r1_with_no_cap_anywhere_a_start_is_admitted(w):
    acme = probe(w, None)

    async def go():
        r = await w.start("worker")
        await w.settle()
        return r
    r = asyncio.run(go())
    assert r.get("agent_id") and acme.spawns() == 1


def test_r1_usd_zero_admits_nothing(w):
    acme = probe(w, {"usd": 0})
    r = asyncio.run(w.start("worker"))
    assert refused(r), r
    assert acme.spawns() == 0


def test_r1_a_model_cap_alone_refuses_that_model_and_admits_its_sibling(w):
    acme = probe(w, {"models": {"acme/big": {"usd": 0}}}, models=("big", "small"))

    async def go():
        big = await w.start("worker")
        small = await w.start("worker2")
        await w.settle()
        return big, small
    big, small = asyncio.run(go())
    assert refused(big), big
    assert small.get("agent_id"), small
    assert acme.spawns() == 1
    assert "acme/small" in " ".join(acme.argv_of_spawn(0))


def test_r1_model_cap_with_null_usd_leaves_the_provider_cap_in_force(w):
    acme = probe(w, {"usd": 0, "models": {"acme/big": {"usd": None}}})
    r = asyncio.run(w.start("worker"))
    assert refused(r), r
    assert acme.spawns() == 0


def test_r1_a_model_cap_is_checked_in_addition_to_the_provider_cap(w):
    acme = probe(w, {"usd": 100, "models": {"acme/big": {"usd": 0}}}, models=("big",))
    assert refused(asyncio.run(w.start("worker")))
    assert acme.spawns() == 0


def test_r1_a_generous_model_cap_does_not_lift_a_provider_cap(w):
    acme = probe(w, {"usd": 0, "models": {"acme/big": {"usd": 100}}}, models=("big",))
    assert refused(asyncio.run(w.start("worker")))
    assert acme.spawns() == 0


def test_r1_a_provider_that_extends_a_capped_one_starts_uncapped(w):
    acme = probe(w, {"usd": 0})
    w.p.providers["child"] = {"extends": "acme", "family": "child"}
    w.agent("kid", "child", "acme/m1")
    w.reload()

    async def go():
        parent = await w.start("worker")
        kid = await w.start("kid")
        await w.settle()
        return parent, kid
    parent, kid = asyncio.run(go())
    assert refused(parent), parent
    assert kid.get("agent_id"), kid


def test_r1_an_extending_provider_may_declare_its_own_cap(w):
    probe(w, None)
    w.p.providers["child"] = {"extends": "acme", "family": "child", "spend_cap": {"usd": 0}}
    w.agent("kid", "child", "acme/m1")
    w.reload()
    assert refused(asyncio.run(w.start("kid")))


def test_r1_a_higher_layer_removes_a_lower_layers_cap_with_null(tmp_path, monkeypatch):
    world = sc.World(tmp_path, monkeypatch)
    try:
        acme = world.provider("acme", spend_cap={"usd": 0})
        acme.costs(0.01)
        world.agent("worker", "acme", "acme/m1")
        # the lower layer: the machine-wide config, holding the whole provider
        glob = Path(os.environ["MULTIAGENTS_CONFIG_DIR"])
        glob.mkdir(parents=True, exist_ok=True)
        (glob / "providers.yaml").write_text(yaml.safe_dump({"providers": world.p.providers}))
        world.p.providers = {}                     # the project layer says nothing yet
        world.up()
        capped = asyncio.run(world.start("worker"))
        assert refused(capped), f"the lower layer's cap is not in force: {capped}"
        # the higher layer: the project, naming only the override
        world.p.providers = {"acme": {"spend_cap": {"usd": None}}}
        world.reload()
        r = asyncio.run(_start_and_settle(world, "worker"))
        assert r.get("agent_id"), r
        assert acme.spawns() == 1
    finally:
        world.down()


def test_r1_layers_merge_recursively_a_higher_usd_wins_a_lower_period_stays(tmp_path, monkeypatch):
    world = sc.World(tmp_path, monkeypatch)
    try:
        acme = world.provider("acme", spend_cap={"usd": 100, "period": "week"})
        acme.costs(0.01)
        world.agent("worker", "acme", "acme/m1")
        glob = Path(os.environ["MULTIAGENTS_CONFIG_DIR"])
        glob.mkdir(parents=True, exist_ok=True)
        (glob / "providers.yaml").write_text(yaml.safe_dump({"providers": world.p.providers}))
        world.p.providers = {"acme": {"spend_cap": {"usd": 0}}}      # only `usd` re-stated
        world.up()
        r = asyncio.run(world.start("worker"))
        assert refused(r), r
        # the lower layer's `period: week` survived the merge: it sets the reset
        assert "spend_cap" in str(r) and sc.has_time(r, sc.period_end(sc.WED, "week")), r
    finally:
        world.down()


async def _start_and_settle(world, agent):
    r = await world.start(agent)
    await world.settle()
    return r


def test_r1_a_cap_edited_in_the_config_is_seen_by_the_next_start_without_restart(w):
    acme = probe(w, None)

    async def go():
        first = await w.start("worker")
        await w.settle()
        w.p.cap("acme", {"usd": 0})
        capped = await w.start("worker")
        w.p.cap("acme", {"usd": 50})
        freed = await w.start("worker")
        await w.settle()
        return first, capped, freed
    first, capped, freed = asyncio.run(go())
    assert first.get("agent_id") and freed.get("agent_id")
    assert refused(capped), capped
    assert acme.spawns() == 2


def test_r1_an_invalid_cap_edit_never_weakens_the_cap_in_force(w):
    acme = probe(w, {"usd": 0})
    asyncio.run(w.start("worker"))                       # settle the config in force
    w.p.cap("acme", {"usd": "unlimited"})                # a typo, not a removal
    result = server.list_agents()
    assert sc.mentions(result, "load_error") or sc.mentions(result, "spend_cap"), result
    r = asyncio.run(w.start("worker"))
    assert refused(r), "the broken edit silently lifted the cap"
    assert acme.spawns() == 0
