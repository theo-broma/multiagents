"""Adversary, user-decisions slice 3: the exact boundaries the slice tests
step around.

These pass today. Each one kills a mutation the UD-C11/UD-C12 tests let
survive (see the adversary report for nd-eb2cb4e2):
- `<= 900` -> `< 900` in `due_for_delivery` (the tests use 880 and 920);
- the cut `text[:299] + "…"` -> `text[:300] + "…"`, and `> 300` -> `>= 300`;
- the `answered` guard in `record_delivery`, which the watcher relies on when
  a decision is handled between its snapshot and the record;
- the printable filter on the report line.
"""
import json

import pytest

from multiagents import decisions as D

from support.ud_s3_support import (SESSION, answered_decision, bind, delivered_events,
                                   stored, tree_of, watch)
from support.ud_support import ORCH
from test_dj_seams import live_pid  # noqa: F401  (fixture)
from test_dj_support import jobs  # noqa: F401  (fixture)

T0 = 1_000_000.0


def watch_at(jobs, moment):
    return jobs.cli("watch", "--owner", "orchestrator", "--timeout", 1,
                    input=json.dumps({"session_id": SESSION}),
                    env=dict(jobs.env, MULTIAGENTS_BG_NOW=repr(moment)))


@pytest.mark.parametrize("offset, due", [(900.0, False), (900.5, True)])
def test_adv_redelivery_to_the_same_session_is_strictly_after_900_s(jobs, live_pid,
                                                                    offset, due):
    tree = tree_of(jobs)
    rec = answered_decision(tree)
    D.record_delivery(tree, rec["id"], session=SESSION, now=T0)
    bind(jobs, live_pid.pid)
    result = watch_at(jobs, T0 + offset)
    assert (rec["id"] in result.stderr) is due, result.stderr
    assert result.returncode == (2 if due else 0)
    assert stored(tree, rec["id"])["delivery"]["delivered_at"] == (T0 + offset if due else T0)


def _line(result, id):
    [line] = [l for l in result.stderr.splitlines() if id in l]
    return line


def test_adv_a_text_of_exactly_300_characters_is_not_cut(jobs, live_pid):
    tree = tree_of(jobs)
    text = "Q" * 300
    rec = answered_decision(tree, comment=text)
    bind(jobs, live_pid.pid)
    line = _line(watch(jobs), rec["id"])
    assert text in line and "…" not in line


def test_adv_a_text_of_301_characters_is_299_plus_an_ellipsis(jobs, live_pid):
    tree = tree_of(jobs)
    rec = answered_decision(tree, comment="Z" * 301)
    bind(jobs, live_pid.pid)
    line = _line(watch(jobs), rec["id"])
    assert "Z" * 299 + "…" in line and "Z" * 300 not in line


def test_adv_control_characters_in_a_comment_do_not_reach_the_session(jobs, live_pid):
    tree = tree_of(jobs)
    rec = answered_decision(tree, comment="plain \x1b[2Jcleared\x07 \x00end")
    bind(jobs, live_pid.pid)
    result = watch(jobs)
    line = _line(result, rec["id"])
    assert "cleared" in line and "end" in line
    assert not any(c in result.stderr for c in "\x1b\x07\x00")


@pytest.mark.parametrize("final", ["handled", "open"])
def test_adv_record_delivery_refuses_a_decision_that_is_not_answered(jobs, final):
    tree = tree_of(jobs)
    if final == "handled":
        rec = answered_decision(tree)
        rec = D.mark_handled(tree, rec["id"], revision=rec["revision"], by=ORCH,
                             outcome="applied")
    else:
        from support.ud_support import create
        rec = create(tree, by=ORCH)
    with pytest.raises(D.DecisionError) as err:
        D.record_delivery(tree, rec["id"], session=SESSION, now=T0)
    assert err.value.code == "bad_transition"
    assert stored(tree, rec["id"]) == rec
    assert delivered_events(tree, rec["id"]) == []


@pytest.mark.parametrize("session", ["", None, 7])
def test_adv_record_delivery_refuses_a_session_that_is_not_a_name(jobs, session):
    tree = tree_of(jobs)
    rec = answered_decision(tree)
    with pytest.raises(D.DecisionError):
        D.record_delivery(tree, rec["id"], session=session, now=T0)
    assert stored(tree, rec["id"]) == rec
