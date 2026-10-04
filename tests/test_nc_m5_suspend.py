"""M5 — suspension, resumption and recovery: NC-R40, NC-R62's "a window
resumption never re-seats or cleans", NC-R69 (window races, evaluation at
scheduler start), and the "window closing mid-loop" item of NC-R47.

Contract: `context/specs/phase7-part1-contract.md`, revision sections included.
Time is the clock seam of NC-R82 (`ClockWorld`); runs are fixture-agent
processes (`tests/nc_fixture`). No test sleeps on wall time for a window
boundary; the only real waiting is for the scheduler's own ticks (1 s).

Fixed geometry: window `mon..sun 09:00-17:00` Europe/Paris (default zone);
Mon 2026-10-05 is CEST. `window_tolerance_seconds` is the shipped 60.

Assumptions where the contract is silent (kept loose):
- a suspended run's process is gone (it was stopped); "resumed" means a new
  provider invocation with `-s <the first run's session id>` and a prompt that
  says to resume the interrupted task (tested as the word "resume").
- the resumed activation's prompt need not repeat the fixture script, so a
  resumed fixture run usually finishes at once; tests do not depend on a
  resumed run staying alive.
- `get_node` of a suspended node shows state `suspended`; a resume blocked by
  admission shows the reason in `blocked` as `admission:<code>`.
- a window closing while the scheduler is down is handled at its next start
  even when that is past the tolerance (NC-R69, "at scheduler start, windows are
  evaluated first").
- unconfirmed termination (`termination_unconfirmed`) cannot be forced from
  outside; only the invariant "not `suspended`, lock kept and no second live
  run while the old process is alive" is tested (as in the M2 lock tests).

Not expressible with the fixture agent (reported in the result): a verdict
given before termination is confirmed (NC-R69, needs the fixture to speak the
M4 verdict RPC), and a forced `termination_unconfirmed` hold.
"""
from __future__ import annotations

import sys
import time
from datetime import timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from nc_fixture.agent import alive  # noqa: E402
from nc_fixture.clock import ALL_DAYS, ClockWorld, local  # noqa: E402
from nc_fixture.world import call_tool, run_id_of, unwrap  # noqa: E402

WIN = {"days": ALL_DAYS, "ranges": ["09:00-17:00"]}
NOON = local(2026, 10, 5, 12, 0)
CLOSE = local(2026, 10, 5, 17, 0)          # Monday 17:00
REOPEN = local(2026, 10, 6, 9, 0)          # Tuesday 09:00


@pytest.fixture
def w(tmp_path, monkeypatch):
    world = ClockWorld(tmp_path, monkeypatch, now=NOON,
                       scheduler={"starvation_after_seconds": 10**7})
    world.start_scheduler()
    yield world
    world.close()


def running(w: ClockWorld, tag: str = "A", **fields):
    """A windowed node, running at noon. Returns (node id, pid, session)."""
    fields.setdefault("window", WIN)
    fields.setdefault("fx", {"gate": "ga"})
    node = w.simple(tag, **fields)
    w.wait_running(node)
    call = w.wait_spawn(tag)
    return node, call["pid"], call["session"]


def until_suspended(w: ClockWorld, node: str, timeout: float = 30) -> dict:
    return w.wait_state(node, "suspended", timeout)


def suspend(w: ClockWorld, node: str, after: int = 20) -> dict:
    w.set_clock(CLOSE + timedelta(seconds=after))
    return until_suspended(w, node)


def resumes(w: ClockWorld, tag: str = "A") -> list[dict]:
    return [c for c in w.fx.by_tag(tag) if c["resume"]]


# ------------------------------------------------------- not too early

def test_nc_r40_a_run_is_left_alone_while_its_window_is_open(w):
    node, pid, _ = running(w)
    w.set_clock(CLOSE - timedelta(seconds=30))
    w.quiet(1.5)
    assert alive(pid)
    assert w.get(node)["state"] == "running"
    assert w.transitions(node).count("suspended") == 0


def test_nc_r40_a_run_in_a_node_without_a_window_is_never_suspended(w):
    node, pid, _ = running(w, window=None)
    w.set_clock(local(2026, 10, 6, 3))
    w.quiet(1.5)
    assert alive(pid) and w.get(node)["state"] == "running"


# --------------------------------------------------- suspension proper

