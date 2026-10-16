"""UD-C5 (with P1, P2, P9): legacy agent questions through the module and
through the legacy calls."""
import asyncio
import copy
import json
import os
import subprocess
import sys
import time

import pytest

from multiagents.tree import Tree

from support.ud_support import (AGENT, LEGACY_ANSWERED, LEGACY_OPEN, ORCH, USER, WAIT_S, D,
                                REPO, assert_valid, clock, code_of, create, decision_events,
                                events, make_tree, seed_legacy)

LEGACY_KEYS = {"id", "agent", "topic", "question", "proposed_default", "asked_at", "status",
               "answer", "answered_at", "answered_by"}


def _legacy_actor(role):
    return {"role": role, "agent": "", "channel": "legacy"}


def _fixture(tmp_path):
    tree = make_tree(tmp_path)
    seed_legacy(tree, LEGACY_OPEN, LEGACY_ANSWERED)
    return tree


def _subset(small, big):
    return all(big.get(k) == v for k, v in small.items())


# ------------------------------------------------------- P2: the projection --

def test_c5_p2_an_open_legacy_record_reads_field_by_field(tmp_path):
    tree = _fixture(tmp_path)
    rec = D.get(tree, LEGACY_OPEN["id"])
    assert rec["id"] == "q-0a1b2c" and rec["schema_version"] == 1
    assert rec["revision"] == 1 and rec["status"] == "open"
    assert rec["mode"] == "free_text" and rec["audience"] == "orchestrator"
    assert rec["asked_by"] == {"role": "agent", "agent": "ag-old"}
    assert rec["asked_at"] == 1_699_000_000.5
    assert rec["topic"] == "store" and rec["title"] == "store"
    assert rec["context"] == LEGACY_OPEN["question"]
    assert rec["default_text"] == "SQLite"
    assert rec["implications"] == {"blocks": "", "if_unanswered": ""}
    assert rec["options"] == [] and rec["recommended"] == [] and rec["refs"] == []
    assert rec["allow_free_text"] is True
    assert rec["answer"] is None and rec["handled"] is None and rec["delivery"] is None
    assert len(rec["history"]) == 1
    assert _subset({"status": "open", "at": 1_699_000_000.5,
                    "by": {"role": "agent", "agent": "ag-old", "channel": "legacy"}},
                   rec["history"][0])
    assert_valid(rec)


def test_c5_p2_an_answered_legacy_record_reads_field_by_field(tmp_path):
    tree = _fixture(tmp_path)
    rec = D.get(tree, LEGACY_ANSWERED["id"])
    assert rec["status"] == "answered" and rec["revision"] == 2
    assert rec["asked_by"] == {"role": "agent", "agent": "ag-old2"}
    assert rec["default_text"] == ""
    assert rec["answer"] == {"options": [], "comment": "", "free_text": "widget",
                             "answered_by": _legacy_actor("orchestrator"),
                             "answered_at": 1_699_000_200.25, "revision": 1,
                             "request_id": None}
    assert [h["status"] for h in rec["history"]] == ["open", "answered"]
    assert _subset({"status": "open", "at": 1_699_000_100.0,
                    "by": {"role": "agent", "agent": "ag-old2", "channel": "legacy"}},
                   rec["history"][0])
    assert _subset({"status": "answered", "at": 1_699_000_200.25,
                    "by": _legacy_actor("orchestrator")}, rec["history"][1])
    assert rec["handled"] is None
    assert_valid(rec)


def test_c5_p2_title_is_the_first_line_of_the_topic(tmp_path):
    tree = make_tree(tmp_path)
    seed_legacy(tree, dict(LEGACY_OPEN, topic="first line\nsecond line"))
    assert D.get(tree, LEGACY_OPEN["id"])["title"] == "first line"


