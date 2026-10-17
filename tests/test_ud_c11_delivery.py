"""UD-C11: delivery to the asker's session.

Delivery is observed through the public record (`decisions.get(...)["delivery"]`),
the `decision` event, and the Stop-hook watcher that performs it. The agent
question path is observed through `Runner.answer_question`, whose resume
(`Runner.steer`) is the one seam varied.
"""
import asyncio
import time

import pytest

from support.ud_s3_support import (INSIDE, OTHER, OUTSIDE, SESSION, answered_decision,
                                   bind, delivered_events, initializer_decision,
                                   seed_delivery, stable_part, stored, tree_of, watch)
from support.ud_support import (ORCH, USER, D, assert_valid, clock, code_of, create,
                                decision_events, in_status)
from test_dj_seams import live_pid
from test_dj_support import jobs
from test_ud_c4_transitions import PAYLOAD

# ------------------------------------------------------------ first delivery --


def test_c11_first_delivery_records_the_session_and_time(jobs, live_pid):
    tree = tree_of(jobs)
    rec = answered_decision(tree)
    bind(jobs, live_pid.pid)
    before = time.time()
    result = watch(jobs)
    after = time.time()
    assert result.returncode == 2, result.stderr
    got = stored(tree, rec["id"])
    assert got["delivery"]["session"] == SESSION
    assert before - 1 <= got["delivery"]["delivered_at"] <= after + 1
    assert set(got["delivery"]) == {"delivered_at", "session"}
    assert_valid(got)


def test_c11_delivery_bumps_revision_but_is_not_a_transition(jobs, live_pid):
    tree = tree_of(jobs)
    rec = answered_decision(tree)
    bind(jobs, live_pid.pid)
    assert watch(jobs).returncode == 2
    got = stored(tree, rec["id"])
    assert got["revision"] == rec["revision"] + 1
    assert got["status"] == "answered"
    assert got["history"] == rec["history"]          # no history entry
    assert got["answer"] == rec["answer"]            # answer.revision untouched
    assert stable_part(got) == stable_part(rec)
    assert got["handled"] is None


def test_c11_delivery_emits_one_delivered_event_with_only_routing_fields(jobs, live_pid):
    tree = tree_of(jobs)
    rec = answered_decision(tree, title="SECRET-TITLE-XYZ", comment="SECRET-COMMENT-XYZ")
    bind(jobs, live_pid.pid)
    assert watch(jobs).returncode == 2
    got = stored(tree, rec["id"])
    evs = delivered_events(tree, rec["id"])
    assert len(evs) == 1
    ev = evs[0]
    assert set(ev) == PAYLOAD["payload"] | PAYLOAD["envelope"], ev
    assert ev["revision"] == got["revision"]
    assert ev["audience"] == got["audience"] and ev["asker_role"] == "orchestrator"
    raw = tree.events_path.read_text()
    assert "SECRET-TITLE-XYZ" not in raw and "SECRET-COMMENT-XYZ" not in raw
    # created + answered + delivered, nothing more
    assert [e["transition"] for e in decision_events(tree)] == ["created", "answered", "delivered"]


@pytest.mark.parametrize("audience", ["user", "orchestrator"])
def test_c11_an_orchestrator_asker_is_delivered_whatever_the_audience(jobs, live_pid, audience):
    tree = tree_of(jobs)
    rec = answered_decision(tree, audience=audience)
    bind(jobs, live_pid.pid)
    assert watch(jobs).returncode == 2
    assert stored(tree, rec["id"])["delivery"]["session"] == SESSION


def test_c11_an_initializer_decision_is_delivered_to_the_initializer_session(jobs, live_pid):
    tree = tree_of(jobs)
    rec = initializer_decision(tree)
    bind(jobs, live_pid.pid, role="initializer")
    result = watch(jobs, owner="initializer")
    assert result.returncode == 2, result.stderr
    assert rec["id"] in result.stderr
    assert stored(tree, rec["id"])["delivery"]["session"] == SESSION


def test_c11_an_open_decision_is_not_delivered(jobs, live_pid):
    tree = tree_of(jobs)
    rec = create(tree, by=ORCH)
    bind(jobs, live_pid.pid)
    result = watch(jobs)
    assert result.returncode == 0 and rec["id"] not in result.stderr
    got = stored(tree, rec["id"])
    assert got["delivery"] is None and got["revision"] == 1
    assert delivered_events(tree) == []


