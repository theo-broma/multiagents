"""UD-C3 (with P4, P5, P6, P7 on answers): answering a decision."""
import json
import subprocess
import sys
import time

import pytest

from support.ud_support import (ORCH, TOKEN, USER, WAIT_S, D, REPO, assert_valid, clock,
                                code_of, create, decision_events, detail, events, in_status,
                                kwargs, make_tree)


def _ans(tree, rec, **over):
    args = dict(revision=rec["revision"], by=USER)
    args.update(over)
    return D.answer(tree, rec["id"], **args)


def _unchanged(tree, rec, n_events):
    assert D.get(tree, rec["id"]) == rec
    assert len(events(tree)) == n_events


def _refused(tree, rec, code, **over):
    before = len(events(tree))
    stored = D.get(tree, rec["id"])
    with pytest.raises(D.DecisionError) as err:
        _ans(tree, rec, **over)
    assert code_of(err.value) == code, err.value
    _unchanged(tree, stored, before)
    return err.value


# --------------------------------------------------------------- recording --

def test_c3_recording_a_single_choice(tmp_path, clock):
    tree = make_tree(tmp_path)
    rec = create(tree)
    clock.tick()
    out = _ans(tree, rec, options=["no"], comment="not before Friday", request_id="req-1")
    assert set(out) == {"decision", "already_answered"}
    assert out["already_answered"] is False
    dec = out["decision"]
    assert dec["status"] == "answered" and dec["revision"] == 2
    assert dec["answer"] == {"options": ["no"], "comment": "not before Friday",
                             "free_text": "", "answered_by": USER,
                             "answered_at": clock.t, "revision": 1, "request_id": "req-1"}
    assert [h["status"] for h in dec["history"]] == ["open", "answered"]
    assert dec["history"][1]["by"] == USER and dec["history"][1]["at"] == clock.t
    assert dec["handled"] is None
    assert_valid(dec)
    assert D.get(tree, rec["id"]) == dec


def test_c3_the_answer_defaults(tmp_path):
    tree = make_tree(tmp_path)
    rec = create(tree)
    ans = _ans(tree, rec, options=["yes"])["decision"]["answer"]
    assert ans["comment"] == "" and ans["free_text"] == "" and ans["request_id"] is None


def test_c3_the_stored_revision_is_the_one_answered(tmp_path):
    tree = make_tree(tmp_path)
    rec = create(tree, audience="orchestrator")
    rec = D.escalate(tree, rec["id"], revision=1, by=ORCH)        # revision 2
    dec = _ans(tree, rec, options=["yes"])["decision"]
    assert dec["answer"]["revision"] == 2 and dec["revision"] == 3


def test_c3_the_answer_keeps_the_options_in_the_order_given(tmp_path):
    tree = make_tree(tmp_path)
    rec = create(tree, "multi", min=1, max=3)
    dec = _ans(tree, rec, options=["later", "yes"])["decision"]
    assert dec["answer"]["options"] == ["later", "yes"]


def test_c3_a_recommendation_or_default_is_never_recorded_as_an_answer(tmp_path):
    tree = make_tree(tmp_path)
    rec = create(tree, recommended=["yes"], default_text="DEFAULT: yes")
    assert rec["answer"] is None and rec["status"] == "open"
    _refused(tree, rec, "invalid_answer")                 # nothing chosen: not "yes"
    _refused(tree, rec, "invalid_answer", comment="go with the recommendation")
    assert D.get(tree, rec["id"])["answer"] is None


# ----------------------------------------------------------------- validity --

def test_c3_single_free_text_in_place_of_an_option(tmp_path):
    tree = make_tree(tmp_path)
    rec = create(tree)
    dec = _ans(tree, rec, free_text="something else entirely")["decision"]
    assert dec["answer"]["options"] == [] and dec["answer"]["free_text"] == "something else entirely"


def test_c3_single_free_text_refused_when_not_allowed(tmp_path):
    tree = make_tree(tmp_path)
    rec = create(tree, allow_free_text=False)
    _refused(tree, rec, "invalid_answer", free_text="other")