def test_c5_p2_a_long_title_is_cut_to_200_characters_with_an_ellipsis(tmp_path):
    tree = make_tree(tmp_path)
    topic = "w" * 450
    seed_legacy(tree, dict(LEGACY_OPEN, topic=topic))
    title = D.get(tree, LEGACY_OPEN["id"])["title"]
    assert len(title) == 200 and title.endswith("…") and topic.startswith(title[:-1])
    seed_legacy(tree, dict(LEGACY_ANSWERED, topic="e" * 200))
    assert D.get(tree, LEGACY_ANSWERED["id"])["title"] == "e" * 200, "exactly 200 is kept whole"


def test_c5_p2_listing_includes_legacy_records_in_the_contract_order(tmp_path):
    tree = _fixture(tmp_path)
    newer = dict(LEGACY_OPEN, id="q-000002", asked_at=1_699_000_050.0)
    seed_legacy(tree, newer)
    order = [r["id"] for r in D.list(tree)["decisions"]]
    assert order == ["q-0a1b2c", "q-000002", "q-ffee01"]
    assert [r["id"] for r in D.list(tree, status="answered")["decisions"]] == ["q-ffee01"]
    assert [r["id"] for r in D.list(tree, audience="user")["decisions"]] == []


def test_c5_reading_legacy_records_does_not_rewrite_them(tmp_path):
    tree = _fixture(tmp_path)
    D.get(tree, LEGACY_OPEN["id"])
    D.list(tree)
    D.legacy_questions(tree)
    create(tree)                                    # a write to the store, other records
    raw = {q["id"]: q for q in tree.read()["questions"]}
    assert raw[LEGACY_OPEN["id"]] == LEGACY_OPEN
    assert raw[LEGACY_ANSWERED["id"]] == LEGACY_ANSWERED


# --------------------------------------------------- P1: legacy_questions --

def test_c5_p1_legacy_questions_returns_todays_shape(tmp_path):
    tree = _fixture(tmp_path)
    assert D.legacy_questions(tree) == [LEGACY_OPEN]
    assert D.legacy_questions(tree, status="open") == [LEGACY_OPEN]
    assert D.legacy_questions(tree, status="answered") == [LEGACY_ANSWERED]


def test_c5_p1_legacy_questions_filters_by_agent(tmp_path):
    tree = _fixture(tmp_path)
    assert D.legacy_questions(tree, agent="ag-old") == [LEGACY_OPEN]
    assert D.legacy_questions(tree, agent="ag-nobody") == []
    assert D.legacy_questions(tree, status="answered", agent="ag-old") == []


def test_c5_p1_legacy_questions_selects_on_status_not_on_answered_at(tmp_path):
    tree = make_tree(tmp_path)
    odd_open = dict(LEGACY_OPEN, id="q-aaaaaa", answered_at=5.0)       # open, with a time
    odd_answered = dict(LEGACY_ANSWERED, id="q-bbbbbb", answered_at=None)
    seed_legacy(tree, odd_open, odd_answered)
    assert [q["id"] for q in D.legacy_questions(tree)] == ["q-aaaaaa"]
    assert [q["id"] for q in D.legacy_questions(tree, status="answered")] == ["q-bbbbbb"]


def test_c5_p1_legacy_questions_excludes_d_decisions(tmp_path):
    tree = _fixture(tmp_path)
    mine = create(tree, audience="orchestrator", by=AGENT, mode="free_text")
    assert mine["id"].startswith("d-")
    assert [q["id"] for q in D.legacy_questions(tree)] == ["q-0a1b2c"]
    assert D.legacy_questions(tree, agent="ag-1") == []
    D.answer(tree, mine["id"], revision=1, by=USER, free_text="x")
    assert D.legacy_questions(tree, status="answered") == [LEGACY_ANSWERED]


def test_c5_p1_legacy_questions_serves_a_new_agent_question_in_the_legacy_shape(tmp_path):
    tree = make_tree(tmp_path)
    q = tree.add_question("ag-7", "store", "Postgres?", "SQLite")
    [got] = D.legacy_questions(tree)
    assert got == q and set(got) == LEGACY_KEYS


