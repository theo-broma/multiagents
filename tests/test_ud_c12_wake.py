"""UD-C12: wake through the Stop hook (`bg.py watch()`).

The watcher is run as the real `multiagents bg watch` command, as the Stop hook
runs it. The report is its stderr (exit status 2 wakes the session).
"""
import json
import re
import socket
import subprocess
import select
import sys

import pytest

from support.ud_s3_support import (OTHER, SESSION, answered_decision, bind, delivered_events,
                                   initializer_decision, registry, seed_delivery, spawn_watch,
                                   stored, tree_of, watch, watch_argv)
from support.ud_support import ORCH, D, create
from test_dj_seams import live_pid
from test_dj_support import CLI_BOUND, jobs

CUT = 300


def longest_run(text, char):
    runs = re.findall(re.escape(char) + "+", text)
    return max(map(len, runs), default=0)


def lines_with(text, needle):
    return [line for line in text.splitlines() if needle in line]


# ------------------------------------------------------------------ report ---


def test_c12_reports_id_title_option_ids_and_comment_on_one_line(jobs, live_pid):
    tree = tree_of(jobs)
    rec = answered_decision(tree, title="Merge run D now?", options=("no",),
                            comment="not before the review")
    bind(jobs, live_pid.pid)
    result = watch(jobs)
    assert result.returncode == 2, result.stderr
    [line] = lines_with(result.stderr, rec["id"])
    assert "Merge run D now?" in line
    assert re.search(r"\bno\b", line)
    assert "not before the review" in line
    assert not result.stdout


def test_c12_a_multi_option_answer_names_every_chosen_option(jobs, live_pid):
    tree = tree_of(jobs)
    rec = answered_decision(tree, mode="multi", options=("yes", "later"))
    bind(jobs, live_pid.pid)
    result = watch(jobs)
    [line] = lines_with(result.stderr, rec["id"])
    assert re.search(r"\byes\b", line) and re.search(r"\blater\b", line)
    assert not re.search(r"\bno\b", line)       # an option not chosen is not named


def test_c12_a_free_text_answer_is_reported(jobs, live_pid):
    tree = tree_of(jobs)
    rec = answered_decision(tree, mode="free_text", free_text="ship it on Friday")
    bind(jobs, live_pid.pid)
    result = watch(jobs)
    [line] = lines_with(result.stderr, rec["id"])
    assert "ship it on Friday" in line


@pytest.mark.parametrize("field", ["comment", "free_text"])
def test_c12_the_comment_or_free_text_is_cut_to_300_characters(jobs, live_pid, field):
    tree = tree_of(jobs)
    long = "Z" * 500
    if field == "comment":
        rec = answered_decision(tree, comment=long)
    else:
        rec = answered_decision(tree, mode="free_text", free_text=long)
    bind(jobs, live_pid.pid)
    result = watch(jobs)
    [line] = lines_with(result.stderr, rec["id"])
    # cut to 300: an ellipsis may take the last place, but never more than 300
    assert 299 <= longest_run(line, "Z") <= CUT


def test_c12_a_text_that_fits_is_not_cut(jobs, live_pid):
    tree = tree_of(jobs)
    rec = answered_decision(tree, comment="Q" * 299)
    bind(jobs, live_pid.pid)
    result = watch(jobs)
    assert longest_run(result.stderr, "Q") == 299


def test_c12_a_multi_line_comment_stays_on_the_decisions_line(jobs, live_pid):
    tree = tree_of(jobs)
    rec = answered_decision(tree, comment="first part\nsecond part")
    bind(jobs, live_pid.pid)
    result = watch(jobs)
    [line] = lines_with(result.stderr, rec["id"])
    assert "first part" in line and "second part" in line


def test_c12_two_due_decisions_give_one_line_each_and_both_are_delivered(jobs, live_pid):
    tree = tree_of(jobs)
    a = answered_decision(tree, title="First question?")
    b = answered_decision(tree, title="Second question?")
    bind(jobs, live_pid.pid)
    result = watch(jobs)
    assert result.returncode == 2
    assert len(lines_with(result.stderr, a["id"])) == 1
    assert len(lines_with(result.stderr, b["id"])) == 1
    assert lines_with(result.stderr, a["id"]) != lines_with(result.stderr, b["id"])
    for rec in (a, b):
        assert stored(tree, rec["id"])["delivery"]["session"] == SESSION
        assert len(delivered_events(tree, rec["id"])) == 1


def test_c12_a_secret_in_a_comment_is_not_reported(jobs, live_pid):
    tree = tree_of(jobs)
    key = "sk-" + "a" * 24
    rec = answered_decision(tree, comment=f"use {key} for it")
    bind(jobs, live_pid.pid)
    result = watch(jobs)
    assert rec["id"] in result.stderr and key not in result.stderr + result.stdout


# --------------------------------------------------------- once, and recorded ---