# --------------------------------------------------------------- no repeat ---


def test_c11_no_repeat_inside_the_window_to_the_same_session(jobs, live_pid):
    tree = tree_of(jobs)
    rec = answered_decision(tree)
    seeded = seed_delivery(tree, rec["id"], age=INSIDE, session=SESSION)
    bind(jobs, live_pid.pid)
    result = watch(jobs)
    assert result.returncode == 0 and rec["id"] not in result.stderr
    got = stored(tree, rec["id"])
    assert got["delivery"] == seeded["delivery"] and got["revision"] == seeded["revision"]
    assert delivered_events(tree) == []


def test_c11_repeat_after_the_window_to_the_same_session(jobs, live_pid):
    tree = tree_of(jobs)
    rec = answered_decision(tree)
    seeded = seed_delivery(tree, rec["id"], age=OUTSIDE, session=SESSION)
    bind(jobs, live_pid.pid)
    result = watch(jobs)
    assert result.returncode == 2 and rec["id"] in result.stderr
    got = stored(tree, rec["id"])
    assert got["delivery"]["session"] == SESSION
    assert got["delivery"]["delivered_at"] > seeded["delivery"]["delivered_at"] + OUTSIDE - 5
    assert got["revision"] == seeded["revision"] + 1
    assert got["status"] == "answered"
    assert len(delivered_events(tree, rec["id"])) == 1


def test_c11_redelivery_to_a_new_session_inside_the_window(jobs, live_pid):
    tree = tree_of(jobs)
    rec = answered_decision(tree)
    seeded = seed_delivery(tree, rec["id"], age=10, session=OTHER)
    bind(jobs, live_pid.pid, session=SESSION)
    result = watch(jobs, session=SESSION)
    assert result.returncode == 2 and rec["id"] in result.stderr
    got = stored(tree, rec["id"])
    assert got["delivery"]["session"] == SESSION
    assert got["revision"] == seeded["revision"] + 1


def test_c11_delivery_is_not_a_second_answer_or_a_status_change(jobs, live_pid):
    tree = tree_of(jobs)
    rec = answered_decision(tree)
    bind(jobs, live_pid.pid)
    assert watch(jobs).returncode == 2
    again = D.answer(tree, rec["id"], revision=rec["revision"], by=USER, options=["yes"])
    assert again["already_answered"] is True      # an equal retry still succeeds
    assert again["decision"]["status"] == "answered"


# ---------------------------------------------------- revision after delivery --


def test_c11_the_revision_a_caller_held_before_delivery_is_stale(jobs, live_pid):
    tree = tree_of(jobs)
    rec = answered_decision(tree)
    bind(jobs, live_pid.pid)
    assert watch(jobs).returncode == 2
    with pytest.raises(D.DecisionError) as err:
        D.mark_handled(tree, rec["id"], revision=rec["revision"], by=ORCH, outcome="applied")
    assert code_of(err.value) == "stale_revision"
    current = stored(tree, rec["id"])
    done = D.mark_handled(tree, rec["id"], revision=current["revision"], by=ORCH,
                          outcome="applied")
    assert done["status"] == "handled"
    assert done["delivery"] == current["delivery"]      # kept on the record
    assert_valid(done)


# ----------------------------------------------------------- never after... ---


@pytest.mark.parametrize("status", ["handled", "withdrawn", "superseded"])
def test_c11_never_delivered_in_a_terminal_status(jobs, live_pid, status):
    tree = tree_of(jobs)
    rec = in_status(tree, status)
    bind(jobs, live_pid.pid)
    result = watch(jobs)
    assert result.returncode == 0 and rec["id"] not in result.stderr
    got = stored(tree, rec["id"])
    assert got == rec and got["delivery"] is None
    assert delivered_events(tree) == []


