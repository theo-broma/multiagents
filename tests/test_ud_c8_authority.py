"""UD-C8 (with D-UD2.2): `decisions.authorize`, the channel x audience x
operation matrix, the tools that ride on it, and the legacy answer path.

`authorize(record, actor, operation)` is assumed to return normally when the
operation is allowed and to raise `DecisionError("forbidden", reason=...)`
when it is not (NEED_INFO in the report: the contract does not say)."""
import asyncio

import pytest

from support.ud2_support import (CLI, MONITOR, ROOT_INIT, ROOT_ORCH, RUN, as_role, as_run, call,
                                 make, n_events, refused, snapshot_of, succeeded, world)  # noqa: F401
from support.ud_support import D, code_of, decision_events, events

REASONS = {"user_only", "not_answerable_here"}
ACTORS = {"root_orch": ROOT_ORCH, "root_init": ROOT_INIT, "run": RUN, "cli": CLI, "monitor": MONITOR}


def allowed(record, actor, operation):
    """True if authorize allows, else the refusal reason."""
    try:
        D.authorize(record, actor, operation)
    except D.DecisionError as exc:
        assert code_of(exc) == "forbidden", exc
        return getattr(exc, "reason", None) or "<no reason>"
    return True


# ------------------------------------------------------ answer: the matrix --

ANSWER_ORCH_AUDIENCE = {"root_orch": True, "root_init": REASONS, "run": REASONS,
                        "cli": True, "monitor": True}
ANSWER_USER_AUDIENCE = {"root_orch": "user_only", "root_init": "user_only", "run": REASONS,
                        "cli": True, "monitor": True}


@pytest.mark.parametrize("actor", sorted(ACTORS))
@pytest.mark.parametrize("audience,expect", [("orchestrator", ANSWER_ORCH_AUDIENCE),
                                              ("user", ANSWER_USER_AUDIENCE)])
@pytest.mark.parametrize("asker", ["orchestrator", "initializer", "agent"])
def test_c8_answer_matrix(tmp_path, actor, audience, expect, asker):
    from support.ud_support import make_tree, create
    from support.ud_support import AGENT
    tree = make_tree(tmp_path)
    by = {"orchestrator": ROOT_ORCH, "initializer": ROOT_INIT,
          "agent": {"role": "agent", "agent": "ag-3", "channel": "mcp_run"}}[asker]
    mode = "free_text" if asker == "agent" else "single"
    rec = create(tree, mode, by=by, audience=audience)
    got = allowed(rec, ACTORS[actor], "answer")
    want = expect[actor]
    if want is True:
        assert got is True, got
    elif isinstance(want, str):
        assert got == want
    else:
        assert got in want, got


def test_c8_authorize_does_not_change_the_record_or_the_store(world):
    rec = make(world, audience="user")
    before, n = snapshot_of(world.tree), n_events(world.tree)
    allowed(rec, ROOT_ORCH, "answer")
    allowed(rec, CLI, "answer")
    assert snapshot_of(world.tree) == before and n_events(world.tree) == n


@pytest.mark.parametrize("channel", ["telepathy", "", "mcp", "legacy", "CLI"])
@pytest.mark.parametrize("role", ["user", "orchestrator"])
@pytest.mark.parametrize("audience", ["orchestrator", "user"])
def test_c8_an_unknown_channel_answers_nothing(tmp_path, channel, role, audience):
    from support.ud_support import make_tree, create
    rec = create(make_tree(tmp_path), audience=audience)
    assert allowed(rec, {"role": role, "agent": "", "channel": channel}, "answer") in REASONS


def test_c8_a_user_role_on_an_mcp_channel_is_not_the_user(tmp_path):
    from support.ud_support import make_tree, create
    rec = create(make_tree(tmp_path), audience="user")
    forged = {"role": "user", "agent": "", "channel": "mcp_root"}
    assert allowed(rec, forged, "answer") in REASONS
    forged = {"role": "user", "agent": "", "channel": "mcp_run"}
    assert allowed(rec, forged, "answer") in REASONS


