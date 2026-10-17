"""UD-C7 (with D-UD2.1): asking, the five MCP tools, channel derivation,
identity arguments, supersession, escalation."""
import asyncio
import threading

import pytest

from support.ud2_support import (IDENTITY_ARGS, ROOT_INIT, ROOT_ORCH, as_role, as_run, ask,
                                 ask_args, call, find, make, n_events, refused, snapshot_of,
                                 succeeded, world)  # noqa: F401
from support.ud_support import D, assert_valid, decision_events, events

TOOLS = ["ask_user", "withdraw_decision", "mark_decision_handled", "escalate_decision",
         "answer_decision"]

EVENT_KEYS = {"t", "agent", "kind", "transition", "id", "revision", "audience", "asker_role"}


def _tool_names():
    import multiagents.server as srv
    return {t.name for t in asyncio.run(srv.mcp.list_tools())}


def _answered_for_orchestrator(world, by=ROOT_INIT):
    rec = make(world, by=by, audience="orchestrator")
    out = D.answer(world.tree, rec["id"], revision=1, by=ROOT_ORCH, options=["yes"])
    return out["decision"]


# ------------------------------------------------------------ registration --

def test_c7_the_five_tools_are_registered_with_their_signatures():
    import multiagents.server as srv
    tools = {t.name: t for t in asyncio.run(srv.mcp.list_tools())}
    for name in TOOLS:
        assert name in tools, name
    props = lambda n: set((getattr(tools[n], "input_schema", None) or tools[n].inputSchema).get("properties", {}))
    assert props("ask_user") == {"topic", "title", "context", "implications", "mode", "options",
                                 "min", "max", "recommended", "allow_free_text", "refs",
                                 "audience", "supersedes"}
    assert props("withdraw_decision") == {"id", "revision", "reason"}
    assert props("mark_decision_handled") == {"id", "revision", "outcome", "reason"}
    assert props("escalate_decision") == {"id", "revision"}
    assert props("answer_decision") == {"id", "revision", "options", "comment", "free_text",
                                        "request_id"}


# ---------------------------------------------------------------- ask_user --

def test_c7_ask_user_creates_an_open_decision_and_returns_the_four_fields(world):
    out = succeeded(ask())
    assert set(out) == {"id", "revision", "status", "refs"}
    assert out["id"].startswith("d-") and len(out["id"]) == 10
    assert out["revision"] == 1 and out["status"] == "open" and out["refs"] == []
    rec = D.get(world.tree, out["id"])
    assert rec["audience"] == "user" and rec["mode"] == "single"
    assert rec["title"] == "Merge run D?" and rec["allow_free_text"] is True
    assert [o["id"] for o in rec["options"]] == ["yes", "no", "later"]
    assert_valid(rec)


def test_c7_ask_user_records_the_channel_and_the_role_of_the_root_server(world):
    rec = D.get(world.tree, ask()["id"])
    assert rec["asked_by"] == {"role": "orchestrator", "agent": ""}
    assert rec["history"][0]["by"] == ROOT_ORCH


def test_c7_ask_user_with_no_role_in_the_environment_is_the_orchestrator(world, monkeypatch):
    as_role(monkeypatch, None)
    rec = D.get(world.tree, ask()["id"])
    assert rec["asked_by"]["role"] == "orchestrator"
    assert rec["history"][0]["by"]["channel"] == "mcp_root"


def test_c7_ask_user_by_the_initializer_records_the_initializer(world, monkeypatch):
    as_role(monkeypatch, "initializer")
    rec = D.get(world.tree, ask()["id"])
    assert rec["asked_by"] == {"role": "initializer", "agent": ""}
    assert rec["history"][0]["by"] == ROOT_INIT


@pytest.mark.parametrize("role", ["user", "agent", "root", "ORCHESTRATOR", "x"])
def test_c7_an_unknown_role_in_the_environment_asks_nothing(world, monkeypatch, role):
    as_role(monkeypatch, role)
    refused(ask(), "forbidden")
    assert D.list(world.tree)["decisions"] == []


@pytest.mark.parametrize("audience", ["user", "orchestrator"])
def test_c7_ask_user_audience(world, audience):
    out = succeeded(ask(audience=audience))
    assert D.get(world.tree, out["id"])["audience"] == audience


def test_c7_ask_user_default_audience_is_user(world):
    args = ask_args()
    out = succeeded(call("ask_user", **args))
    assert D.get(world.tree, out["id"])["audience"] == "user"


def test_c7_ask_user_refuses_a_bad_audience_naming_the_field(world):
    refused(ask(audience="everyone"), "invalid_record", field="audience")
    assert D.list(world.tree)["decisions"] == []