def test_c5_p1_the_tree_and_the_monitor_do_not_show_d_decisions_as_questions(tmp_path):
    from multiagents.config import Config
    from multiagents.monitor import snapshot as snap
    from multiagents.paths import ProjectPaths
    from multiagents.tree import Tree
    paths = ProjectPaths(tmp_path)
    paths.ensure()
    tree = Tree(paths.tree_file, paths.events_file)
    create(tree, topic="SHOULD-NOT-SHOW", title="SHOULD-NOT-SHOW")
    seed_legacy(tree, LEGACY_OPEN)
    assert "SHOULD-NOT-SHOW" not in tree.render()
    assert "asks about store" in tree.render()
    state = snap.snapshot(paths, Config(project={}, providers={}, agents={}, models={},
                                        instruction_dirs=[]), with_scripts=False)
    assert state["counts"]["open_questions"] == 1
    assert all(q["id"].startswith("q-") for q in state["questions"])
    waiting = [a for a in state["alerts"] if a["kind"] == "question"]
    assert len(waiting) == 1 and waiting[0]["text"].startswith("1 agent(s)")


def test_c5_p1_the_monitor_counts_by_status_not_by_answered_at(tmp_path):
    from multiagents.config import Config
    from multiagents.monitor import snapshot as snap
    from multiagents.paths import ProjectPaths
    from multiagents.tree import Tree
    paths = ProjectPaths(tmp_path)
    paths.ensure()
    tree = Tree(paths.tree_file, paths.events_file)
    seed_legacy(tree, dict(LEGACY_OPEN, id="q-aaaaaa"),
                dict(LEGACY_ANSWERED, id="q-bbbbbb", answered_at=None))
    state = snap.snapshot(paths, Config(project={}, providers={}, agents={}, models={},
                                        instruction_dirs=[]), with_scripts=False)
    assert state["counts"]["open_questions"] == 1


# ------------------------------------------------------ the legacy wrappers --

def test_c5_legacy_calls_on_a_legacy_fixture_return_todays_shapes(tmp_path):
    tree = _fixture(tmp_path)
    assert tree.open_questions() == [LEGACY_OPEN]
    assert tree.open_questions("ag-old") == [LEGACY_OPEN]
    assert tree.open_questions("ag-old2") == []
    assert tree.get_question("q-0a1b2c") == LEGACY_OPEN
    assert tree.get_question("q-ffee01") == LEGACY_ANSWERED
    assert tree.get_question("q-nope00") is None


def test_c5_add_question_creates_through_the_module(tmp_path):
    tree = make_tree(tmp_path)
    q = tree.add_question("ag-7", "store", "Postgres or SQLite?", "SQLite")
    assert set(q) == LEGACY_KEYS
    assert q["id"].startswith("q-") and len(q["id"]) == 8
    assert (q["agent"], q["topic"], q["question"], q["proposed_default"]) == \
        ("ag-7", "store", "Postgres or SQLite?", "SQLite")
    assert (q["status"], q["answer"], q["answered_at"], q["answered_by"]) == ("open", "", None, "")
    rec = D.get(tree, q["id"])
    assert rec["mode"] == "free_text" and rec["audience"] == "orchestrator"
    assert rec["asked_by"] == {"role": "agent", "agent": "ag-7"}
    assert rec["title"] == "store" and rec["context"] == "Postgres or SQLite?"
    assert rec["default_text"] == "SQLite" and rec["revision"] == 1 and rec["status"] == "open"
    assert rec["asked_at"] == q["asked_at"]
    assert_valid(rec)
    assert [r["id"] for r in D.list(tree)["decisions"]] == [q["id"]]