@pytest.mark.parametrize("actor", [None, "orchestrator", {}, {"role": "user"}, 5])
def test_c8_a_malformed_actor_is_refused_never_allowed(tmp_path, actor):
    from support.ud_support import make_tree, create
    rec = create(make_tree(tmp_path), audience="orchestrator")
    try:
        got = allowed(rec, actor, "answer")
    except D.DecisionError as exc:
        assert code_of(exc) in ("forbidden", "invalid_argument")
        return
    assert got is not True


# ------------------------------------- the other operations: asker's role --

OPS = ["withdraw", "supersede", "mark_handled", "escalate"]


@pytest.mark.parametrize("op", ["withdraw", "supersede", "mark_handled"])
@pytest.mark.parametrize("audience", ["orchestrator", "user"])
def test_c8_withdraw_supersede_mark_handled_belong_to_the_askers_role(tmp_path, op, audience):
    from support.ud_support import make_tree, create
    tree = make_tree(tmp_path)
    by_orch = create(tree, by=ROOT_ORCH, audience=audience)
    by_init = create(tree, by=ROOT_INIT, audience=audience)
    assert allowed(by_orch, ROOT_ORCH, op) is True
    assert allowed(by_init, ROOT_INIT, op) is True
    assert allowed(by_orch, ROOT_INIT, op) is not True
    assert allowed(by_init, ROOT_ORCH, op) is not True
    for outsider in (CLI, MONITOR, RUN):
        assert allowed(by_orch, outsider, op) is not True
        assert allowed(by_init, outsider, op) is not True


def test_c8_the_orchestrator_may_mark_an_agent_question_handled_but_not_withdraw_it(tmp_path):
    from support.ud_support import make_tree, create
    q = create(make_tree(tmp_path), "free_text", audience="orchestrator",
               by={"role": "agent", "agent": "ag-3", "channel": "mcp_run"})
    assert allowed(q, ROOT_ORCH, "mark_handled") is True
    assert allowed(q, ROOT_ORCH, "withdraw") is not True
    assert allowed(q, ROOT_ORCH, "supersede") is not True
    assert allowed(q, ROOT_INIT, "mark_handled") is not True
    assert allowed(q, CLI, "mark_handled") is not True


def test_c8_escalate_is_the_orchestrators_and_the_askers(tmp_path):
    from support.ud_support import make_tree, create
    tree = make_tree(tmp_path)
    own = create(tree, by=ROOT_ORCH, audience="orchestrator")
    q = create(tree, "free_text", audience="orchestrator",
               by={"role": "agent", "agent": "ag-3", "channel": "mcp_run"})
    assert allowed(own, ROOT_ORCH, "escalate") is True
    assert allowed(q, ROOT_ORCH, "escalate") is True
    assert allowed(own, RUN, "escalate") is not True
    assert allowed(q, RUN, "escalate") is not True


def test_c8_an_unknown_operation_is_never_allowed(tmp_path):
    from support.ud_support import make_tree, create
    rec = create(make_tree(tmp_path), audience="orchestrator")
    try:
        got = allowed(rec, ROOT_ORCH, "delete")
    except D.DecisionError as exc:
        assert code_of(exc) in ("forbidden", "invalid_argument")
        return
    assert got is not True


# --------------------------------------------- the same matrix over the tools --

def test_c8_a_user_decision_answered_over_mcp_is_refused_and_unchanged(world):
    rec = make(world, audience="user")
    before, n = snapshot_of(world.tree), n_events(world.tree)
    out = call("answer_decision", id=rec["id"], revision=1, options=["yes"])
    refused(out, "forbidden", reason="user_only")
    assert snapshot_of(world.tree) == before and n_events(world.tree) == n


def test_c8_the_initializer_cannot_answer_even_an_orchestrator_decision(world, monkeypatch):
    rec = make(world, audience="orchestrator", by=ROOT_INIT)
    as_role(monkeypatch, "initializer")
    out = call("answer_decision", id=rec["id"], revision=1, options=["yes"])
    assert out.get("error") == "forbidden" and out.get("reason") in REASONS, out
    assert D.get(world.tree, rec["id"])["status"] == "open"


