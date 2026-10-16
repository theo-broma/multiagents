"""UD-C4 (and P7 events): withdraw, supersede, mark_handled, escalate, the
transition matrix, revision checks and the `decision` event."""
import pytest

from support.ud_support import (AGENT, INIT, ORCH, USER, D, assert_valid, clock, code_of,
                                create, decision_events, detail, events, in_status, make_tree)

STATUSES = ["open", "answered", "handled", "withdrawn", "superseded"]
NOT_ALLOWED = ("not_open", "bad_transition")
PAYLOAD = {"envelope": {"t", "agent", "kind"},
           "payload": {"transition", "id", "revision", "audience", "asker_role"}}


def _call(op, tree, rec, **over):
    """Run `op` on `rec` at its current revision."""
    other = over.pop("_other", None)
    args = dict(revision=rec["revision"], by=ORCH)
    if op == "answer":
        args.update(by=USER, options=["no"])
    elif op == "withdraw":
        args.update(reason="not needed")
    elif op == "supersede":
        args.update(superseded_by=other or create(tree, topic="Other")["id"])
    elif op == "mark_handled":
        args.update(outcome="applied")
    args.update(over)
    return getattr(D, op)(tree, rec["id"], **args)


OPS = ["answer", "withdraw", "supersede", "mark_handled", "escalate"]


# ------------------------------------------------------------------ matrix --

ALLOWED = {("open", "answer"), ("open", "withdraw"), ("open", "supersede"),
           ("open", "escalate"), ("answered", "mark_handled")}


@pytest.mark.parametrize("op", OPS)
@pytest.mark.parametrize("status", STATUSES)
def test_c4_the_transition_matrix(tmp_path, status, op):
    tree = make_tree(tmp_path)
    rec = in_status(tree, status)
    if status == "open" and op == "escalate":
        rec = D.get(tree, create(tree, audience="orchestrator", by=AGENT, mode="free_text")["id"])
    other = create(tree, topic="Other")["id"] if op == "supersede" else None
    snapshot_events = len(events(tree))
    snapshot = {r["id"]: r for r in D.list(tree, limit=200)["decisions"]}
    if (status, op) in ALLOWED:
        out = _call(op, tree, rec, _other=other)
        new = out["decision"] if op == "answer" else out
        assert new["id"] == rec["id"] and new["revision"] == rec["revision"] + 1
        assert len(new["history"]) == len(rec["history"]) + 1
        assert_valid(new)
        assert D.get(tree, rec["id"]) == new
        return
    expected = NOT_ALLOWED
    if op == "answer" and status in ("answered", "handled"):
        with pytest.raises(D.DecisionError) as err:
            _call(op, tree, rec)                      # a different answer ("no" vs "yes")
        assert code_of(err.value) == "conflicting_answer"
    else:
        if op == "answer":
            expected = ("not_open",)
        with pytest.raises(D.DecisionError) as err:
            _call(op, tree, rec, _other=other)
        assert code_of(err.value) in expected, err.value
    assert {r["id"]: r for r in D.list(tree, limit=200)["decisions"]} == snapshot, "nothing changes"
    assert len(events(tree)) == snapshot_events


# --------------------------------------------------------------- withdraw --

def test_c4_withdraw_records_status_reason_actor_and_time(tmp_path, clock):
    tree = make_tree(tmp_path)
    rec = create(tree)
    clock.tick()
    out = D.withdraw(tree, rec["id"], revision=1, by=ORCH, reason="the user answered in chat")
    assert out["status"] == "withdrawn" and out["revision"] == 2
    entry = out["history"][-1]
    assert entry["status"] == "withdrawn" and entry["reason"] == "the user answered in chat"
    assert entry["by"] == ORCH and entry["at"] == clock.t
    assert out["answer"] is None
    assert_valid(out)
    assert D.get(tree, rec["id"]) == out