@pytest.mark.parametrize("after", [0, 20, 59])
def test_nc_r40_a_run_is_stopped_and_suspended_within_the_tolerance_after_close(w, after):
    node, pid, _ = running(w)
    got = suspend(w, node, after)
    assert not alive(pid), "a suspended node's run must be dead (stopped, confirmed)"
    assert got["outcome"] is None
    assert got.get("hold") is None
    assert w.transitions(node).count("suspended") == 1


def test_nc_r40_suspension_is_not_a_completion_a_failure_or_a_second_launch(w):
    node, pid, _ = running(w)
    suspend(w, node)
    w.quiet(3)
    got = w.get(node)
    assert got["state"] == "suspended" and got["outcome"] is None
    assert w.fx.done_tags() == []
    assert len(w.fx.by_tag("A")) == 1
    kinds = w.transitions(node)
    assert kinds.count("suspended") == 1
    assert "done" not in kinds and "held" not in kinds


def test_nc_r40_a_suspended_node_is_not_resumed_before_its_window_reopens(w):
    node, _, _ = running(w)
    suspend(w, node)
    w.set_clock(REOPEN - timedelta(seconds=30))
    w.quiet(1.5)
    assert w.get(node)["state"] == "suspended"
    assert len(w.fx.by_tag("A")) == 1
    assert w.transitions(node).count("resumed") == 0


# ----------------------------------------------------------- resumption

@pytest.mark.parametrize("after", [0, 20, 59])
def test_nc_r40_a_suspended_node_resumes_the_same_session_within_the_tolerance_of_reopening(w, after):
    node, _, session = running(w)
    suspend(w, node)
    w.set_clock(REOPEN + timedelta(seconds=after))
    w.until(lambda: resumes(w), what="the resumed invocation")
    call = resumes(w)[0]
    assert call["resume"] == session, "resume must use the interrupted run's provider session"
    assert "resume" in call["prompt"].lower()
    assert w.transitions(node).count("resumed") == 1
    w.gate("ga")
    done = w.wait_state(node, "done")
    assert done["outcome"] == "completed"
    assert w.transitions(node).index("resumed") > w.transitions(node).index("suspended")


def test_nc_r62_a_window_resumption_runs_in_place_without_cleaning_the_checkout(w):
    # the first run leaves an uncommitted file in its working directory; the
    # resumption must come back to the same directory with the file intact.
    node, _, _ = running(w, fx={"gate": "ga", "write": {"left_behind.txt": "wip\n"}})
    # the file is written after the gate; open it, then re-gate by suspending
    # a second activation instead: use a run that wrote first, then hangs.
    w.gate("ga")
    w.wait_state(node, "done")
    node2, _, session = running(w, "B", fx={"write": {"wip.txt": "half done\n"}, "hang": True})
    first_cwd = w.wait_spawn("B")["cwd"]
    wip = Path(first_cwd) / "wip.txt"
    w.until(lambda: wip.exists(), what="the interrupted run's file")
    suspend(w, node2)
    assert wip.read_text() == "half done\n", "suspension must not clean the checkout"
    w.set_clock(REOPEN + timedelta(seconds=5))
    w.until(lambda: resumes(w, "B"), what="the resumed invocation")
    call = resumes(w, "B")[0]
    assert call["cwd"] == first_cwd, "resumption must run in the same working directory"
    assert wip.exists() and wip.read_text() == "half done\n"
    assert w.get(node2)["state"] != "held", w.get(node2).get("hold")


def test_nc_r40_a_node_can_be_suspended_and_resumed_repeatedly_on_one_session(w):
    node, _, session = running(w, fx={"hang": True})
    for day in range(3):
        close = CLOSE + timedelta(days=day)
        w.set_clock(close + timedelta(seconds=10))
        until_suspended(w, node)
        w.set_clock(close + timedelta(hours=16, seconds=10))      # next 09:00
        w.until(lambda d=day: len(resumes(w)) == d + 1, what=f"resume #{day + 1}")
        w.wait_running(node)
    assert {c["resume"] for c in resumes(w)} == {session}
    kinds = w.transitions(node)
    assert kinds.count("suspended") == 3 and kinds.count("resumed") == 3
    assert len(w.fx.by_tag("A")) == 4


