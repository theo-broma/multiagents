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
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

from nc_harness import code, live, nc, stored  # noqa: E402,F401

STEPS = 120


def _plan(live):
    return {n["id"]: {k: v for k, v in stored(n).items() if k != "plan_revision"}
            for n in live.ok("list_nodes", {})["nodes"]}


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
def test_adv_fuzz_run_token_never_touches_or_reads_outside_its_subtree(live, seed):
    rng = random.Random(seed)
    outside = [live.create(task=f"outside {i}")["id"] for i in range(3)]
    live.create(kind="group", children=[outside[0], outside[1]])
    own = live.create(task="the run's node")["id"]
    token = live.issue("run-f", own, {"read", "delegate", "verdict"})
    before = _plan(live)
    foreign = set(before) - {own}
    junk = ["nd-00000000", "", None, 7, ["x"], {"node": "x"}]

    def pick():
        current = list(_plan(live))
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
                    "plan_revision": live.plan_revision()}
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
                rev = live.get(target)["revision"]
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
        reply = live.rpc(op, args, token)
        accepted += reply.get("ok") is True and op in ("create_node", "update_node", "cancel_node")
        now = _plan(live)
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
def test_adv_fuzz_root_writes_keep_the_plan_valid_or_leave_it_identical(live, seed):
    rng = random.Random(seed)
    accepted = 0
    for step in range(STEPS):
        plan = _plan(live)
        ids = list(plan) or ["nd-00000000"]
        op = rng.choice(["create", "create", "update", "cancel"])
        if op == "create":
            kind = rng.choice(["simple", "simple", "group", "sequence", "loop"])
            args = {"kind": kind, "plan_revision": live.plan_revision()}
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
            reply = live.rpc("create_node", args)
        else:
            target = rng.choice(ids)
            rev = plan.get(target, {}).get("revision", 1)
            if op == "cancel":
                reply = live.rpc("cancel_node", {"id": target, "revision": rev})
            else:
                field = rng.choice(["depends_on", "children", "inputs"])
                value = ([{"node": rng.choice(ids)}] if field != "children"
                         else rng.sample(ids, k=min(len(ids), rng.randint(0, 2))))
                reply = live.rpc("update_node", {"id": target, "revision": rev, field: value})
        after = _plan(live)
        accepted += reply.get("ok") is True
        if reply.get("ok") is not True:
            assert after == plan, f"seed={seed} step={step} refused {reply} but plan changed"
        try:
            _acyclic_and_consistent(after)
        except AssertionError as exc:
            raise AssertionError(f"seed={seed} step={step} {op} {reply}: {exc}") from None
    assert accepted >= 20, f"seed={seed}: only {accepted} root writes accepted; fuzz is vacuous"
