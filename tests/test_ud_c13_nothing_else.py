"""UD-C13: nothing else changes in slice 3.

The DJ watcher tests (tests/test_dj_*.py) stay green; they are the main guard.
These add what delivery must not touch.
"""
import json

from support.ud_s3_support import (SESSION, answered_decision, bind, registry, stable_part,
                                   stored, tree_of, watch)
from test_dj_seams import NOW, live_pid, record
from test_dj_support import jobs
from multiagents.procs import start_time


def other_sections(tree):
    data = tree.read()
    return {k: v for k, v in data.items() if k != "questions"}


def test_c13_delivery_changes_nothing_but_revision_and_delivery(jobs, live_pid):
    tree = tree_of(jobs)
    a = answered_decision(tree)
    b = answered_decision(tree, mode="free_text", free_text="later")
    untouched = answered_decision(tree, title="Not due yet")
    # make `untouched` not due: delivered a moment ago to this session
    from support.ud_s3_support import seed_delivery
    seeded = seed_delivery(tree, untouched["id"], age=5, session=SESSION)
    sections = other_sections(tree)
    bind(jobs, live_pid.pid)
    assert watch(jobs).returncode == 2
    for rec in (a, b):
        got = stored(tree, rec["id"])
        assert stable_part(got) == stable_part(rec)
        assert got["revision"] == rec["revision"] + 1 and got["delivery"]
    assert stored(tree, untouched["id"]) == seeded     # the one not due is byte-identical
    assert other_sections(tree) == sections            # nodes, tickets, pause ... unchanged


def test_c13_the_dj_job_report_is_unchanged_when_decisions_exist(jobs, live_pid):
    tree = tree_of(jobs)
    answered_decision(tree)
    jobs.env["MULTIAGENTS_BG_NOW"] = str(NOW)
    value = record(jobs, state="finished", ended_at=NOW)
    (registry(jobs) / f"{value['id']}.exit").write_text("0\n")
    bind(jobs, live_pid.pid)
    result = watch(jobs)
    assert result.returncode == 2
    assert value["id"] in result.stderr and "Detached job report" in result.stderr
    row = next(r for r in jobs.listing() if r["id"] == value["id"])
    assert row["acked_at"] is None and row["emitted_at"] == NOW
    assert row["emitted_session"] == SESSION


def test_c13_with_no_decisions_the_watcher_is_silent_and_writes_no_tree(jobs, live_pid):
    bind(jobs, live_pid.pid)
    result = watch(jobs)
    assert result.returncode == 0 and not result.stdout and not result.stderr
    assert not jobs.paths.events_file.exists() or not jobs.paths.events_file.read_text().strip()


def test_c13_an_unbound_hook_still_does_nothing(jobs):
    tree = tree_of(jobs)
    rec = answered_decision(tree)
    result = watch(jobs, session="nobody")
    assert result.returncode == 0 and not result.stdout and not result.stderr
    assert stored(tree, rec["id"]) == rec
