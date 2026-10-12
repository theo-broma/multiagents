"""Adversary (M1, NC-R6/R7/R10): seeded random op sequences.

* A run token issuing random ops with random ids never changes a node outside
  its subtree, and never reads one (no node outside appears in any reply).
* Root issuing random writes: a refused write leaves the plan identical; an
  accepted write leaves containment consistent and the dependency graph
  (explicit, inherited, implicit sequence edges, children) acyclic.

Seeds are fixed; a failure message names the seed and the step.
"""
from __future__ import annotations

import json
import random
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

from nc_harness import nc, stored  # noqa: E402,F401
from multiagents.scheduler.engine import Engine  # noqa: E402
from multiagents.scheduler.rpc import Handler, Server, Service  # noqa: E402
from multiagents.tree import now  # noqa: E402

STEPS = 120
SERVER_JOIN_TIMEOUT = 3


@pytest.fixture
def plan_rpc(nc):
    """Real RPC and store with background evaluation stopped (ST7, NC-R7).

    These M1 tests attribute stored changes to a client write. A live engine
    can independently admit simple nodes or advance composites between the
    snapshots, even when that write was refused. Start and stop the real
    engine while the plan is empty, then serve the same RPC with that engine
    attached: only the test's requests can change the plan.
    """
    service = Service(nc.root, now())
    service.store.initialize()
    engine = Engine(service)
    service.engine = engine
    engine.start()
    engine.stop()
    nc.rpc_dir.mkdir(parents=True, exist_ok=True)
    try:
        with Server(str(nc.sock), Handler) as server:
            server.service = service
            thread = threading.Thread(
                target=server.serve_forever, kwargs={"poll_interval": 0.05})
            thread.start()
            try:
                yield nc
            finally:
                server.shutdown()
                thread.join(timeout=SERVER_JOIN_TIMEOUT)
                assert not thread.is_alive(), "plan RPC server did not stop"
    finally:
        nc.sock.unlink(missing_ok=True)


def _plan(plan_rpc):
    return {n["id"]: {k: v for k, v in stored(n).items() if k != "plan_revision"}
            for n in plan_rpc.ok("list_nodes", {})["nodes"]}


def _subtree(nodes, root):
    out, todo = set(), [root]
    while todo:
        i = todo.pop()
        if i in out or i not in nodes:
            continue
        out.add(i)
        todo.extend(nodes[i]["children"])
    return out


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_adv_fuzz_run_token_never_touches_or_reads_outside_its_subtree(plan_rpc, seed):
    rng = random.Random(seed)
    outside = [plan_rpc.create(task=f"outside {i}")["id"] for i in range(3)]
    plan_rpc.create(kind="group", children=[outside[0], outside[1]])
    own = plan_rpc.create(task="the run's node")["id"]
    token = plan_rpc.issue("run-f", own, {"read", "delegate", "verdict"})
    before = _plan(plan_rpc)
    foreign = set(before) - {own}
    junk = ["nd-00000000", "", None, 7, ["x"], {"node": "x"}]

    def pick():
        current = list(_plan(plan_rpc))
        mine = [i for i in current if i not in foreign]
        return rng.choice(mine * 3 + current + junk)

    accepted = 0
    for step in range(STEPS):
        op = rng.choice(["create_node", "create_node", "update_node", "update_node",
                         "cancel_node", "get_node",
                         "list_nodes", "wait_for_nodes", "give_verdict",
                         "instantiate_template", "ack_nodes", "close_node"])
        args = {}
        if op == "create_node":
            args = {"kind": rng.choice(["simple", "group", "sequence"]),
                    "plan_revision": plan_rpc.plan_revision()}
            if args["kind"] == "simple":
                args.update(agent="worker", task="t")
            if rng.random() < 0.6:
                args["parent"] = pick()
            if rng.random() < 0.3:
                args["depends_on"] = [{"node": pick()}]
            if args["kind"] != "simple" and rng.random() < 0.4:
                args["children"] = [pick()]
        elif op in ("update_node", "cancel_node"):
            target = pick()
            rev = before.get(target, {}).get("revision", 1) if isinstance(target, str) else 1
            try:
                rev = plan_rpc.get(target)["revision"]
            except Exception:
                pass
            args = {"id": target, "revision": rev}
            if op == "update_node":
                field = rng.choice(["task", "children", "depends_on", "inputs", "urgent", "parent"])
                args[field] = {"task": "changed", "children": [pick()],
                               "depends_on": [{"node": pick()}], "inputs": [{"node": pick()}],
                               "urgent": True, "parent": pick()}[field]
        elif op == "get_node":
            args = {rng.choice(["id", "node_id"]): pick()}
        elif op == "list_nodes":
            args = {"parent": pick()} if rng.random() < 0.5 else {}
        elif op == "wait_for_nodes":
            args = {"cursor": 0, "timeout": 0, **({"node_ids": [pick()]} if rng.random() < .5 else {})}
        else:
            args = {"node_id": pick(), "id": pick(), "parent": pick(), "cursor": 0,
                    "revision": 1, "name": "x"}
        if rng.random() < 0.3:
            args.update(caller="root", run_id="root")
        reply = plan_rpc.rpc(op, args, token)
        accepted += reply.get("ok") is True and op in ("create_node", "update_node", "cancel_node")
        now = _plan(plan_rpc)
        for fid in foreign:
            assert now[fid] == before[fid], (
                f"seed={seed} step={step} {op} {args} -> {reply}: changed {fid}\n"
                f"before={before[fid]}\nafter={now[fid]}")
        scope = _subtree(now, own)
        text = json.dumps(reply)
        for fid in foreign:
            if fid not in scope and reply.get("ok") is True:
                assert fid not in text, (
                    f"seed={seed} step={step} {op} {args} leaked {fid}: {reply}")
        # the run's own node changes only by gaining children
        before[own] = now[own]
    assert accepted >= 10, f"seed={seed}: only {accepted} run writes accepted; fuzz is vacuous"


