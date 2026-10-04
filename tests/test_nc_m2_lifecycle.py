"""M2 — the scheduler's lifecycle: NC-R16, NC-R57, NC-R17 (+ NC-R61).

Contract: `context/specs/phase7-part1-contract.md`, revision section included.
The scheduler is a real process started with `multiagents scheduler start`;
runs are fixture-agent processes (`tests/nc_fixture`); observations are the
NC-R8 socket, the CLI, `tree.json` and `events.jsonl`.

Assumptions where the contract is silent (kept loose):
- `scheduler start` returns once the scheduler is ready or stays in the
  foreground; a second start prints the live pid (NC-R16) in stdout or stderr.
- crash points NC-R61 names cannot be hit deterministically from outside the
  process, so `test_nc_r17_*_crash_sweep` kills the scheduler (SIGKILL) at a
  sweep of delays after the deposit and checks the invariant at each.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from nc_fixture.agent import alive  # noqa: E402
from nc_fixture.world import World, blocked_codes, run_id_of  # noqa: E402


@pytest.fixture
def w(tmp_path, monkeypatch):
    world = World(tmp_path, monkeypatch)
    yield world
    world.close()


def pc_world(w: World, limit: int = 1):
    pc = w.provider("pcfx", max_concurrent=limit)
    w.agent("pcworker", "pcfx")
    return pc


# ----------------------------------------------------------------- NC-R16

def test_nc_r16_a_second_start_reports_the_live_pid_and_starts_nothing(w):
    pid = w.start_scheduler()
    second = w.cli("scheduler", "start")
    assert second.returncode == 0, second.stdout + second.stderr
    assert str(pid) in second.stdout + second.stderr
    assert w.status()["pid"] == pid
    assert alive(pid)


def test_nc_r16_status_command_names_the_running_scheduler(w):
    pid = w.start_scheduler()
    res = w.cli("scheduler", "status")
    assert res.returncode == 0, res.stdout + res.stderr
    assert str(pid) in res.stdout + res.stderr


def test_nc_r16_after_stop_the_status_command_no_longer_names_the_old_pid(w):
    pid = w.start_scheduler()
    w.stop_scheduler()
    res = w.cli("scheduler", "status")
    assert str(pid) not in res.stdout + res.stderr


def test_nc_r16_stop_ends_the_process_and_releases_the_lock_for_a_new_start(w):
    pid = w.start_scheduler()
    stop = w.stop_scheduler()
    assert stop.returncode == 0, stop.stdout + stop.stderr
    w.until(lambda: not alive(pid), 10, what="the old scheduler to exit")
    new = w.start_scheduler()
    assert new != pid and alive(new)


def test_nc_r16_stop_records_scheduler_stopped_and_start_records_scheduler_started(w):
    w.start_scheduler()
    w.stop_scheduler()
    w.start_scheduler()
    kinds = w.kinds()
    assert kinds.count("node.scheduler_started") == 2
    assert kinds.count("node.scheduler_stopped") == 1
    assert kinds.index("node.scheduler_stopped") > kinds.index("node.scheduler_started")


def test_nc_r16_stop_with_a_live_run_leaves_it_running_and_the_next_start_reconciles_it(w):
    w.start_scheduler()
    node = w.simple("A", fx={"gate": "ga"})
    w.wait_running(node)
    run_id = run_id_of(w.get(node)["active_run"])
    assert run_id
    pid = w.wait_spawn("A")["pid"]

    w.stop_scheduler()
    assert alive(pid), "stop must leave the run running"
    assert w.tree_nodes()[run_id]["status"] == "running"

    w.start_scheduler()
    again = w.get(node)
    assert again["state"] == "running"
    assert run_id_of(again["active_run"]) == run_id
    assert w.fx.spawns() == 1
    assert w.transitions(node).count("launched") == 1

    w.gate("ga")
    done = w.wait_state(node, "done")
    assert done["outcome"] == "completed"
    assert w.fx.spawns() == 1
    assert len(done["runs"]) == 1


def test_nc_r16_stop_does_not_launch_what_was_waiting(w):
    pc_world(w)
    w.start_scheduler()
    a = w.simple("A", "pcworker", fx={"gate": "ga"})
    w.wait_running(a)
    b = w.simple("B", "pcworker")
    w.until(lambda: "admission:provider_concurrency" in blocked_codes(w.get(b)),
            what="B blocked by the provider limit")
    w.stop_scheduler()
    w.gate("ga")
    w.until(lambda: len(w.providers["pcfx"].done()) == 1, what="A to finish")
    w.quiet(3)
    assert w.providers["pcfx"].by_tag("B") == []


def test_nc_r16_status_shows_the_locks_held(w):
    w.start_scheduler()
    a = w.simple("A", locks=["runner.py"], fx={"gate": "ga"})
    w.wait_running(a)
    assert "runner.py" in str(w.status())
    res = w.cli("scheduler", "status")
    assert "runner.py" in res.stdout + res.stderr


# ----------------------------------------------------------------- NC-R57

def test_nc_r57_killing_the_scheduler_changes_no_run_and_frees_no_slot(w):
    pc = pc_world(w)
    w.start_scheduler()
    a = w.simple("A", "pcworker", fx={"gate": "ga"})
    w.wait_running(a)
    run_a = run_id_of(w.get(a)["active_run"])
    pid_a = w.wait_spawn("A", pc)["pid"]
    b = w.simple("B", "pcworker")
    w.until(lambda: "admission:provider_concurrency" in blocked_codes(w.get(b)),
            what="B blocked by the provider limit")

    w.kill9()
    assert alive(pid_a)
    assert w.tree_nodes()[run_a]["status"] == "running"

    w.start_scheduler()
    w.quiet(3)
    assert pc.by_tag("B") == [], "the slot of the live run was freed by the scheduler's death"
    assert w.get(a)["state"] == "running"
    assert w.get(b)["state"] == "open"
    assert "admission:provider_concurrency" in blocked_codes(w.get(b))

    w.gate("ga")
    w.wait_state(a, "done")
    w.wait_state(b, "done")
    assert [len(pc.by_tag(t)) for t in ("A", "B")] == [1, 1]


def test_nc_r57_killing_the_scheduler_keeps_a_lock_held(w):
    w.start_scheduler()
    a = w.simple("A", locks=["runner.py"], fx={"gate": "ga"})
    w.wait_running(a)
    b = w.simple("B", locks=["runner.py"])
    w.until(lambda: "lock" in blocked_codes(w.get(b)), what="B blocked by the lock")
    w.kill9()
    w.start_scheduler()
    w.quiet(3)
    assert w.fx.by_tag("B") == []
    assert "lock" in blocked_codes(w.get(b))
    w.gate("ga")
    w.wait_state(b, "done")
    assert w.fx.by_tag("B")[0]["t"] >= w.fx.done()[0]["t"] - 0.01


def test_nc_r57_r61_a_run_that_ends_while_the_scheduler_is_dead_is_captured_before_admission(w):
    pc = pc_world(w)
    w.start_scheduler()
    a = w.simple("A", "pcworker", fx={"gate": "ga"})
    w.wait_running(a)
    run_a = run_id_of(w.get(a)["active_run"])
    b = w.simple("B", "pcworker")
    w.until(lambda: "admission:provider_concurrency" in blocked_codes(w.get(b)),
            what="B blocked by the provider limit")
    w.kill9()
    w.gate("ga")
    w.until(lambda: w.tree_nodes()[run_a]["status"] == "done",
            what="today's supervision to record the end of the run")
    w.start_scheduler()
    w.wait_state(b, "done")
    done_a = w.get(a)
    assert done_a["state"] == "done" and done_a["outcome"] == "completed"
    assert len(done_a["runs"]) == 1
    assert len(pc.by_tag("A")) == 1 and len(pc.by_tag("B")) == 1
    assert w.index_of("done", a) < w.index_of("launched", b)


# ------------------------------------------------------- NC-R17 (+ NC-R61)

@pytest.mark.parametrize("delay", [0.0, 0.03, 0.08, 0.15, 0.3, 0.6, 1.2])
def test_nc_r17_crash_sweep_a_node_never_has_two_live_runs(w, delay):
    w.start_scheduler()
    node = w.simple("A", fx={"sleep": 0.8})
    w.quiet(delay)
    w.kill9()
    w.start_scheduler()
    done = w.wait_state(node, "done", timeout=45)
    assert done["outcome"] == "completed"
    assert len(w.fx.by_tag("A")) == 1, "the node was launched twice from one eligibility"
    assert len(done["runs"]) == 1
    assert len([n for n in w.tree_nodes().values() if n.get("agent") == "worker"]) == 1


@pytest.mark.parametrize("delay", [0.0, 0.1, 0.5])
def test_nc_r17_repeated_crashes_still_leave_one_run_per_node(w, delay):
    w.start_scheduler()
    ids = [w.simple(t, fx={"sleep": 1.5}) for t in ("A", "B", "C")]
    for _ in range(2):
        w.quiet(delay)
        w.kill9()
        w.start_scheduler()
    for n in ids:
        assert w.wait_state(n, "done", timeout=60)["outcome"] == "completed"
    assert [len(w.fx.by_tag(t)) for t in ("A", "B", "C")] == [1, 1, 1]


def test_nc_r17_the_tree_node_carries_the_attempt_id_of_its_node(w):
    w.start_scheduler()
    node = w.simple("A", fx={"gate": "ga"})
    w.wait_running(node)
    run = w.get(node)["runs"][0]
    assert run["attempt_id"]
    assert w.tree_nodes()[run["run_id"]].get("attempt_id") == run["attempt_id"]


def test_nc_r61_every_launch_try_has_its_own_attempt_id(w):
    w.start_scheduler()
    a = w.simple("A", fx={"gate": "ga"})
    b = w.simple("B", fx={"gate": "gb"})
    w.wait_running(a)
    w.wait_running(b)
    attempts = {w.get(n)["runs"][0]["attempt_id"] for n in (a, b)}
    assert len(attempts) == 2


def test_nc_r17_a_finished_run_goes_through_result_capture_exactly_once(w):
    w.start_scheduler()
    node = w.simple("A")
    done = w.wait_state(node, "done")
    assert done["outcome"] == "completed"
    seq = w.transitions(node)
    for kind in ("created", "launched", "run_finished", "done"):
        assert seq.count(kind) == 1, seq
    assert seq.index("launched") < seq.index("run_finished") < seq.index("done")
    w.restart_scheduler()
    w.quiet(2)
    assert w.transitions(node) == seq, "a restart replayed a transition"


def test_nc_r17_a_failed_run_ends_the_node_failed_without_a_second_launch(w):
    w.start_scheduler()
    node = w.simple("A", fx={"crash": True})
    done = w.wait_state(node, "done")
    assert done["outcome"] == "failed"
    w.quiet(3)
    assert len(w.fx.by_tag("A")) == 1