def test_nc_r40_two_runs_in_the_same_window_are_both_suspended_once(w):
    a, pa, _ = running(w, "A", fx={"hang": True})
    b, pb, _ = running(w, "B", fx={"hang": True})
    w.set_clock(CLOSE + timedelta(seconds=15))
    until_suspended(w, a)
    until_suspended(w, b)
    assert not alive(pa) and not alive(pb)
    assert w.transitions(a).count("suspended") == 1
    assert w.transitions(b).count("suspended") == 1


def test_nc_r40_the_window_of_an_ancestor_suspends_its_descendants_too(w):
    w.hold_lock("L")
    child = w.simple("A", locks=["L"], fx={"hang": True})
    group = w.create({"kind": "group", "children": [child], "window": WIN})
    assert group.get("ok"), group
    w.gate("holder")
    w.wait_running(child)
    suspend(w, child)
    assert w.transitions(child).count("suspended") == 1
    w.set_clock(REOPEN + timedelta(seconds=5))
    w.until(lambda: resumes(w), what="the resumed invocation")


# ------------------------------------------------ slots, locks, admission

def test_nc_r40_the_lock_is_released_only_once_the_stopped_run_is_really_dead(w):
    a, pid_a, _ = running(w, locks=["L"], fx={"hang": True})
    x = w.simple("X", locks=["L"], fx={"watch_pid": pid_a})
    w.until(lambda: "lock" in w.codes(x), what="X blocked by A's lock")
    suspend(w, a)
    w.wait_state(x, "done", timeout=60)
    assert w.fx.by_tag("X")[0]["watch_alive"] is False


def test_nc_r40_a_suspended_node_frees_its_provider_slot(w):
    pc = w.provider("pcfx", max_concurrent=1)
    w.agent("pcworker", "pcfx")
    a, _, _ = running(w, "A", agent="pcworker", fx={"hang": True})
    b = w.simple("B", "pcworker", fx={"gate": "gb"})
    w.until(lambda: "admission:provider_concurrency" in w.codes(b), what="B blocked")
    suspend(w, a)
    w.wait_running(b)
    assert len(pc.by_tag("B")) == 1


def test_nc_r40_a_resume_blocked_by_admission_leaves_the_node_suspended_with_the_reason(w):
    pc = w.provider("pcfx", max_concurrent=1)
    w.agent("pcworker", "pcfx")
    a, _, session = running(w, "A", agent="pcworker", fx={"hang": True})
    b = w.simple("B", "pcworker", fx={"gate": "gb"})
    suspend(w, a)
    w.wait_running(b)
    w.set_clock(REOPEN + timedelta(seconds=30))
    w.quiet(1.5)
    got = w.get(a)
    assert got["state"] == "suspended"
    assert "admission:provider_concurrency" in w.codes(a), w.codes(a)
    assert [c for c in pc.by_tag("A") if c["resume"]] == []
    assert w.transitions(a).count("resumed") == 0
    # the tolerance bounds when stopping starts, not re-admission: once the
    # slot frees (well past the tolerance) the node still resumes.
    w.set_clock(REOPEN + timedelta(minutes=30))
    w.gate("gb") if False else pc.open_gate("gb")
    w.until(lambda: [c for c in pc.by_tag("A") if c["resume"]], timeout=45,
            what="the resumed invocation after admission freed")
    assert [c for c in pc.by_tag("A") if c["resume"]][0]["resume"] == session
    assert w.transitions(a).count("resumed") == 1


# ---------------------------------------------------- recovery (restart)

@pytest.mark.parametrize("hard", [False, True], ids=["stop", "kill9"])
def test_nc_r40_a_scheduler_restart_while_suspended_preserves_the_suspension(w, hard):
    node, _, session = running(w)
    suspend(w, node)
    if hard:
        w.kill9()
        w.start_scheduler()
    else:
        w.restart_scheduler()
    w.quiet(2)
    got = w.get(node)
    assert got["state"] == "suspended" and got["outcome"] is None
    assert len(w.fx.by_tag("A")) == 1
    assert w.transitions(node).count("suspended") == 1
    assert w.transitions(node).count("resumed") == 0
    w.set_clock(REOPEN + timedelta(seconds=10))
    w.until(lambda: resumes(w), what="the resumed invocation after the restart")
    assert resumes(w)[0]["resume"] == session
    assert w.transitions(node).count("resumed") == 1