@pytest.mark.parametrize("over,field", [
    (dict(topic=""), "topic"), (dict(title="a\nb"), "title"), (dict(context=""), "context"),
    (dict(mode="radio"), "mode"), (dict(options=[]), "options"),
    (dict(mode="multi", min=2, max=1), "min"), (dict(recommended=["nope"]), "recommended"),
    (dict(mode="free_text", options=[]), None),
])
def test_c7_ask_user_runs_the_slice_one_validation(world, over, field):
    out = ask(**over)
    if field is None:
        succeeded(out)                                  # a valid free_text ask
        return
    assert out.get("error") == "invalid_record", out
    assert out.get("field") in ((field, "max") if field == "min" else (field,)), out
    assert D.list(world.tree)["decisions"] == []
    assert events(world.tree) == []


def test_c7_ask_user_round_trips_multi_and_free_text(world):
    multi = succeeded(ask(mode="multi", min=1, max=2, recommended=["yes", "no"]))
    rec = D.get(world.tree, multi["id"])
    assert rec["min"] == 1 and rec["max"] == 2 and rec["recommended"] == ["yes", "no"]
    free = succeeded(ask(mode="free_text", options=[], allow_free_text=True))
    assert D.get(world.tree, free["id"])["mode"] == "free_text"


def test_c7_ask_user_never_blocks_and_waits_for_nobody(world):
    done = []
    t = threading.Thread(target=lambda: done.append(ask()))
    t.start()
    t.join(15)
    assert done and "error" not in done[0], "ask_user must return at once"


def test_c7_ask_user_emits_one_created_decision_event_with_routing_fields_only(world):
    out = succeeded(ask(title="Merge run D? (secret-title-xyz)", context="private-context-xyz"))
    evs = decision_events(world.tree)
    assert len(evs) == 1
    ev = evs[0]
    assert set(ev) <= EVENT_KEYS and {"transition", "id", "revision", "audience", "asker_role"} <= set(ev)
    assert (ev["transition"], ev["id"], ev["revision"], ev["audience"], ev["asker_role"]) == \
        ("created", out["id"], 1, "user", "orchestrator")
    assert "xyz" not in repr(events(world.tree))


def test_c7_ask_user_from_a_run_is_forbidden_and_creates_nothing(world, monkeypatch):
    as_run(monkeypatch)
    refused(ask(), "forbidden")
    assert D.list(world.tree)["decisions"] == [] and events(world.tree) == []


# ------------------------------------------------------- channel derivation --

RUN_ENVS = [dict(), dict(token=True), dict(role="orchestrator"),
            dict(role="initializer", token=True)]


@pytest.mark.parametrize("env", RUN_ENVS)
@pytest.mark.parametrize("tool", TOOLS)
def test_c7_every_tool_is_forbidden_on_a_run_whatever_its_environment_says(world, monkeypatch, tool, env):
    rec = make(world, audience="orchestrator", by=ROOT_ORCH)
    before, n = snapshot_of(world.tree), n_events(world.tree)
    as_run(monkeypatch, **env)
    args = {"ask_user": ask_args(),
            "withdraw_decision": dict(id=rec["id"], revision=1, reason="r"),
            "mark_decision_handled": dict(id=rec["id"], revision=1, outcome="applied"),
            "escalate_decision": dict(id=rec["id"], revision=1),
            "answer_decision": dict(id=rec["id"], revision=1, options=["yes"])}[tool]
    refused(call(tool, **args), "forbidden")
    assert snapshot_of(world.tree) == before and n_events(world.tree) == n


def test_c7_a_run_cannot_pose_as_the_user_by_the_environment(world, monkeypatch):
    rec = make(world, audience="user")
    as_run(monkeypatch, role="user")
    refused(call("answer_decision", id=rec["id"], revision=1, options=["yes"]), "forbidden")
    assert D.get(world.tree, rec["id"])["status"] == "open"


# ------------------------------------------------------- identity arguments --

@pytest.mark.parametrize("name", sorted(IDENTITY_ARGS))
def test_c7_ask_user_refuses_an_identity_argument(world, name):
    out = ask(**{name: IDENTITY_ARGS[name]})
    assert out.get("error") == "invalid_argument", out
    assert name in repr(out)
    assert D.list(world.tree)["decisions"] == [] and events(world.tree) == []


@pytest.mark.parametrize("name", sorted(IDENTITY_ARGS))
@pytest.mark.parametrize("tool", ["withdraw_decision", "mark_decision_handled",
                                  "escalate_decision", "answer_decision"])