def test_c12_reports_once_and_records_the_delivery(jobs, live_pid):
    tree = tree_of(jobs)
    rec = answered_decision(tree)
    bind(jobs, live_pid.pid)
    first = watch(jobs)
    assert first.returncode == 2 and rec["id"] in first.stderr
    after_first = stored(tree, rec["id"])
    assert after_first["delivery"]["session"] == SESSION
    second = watch(jobs)
    assert second.returncode == 0 and rec["id"] not in second.stderr
    assert stored(tree, rec["id"]) == after_first
    assert len(delivered_events(tree, rec["id"])) == 1


def test_c12_an_answer_given_while_no_session_runs_reaches_the_next_session_once(jobs, live_pid):
    tree = tree_of(jobs)
    rec = answered_decision(tree)                       # nobody is bound yet
    unbound = watch(jobs, session="ud-never-bound")
    assert unbound.returncode == 0 and rec["id"] not in unbound.stderr
    assert stored(tree, rec["id"])["delivery"] is None  # a refused watcher delivers nothing
    bind(jobs, live_pid.pid)                            # the next session starts
    first = watch(jobs)
    assert first.returncode == 2 and rec["id"] in first.stderr
    assert watch(jobs).returncode == 0
    assert watch(jobs).returncode == 0
    assert len(delivered_events(tree, rec["id"])) == 1
    assert stored(tree, rec["id"])["revision"] == rec["revision"] + 1


def test_c12_a_dead_session_gets_nothing_and_the_next_one_does(jobs):
    tree = tree_of(jobs)
    rec = answered_decision(tree)
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait(timeout=CLI_BOUND)
    bind(jobs, dead.pid, session="ud-dead")
    result = watch(jobs, session="ud-dead")
    assert result.returncode == 0 and not result.stderr
    assert stored(tree, rec["id"]) == rec               # untouched, still due


def test_c12_the_next_session_after_a_delivered_one_is_redelivered_to(jobs, live_pid):
    tree = tree_of(jobs)
    rec = answered_decision(tree)
    seed_delivery(tree, rec["id"], age=30, session=OTHER)
    bind(jobs, live_pid.pid)
    result = watch(jobs)
    assert result.returncode == 2 and rec["id"] in result.stderr
    assert stored(tree, rec["id"])["delivery"]["session"] == SESSION


def test_c12_the_decision_is_not_reported_before_it_is_answered(jobs, live_pid):
    tree = tree_of(jobs)
    rec = create(tree, by=ORCH)
    bind(jobs, live_pid.pid)
    result = watch(jobs)
    assert rec["id"] not in result.stderr
    assert stored(tree, rec["id"])["delivery"] is None


# ------------------------------------------------------------------- roles ---


def test_c12_an_initializer_decision_never_reaches_the_orchestrators_watcher(jobs, live_pid):
    tree = tree_of(jobs)
    rec = initializer_decision(tree)
    bind(jobs, live_pid.pid)
    result = watch(jobs, owner="orchestrator")
    assert result.returncode == 0 and rec["id"] not in result.stderr
    got = stored(tree, rec["id"])
    assert got == rec and got["delivery"] is None
    assert delivered_events(tree) == []


def test_c12_an_orchestrator_decision_never_reaches_the_initializers_watcher(jobs, live_pid):
    tree = tree_of(jobs)
    rec = answered_decision(tree)
    bind(jobs, live_pid.pid, role="initializer")
    result = watch(jobs, owner="initializer")
    assert result.returncode == 0 and rec["id"] not in result.stderr
    assert stored(tree, rec["id"]) == rec


def test_c12_each_role_gets_only_its_own_decision(jobs, live_pid):
    tree = tree_of(jobs)
    mine = answered_decision(tree, title="For the orchestrator")
    theirs = initializer_decision(tree, title="For the initializer")
    bind(jobs, live_pid.pid)
    result = watch(jobs)
    assert mine["id"] in result.stderr and theirs["id"] not in result.stderr
    assert stored(tree, theirs["id"])["delivery"] is None
    bind(jobs, live_pid.pid, session="ud-init", role="initializer")
    result = watch(jobs, session="ud-init", owner="initializer")
    assert theirs["id"] in result.stderr and mine["id"] not in result.stderr


def test_c12_a_session_bound_to_the_wrong_role_gets_nothing(jobs, live_pid):
    tree = tree_of(jobs)
    rec = answered_decision(tree)
    bind(jobs, live_pid.pid, role="initializer")        # bound as initializer ...
    result = watch(jobs, owner="orchestrator")          # ... asking as orchestrator
    assert result.returncode == 0 and rec["id"] not in result.stderr
    assert stored(tree, rec["id"])["delivery"] is None


# ---------------------------------------------------------------- no fake jobs ---


def test_c12_no_bg_job_row_is_created_for_a_decision(jobs, live_pid):
    tree = tree_of(jobs)
    answered_decision(tree)
    answered_decision(tree)
    bind(jobs, live_pid.pid)
    assert jobs.listing() == []
    assert watch(jobs).returncode == 2
    assert jobs.listing() == []
    assert jobs.listing("orchestrator") == []
    job_files = [p for p in registry(jobs).glob("*.json") if p.is_file()]
    assert job_files == []                              # bindings/ is a sub-directory