def _acyclic_and_consistent(nodes):
    graph = {i: set() for i in nodes}
    for i, n in nodes.items():
        p = n["parent"]
        assert p is None or (p in nodes and i in nodes[p]["children"]), (i, p)
        for c in n["children"]:
            assert nodes[c]["parent"] == i, (i, c)
            graph[i].add(c)
        for f in ("depends_on", "inputs"):
            for r in n[f]:
                graph[i].add(r["node"])
        cur, seen = p, set()
        while cur is not None:
            assert cur not in seen and cur != i, ("containment cycle", i)
            seen.add(cur)
            graph[i].update(r["node"] for r in nodes[cur]["depends_on"])
            cur = nodes[cur]["parent"]
        if n["kind"] in ("sequence", "loop"):
            for a, b in zip(n["children"], n["children"][1:]):
                graph[b].add(a)
    state = {}

    def visit(i):
        if state.get(i) == 1:
            raise AssertionError(f"cycle through {i}")
        if state.get(i) == 2:
            return
        state[i] = 1
        for d in graph[i]:
            visit(d)
        state[i] = 2

    for i in graph:
        visit(i)


@pytest.mark.parametrize("seed", [11, 12])
def test_adv_fuzz_root_writes_keep_the_plan_valid_or_leave_it_identical(plan_rpc, seed):
    rng = random.Random(seed)
    accepted = 0
    for step in range(STEPS):
        before = plan_rpc.snapshot()
        plan = {n["id"]: n for n in before["nodes"]}
        ids = list(plan) or ["nd-00000000"]
        op = rng.choice(["create", "create", "update", "cancel"])
        if op == "create":
            kind = rng.choice(["simple", "simple", "group", "sequence", "loop"])
            args = {"kind": kind, "plan_revision": before["plan_revision"]}
            if kind == "simple":
                args.update(agent="worker", task="t")
            if rng.random() < 0.4:
                args["parent"] = rng.choice(ids)
            if rng.random() < 0.4:
                args["depends_on"] = [{"node": rng.choice(ids)}]
            if kind != "simple":
                args["children"] = rng.sample(ids, k=min(len(ids), rng.randint(0, 2)))
                if kind == "loop":
                    args["loop"] = {"verdict_child": (args["children"] or ["x"])[-1],
                                    "max_rounds": rng.randint(0, 2)}
            reply = plan_rpc.rpc("create_node", args)
        else:
            target = rng.choice(ids)
            rev = plan.get(target, {}).get("revision", 1)
            if op == "cancel":
                reply = plan_rpc.rpc("cancel_node", {"id": target, "revision": rev})
            else:
                field = rng.choice(["depends_on", "children", "inputs"])
                value = ([{"node": rng.choice(ids)}] if field != "children"
                         else rng.sample(ids, k=min(len(ids), rng.randint(0, 2))))
                reply = plan_rpc.rpc("update_node", {"id": target, "revision": rev, field: value})
        snapshot = plan_rpc.snapshot()
        after = {n["id"]: n for n in snapshot["nodes"]}
        accepted += reply.get("ok") is True
        if reply.get("ok") is not True:
            assert snapshot == before, (
                f"seed={seed} step={step} refused {reply} but plan changed\n"
                f"before={before}\nafter={snapshot}")
        try:
            _acyclic_and_consistent(after)
        except AssertionError as exc:
            raise AssertionError(f"seed={seed} step={step} {op} {reply}: {exc}") from None
    assert accepted >= 20, f"seed={seed}: only {accepted} root writes accepted; fuzz is vacuous"