def test_c7_every_other_tool_refuses_an_identity_argument(world, tool, name):
    rec = make(world, audience="orchestrator", by=ROOT_INIT)
    args = {"withdraw_decision": dict(id=rec["id"], revision=1, reason="r"),
            "mark_decision_handled": dict(id=rec["id"], revision=1, outcome="applied"),
            "escalate_decision": dict(id=rec["id"], revision=1),
            "answer_decision": dict(id=rec["id"], revision=1, options=["yes"])}[tool]
    before, n = snapshot_of(world.tree), n_events(world.tree)
    out = call(tool, **args, **{name: IDENTITY_ARGS[name]})
    assert out.get("error") == "invalid_argument", out
    assert name in repr(out)
    assert snapshot_of(world.tree) == before and n_events(world.tree) == n


def test_c7_a_forged_user_identity_does_not_answer_a_user_decision(world):
    rec = make(world, audience="user")
    out = call("answer_decision", id=rec["id"], revision=1, options=["yes"],
               by={"role": "user", "agent": "", "channel": "cli"})
    assert out.get("error") in ("invalid_argument", "forbidden")
    assert D.get(world.tree, rec["id"])["status"] == "open"


# ------------------------------------------------------------- supersession --

def test_c7_supersedes_creates_and_supersedes_in_one_step(world):
    old = succeeded(ask(title="Old question"))
    new = succeeded(ask(title="New question", supersedes=old["id"]))
    assert new["id"] != old["id"] and new["status"] == "open"
    o, n = D.get(world.tree, old["id"]), D.get(world.tree, new["id"])
    assert o["status"] == "superseded" and o["revision"] == 2
    assert o["history"][-1]["superseded_by"] == new["id"]
    assert o["history"][-1]["by"] == ROOT_ORCH
    assert n["status"] == "open" and n["title"] == "New question"
    assert_valid(o) and assert_valid(n)
    transitions = [(e["id"], e["transition"]) for e in decision_events(world.tree)]
    assert transitions.count((old["id"], "created")) == 1
    assert transitions.count((new["id"], "created")) == 1
    assert transitions.count((old["id"], "superseded")) == 1


@pytest.mark.parametrize("how", ["answered", "handled", "withdrawn", "superseded"])
def test_c7_supersedes_a_decision_that_is_not_open_creates_nothing(world, how):
    from support.ud_support import in_status
    rec = in_status(world.tree, how)
    before, n = snapshot_of(world.tree), n_events(world.tree)
    out = ask(supersedes=rec["id"])
    refused(out, "not_open", status=how)
    assert snapshot_of(world.tree) == before and n_events(world.tree) == n


def test_c7_supersedes_a_decision_asked_by_someone_else_creates_nothing(world):
    theirs = make(world, by=ROOT_INIT)
    agents = world.tree.add_question("ag-3", "t", "q?")
    before, n = snapshot_of(world.tree), n_events(world.tree)
    for target in (theirs["id"], agents["id"]):
        refused(ask(supersedes=target), "forbidden")
    assert snapshot_of(world.tree) == before and n_events(world.tree) == n


def test_c7_the_initializer_may_supersede_its_own_but_not_the_orchestrators(world, monkeypatch):
    mine, theirs = make(world, by=ROOT_INIT), make(world, by=ROOT_ORCH)
    as_role(monkeypatch, "initializer")
    refused(ask(supersedes=theirs["id"]), "forbidden")
    succeeded(ask(supersedes=mine["id"]))
    assert D.get(world.tree, mine["id"])["status"] == "superseded"
    assert D.get(world.tree, theirs["id"])["status"] == "open"


def test_c7_supersedes_an_unknown_id_creates_nothing(world):
    out = ask(supersedes="d-00000000")
    assert out.get("error") in ("unknown", "invalid_argument", "not_open"), out
    assert D.list(world.tree)["decisions"] == []


def test_c7_supersedes_with_an_invalid_new_record_leaves_the_old_one_open(world):
    old = succeeded(ask())
    before, n = snapshot_of(world.tree), n_events(world.tree)
    refused(ask(mode="radio", supersedes=old["id"]), "invalid_record", field="mode")
    refused(ask(refs=[{"path": "../x"}], supersedes=old["id"]), "invalid_record")
    assert snapshot_of(world.tree) == before and n_events(world.tree) == n