def test_c8_a_run_cannot_answer_an_orchestrator_decision_over_mcp(world, monkeypatch):
    rec = make(world, audience="orchestrator", by=ROOT_INIT)
    as_run(monkeypatch)
    out = call("answer_decision", id=rec["id"], revision=1, options=["yes"])
    assert out.get("error") == "forbidden" and out.get("reason") in REASONS, out
    assert D.get(world.tree, rec["id"])["status"] == "open"


def test_c8_the_user_answers_a_user_decision_through_the_module_and_the_channel_is_recorded(world):
    rec = make(world, audience="user")
    for actor in (CLI, MONITOR):
        fresh = make(world, audience="user")
        D.authorize(fresh, actor, "answer")
        new = D.answer(world.tree, fresh["id"], revision=1, by=actor, options=["yes"])["decision"]
        assert new["answer"]["answered_by"]["channel"] == actor["channel"]
        assert new["history"][-1]["by"]["channel"] == actor["channel"]


def test_c8_a_refusal_changes_nothing_whatever_the_operation(world):
    rec = make(world, by=ROOT_INIT, audience="user")
    before, n = snapshot_of(world.tree), n_events(world.tree)
    refused(call("withdraw_decision", id=rec["id"], revision=1, reason="r"), "forbidden")
    refused(call("answer_decision", id=rec["id"], revision=1, options=["yes"]), "forbidden")
    assert snapshot_of(world.tree) == before and n_events(world.tree) == n


# ------------------------------------------------- the legacy answer path --

def _escalated_agent_question(world):
    q = world.tree.add_question("ag-3", "store", "Postgres?", "SQLite")
    succeeded(call("escalate_decision", id=q["id"], revision=1))
    return q


def test_c8_the_legacy_mcp_answer_of_an_escalated_question_is_refused(world):
    q = _escalated_agent_question(world)
    before, n = snapshot_of(world.tree), n_events(world.tree)
    out = call("answer_question", question_id=q["id"], answer="postgres")
    assert out.get("error") and "forbidden" in str(out["error"]).lower(), out
    assert not out.get("resumed")
    assert snapshot_of(world.tree) == before and n_events(world.tree) == n
    assert world.tree.get_question(q["id"])["status"] == "open"


def test_c8_the_legacy_runner_answer_of_an_escalated_question_is_refused(world):
    q = _escalated_agent_question(world)
    before, n = snapshot_of(world.tree), n_events(world.tree)
    out = asyncio.run(world.runner.answer_question(q["id"], "postgres"))
    assert out.get("error") and "forbidden" in str(out["error"]).lower(), out
    assert not out.get("resumed")
    assert snapshot_of(world.tree) == before and n_events(world.tree) == n


def test_c8_the_refusal_comes_before_any_resume_check(world):
    # No node and no session exist for the asking agent: the refusal must still
    # be `forbidden`, not "unknown agent" or "no resumable session".
    q = _escalated_agent_question(world)
    out = call("answer_question", question_id=q["id"], answer="postgres")
    assert "forbidden" in str(out.get("error", "")).lower(), out


def test_c8_a_non_escalated_agent_question_is_still_answerable_by_the_legacy_path(world):
    q = world.tree.add_question("ag-3", "store", "Postgres?")
    out = call("answer_question", question_id=q["id"], answer="postgres")
    # no node or session in this world: the legacy error, not a refusal
    assert "forbidden" not in str(out.get("error", "")).lower(), out


def test_c8_an_already_answered_escalated_question_is_not_reopened_by_the_refusal(world):
    q = _escalated_agent_question(world)
    D.answer(world.tree, q["id"], revision=2, by=CLI, free_text="postgres")
    out = call("answer_question", question_id=q["id"], answer="sqlite")
    assert out.get("error")
    assert D.get(world.tree, q["id"])["answer"]["free_text"] == "postgres"


def test_c8_escalate_then_cli_answer_works(world):
    q = _escalated_agent_question(world)
    D.authorize(D.get(world.tree, q["id"]), CLI, "answer")
    new = D.answer(world.tree, q["id"], revision=2, by=CLI, free_text="postgres")["decision"]
    assert new["status"] == "answered" and new["answer"]["answered_by"] == CLI