def test_nc_r69_at_start_a_run_found_outside_its_window_is_suspended_even_past_the_tolerance(w):
    node, pid, _ = running(w)
    w.stop_scheduler()
    assert alive(pid), "stopping the scheduler leaves runs running (NC-R57)"
    w.write_clock(CLOSE + timedelta(hours=1))
    w.start_scheduler()
    until_suspended(w, node)
    assert not alive(pid)
    assert w.transitions(node).count("suspended") == 1
    assert len(w.fx.by_tag("A")) == 1


def test_nc_r69_at_start_a_suspended_node_inside_an_open_window_is_resumed(w):
    node, _, session = running(w)
    suspend(w, node)
    w.stop_scheduler()
    w.write_clock(REOPEN + timedelta(hours=1))
    w.start_scheduler()
    w.until(lambda: resumes(w), what="the resumption at start")
    assert resumes(w)[0]["resume"] == session
    assert w.transitions(node).count("resumed") == 1
    assert len([c for c in w.fx.by_tag("A") if c["resume"]]) == 1


def test_nc_r69_windows_are_evaluated_before_anything_is_admitted_at_start(w):
    # a closed-window node waiting to launch must not be launched by the first
    # admission pass of a restarted scheduler that sees the window shut.
    w.stop_scheduler()
    w.write_clock(local(2026, 10, 5, 18))
    node = w.simple("W", window=WIN, fx={"gate": "gw"})
    w.start_scheduler()
    w.quiet(2.5)
    assert w.fx.by_tag("W") == []
    assert w.get(node)["state"] == "open"


def test_nc_r40_a_run_that_ends_by_itself_in_the_tolerance_is_not_suspended(w):
    node, pid, _ = running(w, fx={"gate": "ga"})
    w.gate("ga")
    w.wait_state(node, "done")
    w.set_clock(CLOSE + timedelta(seconds=20))
    w.quiet(1.5)
    got = w.get(node)
    assert got["state"] == "done" and got["outcome"] == "completed"
    assert w.transitions(node).count("suspended") == 0


# ------------------------------------------------ unconfirmed termination

def test_nc_r69_a_run_that_ignores_sigterm_is_never_marked_suspended_while_alive(w):
    node, pid, _ = running(w, locks=["L"], fx={"hang": True, "ignore_term": True})
    x = w.simple("X", locks=["L"], fx={"watch_pid": pid})
    w.until(lambda: "lock" in w.codes(x), what="X blocked by the lock")
    w.set_clock(CLOSE + timedelta(seconds=15))
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline and alive(pid):
        got = w.get(node)
        assert got["state"] != "suspended", "suspended while the process lives"
        assert "lock" in w.codes(x) or w.get(x)["state"] != "open", \
            "the lock was released while the holder lives"
        time.sleep(0.3)
    if not alive(pid):
        until_suspended(w, node, timeout=30)
        if w.fx.by_tag("X"):
            assert w.fx.by_tag("X")[0]["watch_alive"] is False


def test_nc_r69_a_reopened_window_does_not_start_a_second_run_beside_a_surviving_one(w):
    node, pid, _ = running(w, fx={"hang": True, "ignore_term": True})
    w.set_clock(CLOSE + timedelta(seconds=15))
    w.set_clock(REOPEN + timedelta(seconds=15))
    if alive(pid):
        assert len(w.fx.by_tag("A")) == 1, "a second live run for one node"
    else:
        assert len(w.fx.by_tag("A")) <= 2


# ------------------------------------------- other paths out of suspended

def test_nc_r56_steering_a_suspended_nodes_run_is_refused_node_suspended(w):
    node, _, _ = running(w)
    run_id = run_id_of(w.get(node)["active_run"])
    suspend(w, node)
    spawns = len(w.fx.calls())
    out = call_tool(w, "steer_agent", run_id, "go faster")
    assert "node_suspended" in str(out), out
    assert len(w.fx.calls()) == spawns


def test_nc_r20_stopping_a_suspended_nodes_run_holds_it_stopped_by_orchestrator(w):
    node, _, _ = running(w)
    run_id = run_id_of(w.get(node)["active_run"])
    suspend(w, node)
    call_tool(w, "stop_agent", run_id)
    got = w.until(lambda: (n := w.get(node))["state"] == "held" and n, what="held")
    assert got["hold"]["reason"] == "stopped_by_orchestrator"
    w.set_clock(REOPEN + timedelta(seconds=10))
    w.quiet(2)
    assert w.get(node)["state"] == "held"
    assert resumes(w) == []


