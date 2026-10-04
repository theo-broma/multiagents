"""M2 — session aliases: NC-R30 (and the `session_busy` / `session_unavailable`
codes of NC-R22/R52).

NEED_INFO(session-tests-need-templates): an alias exists only inside a template
instance (NC-R30), and instantiating one (`register_template` /
`instantiate_template`, NC-R41/R42) is milestone M4. These tests therefore need
the template ops to exist; they stay red until an implementation has them,
whatever M2 ships. They use the smallest template the contract allows: a
`group` of two independent simple nodes sharing the alias `B`.

Assumptions where the contract is silent (kept loose):
- `register_template` takes its YAML text under one of the keys `yaml`, `text`,
  `template`; `instantiate_template` takes `{name, params}` as in NC-R13 and
  returns (or lists, via `list_nodes(parent=<top>)`) the instance's nodes.
- which of two simultaneously-ready children launches first is not specified:
  the tests find out which one did.
- a provider that is `enabled: false` is "unusable" (NC-R30's example is quota
  or disabled).
- "the orchestrator is notified once" is observed as exactly one event naming
  the node and `session_unavailable` in `events.jsonl`.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from nc_fixture.agent import task  # noqa: E402
from nc_fixture.world import World, blocked_codes, err_code, unwrap  # noqa: E402

TEMPLATE = """\
template: aliased-pair
version: 1
params:
  t1: {type: text, default: ""}
  t2: {type: text, default: ""}
root:
  key: top
  kind: group
  children:
    - {key: first, kind: simple, agent: worker, task: {param: t1}, session: B}
    - {key: second, kind: simple, agent: worker, task: {param: t2}, session: B}
"""


@pytest.fixture
def w(tmp_path, monkeypatch):
    world = World(tmp_path, monkeypatch)
    world.fx2 = world.provider("fx2")
    world.agents["worker"]["models"] = {"fx2": "fx2/m1"}
    yield world
    world.close()


def register(w: World) -> None:
    last = None
    for key in ("yaml", "text", "template"):
        last = w.rpc("register_template", {key: TEMPLATE})
        if last.get("ok"):
            return
    raise AssertionError(f"register_template refused every spelling: {last}")


def instantiate(w: World, tag1: str, tag2: str, **directives) -> tuple[str, list[dict]]:
    reply = w.rpc("instantiate_template", {"name": "aliased-pair", "params": {
        "t1": task(tag1, **directives.get(tag1, {})),
        "t2": task(tag2, **directives.get(tag2, {}))}})
    assert reply.get("ok"), reply
    top = unwrap(reply["result"])
    top_id = top["id"] if "id" in top else top["root"]
    kids = w.list(parent=top_id)
    assert len(kids) == 2, kids
    return top_id, kids


def first_running(w: World, kids: list[dict]) -> tuple[dict, dict]:
    ids = [k["id"] for k in kids]
    running = w.until(lambda: [i for i in ids if w.get(i)["state"] == "running"],
                      what="one child to run")
    assert len(running) == 1, "two activations of one alias at the same time"
    other = [i for i in ids if i != running[0]][0]
    return w.get(running[0]), w.get(other)


def tag_of(node: dict) -> str:
    import re
    return re.search(r"\[(\w+)\]", node["task"]).group(1)


def test_nc_r30_an_alias_has_one_active_turn_and_the_next_resumes_its_session(w):
    w.start_scheduler()
    register(w)
    top, kids = instantiate(w, "F", "G", F={"gate": "gf"}, G={"gate": "gg"})
    first, second = first_running(w, kids)
    w.quiet(2)
    assert "session_busy" in blocked_codes(w.get(second["id"]))
    assert w.fx.by_tag(tag_of(second)) == []
    session = w.fx.by_tag(tag_of(first))[0]["session"]
    w.gate("gf" if tag_of(first) == "F" else "gg")
    w.wait_running(second["id"])
    call = w.wait_spawn(tag_of(second))
    assert call["resume"] == session, "the second activation did not resume the alias session"


def test_nc_r30_two_instances_never_share_an_alias(w):
    w.start_scheduler()
    register(w)
    _, kids1 = instantiate(w, "F1", "G1", F1={"gate": "g1"}, G1={"gate": "g1b"})
    _, kids2 = instantiate(w, "F2", "G2", F2={"gate": "g2"}, G2={"gate": "g2b"})
    # one child per instance runs at the same time
    running = w.until(
        lambda: [k["id"] for k in kids1 + kids2 if w.get(k["id"])["state"] == "running"]
        if sum(w.get(k["id"])["state"] == "running" for k in kids1 + kids2) == 2 else None,
        what="one child of each instance to run")
    sessions = {c["session"] for c in w.fx.calls()}
    assert len(sessions) == 2
    assert all(c["resume"] is None for c in w.fx.calls())


def test_nc_r30_the_binding_and_session_id_survive_a_scheduler_restart(w):
    w.start_scheduler()
    register(w)
    top, kids = instantiate(w, "F", "G", F={"gate": "gf"}, G={"gate": "gg"})
    first, second = first_running(w, kids)
    session = w.fx.by_tag(tag_of(first))[0]["session"]
    w.restart_scheduler()
    assert w.get(first["id"])["state"] == "running"
    w.gate("gf" if tag_of(first) == "F" else "gg")
    w.wait_state(first["id"], "done")
    w.wait_spawn(tag_of(second))
    assert w.fx.by_tag(tag_of(second))[0]["resume"] == session
    assert w.fx.spawns() == 2


def test_nc_r30_an_unusable_bound_provider_blocks_instead_of_rerouting(w):
    w.start_scheduler()
    register(w)
    top, kids = instantiate(w, "F", "G", F={"gate": "gf"}, G={"gate": "gg"})
    first, second = first_running(w, kids)
    w.stop_scheduler()
    w.providers["fx"].entry["enabled"] = False
    w.start_scheduler()
    w.gate("gf" if tag_of(first) == "F" else "gg")
    w.wait_state(first["id"], "done")
    view = w.until(lambda: "session_unavailable" in blocked_codes(w.get(second["id"]))
                   and w.get(second["id"]), what="session_unavailable")
    assert view["state"] == "open" and view["eligible"] is False
    w.quiet(4)
    assert w.fx2.spawns() == 0, "the alias was silently moved to another provider"
    assert w.fx.spawns() == 1
    notices = [e for e in w.events() if second["id"] in json.dumps(e)
               and "session_unavailable" in json.dumps(e)]
    assert len(notices) == 1, notices
    # usable again: the same session is resumed, on the same provider
    w.stop_scheduler()
    w.providers["fx"].entry["enabled"] = True
    w.start_scheduler()
    w.wait_state(second["id"], "done", timeout=60)
    assert w.fx.by_tag(tag_of(second))[0]["resume"] == w.fx.by_tag(tag_of(first))[0]["session"]
    assert w.fx2.spawns() == 0