@pytest.mark.parametrize("reason", ["", None])
def test_c4_withdraw_needs_a_reason(tmp_path, reason):
    tree = make_tree(tmp_path)
    rec = create(tree)
    n = len(events(tree))
    with pytest.raises(D.DecisionError):
        D.withdraw(tree, rec["id"], revision=1, by=ORCH, reason=reason)
    assert D.get(tree, rec["id"]) == rec and len(events(tree)) == n


def test_c4_withdraw_of_an_unknown_id(tmp_path):
    with pytest.raises(D.DecisionError) as err:
        D.withdraw(make_tree(tmp_path), "d-00000000", revision=1, by=ORCH, reason="r")
    assert code_of(err.value) == "unknown"


def test_c4_withdrawing_twice_is_refused_not_repeated(tmp_path):
    tree = make_tree(tmp_path)
    rec = in_status(tree, "withdrawn")
    with pytest.raises(D.DecisionError) as err:
        D.withdraw(tree, rec["id"], revision=rec["revision"], by=ORCH, reason="again")
    assert code_of(err.value) in NOT_ALLOWED
    assert D.get(tree, rec["id"])["revision"] == rec["revision"]


# -------------------------------------------------------------- supersede --

def test_c4_supersede_names_its_replacement_in_the_history(tmp_path):
    tree = make_tree(tmp_path)
    old, new = create(tree), create(tree, topic="Replacement")
    out = D.supersede(tree, old["id"], revision=1, by=ORCH, superseded_by=new["id"])
    assert out["status"] == "superseded" and out["revision"] == 2
    assert out["history"][-1]["status"] == "superseded"
    assert out["history"][-1]["superseded_by"] == new["id"]
    assert D.get(tree, new["id"]) == new, "the replacement is untouched"
    assert_valid(out)


def test_c4_supersede_by_itself_is_refused(tmp_path):
    tree = make_tree(tmp_path)
    rec = create(tree)
    with pytest.raises(D.DecisionError):
        D.supersede(tree, rec["id"], revision=1, by=ORCH, superseded_by=rec["id"])
    assert D.get(tree, rec["id"]) == rec


def test_c4_supersede_by_an_unknown_decision_is_refused(tmp_path):
    tree = make_tree(tmp_path)
    rec = create(tree)
    n = len(events(tree))
    with pytest.raises(D.DecisionError):
        D.supersede(tree, rec["id"], revision=1, by=ORCH, superseded_by="d-00000000")
    assert D.get(tree, rec["id"]) == rec and len(events(tree)) == n


@pytest.mark.parametrize("status", ["answered", "handled", "withdrawn", "superseded"])
def test_c4_supersede_by_a_decision_that_is_not_open_is_refused(tmp_path, status):
    tree = make_tree(tmp_path)
    rec = create(tree)
    closed = in_status(tree, status)
    with pytest.raises(D.DecisionError):
        D.supersede(tree, rec["id"], revision=1, by=ORCH, superseded_by=closed["id"])
    assert D.get(tree, rec["id"]) == rec


def test_c4_supersede_chain_a_by_b_then_b_by_c(tmp_path):
    tree = make_tree(tmp_path)
    a, b, c = create(tree), create(tree), create(tree)
    D.supersede(tree, a["id"], revision=1, by=ORCH, superseded_by=b["id"])
    D.supersede(tree, b["id"], revision=1, by=ORCH, superseded_by=c["id"])
    assert [D.get(tree, x["id"])["status"] for x in (a, b, c)] == ["superseded", "superseded", "open"]
    with pytest.raises(D.DecisionError):             # a superseded decision cannot replace
        D.supersede(tree, c["id"], revision=1, by=ORCH, superseded_by=a["id"])


# ----------------------------------------------------------- mark_handled --

@pytest.mark.parametrize("outcome,reason", [("applied", ""), ("applied", "done"),
                                            ("not_applied", "the user changed their mind"),
                                            ("error", "the merge conflicted")])