def test_c7_two_concurrent_supersessions_of_one_decision_yield_exactly_one_winner(world):
    old = succeeded(ask())
    results = []

    def go(i):
        results.append(ask(title=f"Replacement {i}", supersedes=old["id"]))

    threads = [threading.Thread(target=go, args=(i,)) for i in range(2)]
    [t.start() for t in threads]
    [t.join(30) for t in threads]
    assert len(results) == 2
    won = [r for r in results if "error" not in r]
    lost = [r for r in results if "error" in r]
    assert len(won) == 1 and len(lost) == 1, results
    assert lost[0]["error"] == "not_open"
    records = D.list(world.tree, limit=200)["decisions"]
    assert len(records) == 2, "the loser created nothing"
    assert D.get(world.tree, old["id"])["history"][-1]["superseded_by"] == won[0]["id"]


def test_c7_supersedes_must_be_a_string(world):
    refused(ask(supersedes=5), "invalid_argument")
    assert D.list(world.tree)["decisions"] == []


# ------------------------------------------------------ agent questions kept --

def test_c7_an_agent_question_is_still_a_free_text_decision_for_the_orchestrator(world):
    q = world.tree.add_question("ag-3", "store", "Postgres?\nOr SQLite?", "SQLite")
    rec = D.get(world.tree, q["id"])
    assert rec["mode"] == "free_text" and rec["audience"] == "orchestrator"
    assert rec["asked_by"] == {"role": "agent", "agent": "ag-3"}
    assert q["id"].startswith("q-")


# ----------------------------------------------------------------- withdraw --

def test_c7_withdraw_decision_by_the_asker(world):
    rec = make(world)
    out = succeeded(call("withdraw_decision", id=rec["id"], revision=1, reason="user answered in chat"))
    new = D.get(world.tree, rec["id"])
    assert new["status"] == "withdrawn" and new["revision"] == 2
    assert new["history"][-1]["reason"] == "user answered in chat"
    assert new["history"][-1]["by"] == ROOT_ORCH
    assert find(out, "status") in (None, "withdrawn")
    assert [e["transition"] for e in decision_events(world.tree)] == ["created", "withdrawn"]


def test_c7_withdraw_decision_errors(world):
    rec = make(world)
    refused(call("withdraw_decision", id=rec["id"], revision=1, reason="  "), "invalid_argument", field="reason")
    refused(call("withdraw_decision", id=rec["id"], revision=9, reason="r"), "stale_revision", revision=1)
    refused(call("withdraw_decision", id="d-00000000", revision=1, reason="r"), "unknown")
    assert D.get(world.tree, rec["id"])["revision"] == 1
    succeeded(call("withdraw_decision", id=rec["id"], revision=1, reason="r"))
    refused(call("withdraw_decision", id=rec["id"], revision=2, reason="r"), "not_open", status="withdrawn")


def test_c7_withdraw_decision_only_by_the_askers_role(world):
    theirs = make(world, by=ROOT_INIT)
    agents = world.tree.add_question("ag-3", "t", "q?")
    for target in (theirs, agents):
        refused(call("withdraw_decision", id=target["id"], revision=1, reason="r"), "forbidden")
        assert D.get(world.tree, target["id"])["status"] == "open"


# ------------------------------------------------------------ mark handled --

def test_c7_mark_decision_handled_by_the_asker(world):
    rec = D.answer(world.tree, make(world)["id"], revision=1, by=ROOT_ORCH, options=["yes"])["decision"]
    succeeded(call("mark_decision_handled", id=rec["id"], revision=2, outcome="applied"))
    new = D.get(world.tree, rec["id"])
    assert new["status"] == "handled" and new["answer"] == rec["answer"]
    assert new["handled"]["outcome"] == "applied" and new["handled"]["by"] == ROOT_ORCH


def test_c7_mark_decision_handled_errors(world):
    rec = D.answer(world.tree, make(world)["id"], revision=1, by=ROOT_ORCH, options=["yes"])["decision"]
    refused(call("mark_decision_handled", id=rec["id"], revision=2, outcome="not_applied"),
            "invalid_argument", field="reason")
    refused(call("mark_decision_handled", id=rec["id"], revision=2, outcome="done"),
            "invalid_argument", field="outcome")
    refused(call("mark_decision_handled", id=rec["id"], revision=1, outcome="applied"),
            "stale_revision", revision=2)
    open_ = make(world)
    refused(call("mark_decision_handled", id=open_["id"], revision=1, outcome="applied"), "bad_transition")
    succeeded(call("mark_decision_handled", id=rec["id"], revision=2, outcome="error", reason="merge failed"))
    assert D.get(world.tree, rec["id"])["handled"]["reason"] == "merge failed"


