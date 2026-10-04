"""Adversary, M3: verdicts, disposal and merge refusals on hand-built plans
(NC-R47, NC-R64, NC-R66, NC-R86).

Each test lays the plan out as records and drives the RPC service directly, so
it runs in about a second and does not depend on the loop prompt fixture.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from nc_fixture.m3_adv import Harness  # noqa: E402
from nc_fixture.world import err_code  # noqa: E402
from multiagents.scheduler import issue_run_capability  # noqa: E402
from multiagents.scheduler.engine import attempts, save_attempt  # noqa: E402


@pytest.fixture
def h(tmp_path, monkeypatch):
    harness = Harness(tmp_path, monkeypatch)
    yield harness
    harness.close()


def request(h, op, args, token=None, request_id=None):
    if token is None:
        with h.service.store.transaction(write=False) as db:
            token = h.service.store.meta(db, "root_token")
    request_id = request_id or f"{op}-{len(h.journal())}-{id(args)}"
    return h.service.request({"op": op, "token": token, "args": args, "request_id": request_id})


def reviewed_loop(h):
    """loop L = [impl (done, generation 1), rev (launched reviewer of gen 1)];
    returns (L, impl, rev, rev token, generation commit)."""
    tip = h.world.main_tip()
    impl, rev = h.record(), h.record()
    loop = h.record(kind="loop", children=[impl["id"], rev["id"]],
                    loop={"verdict_child": rev["id"], "max_rounds": 3})
    generation = {"seq": 1, "commit": tip, "run_id": "ag-111111", "verdict": None}
    impl.update(parent=loop["id"], state="done", outcome="completed", generations=[generation])
    rev["parent"] = loop["id"]
    loop.update(state="running", generations=[dict(generation)])
    h.save(loop, impl, rev)
    attempt, run = h.launch(rev["id"])
    with h.service.store.transaction() as db:
        current = attempts(db)[attempt["attempt_id"]]
        current["review"] = {"node_id": loop["id"], "generation_seq": 1, "commit": tip}
        save_attempt(db, current)
    token = issue_run_capability(h.world.root, run.id, rev["id"], {"read", "verdict"})
    return loop, impl, rev, token, tip


def verdict_args(loop, tip, **over):
    args = {"node_id": loop["id"], "generation_seq": 1, "commit": tip,
            "verdict": "approved", "findings": []}
    args.update(over)
    return args


# --------------------------------------------------------------- NC-R47 verdicts

@pytest.mark.parametrize("over", [{"generation_seq": 2}, {"commit": "0" * 40}])
def test_adv_verdict_about_the_wrong_generation_is_refused_and_records_nothing(h, over):
    loop, _, _, token, tip = reviewed_loop(h)
    reply = request(h, "give_verdict", verdict_args(loop, tip, **over), token)
    assert reply.get("ok") is False, reply
    assert not h.nodes()[loop["id"]].get("pending_verdict")


def test_adv_a_second_verdict_does_not_replace_the_first(h):
    loop, _, _, token, tip = reviewed_loop(h)
    first = request(h, "give_verdict", verdict_args(loop, tip), token, "v-1")
    assert first.get("ok"), first
    second = request(h, "give_verdict", verdict_args(loop, tip, verdict="rejected"), token, "v-2")
    assert second.get("ok") is False, second
    assert h.nodes()[loop["id"]]["pending_verdict"]["verdict"] == "approved"


def test_adv_the_reviewer_of_another_loop_cannot_judge_this_one(h):
    loop, _, _, _, tip = reviewed_loop(h)
    other, _, _, other_token, _ = reviewed_loop(h)
    reply = request(h, "give_verdict", verdict_args(loop, tip), other_token)
    assert err_code(reply) == "forbidden", reply
    assert not h.nodes()[loop["id"]].get("pending_verdict")


def test_adv_a_non_reviewer_child_cannot_give_a_verdict(h):
    loop, impl, _, _, tip = reviewed_loop(h)
    token = issue_run_capability(h.world.root, "ag-222222", impl["id"], {"read", "delegate"})
    reply = request(h, "give_verdict", verdict_args(loop, tip), token)
    assert err_code(reply) == "forbidden", reply
    assert not h.nodes()[loop["id"]].get("pending_verdict")


def test_adv_reviewer_commits_are_never_integrated(h):
    loop, _, rev, _, _ = reviewed_loop(h)
    attempt = next(a for a in h.journal().values() if a["node_id"] == rev["id"])
    run = h.engine.runner.tree.get(attempt["run_id"])
    before = h.world.tip(loop["id"])
    h.commit(run, {"reviewer.txt": "sneaked in\n"})
    h.engine.finished(attempt, run)
    assert h.world.tip(loop["id"]) == before
    assert "reviewer.txt" not in h.world.files(h.world.tip(loop["id"]))
    assert h.nodes()[rev["id"]]["generations"] == []


# --------------------------------------------------------------- NC-R64 dispose

def test_adv_dispose_is_refused_while_a_descendant_is_active(h):
    group, (a, b) = h.tree("group", 2)
    h.launch(a["id"])
    before = h.world.all_refs()
    reply = request(h, "dispose_node", {"id": group["id"],
                                        "revision": h.nodes()[group["id"]]["revision"]})
    assert err_code(reply) == "active", reply
    assert h.world.all_refs() == before
    assert "disposed" not in json.dumps(h.nodes()[group["id"]])


def test_adv_dispose_is_refused_while_an_outside_node_takes_a_descendant_as_input(h):
    group, (a, _) = h.tree("group", 1 + 1)
    a.update(state="done", outcome="completed",
             generations=[{"seq": 1, "commit": h.world.main_tip(), "run_id": "ag-333333", "verdict": None}])
    reader = h.record(inputs=[{"node": a["id"]}])
    h.save(a, reader)
    reply = request(h, "dispose_node", {"id": group["id"],
                                        "revision": h.nodes()[group["id"]]["revision"]})
    assert err_code(reply) == "referenced", reply


# --------------------------------------------------------------- NC-R86 merge

def test_adv_merge_node_on_a_child_is_refused_not_top_level(h):
    group, (a, _) = h.tree("group", 2)
    a.update(state="done", outcome="completed")
    h.save(a)
    before = h.world.main_tip()
    reply = request(h, "merge_node", {"id": a["id"]})
    assert err_code(reply) == "not_top_level", reply
    assert h.world.main_tip() == before