def test_c5_add_question_emits_the_question_event_and_a_decision_event(tmp_path):
    tree = make_tree(tmp_path)
    q = tree.add_question("ag-7", "store", "Postgres or SQLite?", "SQLite")
    evs = events(tree)
    questions = [e for e in evs if e["kind"] == "question"]
    assert len(questions) == 1
    assert questions[0]["agent"] == "ag-7" and questions[0]["question_id"] == q["id"]
    assert questions[0]["topic"] == "store" and questions[0]["proposed"] == "SQLite"
    assert questions[0]["question"] == "Postgres or SQLite?"
    [dec] = decision_events(tree)
    assert dec["id"] == q["id"] and dec["revision"] == 1
    assert dec["audience"] == "orchestrator" and dec["asker_role"] == "agent"
    assert len(evs) == 2


def test_c5_answer_question_keeps_the_legacy_shape_and_events(tmp_path, clock):
    tree = make_tree(tmp_path)
    q = tree.add_question("ag-7", "store", "Postgres?", "SQLite")
    clock.tick()
    got = tree.answer_question(q["id"], "postgres", "user")
    assert set(got) == LEGACY_KEYS
    assert (got["status"], got["answer"], got["answered_by"], got["answered_at"]) == \
        ("answered", "postgres", "user", clock.t)
    assert "already_answered" not in got
    assert tree.get_question(q["id"]) == got
    assert tree.open_questions() == []
    answered = [e for e in events(tree) if e["kind"] == "answered"]
    assert len(answered) == 1
    assert answered[0]["agent"] == "ag-7" and answered[0]["question_id"] == q["id"]
    assert answered[0]["answered_by"] == "user" and answered[0]["answer"] == "postgres"
    rec = D.get(tree, q["id"])
    assert rec["status"] == "answered" and rec["revision"] == 2
    assert rec["answer"]["free_text"] == "postgres" and rec["answer"]["options"] == []
    assert rec["answer"]["answered_by"]["role"] == "user"
    assert rec["answer"]["revision"] == 1
    assert_valid(rec)
    assert len([e for e in decision_events(tree) if e["revision"] == 2]) == 1


def test_c5_a_second_answer_is_already_answered_changes_nothing_and_emits_nothing(tmp_path):
    tree = make_tree(tmp_path)
    q = tree.add_question("ag-7", "store", "Postgres?")
    first = tree.answer_question(q["id"], "postgres", "user")
    n = len(events(tree))
    for answer in ("sqlite", "postgres"):
        again = tree.answer_question(q["id"], answer, "orchestrator")
        assert again["already_answered"] is True
        assert again["answer"] == "postgres" and again["answered_by"] == "user"
    assert tree.get_question(q["id"]) == first
    assert len(events(tree)) == n


def test_c5_answering_a_legacy_record_returns_the_legacy_shape(tmp_path):
    tree = _fixture(tmp_path)
    got = tree.answer_question("q-0a1b2c", "sqlite", "user")
    assert set(got) == LEGACY_KEYS
    assert got["status"] == "answered" and got["answer"] == "sqlite"
    assert got["answered_by"] == "user" and got["asked_at"] == LEGACY_OPEN["asked_at"]
    again = tree.answer_question("q-ffee01", "gadget", "user")
    assert again == dict(LEGACY_ANSWERED, already_answered=True)


def test_c5_answering_an_unknown_question_is_none(tmp_path):
    tree = _fixture(tmp_path)
    assert tree.answer_question("q-nope00", "x", "user") is None
    assert tree.answer_question("d-00000000", "x", "user") is None


def test_c5_answer_question_answers_at_the_current_revision(tmp_path):
    tree = make_tree(tmp_path)
    q = tree.add_question("ag-7", "store", "Postgres?")
    D.escalate(tree, q["id"], revision=1, by=ORCH)                  # revision 2
    got = tree.answer_question(q["id"], "postgres", "user")
    assert got["status"] == "answered" and not got.get("already_answered")
    rec = D.get(tree, q["id"])
    assert rec["revision"] == 3 and rec["answer"]["revision"] == 2