def test_c7_mark_decision_handled_authority(world):
    theirs = D.answer(world.tree, make(world, by=ROOT_INIT)["id"], revision=1, by=ROOT_ORCH,
                      options=["yes"])["decision"]
    refused(call("mark_decision_handled", id=theirs["id"], revision=2, outcome="applied"), "forbidden")
    assert D.get(world.tree, theirs["id"])["status"] == "answered"
    q = world.tree.add_question("ag-3", "t", "q?")
    world.tree.answer_question(q["id"], "go", "orchestrator")
    succeeded(call("mark_decision_handled", id=q["id"], revision=2, outcome="applied"))
    assert D.get(world.tree, q["id"])["status"] == "handled"


# ---------------------------------------------------------------- escalate --

def test_c7_escalate_decision_changes_the_audience_in_place(world):
    rec = make(world, audience="orchestrator", by=ROOT_INIT)
    count = len(D.list(world.tree, limit=200)["decisions"])
    q = world.tree.add_question("ag-3", "t", "q?")
    out = succeeded(call("escalate_decision", id=q["id"], revision=1))
    new = D.get(world.tree, q["id"])
    assert new["audience"] == "user" and new["status"] == "open" and new["revision"] == 2
    assert new["history"][-1]["by"] == ROOT_ORCH and len(new["history"]) == 2
    assert len(D.list(world.tree, limit=200)["decisions"]) == count + 1, "never a copy"
    assert decision_events(world.tree)[-1]["transition"] == "escalated"
    assert_valid(new)


def test_c7_escalate_decision_errors(world):
    q = world.tree.add_question("ag-3", "t", "q?")
    refused(call("escalate_decision", id=q["id"], revision=4), "stale_revision", revision=1)
    refused(call("escalate_decision", id="q-nope00", revision=1), "unknown")
    succeeded(call("escalate_decision", id=q["id"], revision=1))
    refused(call("escalate_decision", id=q["id"], revision=2), "bad_transition")
    done = make(world, audience="orchestrator", by=ROOT_ORCH)
    D.withdraw(world.tree, done["id"], revision=1, by=ROOT_ORCH, reason="r")
    refused(call("escalate_decision", id=done["id"], revision=2), "not_open", status="withdrawn")


def test_c7_the_orchestrator_may_escalate_an_agent_question_and_its_own(world):
    own = make(world, audience="orchestrator", by=ROOT_ORCH)
    succeeded(call("escalate_decision", id=own["id"], revision=1))
    assert D.get(world.tree, own["id"])["audience"] == "user"


# ----------------------------------------------------------- answer_decision --

def test_c7_answer_decision_records_the_root_actor_and_channel(world):
    rec = make(world, by=ROOT_INIT, audience="orchestrator")
    out = succeeded(call("answer_decision", id=rec["id"], revision=1, options=["yes"],
                         comment="go", request_id="r-1"))
    new = D.get(world.tree, rec["id"])
    assert new["status"] == "answered" and new["answer"]["options"] == ["yes"]
    assert new["answer"]["answered_by"] == ROOT_ORCH
    assert new["history"][-1]["by"] == ROOT_ORCH
    assert new["answer"]["request_id"] == "r-1" and new["answer"]["revision"] == 1
    assert_valid(new)


def test_c7_answer_decision_retry_and_conflict(world):
    rec = make(world, by=ROOT_INIT, audience="orchestrator")
    call("answer_decision", id=rec["id"], revision=1, options=["yes"])
    n = n_events(world.tree)
    again = succeeded(call("answer_decision", id=rec["id"], revision=1, options=["yes"]))
    assert find(again, "already_answered") is True
    assert n_events(world.tree) == n and D.get(world.tree, rec["id"])["revision"] == 2
    refused(call("answer_decision", id=rec["id"], revision=1, options=["no"]), "conflicting_answer")


def test_c7_answer_decision_error_codes(world):
    rec = make(world, by=ROOT_INIT, audience="orchestrator")
    refused(call("answer_decision", id=rec["id"], revision=3, options=["yes"]), "stale_revision", revision=1)
    refused(call("answer_decision", id=rec["id"], revision=1, options=["zzz"]), "invalid_answer")
    refused(call("answer_decision", id=rec["id"], revision=1), "invalid_answer")
    refused(call("answer_decision", id="d-00000000", revision=1, options=["yes"]), "unknown")
    D.withdraw(world.tree, rec["id"], revision=1, by=ROOT_INIT, reason="r")
    refused(call("answer_decision", id=rec["id"], revision=2, options=["yes"]), "not_open", status="withdrawn")


def test_c7_answer_decision_free_text_on_a_free_text_decision(world):
    rec = make(world, mode="free_text", by=ROOT_INIT, audience="orchestrator")
    succeeded(call("answer_decision", id=rec["id"], revision=1, free_text="ship it"))
    assert D.get(world.tree, rec["id"])["answer"]["free_text"] == "ship it"