@pytest.mark.parametrize("over", [
    {"options": ["yes", "no"]},                       # two options in single mode
    {"options": ["maybe"]},                           # unknown option
    {"options": ["yes", "yes"]},                      # duplicate
    {"options": ["yes"], "free_text": "and more"},    # free text only *in place of* an option
    {},                                               # empty
    {"comment": "only a comment"},                    # a comment alone is no answer
])
def test_c3_single_invalid_answers(tmp_path, over):
    tree = make_tree(tmp_path)
    _refused(tree, create(tree), "invalid_answer", **over)


@pytest.mark.parametrize("opts,ok", [
    (["yes"], True), (["yes", "no"], True), ([], False),
    (["yes", "no", "later"], False),                  # above max
    (["yes", "yes"], False), (["yes", "maybe"], False),
])
def test_c3_multi_counts_between_min_and_max(tmp_path, opts, ok):
    tree = make_tree(tmp_path)
    rec = create(tree, "multi", min=1, max=2)
    if ok:
        assert _ans(tree, rec, options=opts)["decision"]["answer"]["options"] == opts
    else:
        _refused(tree, rec, "invalid_answer", options=opts)


def test_c3_multi_min_above_one_is_enforced(tmp_path):
    tree = make_tree(tmp_path)
    rec = create(tree, "multi", min=2, max=3)
    _refused(tree, rec, "invalid_answer", options=["yes"])
    assert _ans(tree, rec, options=["yes", "no"])["decision"]["status"] == "answered"


def test_c3_multi_free_text_in_place_of_options(tmp_path):
    tree = make_tree(tmp_path)
    rec = create(tree, "multi", min=1, max=2)
    assert _ans(tree, rec, free_text="neither")["decision"]["answer"]["free_text"] == "neither"
    rec2 = create(tree, "multi", min=1, max=2)
    _refused(tree, rec2, "invalid_answer", options=["yes"], free_text="and also")


def test_c3_free_text_mode(tmp_path):
    tree = make_tree(tmp_path)
    rec = create(tree, "free_text")
    _refused(tree, rec, "invalid_answer")
    _refused(tree, rec, "invalid_answer", free_text="")
    _refused(tree, rec, "invalid_answer", options=["yes"], free_text="x")
    _refused(tree, rec, "invalid_answer", comment="comment but no answer")
    dec = _ans(tree, rec, free_text="do it", comment="thanks")["decision"]
    assert dec["answer"]["free_text"] == "do it" and dec["answer"]["comment"] == "thanks"


def test_c3_a_comment_goes_with_any_mode(tmp_path):
    tree = make_tree(tmp_path)
    for mode, over in (("single", {"options": ["yes"]}),
                       ("multi", {"options": ["yes", "no"]}),
                       ("free_text", {"free_text": "x"})):
        rec = create(tree, mode)
        assert _ans(tree, rec, comment="c", **over)["decision"]["answer"]["comment"] == "c"


def test_c3_comment_limit_is_4000_inclusive(tmp_path):
    tree = make_tree(tmp_path)
    ok = create(tree)
    assert len(_ans(tree, ok, options=["yes"], comment="c" * 4000)
               ["decision"]["answer"]["comment"]) == 4000
    _refused(tree, create(tree), "invalid_answer", options=["yes"], comment="c" * 4001)


def test_c3_p4_comment_limit_applies_after_scrubbing(tmp_path):
    tree = make_tree(tmp_path)
    comment = "c" * 3985 + " " + TOKEN
    assert len(comment) > 4000
    assert "[redacted]" in _ans(tree, create(tree), options=["yes"],
                                comment=comment)["decision"]["answer"]["comment"]


# --------------------------------------------------------- order of checks --

def test_c3_1_unknown_id(tmp_path):
    tree = make_tree(tmp_path)
    create(tree)
    n = len(events(tree))
    with pytest.raises(D.DecisionError) as err:
        D.answer(tree, "d-00000000", revision=1, by=USER, options=["yes"])
    assert code_of(err.value) == "unknown"
    assert len(events(tree)) == n