@pytest.mark.parametrize("age,session", [(INSIDE, SESSION), (OUTSIDE, SESSION), (10, OTHER)])
def test_c11_a_delivered_then_handled_decision_is_never_redelivered(jobs, live_pid, age, session):
    tree = tree_of(jobs)
    rec = answered_decision(tree)
    seeded = seed_delivery(tree, rec["id"], age=age, session=session)
    done = D.mark_handled(tree, rec["id"], revision=seeded["revision"], by=ORCH,
                          outcome="applied")
    bind(jobs, live_pid.pid)
    result = watch(jobs)
    assert result.returncode == 0 and rec["id"] not in result.stderr
    assert stored(tree, rec["id"]) == done
    assert delivered_events(tree) == []


def test_c11_agent_questions_are_not_delivered_to_a_root_session(jobs, live_pid):
    tree = tree_of(jobs)
    q = tree.add_question("ag-1", "store", "Postgres or SQLite?", "SQLite")
    tree.answer_question(q["id"], "postgres", "user")
    bind(jobs, live_pid.pid)
    result = watch(jobs)
    assert result.returncode == 0 and q["id"] not in result.stderr
    assert stored(tree, q["id"])["delivery"] is None
    assert delivered_events(tree) == []


# ------------------------------------------------- agent question: the runner --


def _runner_with_parked_agent(tmp_path):
    from multiagents.tree import Node
    from test_core import _runner
    r = _runner(tmp_path)
    r.tree.add(Node(id="ag-1", agent="a", provider="p", model="m", parent=None, depth=1,
                    session_id="ses-1"))
    q = r.tree.add_question("ag-1", "store", "Postgres or SQLite?", "SQLite")
    return r, q


def _answer(r, q, monkeypatch, steer):
    async def fake_steer(agent_id, message, *a, **kw):
        return steer()

    monkeypatch.setattr(r, "steer", fake_steer)
    return asyncio.run(r.answer_question(q["id"], "postgres"))


def test_c11_a_successful_resume_marks_the_agent_question_handled(tmp_path, monkeypatch):
    r, q = _runner_with_parked_agent(tmp_path)
    out = _answer(r, q, monkeypatch, lambda: {"steered": True})
    assert out.get("resumed") is True, out
    got = stored(r.tree, q["id"])
    assert got["status"] == "handled"
    assert got["handled"]["outcome"] == "applied"
    assert got["handled"]["by"]["role"] == "runner"
    assert got["answer"]["free_text"] == "postgres"          # the answer is kept
    assert [h["status"] for h in got["history"]] == ["open", "answered", "handled"]
    assert got["history"][-1]["by"]["role"] == "runner"
    assert_valid(got)
    assert [e["transition"] for e in decision_events(r.tree)] == ["created", "answered", "handled"]


@pytest.mark.parametrize("steer", [
    lambda: {"steered": False, "error": "no slot"},
    lambda: {"error": "launch refused"},
])
def test_c11_a_failed_resume_leaves_the_agent_question_answered(tmp_path, monkeypatch, steer):
    r, q = _runner_with_parked_agent(tmp_path)
    out = _answer(r, q, monkeypatch, steer)
    assert not out.get("resumed")
    got = stored(r.tree, q["id"])
    assert got["status"] == "answered" and got["handled"] is None
    assert got["answer"]["free_text"] == "postgres"
    assert "handled" not in [t["transition"] for t in decision_events(r.tree)]


def test_c11_a_resume_that_raises_leaves_the_agent_question_answered(tmp_path, monkeypatch):
    r, q = _runner_with_parked_agent(tmp_path)

    def boom():
        raise RuntimeError("process would not start")

    try:
        _answer(r, q, monkeypatch, boom)
    except RuntimeError:
        pass                                   # propagating is allowed; the record is the contract
    got = stored(r.tree, q["id"])
    assert got["status"] == "answered" and got["handled"] is None


def test_c11_a_refused_resume_before_the_answer_leaves_the_question_open(tmp_path, monkeypatch):
    from multiagents.tree import Node
    from test_core import _runner
    r = _runner(tmp_path)
    r.tree.add(Node(id="ag-1", agent="a", provider="p", model="m", parent=None, depth=1))
    q = r.tree.add_question("ag-1", "t", "q?", "d")      # no session: nothing to resume
    out = asyncio.run(r.answer_question(q["id"], "x"))
    assert "error" in out
    got = stored(r.tree, q["id"])
    assert got["status"] == "open" and got["handled"] is None
