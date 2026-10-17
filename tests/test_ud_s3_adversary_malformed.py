"""Adversary, user-decisions slice 3 (UD-C12, UD-C13): one bad decision record
must not take the Stop-hook watcher down.

Before slice 3 `bg watch` never read tree.json, so nothing in `questions`
could stop a job end from being reported. Now every pass projects every
decision record. tree.json is a file a docker agent can write (SG-R7), and a
hand edit or an older writer can leave a record short of a field. A record
the projection cannot read must not turn into a traceback that loses the job
end (UD-C13: the DJ watcher behaves as before) or every other decision.
"""
import pytest

from support.ud_s3_support import (SESSION, answered_decision, bind, stored, tree_of,
                                   watch)
from test_dj_seams import live_pid  # noqa: F401  (fixture)
from test_dj_support import jobs  # noqa: F401  (fixture)

MALFORMED = {
    # an answered orchestrator decision whose delivery time is not a number,
    # last delivered to this very session (so the time is actually compared)
    "delivered_at-string": lambda good: dict(good, id="d-badtime1",
                                             delivery={"delivered_at": "yesterday",
                                                       "session": SESSION}),
    # a schema-1 record missing its status
    "no-status": lambda good: {k: v for k, v in dict(good, id="d-nostat1").items()
                               if k != "status"},
    # a legacy question record missing its id
    "legacy-no-id": lambda good: {"topic": "x", "question": "y", "status": "open"},
    # a schema-1 record whose asker is not an object
    "asked_by-null": lambda good: dict(good, id="d-noask01", asked_by=None),
}


def plant(tree, record):
    with tree.transaction() as data:
        data["questions"].insert(0, record)


@pytest.mark.parametrize("kind", sorted(MALFORMED))
def test_adv_a_malformed_decision_record_does_not_lose_a_job_end(jobs, live_pid, kind):
    tree = tree_of(jobs)
    good = answered_decision(tree)
    plant(tree, MALFORMED[kind](good))
    done, _ = jobs.run("print('hi')", name="finished-job")
    jobs.ended(done)
    bind(jobs, live_pid.pid)
    result = watch(jobs)
    assert "Traceback" not in result.stderr, result.stderr[-2000:]
    assert result.returncode == 2, (result.returncode, result.stderr[-2000:])
    assert done["id"] in result.stderr


@pytest.mark.parametrize("kind", sorted(MALFORMED))
def test_adv_a_malformed_record_does_not_block_a_good_decision(jobs, live_pid, kind):
    tree = tree_of(jobs)
    good = answered_decision(tree)
    plant(tree, MALFORMED[kind](good))
    bind(jobs, live_pid.pid)
    result = watch(jobs)
    assert result.returncode == 2, (result.returncode, result.stderr[-2000:])
    assert good["id"] in result.stderr
    assert stored(tree, good["id"])["delivery"]["session"] == SESSION