def test_c3_2_withdrawn_or_superseded_is_not_open_and_says_why(tmp_path):
    tree = make_tree(tmp_path)
    w = in_status(tree, "withdrawn")
    err = _refused(tree, w, "not_open", options=["yes"])
    assert detail(err, "status") == "withdrawn"
    s = in_status(tree, "superseded")
    err = _refused(tree, s, "not_open", options=["yes"])
    assert detail(err, "status") == "superseded"
    assert detail(err, "superseded_by") == s["history"][-1]["superseded_by"]
    assert detail(err, "superseded_by")


def test_c3_2_not_open_wins_over_a_stale_revision_and_an_invalid_answer(tmp_path):
    tree = make_tree(tmp_path)
    w = in_status(tree, "withdrawn")
    _refused(tree, w, "not_open", revision=99, options=["maybe"])


def test_c3_3_answered_with_the_same_answer_is_already_answered(tmp_path):
    tree = make_tree(tmp_path)
    rec = create(tree, "multi", min=1, max=3)
    first = _ans(tree, rec, options=["yes", "no"], comment="c")["decision"]
    n = len(events(tree))
    again = _ans(tree, rec, options=["no", "yes"], comment="c")   # same set, other order
    assert again["already_answered"] is True and again["decision"] == first
    _unchanged(tree, first, n)


def test_c3_3_the_retry_is_idempotent_after_another_field_changed_the_revision(tmp_path):
    tree = make_tree(tmp_path)
    rec = create(tree)
    first = _ans(tree, rec, options=["yes"], request_id="r1")["decision"]
    handled = D.mark_handled(tree, rec["id"], revision=first["revision"], by=ORCH,
                             outcome="applied")
    n = len(events(tree))
    again = _ans(tree, rec, options=["yes"], request_id="r1")      # revision 1: long stale
    assert again["already_answered"] is True and again["decision"] == handled
    assert again["decision"]["status"] == "handled"
    again2 = _ans(tree, rec, options=["yes"])
    assert again2["already_answered"] is True
    assert len(events(tree)) == n


def test_c3_3_a_different_request_id_with_equal_content_is_still_idempotent(tmp_path):
    tree = make_tree(tmp_path)
    rec = create(tree)
    _ans(tree, rec, options=["yes"], request_id="r1")
    assert _ans(tree, rec, options=["yes"], request_id="r2")["already_answered"] is True


@pytest.mark.parametrize("over", [{"options": ["no"]}, {"options": ["yes"], "comment": "new"},
                                  {"free_text": "other"}])
def test_c3_3_a_different_answer_conflicts(tmp_path, over):
    tree = make_tree(tmp_path)
    rec = create(tree)
    first = _ans(tree, rec, options=["yes"])["decision"]
    n = len(events(tree))
    _refused(tree, rec, "conflicting_answer", **over)
    _unchanged(tree, first, n)


def test_c3_3_a_conflicting_answer_to_a_handled_decision(tmp_path):
    tree = make_tree(tmp_path)
    handled = in_status(tree, "handled")
    n = len(events(tree))
    with pytest.raises(D.DecisionError) as err:
        D.answer(tree, handled["id"], revision=handled["revision"], by=USER, options=["no"])
    assert code_of(err.value) == "conflicting_answer"
    _unchanged(tree, handled, n)


def test_c3_3_conflict_wins_over_a_stale_revision(tmp_path):
    tree = make_tree(tmp_path)
    rec = create(tree)
    _ans(tree, rec, options=["yes"])
    _refused(tree, rec, "conflicting_answer", revision=42, options=["no"])


def test_c3_4_stale_revision_names_the_current_one(tmp_path):
    tree = make_tree(tmp_path)
    rec = create(tree, audience="orchestrator")
    rec = D.escalate(tree, rec["id"], revision=1, by=ORCH)        # now revision 2
    for wrong in (1, 3, 0, 99):
        err = _refused(tree, rec, "stale_revision", revision=wrong, options=["yes"])
        assert detail(err, "revision") == 2
    assert _ans(tree, rec, options=["yes"])["decision"]["status"] == "answered"


def test_c3_4_stale_revision_wins_over_an_invalid_answer(tmp_path):
    tree = make_tree(tmp_path)
    rec = create(tree)
    _refused(tree, rec, "stale_revision", revision=7, options=["maybe"])