def test_c4_mark_handled_records_outcome_and_keeps_the_answer(tmp_path, clock, outcome, reason):
    tree = make_tree(tmp_path)
    ans = in_status(tree, "answered")
    clock.tick()
    out = D.mark_handled(tree, ans["id"], revision=ans["revision"], by=ORCH,
                         outcome=outcome, reason=reason)
    assert out["status"] == "handled" and out["revision"] == ans["revision"] + 1
    assert out["handled"] == {"at": clock.t, "by": ORCH, "outcome": outcome, "reason": reason}
    assert out["answer"] == ans["answer"], "the answer is kept unchanged"
    assert out["history"][-1]["status"] == "handled" and out["history"][-1]["by"] == ORCH
    assert_valid(out)
    assert D.get(tree, ans["id"]) == out


@pytest.mark.parametrize("outcome", ["not_applied", "error"])
@pytest.mark.parametrize("reason", ["", None])
def test_c4_mark_handled_needs_a_reason_unless_applied(tmp_path, outcome, reason):
    tree = make_tree(tmp_path)
    ans = in_status(tree, "answered")
    n = len(events(tree))
    with pytest.raises(D.DecisionError):
        D.mark_handled(tree, ans["id"], revision=ans["revision"], by=ORCH, outcome=outcome,
                       reason=reason)
    assert D.get(tree, ans["id"]) == ans and len(events(tree)) == n


@pytest.mark.parametrize("outcome", ["maybe", "", None, "APPLIED"])
def test_c4_mark_handled_refuses_an_unknown_outcome(tmp_path, outcome):
    tree = make_tree(tmp_path)
    ans = in_status(tree, "answered")
    with pytest.raises(D.DecisionError):
        D.mark_handled(tree, ans["id"], revision=ans["revision"], by=ORCH, outcome=outcome,
                       reason="r")
    assert D.get(tree, ans["id"]) == ans


def test_c4_mark_handled_of_an_open_decision_is_refused_and_it_stays_answerable(tmp_path):
    tree = make_tree(tmp_path)
    rec = create(tree)
    with pytest.raises(D.DecisionError) as err:
        D.mark_handled(tree, rec["id"], revision=1, by=ORCH, outcome="applied")
    assert code_of(err.value) in NOT_ALLOWED
    assert D.answer(tree, rec["id"], revision=1, by=USER, options=["yes"])["already_answered"] is False


def test_c4_a_handled_decision_is_never_reopened(tmp_path):
    tree = make_tree(tmp_path)
    handled = in_status(tree, "handled")
    for op in ("withdraw", "escalate", "mark_handled"):
        with pytest.raises(D.DecisionError):
            _call(op, tree, handled)
    assert D.get(tree, handled["id"]) == handled
    assert D.list(tree, status="open")["decisions"] == []


# --------------------------------------------------------------- escalate --

def test_c4_escalate_changes_the_audience_and_nothing_else_but_the_history(tmp_path, clock):
    tree = make_tree(tmp_path)
    rec = create(tree, "free_text", by=AGENT, audience="orchestrator")
    clock.tick()
    out = D.escalate(tree, rec["id"], revision=1, by=ORCH)
    assert out["audience"] == "user" and out["status"] == "open" and out["revision"] == 2
    assert out["id"] == rec["id"] and out["asked_by"] == rec["asked_by"]
    assert out["asked_at"] == rec["asked_at"]
    assert len(out["history"]) == 2 and out["history"][-1]["by"] == ORCH
    assert out["history"][-1]["at"] == clock.t
    for key in ("topic", "title", "context", "mode", "options", "answer"):
        assert out[key] == rec[key]
    assert_valid(out)
    assert len(D.list(tree)["decisions"]) == 1, "an escalation is never a copy"


