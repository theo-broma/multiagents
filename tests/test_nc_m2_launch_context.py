"""M2 — the trusted launch context and the capability issued at launch:
NC-R12 and the issuance part of NC-R58 (with NC-R9's storage rules).

A fixture run reads the environment of the MCP registration its provider was
given (the opencode `OPENCODE_CONFIG` file) and logs it, so the test sees the
`MULTIAGENTS_RPC_TOKEN` the run's MCP server would receive, and can then act as
that run over the NC-R8 socket.

Assumptions where the contract is silent (kept loose):
- the run's own node id is the `node_id` in `MULTIAGENTS_...` environment or
  found as the node whose `active_run` is the run; we find it from the node
  list, not from the environment.
- a run capability may create a node with `parent` = its own node id.
- the refusal codes are `forbidden` (outside scope / not permitted) and
  `unauthenticated` (revoked, unknown).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from nc_fixture.world import World, blocked_codes, err_code, run_id_of  # noqa: E402


@pytest.fixture
def w(tmp_path, monkeypatch):
    world = World(tmp_path, monkeypatch)
    world.pc = world.provider("pcfx", max_concurrent=1)
    world.agent("pcworker", "pcfx")
    world.agent("pcspawner", "pcfx", can_spawn=True, max_children=6)
    yield world
    world.close()


def token_of(call: dict) -> str:
    return (call["mcp_env"] or {}).get("MULTIAGENTS_RPC_TOKEN") or ""


# ----------------------------------------------------------------- NC-R58

def test_nc_r58_every_node_run_gets_a_run_capability_even_without_spawn_rights(w):
    w.start_scheduler()
    n = w.simple("A", "worker", fx={"gate": "ga"})
    w.wait_running(n)
    call = w.wait_spawn("A")
    tok = token_of(call)
    assert len(tok) >= 32
    assert tok != w.root_token()
    # it reads its own node ...
    assert w.rpc("get_node", {"id": n}, tok)["ok"] is True
    # ... but may not delegate
    reply = w.rpc("create_node", {"kind": "simple", "agent": "worker", "task": "x",
                                  "parent": n, "plan_revision": 0}, tok)
    assert err_code(reply) == "forbidden"
    assert len(w.list()) == 1


def test_nc_r58_a_spawning_agent_run_may_delegate_into_its_own_subtree(w):
    w.start_scheduler()
    s = w.simple("S", "spawner", fx={"gate": "gs"})
    w.wait_running(s)
    tok = token_of(w.wait_spawn("S"))
    child = w.simple("X", "worker", token=tok, parent=s, fx={"gate": "gx"})
    got = w.get(child)
    assert got["parent"] == s
    assert run_id_of(w.get(s)["active_run"]) in json.dumps(got["created_by"])


def test_nc_r58_each_run_has_its_own_token(w):
    w.start_scheduler()
    nodes = [w.simple(t, "worker", fx={"gate": f"g{t}"}) for t in ("A", "B")]
    for n in nodes:
        w.wait_running(n)
    toks = {token_of(w.wait_spawn(t)) for t in ("A", "B")}
    assert len(toks) == 2 and "" not in toks


def test_nc_r58_the_token_is_revoked_when_the_run_ends(w):
    w.start_scheduler()
    n = w.simple("A", "worker", fx={"gate": "ga"})
    w.wait_running(n)
    tok = token_of(w.wait_spawn("A"))
    assert w.rpc("get_node", {"id": n}, tok)["ok"] is True
    w.gate("ga")
    w.wait_state(n, "done")
    assert err_code(w.rpc("get_node", {"id": n}, tok)) == "unauthenticated"


def test_nc_r58_a_run_cannot_read_a_sibling_outside_its_subtree(w):
    w.start_scheduler()
    a = w.simple("A", "worker", fx={"gate": "ga"})
    b = w.simple("B", "worker", fx={"gate": "gb"})
    w.wait_running(a)
    w.wait_running(b)
    tok = token_of(w.wait_spawn("A"))
    assert err_code(w.rpc("get_node", {"id": b}, tok)) == "forbidden"


def test_nc_r9_the_root_capability_never_reaches_a_run(w):
    w.start_scheduler()
    n = w.simple("A", "worker", fx={"gate": "ga"})
    w.wait_running(n)
    w.wait_spawn("A")
    root = w.root_token()
    log = (w.fx.dir / "calls.jsonl").read_text()
    assert root not in log
    for call in w.fx.calls():
        assert call["env_token"] in (None, "", token_of(call))


def test_nc_r9_the_registry_keeps_only_a_hash_of_the_run_token(w):
    w.start_scheduler()
    n = w.simple("A", "worker", fx={"gate": "ga"})
    w.wait_running(n)
    tok = token_of(w.wait_spawn("A"))
    files = w.scheduler_files()
    assert files
    for path in files:
        assert tok.encode() not in path.read_bytes(), f"raw run token stored in {path}"


# ----------------------------------------------------------------- NC-R12

def test_nc_r12_concurrent_launches_get_the_tree_parent_and_depth_of_their_own_context(w):
    w.start_scheduler()
    s = w.simple("S", "spawner", fx={"gate": "gs"})
    w.wait_running(s)
    tok = token_of(w.wait_spawn("S"))
    run_s = run_id_of(w.get(s)["active_run"])
    x = w.simple("X", "worker", token=tok, parent=s, fx={"gate": "gx"})
    y = w.simple("Y", "worker", fx={"gate": "gy"})
    w.wait_running(x)
    w.wait_running(y)
    tree = w.tree_nodes()
    rx = tree[run_id_of(w.get(x)["active_run"])]
    ry = tree[run_id_of(w.get(y)["active_run"])]
    assert (rx["parent"], rx["depth"]) == (run_s, tree[run_s]["depth"] + 1)
    assert (ry["parent"] or None, ry["depth"]) == (None, tree[run_s]["depth"])


def test_nc_r12_a_delegated_child_of_a_finished_run_still_launches(w):
    w.start_scheduler()
    s = w.simple("S", "pcspawner", fx={"gate": "gs"})
    w.wait_running(s)
    pcs = w.pc
    tok = token_of(w.wait_spawn("S", pcs))
    run_s = run_id_of(w.get(s)["active_run"])
    x = w.simple("X", "pcworker", token=tok, parent=s)
    w.until(lambda: "admission:provider_concurrency" in blocked_codes(w.get(x)),
            what="X blocked behind its creator")
    w.gate("gs", pcs)
    w.wait_state(s, "done")
    done = w.wait_state(x, "done", timeout=60)
    assert w.tree_nodes()[done["runs"][0]["run_id"]]["parent"] == run_s
    assert len(pcs.by_tag("X")) == 1
    # the creator is gone; the transition still reached the root log
    assert "done" in w.transitions(x)


def test_nc_r12_the_scheduler_does_not_mutate_the_orchestrators_environment(w, monkeypatch):
    import os
    before = {k: v for k, v in os.environ.items() if k.startswith("MULTIAGENTS_")}
    w.start_scheduler()
    n = w.simple("A", "worker")
    w.wait_state(n, "done")
    assert {k: v for k, v in os.environ.items() if k.startswith("MULTIAGENTS_")} == before