def test_c3_5_an_invalid_answer_leaves_the_decision_open_and_answerable(tmp_path):
    tree = make_tree(tmp_path)
    rec = create(tree)
    _refused(tree, rec, "invalid_answer", options=["maybe"])
    assert _ans(tree, rec, options=["yes"])["already_answered"] is False


# ------------------------------------------------------- P4 scrub, P5 id --

def test_c3_p4_human_text_is_returned_and_stored_scrubbed(tmp_path):
    tree = make_tree(tmp_path)
    rec = create(tree)
    out = _ans(tree, rec, free_text=f"use {TOKEN}", comment=f"because {TOKEN}")
    for stored in (out["decision"], D.get(tree, rec["id"])):
        assert stored["answer"]["free_text"] == "use [redacted]"
        assert stored["answer"]["comment"] == "because [redacted]"
    assert TOKEN not in tree.path.read_text()


def test_c3_p4_equality_compares_scrubbed_forms(tmp_path):
    tree = make_tree(tmp_path)
    rec = create(tree)
    _ans(tree, rec, free_text=f"use {TOKEN}")
    n = len(events(tree))
    # the same text with the secret again, or with a different secret that
    # scrubs to the same thing, is the same answer
    assert _ans(tree, rec, free_text=f"use {TOKEN}")["already_answered"] is True
    assert _ans(tree, rec, free_text="use sk-" + "b" * 20)["already_answered"] is True
    assert _ans(tree, rec, free_text="use [redacted]")["already_answered"] is True
    assert len(events(tree)) == n
    _refused(tree, rec, "conflicting_answer", free_text="use something else")


def test_c3_p4_a_request_id_the_scrubber_would_alter_is_refused(tmp_path):
    tree = make_tree(tmp_path)
    rec = create(tree)
    err = _refused(tree, rec, "invalid_answer", options=["yes"], request_id=TOKEN)
    assert detail(err, "field") == "request_id"


def test_c3_p4_an_option_id_the_scrubber_would_alter_is_refused(tmp_path):
    tree = make_tree(tmp_path)
    rec = create(tree)
    err = _refused(tree, rec, "invalid_answer", options=[TOKEN])
    assert str(detail(err, "field")).startswith("options")


def test_c3_p5_request_id_retry_with_the_same_payload_is_idempotent(tmp_path):
    tree = make_tree(tmp_path)
    rec = create(tree)
    first = _ans(tree, rec, options=["yes"], comment="c", request_id="req-9")["decision"]
    again = _ans(tree, rec, options=["yes"], comment="c", request_id="req-9")
    assert again["already_answered"] is True and again["decision"] == first


@pytest.mark.parametrize("over", [{"options": ["no"]}, {"comment": "different"},
                                  {"free_text": "different", "options": []}])
def test_c3_p5_the_same_request_id_with_another_payload_conflicts(tmp_path, over):
    tree = make_tree(tmp_path)
    rec = create(tree)
    first = _ans(tree, rec, options=["yes"], comment="c", request_id="req-9")["decision"]
    n = len(events(tree))
    payload = dict(options=["yes"], comment="c", request_id="req-9")
    payload.update(over)
    _refused(tree, rec, "conflicting_answer", **payload)
    _unchanged(tree, first, n)


def test_c3_p5_the_same_request_id_on_a_different_decision_is_independent(tmp_path):
    tree = make_tree(tmp_path)
    a, b = create(tree), create(tree)
    _ans(tree, a, options=["yes"], request_id="same")
    out = _ans(tree, b, options=["no"], request_id="same")
    assert out["already_answered"] is False and out["decision"]["answer"]["options"] == ["no"]


# ------------------------------------------------------------------ events --

def test_c3_p7_an_answer_emits_one_decision_event_and_a_duplicate_none(tmp_path):
    tree = make_tree(tmp_path)
    rec = create(tree)
    before = len(decision_events(tree))
    out = _ans(tree, rec, options=["yes"], comment="private comment", free_text="")
    events_now = decision_events(tree)
    assert len(events_now) == before + 1
    ev = events_now[-1]
    assert ev["id"] == rec["id"] and ev["revision"] == out["decision"]["revision"]
    assert ev["audience"] == "user" and ev["asker_role"] == "orchestrator"
    _ans(tree, rec, options=["yes"], comment="private comment")
    assert len(events(tree)) == before + 1 and "private comment" not in tree.events_path.read_text()