def test_c5_answer_question_refuses_a_non_free_text_decision_naming_the_module_call(tmp_path):
    tree = make_tree(tmp_path)
    rec = create(tree)                                              # single choice, d-
    n = len(events(tree))
    try:
        out = tree.answer_question(rec["id"], "yes", "user")
        text = json.dumps(out, default=str)
    except Exception as exc:          # a refusal may be raised or returned
        text = str(exc)
    assert "decisions.answer" in text
    assert D.get(tree, rec["id"]) == rec, "never coerced"
    assert len(events(tree)) == n


def test_c5_answer_question_may_answer_a_free_text_d_decision(tmp_path):
    tree = make_tree(tmp_path)
    rec = create(tree, "free_text", audience="orchestrator", by=AGENT)
    tree.answer_question(rec["id"], "go", "user")
    got = D.get(tree, rec["id"])
    assert got["status"] == "answered" and got["answer"]["free_text"] == "go"


# ----------------------------------------- first transition rewrites a record --

def test_c5_a_legacy_open_record_is_rewritten_in_the_new_shape_on_its_first_transition(tmp_path):
    tree = _fixture(tmp_path)
    before = copy.deepcopy(tree.read()["questions"])
    tree.answer_question("q-0a1b2c", "sqlite", "user")
    raw = {q["id"]: q for q in tree.read()["questions"]}
    rec = raw["q-0a1b2c"]
    assert rec["schema_version"] == 1 and rec["revision"] == 2 and rec["status"] == "answered"
    assert rec["asked_at"] == LEGACY_OPEN["asked_at"] and rec["answer"]["free_text"] == "sqlite"
    assert rec["asked_by"] == {"role": "agent", "agent": "ag-old"}
    assert [h["status"] for h in rec["history"]] == ["open", "answered"]
    assert rec["history"][0]["at"] == LEGACY_OPEN["asked_at"]
    assert_valid(rec)
    assert raw["q-ffee01"] == [q for q in before if q["id"] == "q-ffee01"][0], "no bulk migration"


def test_c5_a_legacy_answered_record_keeps_its_answer_and_times_when_handled(tmp_path):
    tree = _fixture(tmp_path)
    out = D.mark_handled(tree, "q-ffee01", revision=2, by=ORCH, outcome="applied")
    assert out["id"] == "q-ffee01" and out["status"] == "handled" and out["revision"] == 3
    assert out["answer"] == D.get(tree, "q-ffee01")["answer"]
    assert out["answer"]["free_text"] == "widget" and out["answer"]["answered_at"] == 1_699_000_200.25
    assert out["answer"]["answered_by"] == _legacy_actor("orchestrator")
    assert out["asked_at"] == 1_699_000_100.0
    assert [h["status"] for h in out["history"]] == ["open", "answered", "handled"]
    assert out["history"][1]["at"] == 1_699_000_200.25
    raw = [q for q in tree.read()["questions"] if q["id"] == "q-ffee01"][0]
    assert raw["schema_version"] == 1 and raw["revision"] == 3
    assert_valid(raw)
    assert [q["id"] for q in tree.read()["questions"] if q["id"].startswith("q-")] == \
        ["q-0a1b2c", "q-ffee01"]


def test_c5_a_legacy_open_record_can_be_escalated_withdrawn_and_answered_by_revision_1(tmp_path):
    tree = _fixture(tmp_path)
    esc = D.escalate(tree, "q-0a1b2c", revision=1, by=ORCH)
    assert esc["audience"] == "user" and esc["revision"] == 2 and esc["status"] == "open"
    assert esc["id"] == "q-0a1b2c" and esc["asked_at"] == LEGACY_OPEN["asked_at"]
    assert [h["status"] for h in esc["history"]][0] == "open"
    tree2 = make_tree(tmp_path, "2")
    seed_legacy(tree2, LEGACY_OPEN)
    w = D.withdraw(tree2, "q-0a1b2c", revision=1, by=ORCH, reason="obsolete")
    assert w["status"] == "withdrawn" and w["revision"] == 2