def test_c4_escalate_twice_is_refused(tmp_path):
    tree = make_tree(tmp_path)
    rec = create(tree, audience="orchestrator", by=AGENT)
    esc = D.escalate(tree, rec["id"], revision=1, by=ORCH)
    with pytest.raises(D.DecisionError) as err:
        D.escalate(tree, rec["id"], revision=esc["revision"], by=ORCH)
    assert code_of(err.value) in NOT_ALLOWED
    assert D.get(tree, rec["id"]) == esc


def test_c4_escalating_a_user_decision_is_refused(tmp_path):
    tree = make_tree(tmp_path)
    rec = create(tree, audience="user")
    with pytest.raises(D.DecisionError) as err:
        D.escalate(tree, rec["id"], revision=1, by=ORCH)
    assert code_of(err.value) in NOT_ALLOWED
    assert D.get(tree, rec["id"]) == rec


def test_c4_an_escalated_decision_can_still_be_answered_at_the_new_revision(tmp_path):
    tree = make_tree(tmp_path)
    rec = create(tree, audience="orchestrator", by=AGENT)
    esc = D.escalate(tree, rec["id"], revision=1, by=ORCH)
    assert D.answer(tree, rec["id"], revision=esc["revision"], by=USER,
                    options=["yes"])["decision"]["status"] == "answered"


# ------------------------------------------------------- revision checks --

def _fresh(tree, op, bump=False):
    """A decision `op` is allowed on. With `bump`, its revision is already 2
    (an escalation happened), so an earlier revision exists to be stale against."""
    if op == "mark_handled":
        return in_status(tree, "answered")
    rec = create(tree, audience="orchestrator", by=AGENT, mode="free_text")
    if bump:
        rec = D.escalate(tree, rec["id"], revision=1, by=ORCH)
    return rec


def _stale(tmp_path, op, bump, wrong):
    tree = make_tree(tmp_path)
    rec = _fresh(tree, op, bump)
    over = {"revision": wrong(rec["revision"])}
    if op == "supersede":
        over["_other"] = create(tree, topic="Other")["id"]
    if op == "answer":
        over["free_text"] = "an answer"
    n, snapshot = len(events(tree)), D.list(tree, limit=200)
    with pytest.raises(D.DecisionError) as err:
        _call(op, tree, rec, **over)
    assert code_of(err.value) == "stale_revision"
    assert detail(err.value, "revision") == rec["revision"]
    assert D.list(tree, limit=200) == snapshot and len(events(tree)) == n


@pytest.mark.parametrize("op", OPS)
@pytest.mark.parametrize("delta", [1, 100])
def test_c4_stale_revision_ahead_per_operation(tmp_path, op, delta):
    _stale(tmp_path, op, False, lambda current: current + delta)


@pytest.mark.parametrize("op", ["answer", "withdraw", "supersede", "mark_handled"])
def test_c4_stale_revision_behind_per_operation(tmp_path, op):
    _stale(tmp_path, op, True, lambda current: current - 1)


@pytest.mark.parametrize("op", ["answer", "withdraw", "supersede", "mark_handled", "escalate"])
@pytest.mark.parametrize("wrong", [0, -1])
def test_c4_a_revision_below_one_is_stale_not_a_crash(tmp_path, op, wrong):
    _stale(tmp_path, op, False, lambda current: wrong)


def test_c4_revision_rises_by_exactly_one_per_transition(tmp_path):
    tree = make_tree(tmp_path)
    rec = create(tree, audience="orchestrator", by=AGENT)
    assert rec["revision"] == 1
    rec = D.escalate(tree, rec["id"], revision=1, by=ORCH)
    assert rec["revision"] == 2
    rec = D.answer(tree, rec["id"], revision=2, by=USER, options=["yes"])["decision"]
    assert rec["revision"] == 3
    rec = D.mark_handled(tree, rec["id"], revision=3, by=ORCH, outcome="applied")
    assert rec["revision"] == 4 and len(rec["history"]) == 4


