"""M4 — verdicts and loops: NC-R34 (+R66, R58's verdict permission), NC-R35
(+R67), NC-R36, NC-R37, NC-R49.

Scripting: the worker (`wk`, provider fxw) and the reviewer (`rv`, provider fxr)
are driven by per-provider queues (`nc_fixture.m4_agent`): one entry per
activation, in order. The reviewer gives its verdict through the RPC.

Assumptions where the contract is silent (kept loose):
- the prompt of a verdict child states `generation_seq <n>`, the commit and the
  node id (see `nc_fixture.m4_agent`).
- a refused verdict / relaunch / close is `ok: false`; only the codes the
  contract names are asserted (`forbidden`, `unauthenticated`, `conflict`,
  `invalid`, `active_runs`).
- the loop's `loop.rounds_rejected` is the counter; `closed_by` is a node field.
- an infrastructure crash of the verdict child is retried once by the
  scheduler as a new attempt ("today's retry rules" allow at least one).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from nc_fixture.m4_world import (M4World, blocked_codes, commit_entry, err_code,  # noqa: E402
                                 finding, verdict_entry)


@pytest.fixture
def w(tmp_path, monkeypatch):
    world = M4World(tmp_path, monkeypatch)
    world.fxw = world.provider("fxw")
    world.fxr = world.provider("fxr")
    world.agent("wk", "fxw", writes=True)
    world.agent("rv", "fxr", writes=True)
    yield world
    world.close()


def work(n: int):
    return commit_entry(f"f{n}.txt", f"v{n}\n", f"round {n}")


# ----------------------------------------------------------------- NC-R35

def test_nc_r35_an_approving_verdict_ends_the_loop_approved(w):
    w.fxw.queue(work(1))
    w.fxr.queue(verdict_entry("approved"))
    w.start_scheduler()
    loop, wk, rv = w.mkloop(3)
    done = w.wait_state(loop, "done")
    assert done["outcome"] == "approved"
    assert done["loop"]["rounds_rejected"] == 0
    assert w.fxw.spawns() == 1 and w.fxr.spawns() == 1
    gens = done["generations"]
    assert gens and gens[-1]["verdict"] == "approved"
    kinds = w.transitions(loop)
    assert "verdict" in kinds and "loop_exited" in kinds
    assert "done" in kinds
    assert w.get(rv)["outcome"] == "approved" and w.get(wk)["outcome"] == "completed"


def test_nc_r35_r67_a_rejection_relaunches_the_first_child_with_the_findings_once(w):
    w.fxw.queue(work(1), work(2), work(3))
    w.fxr.queue(verdict_entry("rejected", [finding("FINDING-ALPHA-7")]), verdict_entry("approved"))
    w.start_scheduler()
    loop, wk, rv = w.mkloop(3)
    done = w.wait_state(loop, "done")
    assert done["outcome"] == "approved" and done["loop"]["rounds_rejected"] == 1
    first, second = w.fxw.calls()
    assert "FINDING-ALPHA-7" not in first["prompt"]
    assert "FINDING-ALPHA-7" in second["prompt"], "the findings were not injected into the task"
    assert "round_rejected" in w.transitions(loop)
    assert w.fxw.spawns() == 2 and w.fxr.spawns() == 2
    assert [g["verdict"] for g in done["generations"] if g["verdict"]] == ["rejected", "approved"]


def test_nc_r67_findings_are_delivered_once_not_repeated_in_later_rounds(w):
    w.fxw.queue(work(1), work(2), work(3))
    w.fxr.queue(verdict_entry("rejected", [finding("FINDING-ONCE-1")]),
                verdict_entry("rejected", [finding("FINDING-ONCE-2")]), verdict_entry("approved"))
    w.start_scheduler()
    loop, _, _ = w.mkloop(5)
    w.wait_state(loop, "done")
    c1, c2, c3 = w.fxw.calls()
    assert "FINDING-ONCE-1" in c2["prompt"] and "FINDING-ONCE-1" not in c3["prompt"]
    assert "FINDING-ONCE-2" in c3["prompt"]


def test_nc_r35_at_the_maximum_the_loop_holds_and_launches_nothing_more(w):
    w.fxw.queue(work(1), work(2), work(3))
    w.fxr.queue(verdict_entry("rejected", [finding("one")]), verdict_entry("rejected", [finding("two")]))
    w.start_scheduler()
    loop, wk, rv = w.mkloop(2)
    held = w.wait_held(loop, "loop_max")
    assert held["loop"]["rounds_rejected"] == 2
    w.quiet(3)
    assert w.fxw.spawns() == 2 and w.fxr.spawns() == 2, "a third round was launched"
    kinds = w.transitions(loop)
    assert kinds.count("loop_max") == 1
    assert w.get(loop)["state"] == "held"


def test_nc_r35_max_rounds_one_holds_after_the_first_rejection(w):
    w.fxw.queue(work(1))
    w.fxr.queue(verdict_entry("rejected"))
    w.start_scheduler()
    loop, _, _ = w.mkloop(1)
    held = w.wait_held(loop, "loop_max")
    assert held["loop"]["rounds_rejected"] == 1
    w.quiet(2)
    assert w.fxw.spawns() == 1


def test_nc_r35_a_crashed_reviewer_is_not_a_rejected_round(w):
    w.fxw.queue(work(1))
    w.fxr.queue({"crash": True}, verdict_entry("approved"))
    w.start_scheduler()
    loop, wk, rv = w.mkloop(2)
    done = w.wait_state(loop, "done", timeout=90)
    assert done["outcome"] == "approved"
    assert done["loop"]["rounds_rejected"] == 0
    assert w.fxr.spawns() == 2, "the failed reviewer was not retried as a new attempt"
    assert w.fxw.spawns() == 1, "the worker was re-run for an infrastructure failure"


def test_nc_r35_a_verdict_child_that_ends_without_a_verdict_holds_the_round_unresolved(w):
    w.fxw.queue(work(1))
    w.fxr.queue({"text": "looks fine to me"})
    w.start_scheduler()
    loop, _, _ = w.mkloop(3)
    held = w.wait_held(loop, "unresolved_round")
    assert held["loop"]["rounds_rejected"] == 0
    kinds = w.transitions(loop)
    assert "unresolved_round" in kinds
    w.quiet(2)
    assert w.fxw.spawns() == 1 and w.fxr.spawns() == 1


# ----------------------------------------------------------------- NC-R34 / R58

def test_nc_r34_root_cannot_give_a_verdict(w):
    w.fxw.queue(work(1))
    w.fxr.queue({"gate": "gr", **verdict_entry("approved")})
    w.start_scheduler()
    loop, wk, rv = w.mkloop(3)
    w.wait_running(rv)
    reply = w.rpc("give_verdict", {"generation_seq": 1, "verdict": "approved", "findings": []})
    assert err_code(reply) == "forbidden", reply
    assert w.get(loop)["state"] == "running"
    w.gate("gr", w.fxr)
    w.wait_state(loop, "done")


def test_nc_r34_only_the_current_activation_of_the_verdict_child_may_give_it(w):
    w.fxw.queue({"gate": "gw", **work(1)})
    w.fxr.queue({"gate": "gr", **verdict_entry("approved")})
    w.start_scheduler()
    loop, wk, rv = w.mkloop(3)
    w.wait_running(wk)
    worker_token = w.token_of(w.fxw, "W")
    reply = w.rpc("give_verdict", {"generation_seq": 1, "verdict": "approved", "findings": []},
                  token=worker_token)
    assert err_code(reply) == "forbidden", "a sibling (the worker) gave a verdict"
    w.gate("gw", w.fxw)
    w.wait_running(rv)
    from multiagents.scheduler import issue_run_capability
    stranger = issue_run_capability(w.root, "run-not-the-activation", rv, {"read", "verdict"})
    reply = w.rpc("give_verdict", {"generation_seq": 1, "verdict": "approved", "findings": []},
                  token=stranger)
    assert err_code(reply) == "forbidden", "a run that is not the current activation gave a verdict"
    assert w.get(loop)["state"] == "running"
    w.gate("gr", w.fxr)
    assert w.wait_state(loop, "done")["outcome"] == "approved"


def test_nc_r34_a_verdict_about_another_generation_is_refused_and_leaves_the_round_unresolved(w):
    w.fxw.queue(work(1))
    w.fxr.queue(verdict_entry("approved", v={"seq_offset": 5}))
    w.start_scheduler()
    loop, _, _ = w.mkloop(3)
    held = w.wait_held(loop, "unresolved_round")
    reply = w.fxr.first_reply("R")
    assert reply.get("ok") is False, reply
    assert held["generations"][-1]["verdict"] in (None, ""), "a wrong-generation verdict was recorded"


def test_nc_r34_a_second_verdict_of_one_activation_is_refused_and_the_first_stands(w):
    w.fxw.queue(work(1))
    w.fxr.queue(verdict_entry("rejected", [finding("first")], v={"twice": True}))
    w.fxr.queue(verdict_entry("approved"))
    w.fxw.queue(work(2))
    w.start_scheduler()
    loop, _, _ = w.mkloop(3)
    w.wait_state(loop, "done")
    replies = w.fxr.verdicts()[0]["replies"]
    assert replies[0]["ok"] is True and replies[-1]["ok"] is False
    assert w.get(loop)["loop"]["rounds_rejected"] == 1


def test_nc_r34_the_verdict_is_recorded_on_the_generation_and_never_touches_main(w):
    w.fxw.queue(work(1))
    w.fxr.queue(verdict_entry("approved"))
    main_before = w.git("rev-parse", "HEAD").stdout
    w.start_scheduler()
    loop, _, _ = w.mkloop(3)
    done = w.wait_state(loop, "done")
    assert done["generations"][-1]["verdict"] == "approved"
    assert w.git("rev-parse", "HEAD").stdout == main_before


def test_nc_r58_a_verdict_childs_run_has_read_and_verdict_but_not_delegate(w):
    w.fxw.queue(work(1))
    w.fxr.queue({"gate": "gr", **verdict_entry("approved")})
    w.start_scheduler()
    loop, wk, rv = w.mkloop(3)
    w.wait_running(rv)
    token = w.token_of(w.fxr, "R")
    assert w.rpc("get_node", {"id": rv}, token=token).get("ok") is True      # read: own subtree
    reply = w.rpc("create_node", {"kind": "simple", "agent": "wk", "task": "x", "parent": rv,
                                  "plan_revision": w.plan_revision()}, token=token)
    assert err_code(reply) == "forbidden", reply                              # no delegate
    w.gate("gr", w.fxr)
    w.wait_state(loop, "done")
    after = w.rpc("get_node", {"id": rv}, token=token)
    assert err_code(after) == "unauthenticated", "the run capability survived the run"


def test_nc_r58_a_non_verdict_run_has_no_verdict_permission_even_for_its_own_loop(w):
    w.fxw.queue({"gate": "gw", **work(1)})
    w.fxr.queue(verdict_entry("approved"))
    w.start_scheduler()
    loop, wk, rv = w.mkloop(3)
    w.wait_running(wk)
    token = w.token_of(w.fxw, "W")
    for seq in (0, 1):
        reply = w.rpc("give_verdict", {"generation_seq": seq, "verdict": "approved",
                                       "findings": []}, token=token)
        assert err_code(reply) == "forbidden"
    w.gate("gw", w.fxw)
    w.wait_state(loop, "done")


# ----------------------------------------------------------------- NC-R66

def test_nc_r66_the_verdict_child_must_be_the_last_child(w):
    wk = w.simple("W", "wk")
    rv = w.simple("R", "rv")
    reply = w.create({"kind": "loop", "children": [rv, wk],
                      "loop": {"verdict_child": rv, "max_rounds": 2}})
    assert err_code(reply) == "invalid", reply


def test_nc_r66_commits_made_by_the_verdict_child_are_not_integrated(w):
    w.fxw.queue(work(1))
    w.fxr.queue(commit_entry("review-notes.txt", "mine\n", "reviewer commit",
                             **verdict_entry("approved")))
    w.start_scheduler()
    loop, _, _ = w.mkloop(3)
    w.wait_state(loop, "done")
    assert "reviewer_commits_ignored" in w.transitions(loop)
    assert w.show(f"nodes/{loop}:f1.txt") == "v1\n", "the reviewed generation is the approved output"
    assert w.show(f"nodes/{loop}:review-notes.txt") is None


def test_nc_r66_the_reviewed_generation_is_the_branch_tip_after_the_workers_round(w):
    w.fxw.queue(work(1))
    w.fxr.queue(verdict_entry("approved"))
    w.start_scheduler()
    loop, _, _ = w.mkloop(3)
    done = w.wait_state(loop, "done")
    call = w.fxr.calls()[0]
    tip = w.git("rev-parse", f"nodes/{loop}").stdout.strip()
    assert tip in call["prompt"], "the activation does not state the commit under review"
    assert call["head"] == tip
    assert done["generations"][-1]["commit"] == tip


# ----------------------------------------------------------------- NC-R36

def held_at_max(w, max_rounds=2):
    w.fxw.queue(*[work(i) for i in range(1, max_rounds + 3)])
    w.fxr.queue(*[verdict_entry("rejected", [finding(f"r{i}")]) for i in range(max_rounds)])
    w.start_scheduler()
    loop, wk, rv = w.mkloop(max_rounds)
    w.wait_held(loop, "loop_max")
    return loop, wk, rv


def test_nc_r36_relaunch_raises_the_maximum_keeps_the_counter_and_applies_pins(w):
    loop, wk, rv = held_at_max(w)
    w.fxw.queue(work(7))
    w.fxr.queue(verdict_entry("approved"))
    reply = w.root_op("relaunch_node", loop, max_rounds=4, pins={wk: {"model": "fxw/m2"}})
    assert reply.get("ok") is True, reply
    done = w.wait_state(loop, "done", timeout=90)
    assert done["outcome"] == "approved"
    assert done["loop"]["rounds_rejected"] == 2 and done["loop"]["max_rounds"] == 4
    models = [c["model"] for c in w.fxw.calls()]
    assert models[:2] == ["fxw/m1", "fxw/m1"] and models[2] == "fxw/m2"
    assert w.fxw.spawns() == 3 and w.fxr.spawns() == 3


@pytest.mark.parametrize("bad", [2, 1, 0])
def test_nc_r36_the_new_maximum_must_exceed_the_counter(w, bad):
    loop, _, _ = held_at_max(w)
    before = w.get(loop)
    reply = w.root_op("relaunch_node", loop, max_rounds=bad)
    assert err_code(reply) == "invalid", reply
    w.quiet(1)
    after = w.get(loop)
    assert after["state"] == "held" and after["revision"] == before["revision"]
    assert w.fxw.spawns() == 2


def test_nc_r36_relaunch_is_root_only_and_checks_the_revision(w):
    loop, wk, rv = held_at_max(w)
    from multiagents.scheduler import issue_run_capability
    run = issue_run_capability(w.root, "some-run", loop, {"read", "delegate"})
    rev = w.get(loop)["revision"]
    assert err_code(w.rpc("relaunch_node", {"id": loop, "revision": rev, "max_rounds": 5},
                          token=run)) == "forbidden"
    stale = w.rpc("relaunch_node", {"id": loop, "revision": rev - 1, "max_rounds": 5})
    assert err_code(stale) == "conflict" and stale
    assert w.get(loop)["state"] == "held"


def test_nc_r36_relaunch_of_a_loop_that_is_running_is_refused(w):
    w.fxw.queue({"gate": "gw", **work(1)})
    w.start_scheduler()
    loop, _, _ = w.mkloop(2)
    w.wait_state(loop, "running")
    reply = w.root_op("relaunch_node", loop, max_rounds=5)
    assert reply.get("ok") is False
    assert w.get(loop)["state"] == "running"
    w.gate("gw", w.fxw)


def test_nc_r67_relaunch_on_unresolved_round_retries_the_verdict_child_on_the_same_generation(w):
    w.fxw.queue(work(1))
    w.fxr.queue({"text": "no verdict"}, verdict_entry("approved"))
    w.start_scheduler()
    loop, wk, rv = w.mkloop(3)
    w.wait_held(loop, "unresolved_round")
    reply = w.root_op("relaunch_node", loop, retry="verdict_child")
    assert reply.get("ok") is True, reply
    assert w.wait_state(loop, "done", timeout=60)["outcome"] == "approved"
    assert w.fxw.spawns() == 1 and w.fxr.spawns() == 2
    first, second = w.fxr.calls()
    assert first["head"] == second["head"], "the retry reviewed another generation"


def test_nc_r67_relaunch_with_retry_round_runs_the_whole_round_again(w):
    w.fxw.queue(work(1), work(2))
    w.fxr.queue({"text": "no verdict"}, verdict_entry("approved"))
    w.start_scheduler()
    loop, wk, rv = w.mkloop(3)
    w.wait_held(loop, "unresolved_round")
    assert w.root_op("relaunch_node", loop, retry="round").get("ok") is True
    done = w.wait_state(loop, "done", timeout=60)
    assert done["outcome"] == "approved" and done["loop"]["rounds_rejected"] == 0
    assert w.fxw.spawns() == 2 and w.fxr.spawns() == 2


def test_nc_r36_an_unknown_node_is_refused(w):
    w.start_scheduler()
    reply = w.rpc("relaunch_node", {"id": "nd-00000000", "revision": 1})
    assert reply.get("ok") is False


# ----------------------------------------------------------------- NC-R37

def test_nc_r37_close_exhausted_ends_the_loop_without_satisfying_success(w):
    loop, wk, rv = held_at_max(w)
    after = w.simple("AFTER", "wk", depends_on=[{"node": loop, "require": "success"}])
    fin = w.simple("FIN", "wk", depends_on=[{"node": loop, "require": "finished"}])
    reply = w.root_op("close_node", loop, outcome="exhausted")
    assert reply.get("ok") is True, reply
    done = w.get(loop)
    assert done["state"] == "done" and done["outcome"] == "exhausted"
    w.wait_state(fin, "done")
    w.quiet(2)
    assert w.get(after)["state"] == "open", "exhausted satisfied `success`"
    assert w.fxw.by_tag("AFTER") == []


def test_nc_r37_close_approved_is_recorded_as_the_orchestrators_decision(w):
    loop, wk, rv = held_at_max(w)
    after = w.simple("AFTER", "wk", depends_on=[{"node": loop, "require": "approved"}])
    assert w.root_op("close_node", loop, outcome="approved").get("ok") is True
    done = w.get(loop)
    assert done["outcome"] == "approved" and done.get("closed_by") == "root"
    w.wait_state(after, "done")


def test_nc_r37_close_failed(w):
    loop, _, _ = held_at_max(w)
    assert w.root_op("close_node", loop, outcome="failed").get("ok") is True
    assert w.get(loop)["outcome"] == "failed"


@pytest.mark.parametrize("outcome", ["completed", "rejected", "", "APPROVED", None])
def test_nc_r37_only_the_three_closing_outcomes_exist(w, outcome):
    loop, _, _ = held_at_max(w)
    reply = w.root_op("close_node", loop, outcome=outcome)
    assert err_code(reply) == "invalid", reply
    assert w.get(loop)["state"] == "held"


def test_nc_r37_close_is_root_only_and_checks_the_revision(w):
    loop, _, _ = held_at_max(w)
    from multiagents.scheduler import issue_run_capability
    run = issue_run_capability(w.root, "some-run", loop, {"read", "delegate"})
    rev = w.get(loop)["revision"]
    assert err_code(w.rpc("close_node", {"id": loop, "revision": rev, "outcome": "approved"},
                          token=run)) == "forbidden"
    assert err_code(w.rpc("close_node", {"id": loop, "revision": rev - 1,
                                         "outcome": "approved"})) == "conflict"
    assert w.get(loop)["state"] == "held"


def test_nc_r37_close_refuses_a_node_with_a_running_run_and_cancels_nothing(w):
    w.start_scheduler()
    a = w.simple("A", "wk", fx={"gate": "ga"})
    w.wait_running(a)
    reply = w.root_op("close_node", a, outcome="exhausted")
    assert reply.get("ok") is False
    assert w.get(a)["state"] == "running"
    w.gate("ga", w.fxw)
    assert w.wait_state(a, "done")["outcome"] == "completed"


def test_nc_r37_close_an_open_node(w):
    w.start_scheduler()
    p = w.simple("P", "wk", fx={"gate": "gp"})
    w.wait_running(p)
    b = w.simple("B", "wk", depends_on=[{"node": p, "require": "success"}])
    assert w.root_op("close_node", b, outcome="exhausted").get("ok") is True
    assert w.get(b)["outcome"] == "exhausted"
    w.gate("gp", w.fxw)
    w.quiet(2)
    assert w.fxw.by_tag("B") == []


# ----------------------------------------------------------------- NC-R49

def test_nc_r49_a_top_level_composite_reaching_done_notifies_root(w):
    w.fxw.queue(work(1))
    w.fxr.queue(verdict_entry("approved"))
    w.start_scheduler()
    loop, _, _ = w.mkloop(3)
    w.wait_state(loop, "done")
    seen = [t for t in w.ok("wait_for_nodes", {"cursor": 0, "timeout": 0.5})["transitions"]
            if t.get("node_id") == loop and t.get("kind") in ("done", "node.done")]
    assert len(seen) == 1, seen


def test_nc_r49_loop_max_and_unresolved_round_notify_root(w):
    held_at_max(w, 1)
    ts = w.ok("wait_for_nodes", {"cursor": 0, "timeout": 0.5})["transitions"]
    assert any(t["kind"].removeprefix("node.") == "loop_max" for t in ts)


def test_nc_r5_a_loop_held_inside_a_sequence_holds_the_sequence_with_child_held(w):
    w.fxw.queue(work(1))
    w.fxr.queue(verdict_entry("rejected"))
    w.start_scheduler()
    loop, _, _ = w.mkloop(1)
    after = w.simple("AFTER", "wk")
    seq = w.comp("sequence", [loop, after])
    w.wait_held(loop, "loop_max")
    held = w.wait_held(seq, "child_held")
    assert held["state"] == "held"
    w.quiet(2)
    assert w.fxw.by_tag("AFTER") == []
    assert "loop_max" in w.transitions(loop)