def test_nc_r40_cancelling_a_suspended_node_means_it_never_resumes(w):
    node, _, _ = running(w)
    suspend(w, node)
    assert w.cancel(node)["ok"]
    assert w.get(node)["state"] == "cancelled"
    w.set_clock(REOPEN + timedelta(seconds=10))
    w.quiet(2)
    assert w.get(node)["state"] == "cancelled"
    assert resumes(w) == []


def test_nc_r40_widening_the_window_of_a_suspended_node_resumes_it(w):
    node, _, session = running(w)
    suspend(w, node)
    rev = w.get(node)["revision"]
    w.ok("update_node", {"id": node, "revision": rev,
                         "window": {"days": ALL_DAYS, "ranges": ["00:00-24:00"]}})
    w.until(lambda: resumes(w), what="the resumption after widening")
    assert resumes(w)[0]["resume"] == session


# --------------------------------------------------- NC-R47: mid-loop

def test_nc_r47_nc_r40_window_closing_mid_loop_suspends_the_reviewer_and_resumes_it_first(w):
    """Needs M4 (loop launching, sequential children, unresolved rounds); the
    window machinery is M5. The reviewer is the loop's last child and the
    verdict child; its work is interrupted by the window closing.

    The reviewer is stopped, the lock it holds is released only after its death
    (the outside node X observes it dead), the round counter does not move, the
    writer is not run again; on reopening the reviewer resumes in the same
    provider session before anything else in the loop happens (no
    `round_rejected` / `loop_*` / `unresolved_round` precedes `resumed`);
    a scheduler restart while suspended changes none of this."""
    outside_window = local(2026, 10, 5, 8, 0)
    w.set_clock(outside_window)
    # the children carry the window too so they cannot launch before the loop
    # adopts them (the loop's own window is the one under test)
    wr = w.simple("WRITER", window=WIN)
    rv = w.simple("REVIEWER", window=WIN, locks=["L"], fx={"hang": True})
    reply = w.create({"kind": "loop", "children": [wr, rv], "window": WIN,
                      "loop": {"verdict_child": rv, "max_rounds": 3}})
    assert reply.get("ok"), reply
    loop = unwrap(reply["result"])["id"]
    w.set_clock(NOON)
    w.wait_running(rv, timeout=45)
    call = w.wait_spawn("REVIEWER")
    pid, session = call["pid"], call["session"]
    assert len(w.fx.by_tag("WRITER")) == 1
    x = w.simple("X", locks=["L"], fx={"watch_pid": pid})
    w.until(lambda: "lock" in w.codes(x), what="X blocked by the reviewer's lock")
    counter = w.get(loop)["loop"]["rounds_rejected"]

    w.set_clock(CLOSE + timedelta(seconds=15))
    until_suspended(w, rv)
    assert not alive(pid)
    w.wait_state(x, "done", timeout=60)
    assert w.fx.by_tag("X")[0]["watch_alive"] is False
    assert w.get(loop)["loop"]["rounds_rejected"] == counter
    assert len(w.fx.by_tag("WRITER")) == 1
    assert w.get(loop)["state"] != "done"

    w.restart_scheduler()
    w.quiet(2)
    assert w.get(rv)["state"] == "suspended"
    assert w.get(loop)["loop"]["rounds_rejected"] == counter
    assert len(w.fx.by_tag("REVIEWER")) == 1

    w.set_clock(REOPEN + timedelta(seconds=10))
    w.until(lambda: resumes(w, "REVIEWER"), timeout=45, what="the reviewer's resumption")
    assert resumes(w, "REVIEWER")[0]["resume"] == session
    assert len(w.fx.by_tag("WRITER")) == 1, "the loop advanced before the resumption"
    order = w.transitions(loop) + w.transitions(rv)
    assert w.get(loop)["loop"]["rounds_rejected"] == counter
    events = [w.kind_of(e) for e in w.events() if rv in str(e) or loop in str(e)]
    first_resumed = events.index("node.resumed")
    assert not {"node.round_rejected", "node.loop_exited", "node.loop_max",
                "node.unresolved_round"} & set(events[:first_resumed])
    assert order  # (kept for the failure message of the assertions above)