# ------------------------------------------------------------------ events --

def test_c4_every_transition_emits_exactly_one_decision_event_with_only_the_allowed_fields(tmp_path):
    tree = make_tree(tmp_path)
    steps = []
    rec = create(tree, audience="orchestrator", by=AGENT, mode="free_text")
    steps.append(rec)
    rec = D.escalate(tree, rec["id"], revision=1, by=ORCH); steps.append(rec)
    rec = D.answer(tree, rec["id"], revision=2, by=USER, free_text="SECRET-FREE-TEXT",
                   comment="SECRET-COMMENT")["decision"]; steps.append(rec)
    rec = D.mark_handled(tree, rec["id"], revision=3, by=ORCH, outcome="applied"); steps.append(rec)
    w = create(tree, by=INIT, topic="SECRET-TOPIC", title="SECRET-TITLE", context="SECRET-CONTEXT")
    steps.append(w)
    w = D.withdraw(tree, w["id"], revision=1, by=INIT, reason="SECRET-REASON"); steps.append(w)
    a, b = create(tree), create(tree)
    steps += [a, b]
    a = D.supersede(tree, a["id"], revision=1, by=ORCH, superseded_by=b["id"]); steps.append(a)

    evs = decision_events(tree)
    assert len(evs) == len(steps) == 9
    for ev, step in zip(evs, steps):
        assert set(ev) == PAYLOAD["payload"] | PAYLOAD["envelope"], ev
        assert ev["kind"] == "decision"
        assert ev["id"] == step["id"] and ev["revision"] == step["revision"]
        assert ev["audience"] == step["audience"]
        assert ev["asker_role"] == step["asked_by"]["role"]
        assert isinstance(ev["transition"], str) and ev["transition"]
    # transitions of one decision are told apart
    assert len({ev["transition"] for ev in evs[:4]}) == 4
    raw = tree.events_path.read_text()
    for secret in ("SECRET-FREE-TEXT", "SECRET-COMMENT", "SECRET-TOPIC", "SECRET-TITLE",
                   "SECRET-CONTEXT", "SECRET-REASON", "Merge run D", "Yes", "Go ahead"):
        assert secret not in raw, secret


def test_c4_p7_the_event_agent_is_the_asker_agent_or_its_role(tmp_path):
    tree = make_tree(tmp_path)
    create(tree, "free_text", by=AGENT, audience="orchestrator")
    create(tree, by=ORCH)
    create(tree, by=INIT)
    assert [e["agent"] for e in decision_events(tree)] == ["ag-1", "orchestrator", "initializer"]
    assert [e["asker_role"] for e in decision_events(tree)] == ["agent", "orchestrator", "initializer"]


def test_c4_p7_a_transition_that_does_not_commit_emits_nothing(tmp_path):
    tree = make_tree(tmp_path)
    rec = create(tree)
    n = len(events(tree))
    for call in (lambda: D.withdraw(tree, rec["id"], revision=9, by=ORCH, reason="r"),
                 lambda: D.withdraw(tree, rec["id"], revision=1, by=ORCH, reason=""),
                 lambda: D.escalate(tree, rec["id"], revision=1, by=ORCH),      # already user
                 lambda: D.mark_handled(tree, rec["id"], revision=1, by=ORCH, outcome="applied")):
        with pytest.raises(D.DecisionError):
            call()
    assert len(events(tree)) == n


def test_c4_p7_delivery_is_best_effort_an_unwritable_event_log_does_not_undo_a_transition(tmp_path):
    tree = make_tree(tmp_path)
    rec = create(tree)
    tree.events_path.unlink()
    tree.events_path.mkdir()                      # appending to a directory fails with OSError
    out = D.withdraw(tree, rec["id"], revision=1, by=ORCH, reason="r")
    assert out["status"] == "withdrawn"
    assert D.get(tree, rec["id"])["status"] == "withdrawn"