def test_c3_p7_a_refused_answer_emits_nothing(tmp_path):
    tree = make_tree(tmp_path)
    rec = create(tree)
    n = len(events(tree))
    for over in ({"options": ["maybe"]}, {"revision": 5, "options": ["yes"]}):
        with pytest.raises(D.DecisionError):
            _ans(tree, rec, **over)
    assert len(events(tree)) == n


# -------------------------------------------------------------------- race --

_RACER = r"""
import json, os, sys, time
from pathlib import Path
from multiagents.tree import Tree
from multiagents import decisions as D
root, decision_id, label, payload, go, ready = sys.argv[1:7]
tree = Tree(Path(root) / "tree.json", Path(root) / "events.jsonl")
Path(ready).write_text("1")
deadline = time.monotonic() + 15
while not os.path.exists(go) and time.monotonic() < deadline:
    time.sleep(0.002)
try:
    out = D.answer(tree, decision_id, revision=1,
                   by={"role": "user", "agent": "", "channel": label}, **json.loads(payload))
    print(json.dumps({"ok": True, "already_answered": out["already_answered"]}))
except D.DecisionError as exc:
    print(json.dumps({"ok": False, "code": getattr(exc, "code", None) or str(exc.args[0])}))
"""


def _race(tmp_path, decision_id, payloads):
    go = tmp_path / "go"
    procs, readies = [], []
    import os
    env = {**os.environ, "PYTHONPATH": str(REPO / "src")}
    for i, payload in enumerate(payloads):
        ready = tmp_path / f"ready{i}"
        readies.append(ready)
        procs.append(subprocess.Popen(
            [sys.executable, "-c", _RACER, str(tmp_path), decision_id, f"racer{i}",
             json.dumps(payload), str(go), str(ready)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env))
    try:
        deadline = time.monotonic() + WAIT_S
        while not all(r.exists() for r in readies) and time.monotonic() < deadline:
            if any(p.poll() is not None for p in procs):
                break                       # a racer died early: collected below
            time.sleep(0.01)
        go.write_text("1")
        outs = []
        for p in procs:
            out, err = p.communicate(timeout=WAIT_S)
            assert out.strip(), f"racer produced nothing: {err[-800:]}"
            outs.append(json.loads(out.strip().splitlines()[-1]))
        return outs
    finally:
        for p in procs:
            if p.poll() is None:
                p.kill()
                p.wait()


def test_c3_race_two_processes_with_different_answers_exactly_one_wins(tmp_path):
    from multiagents.tree import Tree
    tree = Tree(tmp_path / "tree.json", tmp_path / "events.jsonl")
    rec = create(tree)
    a, b = _race(tmp_path, rec["id"], [{"options": ["yes"]}, {"options": ["no"]}])
    winners = [r for r in (a, b) if r["ok"] and not r["already_answered"]]
    losers = [r for r in (a, b) if r not in winners]
    assert len(winners) == 1 and len(losers) == 1
    assert losers[0] == {"ok": False, "code": "conflicting_answer"}
    final = D.get(tree, rec["id"])
    assert final["status"] == "answered" and final["revision"] == 2
    assert len(final["answer"]["options"]) == 1
    assert [e["revision"] for e in decision_events(tree)
            if e["revision"] == 2] == [2], "exactly one answer event"


def test_c3_race_two_processes_with_the_same_answer_one_wins_one_is_already_answered(tmp_path):
    from multiagents.tree import Tree
    tree = Tree(tmp_path / "tree.json", tmp_path / "events.jsonl")
    rec = create(tree)
    outs = _race(tmp_path, rec["id"], [{"options": ["yes"]}, {"options": ["yes"]},
                                       {"options": ["yes"]}])
    assert all(o["ok"] for o in outs)
    assert sorted(o["already_answered"] for o in outs) == [False, True, True]
    final = D.get(tree, rec["id"])
    assert final["revision"] == 2 and final["answer"]["options"] == ["yes"]
    assert len([e for e in decision_events(tree) if e["revision"] == 2]) == 1