def test_c12_a_decision_report_does_not_acknowledge_or_alter_a_real_job(jobs, live_pid):
    tree = tree_of(jobs)
    rec = answered_decision(tree)
    bind(jobs, live_pid.pid)
    done, _ = jobs.run("print('hi')", name="other-job")
    jobs.ended(done)
    # one pass or two (a job end and a decision may share a report or not),
    # but both are reported and neither report acknowledges the job
    seen = ""
    for _ in range(2):
        result = watch(jobs)
        seen += result.stderr
    assert rec["id"] in seen and done["id"] in seen
    assert stored(tree, rec["id"])["delivery"]["session"] == SESSION
    assert [r["id"] for r in jobs.listing()] == [done["id"]]
    assert jobs.listing()[0]["acked_at"] is None


# ----------------------------------------------------------------- ordering ---


def test_c12_delivery_is_recorded_after_the_report_is_written(jobs, live_pid):
    """If the report cannot be written (the hook's stderr is gone), the decision
    must still be due for the next watcher: delivery follows the report."""
    tree = tree_of(jobs)
    rec = answered_decision(tree)
    bind(jobs, live_pid.pid)
    proc = spawn_watch(jobs)
    proc.stderr.close()                                 # EPIPE on the first write
    proc.wait(timeout=CLI_BOUND)
    got = stored(tree, rec["id"])
    if got["delivery"] is None:
        # not delivered: the next watcher must report it
        again = watch(jobs)
        assert again.returncode == 2 and rec["id"] in again.stderr
    else:
        pytest.fail("a delivery was recorded although the report was never written")


# --------------------------------------------------------------------- lock ---


def hold_lock(jobs, pid, owner="orchestrator"):
    from multiagents.procs import start_time
    path = registry(jobs) / f"watch-{owner}.lock"
    path.write_text(json.dumps({"pid": pid, "pid_start": start_time(pid)}))
    return path


def test_c12_a_live_lock_holder_excludes_another_watcher_of_the_role(jobs, live_pid):
    tree = tree_of(jobs)
    rec = answered_decision(tree)
    bind(jobs, live_pid.pid)
    holder = subprocess.Popen([sys.executable, "-c", "import signal; signal.pause()"])
    try:
        lock = hold_lock(jobs, holder.pid)
        result = watch(jobs)
        assert result.returncode == 0 and not result.stderr
        assert stored(tree, rec["id"]) == rec           # nothing reported, nothing recorded
        assert json.loads(lock.read_text())["pid"] == holder.pid
    finally:
        holder.kill()
        holder.wait(timeout=CLI_BOUND)


def test_c12_the_other_roles_lock_does_not_exclude_this_watcher(jobs, live_pid):
    tree = tree_of(jobs)
    rec = answered_decision(tree)
    bind(jobs, live_pid.pid)
    hold_lock(jobs, live_pid.pid, owner="initializer")
    result = watch(jobs)
    assert result.returncode == 2 and rec["id"] in result.stderr


def test_c12_a_stale_lock_is_taken_over_and_the_decision_is_delivered(jobs, live_pid):
    tree = tree_of(jobs)
    rec = answered_decision(tree)
    bind(jobs, live_pid.pid)
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait(timeout=CLI_BOUND)
    (registry(jobs) / "watch-orchestrator.lock").write_text(
        json.dumps({"pid": dead.pid, "pid_start": "gone"}))
    result = watch(jobs)
    assert result.returncode == 2 and rec["id"] in result.stderr


def test_c12_two_concurrent_watchers_of_one_role_deliver_exactly_once(jobs, live_pid):
    tree = tree_of(jobs)
    recs = [answered_decision(tree, title=f"Question {i}?") for i in range(4)]
    bind(jobs, live_pid.pid)
    procs = [spawn_watch(jobs) for _ in range(2)]
    outs = []
    for proc in procs:
        _, err = proc.communicate(timeout=CLI_BOUND)
        outs.append((proc.returncode, err))
    for rec in recs:
        assert sum(rec["id"] in err for _, err in outs) == 1, outs
        got = stored(tree, rec["id"])
        assert got["revision"] == rec["revision"] + 1
        assert len(delivered_events(tree, rec["id"])) == 1
    assert sorted(code for code, _ in outs) == [0, 2]


def test_c12_nothing_is_sent_to_ntfy(jobs, live_pid):
    import yaml
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(5)
    try:
        config = jobs.paths.config
        config.mkdir(parents=True, exist_ok=True)
        (config / "project.yaml").write_text(yaml.safe_dump({"notify": {
            "ntfy_url": f"http://127.0.0.1:{listener.getsockname()[1]}", "topic": "ud-test",
            "events": ["question", "held", "anomaly", "done"]}}))
        tree = tree_of(jobs)
        rec = answered_decision(tree, comment="a private remark")
        bind(jobs, live_pid.pid)
        result = watch(jobs)
        assert result.returncode == 2 and rec["id"] in result.stderr
        ready, _, _ = select.select([listener], [], [], 0.3)
        assert ready == [], "the watcher opened a connection to the ntfy server"
    finally:
        listener.close()