def test_c5_a_legacy_answered_record_answered_again_by_the_module(tmp_path):
    tree = _fixture(tmp_path)
    same = D.answer(tree, "q-ffee01", revision=2, by=USER, free_text="widget")
    assert same["already_answered"] is True
    with pytest.raises(D.DecisionError) as err:
        D.answer(tree, "q-ffee01", revision=2, by=USER, free_text="gadget")
    assert code_of(err.value) == "conflicting_answer"


# -------------------------------------------------------------- the race --

_RACER = r"""
import json, os, sys, time
from pathlib import Path
from multiagents.tree import Tree
root, qid, answer, go, ready = sys.argv[1:6]
tree = Tree(Path(root) / "tree.json", Path(root) / "events.jsonl")
Path(ready).write_text("1")
deadline = time.monotonic() + 15
while not os.path.exists(go) and time.monotonic() < deadline:
    time.sleep(0.002)
out = tree.answer_question(qid, answer, "racer-" + answer)
print(json.dumps({"already": bool(out.get("already_answered")), "answer": out["answer"]}))
"""


@pytest.mark.parametrize("seed_legacy_record", [False, True])
def test_c5_race_two_processes_answering_one_agent_question_exactly_one_wins(tmp_path, seed_legacy_record):
    tree = make_tree(tmp_path)
    if seed_legacy_record:
        seed_legacy(tree, LEGACY_OPEN)
        qid = LEGACY_OPEN["id"]
    else:
        qid = tree.add_question("ag-7", "store", "Postgres?")["id"]
    go = tmp_path / "go"
    env = {**os.environ, "PYTHONPATH": str(REPO / "src")}
    procs, readies = [], []
    for i, answer in enumerate(("alpha", "beta", "gamma")):
        ready = tmp_path / f"ready{i}"
        readies.append(ready)
        procs.append(subprocess.Popen(
            [sys.executable, "-c", _RACER, str(tmp_path), qid, answer, str(go), str(ready)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env))
    try:
        deadline = time.monotonic() + WAIT_S
        while not all(r.exists() for r in readies) and time.monotonic() < deadline:
            if any(p.poll() is not None for p in procs):
                break
            time.sleep(0.01)
        go.write_text("1")
        outs = []
        for p in procs:
            out, err = p.communicate(timeout=WAIT_S)
            assert out.strip(), err[-800:]
            outs.append(json.loads(out.strip().splitlines()[-1]))
    finally:
        for p in procs:
            if p.poll() is None:
                p.kill()
                p.wait()
    winners = [o for o in outs if not o["already"]]
    assert len(winners) == 1
    assert {o["answer"] for o in outs} == {winners[0]["answer"]}, "everyone sees the winner's answer"
    assert tree.get_question(qid)["answer"] == winners[0]["answer"]
    assert len([e for e in events(tree) if e["kind"] == "answered"]) == 1


# ----------------------------------------- MCP, runner and monitor surfaces --

def _runner(tmp_path):
    from multiagents.config import Config
    from multiagents.paths import ProjectPaths
    from multiagents.runner import Runner
    paths = ProjectPaths(tmp_path)
    paths.ensure()
    return Runner(paths, Config(project={}, providers={}, agents={}, models={},
                                instruction_dirs=[]))


def test_c5_mcp_list_questions_on_legacy_and_new_agent_questions(tmp_path, monkeypatch):
    import multiagents.server as srv
    r = _runner(tmp_path)
    seed_legacy(r.tree, LEGACY_OPEN, LEGACY_ANSWERED)
    new = r.tree.add_question("ag-7", "store", "Postgres?", "SQLite")
    create(r.tree, topic="NOT-A-QUESTION")                           # a d- decision
    monkeypatch.setattr(srv, "runner", lambda: r)
    monkeypatch.delenv("MULTIAGENTS_AGENT_ID", raising=False)
    out = srv.list_questions()
    assert out["count"] == 2 and out["open"] == [LEGACY_OPEN, new]
    assert "remit" in out["note"]
    only = srv.list_questions("ag-7")
    assert only["open"] == [new] and only["count"] == 1
    empty = srv.list_questions("ag-nobody")
    assert empty["open"] == [] and empty["note"] == "nothing is waiting on a decision"


def test_c5_mcp_answer_question_reports_unknown_and_already_answered(tmp_path, monkeypatch):
    import multiagents.server as srv
    r = _runner(tmp_path)
    seed_legacy(r.tree, LEGACY_ANSWERED)
    monkeypatch.setattr(srv, "runner", lambda: r)
    unknown = asyncio.run(srv.answer_question("q-nope00", "x"))
    assert "unknown" in unknown["error"]
    again = asyncio.run(srv.answer_question("q-ffee01", "gadget"))
    assert "already answered by orchestrator" in again["error"] and again["answer"] == "widget"
    assert r.tree.get_question("q-ffee01") == LEGACY_ANSWERED


def test_c5_mcp_answer_question_with_no_session_leaves_the_question_open(tmp_path, monkeypatch):
    import multiagents.server as srv
    from multiagents.tree import Node
    r = _runner(tmp_path)
    r.tree.add(Node(id="ag-7", agent="a", provider="p", model="m", parent=None, depth=1))
    q = r.tree.add_question("ag-7", "store", "Postgres?")
    monkeypatch.setattr(srv, "runner", lambda: r)
    out = asyncio.run(srv.answer_question(q["id"], "postgres"))
    assert "resumable" in out["error"]
    assert r.tree.get_question(q["id"])["status"] == "open"
    assert D.get(r.tree, q["id"])["status"] == "open"


@pytest.mark.parametrize("how", ["withdrawn", "superseded", "handled"])
def test_c5_p9_a_terminal_agent_question_is_refused_and_never_resumed(tmp_path, how):
    from multiagents.tree import Node
    r = _runner(tmp_path)
    r.tree.add(Node(id="ag-7", agent="a", provider="p", model="m", parent=None, depth=1,
                    session_id="sess-1"))
    q = r.tree.add_question("ag-7", "store", "Postgres?")
    other = r.tree.add_question("ag-7", "other", "Other?")
    if how == "withdrawn":
        D.withdraw(r.tree, q["id"], revision=1, by=ORCH, reason="obsolete")
    elif how == "superseded":
        D.supersede(r.tree, q["id"], revision=1, by=ORCH, superseded_by=other["id"])
    else:
        D.answer(r.tree, q["id"], revision=1, by=USER, free_text="postgres")
        D.mark_handled(r.tree, q["id"], revision=2, by=ORCH, outcome="applied")
    before = D.get(r.tree, q["id"])
    n = len(events(r.tree))
    out = asyncio.run(r.answer_question(q["id"], "something else"))
    assert out.get("error"), out
    assert not out.get("resumed")
    msg = out["error"].lower()
    assert how in msg or "answered" in msg, msg
    assert D.get(r.tree, q["id"]) == before
    assert len(events(r.tree)) == n, "no steer, no answered event, no decision event"


def test_c5_monitor_answer_action_on_legacy_records(tmp_path):
    from multiagents.monitor import actions
    from multiagents.paths import ProjectPaths
    from multiagents.tree import Tree
    paths = ProjectPaths(tmp_path)
    paths.ensure()
    tree = Tree(paths.tree_file, paths.events_file)
    seed_legacy(tree, LEGACY_ANSWERED)
    assert actions.perform(paths, "answer_question",
                           {"question_id": "q-ffee01", "answer": "  "})["ok"] is False
    out = actions.perform(paths, "answer_question", {"question_id": "q-nope00", "answer": "x"})
    assert out["ok"] is True and "unknown" in out["result"]["error"]
    out = actions.perform(paths, "answer_question", {"question_id": "q-ffee01", "answer": "gadget"})
    assert "already answered by orchestrator" in out["result"]["error"]
    assert tree.get_question("q-ffee01") == LEGACY_ANSWERED
