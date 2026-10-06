"""AN-R1..AN-R5: the scheduler reports what is stuck instead of waiting silently
(context/specs/scheduler-anomalies.md).

Two layers, both black box (the public surface is the scheduler's transitions as
`wait_for_nodes` delivers them, plus node and run state):

* In-process: a `Service` + `Engine` on a `GitWorld` (tests/nc_fixture/m3_adv.py),
  the engine built with a clock file (the NC-R82 seam, so "now" is a file the
  test rewrites) and driven by `Engine.tick()` -- the evaluation the host
  scheduler runs forever. A "check" is a tick at a clock reading at least one
  `anomaly_interval_seconds` after the last. Nothing sleeps on wall time.
* One real scheduler process (`scheduler start --clock-file`) for the wiring: the
  host loop itself runs the check, and `wait_for_nodes` over the socket returns.

The fake clock starts at the real "now", so the transitions the harness lays
down (stamped when they are written) sit at the start of the scheduler's
timeline. Ages are read on the scheduler's clock (contract, Clarifications), and
the first check runs at start.
Nothing here starts a process except the `real` fixture, whose finalizer stops
the scheduler (context/specs/test-process-leak.md); the in-process engine never
launches a worker, since every node is held, refused, done or blocked.
"""
from __future__ import annotations

import fcntl
import json
import re
import shutil
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

from nc_fixture.clock import ClockWorld  # noqa: E402
from nc_fixture.m3_adv import Harness  # noqa: E402
from nc_fixture.world import blocked_codes  # noqa: E402
import c3_harness as h3  # noqa: E402
from multiagents import config as config_mod  # noqa: E402
from multiagents.paths import ProjectPaths, state_root  # noqa: E402
from multiagents.scheduler_config import SchedulerConfigError  # noqa: E402
from multiagents.scheduler.engine import Engine, attempts  # noqa: E402
from multiagents.scheduler.rpc import Service  # noqa: E402
from multiagents.tree import Node, now  # noqa: E402

WAIT = 3            # every wait_for_nodes in this module is bounded by this
INTERVAL = 10       # anomaly_interval_seconds used unless a test is about the default
THRESHOLD = 60      # anomaly_admission_seconds / anomaly_held_seconds used by most tests
HUGE = 10 ** 9      # keeps the unrelated starvation notice out of byte comparisons
CONTRADICTORY = "review done.\nVERDICT(approved): the first pass\nVERDICT(rejected, 2): the second pass"
STUCK = "silence: no output for 300s"   # the shape runner.py writes: f"{reason}: {detail}"


# ------------------------------------------------------------------ harness
def forget_stale_state(project):
    """The scheduler store lives under the state root, keyed by the project path:
    a rerun with the same --basetemp would otherwise open the previous run's nodes."""
    shutil.rmtree(state_root() / "scheduler-rpc" / ProjectPaths(project).slug, ignore_errors=True)


class Env:
    def __init__(self, tmp_path, monkeypatch, **scheduler):
        self.project = tmp_path / "proj"
        forget_stale_state(self.project)
        self.h = Harness(tmp_path, monkeypatch)
        self.h.world.project["scheduler"].update(
            {"anomaly_interval_seconds": INTERVAL, "anomaly_admission_seconds": THRESHOLD,
             "anomaly_held_seconds": THRESHOLD, "starvation_after_seconds": HUGE})
        self.h.world.project["scheduler"].update(scheduler)
        self.h.world.write_config()
        self.clock_file = tmp_path / "an-clock.iso"
        self.t0 = datetime.now(timezone.utc)
        self.at = self.t0
        self.locks = []
        self.write_clock()
        # Replace the harness engine by one reading the fake clock.
        self.h.engine.loop.close()
        self.engine = Engine(self.h.service, clock_file=self.clock_file)
        self.h.service.engine = self.engine
        self.h.engine = self.engine
        self.service = self.h.service

    def close(self):
        for lock in self.locks:
            lock.close()
        for child in list(getattr(self.engine, "children", [])):
            try:
                child.kill()
            except Exception:       # noqa: BLE001 - teardown must not mask the test
                pass
        self.engine.loop.close()
        self.h.world.close()
        forget_stale_state(self.project)

    # -- clock and checks
    def write_clock(self):
        tmp = self.clock_file.with_suffix(".tmp")
        tmp.write_text(self.at.isoformat() + "\n")
        tmp.replace(self.clock_file)

    def tick(self, advance=0):
        """Move the clock by `advance` seconds and run one evaluation."""
        self.at += timedelta(seconds=advance)
        self.write_clock()
        self.engine.loop.run_until_complete(self.engine.tick())

    def at_offset(self, seconds):
        """Tick at `seconds` after the start, not before the previous tick."""
        target = self.t0 + timedelta(seconds=seconds)
        self.tick((target - self.at).total_seconds())

    # -- reading
    def request(self, op, args):
        with self.service.store.transaction(write=False) as db:
            token = self.service.store.meta(db, "root_token")
        return self.service.request({"op": op, "token": token, "args": args,
                                     "request_id": f"{op}-{uuid.uuid4().hex}"})

    def transitions(self, **args):
        reply = self.request("wait_for_nodes", {"timeout": 0, "cursor": 0, **args})
        assert reply.get("ok"), reply
        return reply["result"]["transitions"]

    def anomalies(self, kind=None, node=None):
        found = [t for t in self.transitions() if t["kind"] == "anomaly"]
        if kind:
            found = [t for t in found if (t["detail"] or {}).get("kind") == kind]
        if node:
            found = [t for t in found if t["node_id"] == node]
        return found

    # -- laying nodes out
    def hold(self, node, reason="input_conflict", detail="x"):
        node = self.h.nodes()[node["id"]]
        with self.service.changed, self.service.store.transaction() as db:
            current = self.service.store.nodes(db)[node["id"]]
            self.engine.hold(db, current, reason, detail)

    def run(self, node, status="running", reason="", text=None, verdict=None):
        """A run of `node` in the project's tree, as the runner leaves it."""
        run_id = "ag-" + uuid.uuid4().hex[:6]
        run = Node(id=run_id, agent="coder", provider="fx", model="fx/m1", parent=None,
                   depth=1, branch="agents/coder/" + run_id[3:], worktree=str(self.h.world.root),
                   status="running", node_id=node["id"], attempt_id=uuid.uuid4().hex)
        self.engine.runner.tree.add(run)
        if status != "running":
            self.engine.runner.tree.set_status(run_id, status, reason)
        if text is not None:
            run_dir = self.h.world.paths.run_dir(run_id)
            run_dir.mkdir(parents=True, exist_ok=True)
            (run_dir / "result.json").write_text(json.dumps({"id": run_id, "status": status, "text": text}))
            self.engine.runner.tree.update(run_id, summary=text[-500:], **(verdict or {}))
        record = self.h.nodes()[node["id"]]
        record["runs"] = record["runs"] + [{"run_id": run_id, "attempt_id": run.attempt_id}]
        self.h.save(record)
        return run_id

    def mark_stuck(self, run_id, reason=STUCK):
        # What the Supervisor's trip does (runner.py: set_status + emit).
        self.engine.runner.tree.set_status(run_id, "stuck", reason)
        self.engine.runner.tree.emit(run_id, "stuck", reason=reason.split(":")[0], detail=reason)

    def clear_stuck(self, run_id):
        self.engine.runner.tree.set_status(run_id, "running", "")

    def live_run(self, node):
        """A launched attempt with a running run, the way the scheduler leaves a
        node mid-run. The attempt's claim lock is held here, as a live worker
        would hold it, so no tick spawns a worker for it (nothing in this module
        starts a process) and a tick does not recover it as a dead launch."""
        attempt, run = self.h.launch(node["id"])
        self.engine.runner.tree.set_status(run.id, "running", "")
        lock = (self.service.store.directory / (attempt["attempt_id"] + ".lock")).open("a+")
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.locks.append(lock)
        return run.id

    def stuck_node(self, reason=STUCK):
        """A node whose current run the Supervisor has marked stuck."""
        node = self.h.record()
        self.h.save(node)
        run_id = self.live_run(node)
        self.mark_stuck(run_id, reason)
        return node, run_id

    def refused_node(self):
        """An open, structurally ready node that admission refuses (depth limit)."""
        node = self.h.record()
        node["depth"] = 99
        self.h.save(node)
        return node

    def unresolved_loop(self, text=CONTRADICTORY, status="done"):
        """loop L = [impl (done), rev (done, its run's text ends in `text`)], held
        `unresolved_round`: the reviewer never called give_verdict. The default text
        holds contradictory verdict lines, which VR's settle step leaves unsettled
        (VR-R3), so the AN check sees a round that stays unresolved."""
        tip = self.h.world.main_tip()
        impl, rev = self.h.record(), self.h.record()
        loop = self.h.record(kind="loop", children=[impl["id"], rev["id"]],
                             loop={"verdict_child": rev["id"], "max_rounds": 3})
        generation = {"seq": 1, "commit": tip, "run_id": "ag-111111", "verdict": None}
        impl.update(parent=loop["id"], state="done", outcome="completed", generations=[generation])
        rev.update(parent=loop["id"], state="done", outcome="completed")
        loop.update(state="held", hold={"reason": "unresolved_round", "detail": ""},
                    generations=[dict(generation)])
        self.h.save(loop, impl, rev)
        found = re_verdict(text)
        self.run(rev, status=status, text=text, verdict=found)
        return loop, rev

    # -- state snapshots
    def snapshot(self):
        nodes = self.h.nodes()
        journal = attempts_snapshot(self.service)
        tree = {id: {k: getattr(n, k, None) for k in ("status", "reason", "verdict", "summary")}
                for id, n in ((i, self.engine.runner.tree.get(i))
                              for i in self.engine.runner.tree.read()["nodes"])}
        return json.dumps({"nodes": nodes, "attempts": journal, "tree": tree},
                          sort_keys=True, default=str)


def attempts_snapshot(service):
    with service.store.transaction(write=False) as db:
        return attempts(db)


def re_verdict(text):
    from multiagents.runner import VERDICT      # the parser the runner uses
    found = list(VERDICT.finditer(text or ""))
    if not found:
        return None
    return {"verdict": found[-1].group(1).lower(), "defects": int(found[-1].group(2) or 0)}


def evidence(transition):
    return json.dumps(transition["detail"], sort_keys=True, default=str)


@pytest.fixture
def env(tmp_path, monkeypatch):
    e = Env(tmp_path, monkeypatch)
    yield e
    e.close()


# -------------------------------------------------------------------- AN-R1
def test_an_r1_a_check_runs_once_the_interval_has_elapsed(env):
    node, _ = env.stuck_node()
    env.at_offset(0)
    env.at_offset(INTERVAL + 1)
    assert [t["node_id"] for t in env.anomalies("run_stuck")] == [node["id"]]


def test_an_r1_no_check_runs_inside_the_interval(env):
    env.at_offset(0)                    # a check here or not: nothing is wrong yet
    node, _ = env.stuck_node()
    env.at_offset(INTERVAL - 4)
    assert env.anomalies() == []        # the condition exists, the interval has not elapsed
    env.at_offset(2 * INTERVAL)
    assert [t["node_id"] for t in env.anomalies("run_stuck")] == [node["id"]]


def test_an_r1_the_interval_defaults_to_120_seconds(tmp_path, monkeypatch):
    e = Env(tmp_path, monkeypatch)
    try:
        sched = e.h.world.project["scheduler"]
        del sched["anomaly_interval_seconds"]
        e.h.world.write_config()
        e.at_offset(0)
        node, _ = e.stuck_node()
        e.at_offset(60)
        e.at_offset(100)
        assert e.anomalies() == [], "checked before 120 s"
        e.at_offset(250)
        assert [t["node_id"] for t in e.anomalies("run_stuck")] == [node["id"]]
    finally:
        e.close()


def test_an_r1_a_longer_configured_interval_is_honoured(tmp_path, monkeypatch):
    e = Env(tmp_path, monkeypatch, anomaly_interval_seconds=500)
    try:
        e.at_offset(0)
        node, _ = e.stuck_node()
        e.at_offset(100)
        e.at_offset(300)
        assert e.anomalies() == []
        e.at_offset(700)
        assert [t["node_id"] for t in e.anomalies("run_stuck")] == [node["id"]]
    finally:
        e.close()


def test_an_r1_an_idle_project_emits_nothing_across_many_checks(env):
    done = env.h.record()
    done.update(state="done", outcome="completed")
    env.h.save(done)
    for step in range(0, 20 * INTERVAL, INTERVAL):
        env.at_offset(step)
    assert env.anomalies() == []
    assert env.transitions() == []


# -------------------------------------------------------------------- AN-R2
def test_an_r2_run_stuck_carries_the_supervisors_reason(env):
    node, run_id = env.stuck_node("doom loop: the same edit three times")
    env.at_offset(0)
    env.at_offset(INTERVAL + 1)
    found = env.anomalies("run_stuck")
    assert len(found) == 1 and found[0]["node_id"] == node["id"]
    assert "doom loop: the same edit three times" in evidence(found[0])
    assert run_id in evidence(found[0]) or run_id in json.dumps(found[0])


def test_an_r2_run_stuck_is_not_reported_for_a_run_that_is_not_stuck(env):
    node = env.h.record()
    env.h.save(node)
    env.live_run(node)
    env.at_offset(0)
    env.at_offset(INTERVAL + 1)
    env.at_offset(3 * INTERVAL)
    assert env.anomalies() == []


def test_an_r2_run_stuck_reads_the_nodes_current_run_only(env):
    node = env.h.record()
    env.h.save(node)
    old = env.run(node, status="failed", reason="was stuck: silence")
    env.live_run(node)                  # the current run, healthy
    env.at_offset(0)
    env.at_offset(INTERVAL + 1)
    assert env.anomalies("run_stuck") == [], f"reported {old}, which is not the current run"


@pytest.mark.parametrize("text", [
    CONTRADICTORY,
    "VERDICT(rejected, 1): bad\nVERDICT(approved): fine after all",
    "VERDICT(approved, 0): fine\nnotes in between\nVERDICT(rejected, 3): three defects",
    "  VERDICT(approved): indented\nVERDICT(approved): again\nVERDICT(rejected): but no",
])
def test_an_r2_verdict_unrecorded_fires_for_contradictory_lines_vr_leaves_unsettled(env, text):
    loop, rev = env.unresolved_loop(text=text)
    env.at_offset(0)
    env.at_offset(INTERVAL + 1)
    found = env.anomalies("verdict_unrecorded")
    assert len(found) == 1
    assert found[0]["node_id"] == loop["id"]         # the loop is the node that needs attention
    assert found[0]["detail"]["reason"]
    assert loop["id"] in evidence(found[0]) or rev["id"] in evidence(found[0])


@pytest.mark.parametrize("line", ["VERDICT(approved): x", "VERDICT(approved, 0): fine",
                                  "VERDICT(rejected, 2): two defects",
                                  "VERDICT(approved): once\nVERDICT(approved): twice"])
def test_an_r2_verdict_unrecorded_does_not_fire_for_lines_vr_settles(env, line):
    """The check runs after VR's settle step in the same tick (Clarifications): a
    round with a single accepted verdict is settled by VR, so there is nothing
    left to report. Red-by-skip until VR lands."""
    loop, rev = env.unresolved_loop(text=f"review done.\n{line}")
    env.at_offset(0)
    env.at_offset(INTERVAL + 1)
    record = env.h.nodes()[loop["id"]]
    if record["state"] == "held" and (record["hold"] or {}).get("reason") == "unresolved_round":
        pytest.skip("VR has not landed: the round is still unresolved after the tick")
    assert env.anomalies("verdict_unrecorded") == []


@pytest.mark.parametrize("text", [
    "I reviewed it and it looks fine.",
    "VERDICT: approved",
    "VERDICT(maybe): unsure",
    "the format is VERDICT(approved) without a colon",
    "",
])
def test_an_r2_verdict_unrecorded_needs_a_line_the_parser_accepts(env, text):
    env.unresolved_loop(text=text)
    env.at_offset(0)
    env.at_offset(INTERVAL + 1)
    assert env.anomalies("verdict_unrecorded") == []


def test_an_r2_verdict_unrecorded_only_while_the_round_is_unresolved(env):
    loop, rev = env.unresolved_loop()
    record = env.h.nodes()[loop["id"]]
    record.update(state="running", hold=None)       # the round was reopened
    env.h.save(record)
    env.at_offset(0)
    env.at_offset(INTERVAL + 1)
    assert env.anomalies("verdict_unrecorded") == []


def test_an_r2_verdict_unrecorded_ignores_a_loop_with_a_held_reason_other_than_unresolved(env):
    loop, rev = env.unresolved_loop()
    record = env.h.nodes()[loop["id"]]
    record["hold"] = {"reason": "loop_max", "detail": ""}
    env.h.save(record)
    env.at_offset(0)
    env.at_offset(INTERVAL + 1)
    assert env.anomalies("verdict_unrecorded") == []


def test_an_r2_verdict_unrecorded_reads_the_verdict_childs_own_run(env):
    """A verdict line in another run's text (the implementer's) does not count."""
    tip = env.h.world.main_tip()
    impl, rev = env.h.record(), env.h.record()
    loop = env.h.record(kind="loop", children=[impl["id"], rev["id"]],
                        loop={"verdict_child": rev["id"], "max_rounds": 3})
    generation = {"seq": 1, "commit": tip, "run_id": "ag-111111", "verdict": None}
    impl.update(parent=loop["id"], state="done", outcome="completed", generations=[generation])
    rev.update(parent=loop["id"], state="done", outcome="completed")
    loop.update(state="held", hold={"reason": "unresolved_round", "detail": ""},
                generations=[dict(generation)])
    env.h.save(loop, impl, rev)
    env.run(impl, status="done", text="VERDICT(approved): quoted by the implementer",
            verdict={"verdict": "approved", "defects": 0})
    env.run(rev, status="done", text="no verdict written")
    env.at_offset(0)
    env.at_offset(INTERVAL + 1)
    assert env.anomalies("verdict_unrecorded") == []


def test_an_r2_admission_blocked_after_the_threshold_with_the_refusal_reason(env):
    node = env.refused_node()
    env.at_offset(0)
    env.at_offset(THRESHOLD - 15)
    assert env.anomalies("admission_blocked") == [], "reported before the threshold"
    env.at_offset(THRESHOLD + 15)
    found = env.anomalies("admission_blocked")
    assert [t["node_id"] for t in found] == [node["id"]]
    assert "max_depth" in evidence(found[0]) or "Depth limit" in evidence(found[0])
    assert found[0]["node_id"] == node["id"]          # the refused node itself


def test_an_r2_admission_blocked_defaults_to_600_seconds(tmp_path, monkeypatch):
    e = Env(tmp_path, monkeypatch)
    try:
        sched = e.h.world.project["scheduler"]
        del sched["anomaly_admission_seconds"]
        e.h.world.write_config()
        node = e.refused_node()
        for step in (0, 130, 260, 390, 520):
            e.at_offset(step)
        assert e.anomalies("admission_blocked") == []
        e.at_offset(650)
        assert [t["node_id"] for t in e.anomalies("admission_blocked")] == [node["id"]]
    finally:
        e.close()


def test_an_r2_admission_blocked_is_not_reported_for_a_node_waiting_on_a_dependency(env):
    first = env.h.record()
    waiting = env.h.record(depends_on=[{"node": first["id"]}])
    waiting["depth"] = 99
    env.h.save(first, waiting)
    env.live_run(first)
    env.at_offset(0)
    env.at_offset(THRESHOLD + 15)
    env.at_offset(3 * THRESHOLD)
    assert [c for c in blocked_codes(env.engine.view(env.h.nodes()[waiting["id"]], env.h.nodes(), {}))
            if c == "dependency"] == ["dependency"]      # control: it is structurally blocked
    assert env.anomalies("admission_blocked") == []


def test_an_r2_held_idle_after_the_threshold(env):
    node = env.h.record()
    env.h.save(node)
    env.hold(node)
    env.at_offset(0)
    env.at_offset(THRESHOLD - 15)
    assert env.anomalies("held_idle") == [], "reported before the threshold"
    env.at_offset(THRESHOLD + 15)
    found = env.anomalies("held_idle")
    assert [t["node_id"] for t in found] == [node["id"]]


def test_an_r2_held_idle_defaults_to_600_seconds(tmp_path, monkeypatch):
    e = Env(tmp_path, monkeypatch)
    try:
        sched = e.h.world.project["scheduler"]
        del sched["anomaly_held_seconds"]
        e.h.world.write_config()
        node = e.h.record()
        e.h.save(node)
        e.hold(node)
        for step in (0, 130, 260, 390, 520):
            e.at_offset(step)
        assert e.anomalies("held_idle") == []
        e.at_offset(650)
        assert [t["node_id"] for t in e.anomalies("held_idle")] == [node["id"]]
    finally:
        e.close()


def test_an_r2_held_idle_is_not_reported_for_a_node_that_is_not_held(env):
    waiting = env.h.record(depends_on=[{"node": "nd-00000000"}])
    env.h.save(waiting)
    env.at_offset(0)
    env.at_offset(THRESHOLD + 15)
    env.at_offset(3 * THRESHOLD)
    assert env.anomalies("held_idle") == []


def test_an_r2_every_anomaly_names_its_kind_and_carries_evidence(env):
    env.stuck_node()
    env.at_offset(0)
    env.at_offset(INTERVAL + 1)
    found = env.anomalies()
    assert found
    for t in found:
        assert t["kind"] == "anomaly" and t["node_id"]
        assert t["detail"]["kind"] in {"run_stuck", "verdict_unrecorded", "admission_blocked", "held_idle"}
        assert len(evidence({"detail": {k: v for k, v in t["detail"].items() if k != "kind"}})) > len('{"detail": {}}')


def test_an_r2_run_stuck_names_the_node_whose_run_it_is_and_that_run(env):
    node, run_id = env.stuck_node()
    other = env.h.record()
    env.h.save(other)
    env.live_run(other)                  # a healthy neighbour must not be named
    env.at_offset(0)
    env.at_offset(INTERVAL + 1)
    found = env.anomalies("run_stuck")
    assert [t["node_id"] for t in found] == [node["id"]]
    assert found[0]["detail"]["run_id"] == run_id


def test_an_r2_held_idle_names_a_held_loop_not_its_children(env):
    impl, rev = env.h.record(), env.h.record()
    loop = env.h.record(kind="loop", children=[impl["id"], rev["id"]],
                        loop={"verdict_child": rev["id"], "max_rounds": 3})
    impl.update(parent=loop["id"], state="done", outcome="completed")
    rev.update(parent=loop["id"], state="done", outcome="completed")
    env.h.save(loop, impl, rev)
    env.hold(loop, reason="loop_max")
    env.at_offset(0)
    env.at_offset(THRESHOLD + 15)
    found = env.anomalies("held_idle")
    assert [t["node_id"] for t in found] == [loop["id"]]


def test_an_r2_every_kind_carries_kind_and_a_human_readable_reason(env):
    stuck, run_id = env.stuck_node()
    loop, rev = env.unresolved_loop()
    refused = env.refused_node()
    held = env.h.record()
    env.h.save(held)
    env.hold(held)
    env.at_offset(0)
    env.at_offset(THRESHOLD + 15)
    by_kind = {}
    for t in env.anomalies():
        by_kind.setdefault(t["detail"]["kind"], []).append(t)
    assert set(by_kind) == {"run_stuck", "verdict_unrecorded", "admission_blocked", "held_idle"}
    for kind, found in by_kind.items():
        for t in found:
            assert isinstance(t["detail"]["reason"], str) and t["detail"]["reason"].strip(), kind
    assert [t["node_id"] for t in by_kind["run_stuck"]] == [stuck["id"]]
    assert by_kind["run_stuck"][0]["detail"]["run_id"] == run_id
    assert [t["node_id"] for t in by_kind["admission_blocked"]] == [refused["id"]]
    assert [t["node_id"] for t in by_kind["verdict_unrecorded"]] == [loop["id"]]
    assert sorted(t["node_id"] for t in by_kind["held_idle"]) == sorted([held["id"], loop["id"]])
    for t in by_kind["verdict_unrecorded"]:
        # where a run exists it is the verdict child's own
        assert t["detail"].get("run_id", None) in (None, *[r["run_id"] for r in env.h.nodes()[rev["id"]]["runs"]])
    stuck_reason = by_kind["run_stuck"][0]["detail"]["reason"]
    assert "silence" in stuck_reason


def test_an_r2_the_first_check_runs_at_start_without_waiting_an_interval(env):
    node, _ = env.stuck_node()
    env.at_offset(0)                     # the very first evaluation, no interval has elapsed
    assert [t["node_id"] for t in env.anomalies("run_stuck")] == [node["id"]]


def test_an_r2_the_first_check_at_start_applies_to_conditions_already_old(env):
    node = env.h.record()
    env.h.save(node)
    env.hold(node)
    env.at_offset(5 * THRESHOLD)         # the scheduler first runs long after the hold
    assert [t["node_id"] for t in env.anomalies("held_idle")] == [node["id"]]


def test_an_r2_a_transition_during_a_hold_resets_held_idle(env):
    node = env.h.record()
    env.h.save(node)
    env.hold(node)                                       # transition at t0
    env.at_offset(0)
    env.at_offset(THRESHOLD - 15)
    env.hold(node, reason="loop_max")                    # a new transition at t0+45, still held
    env.at_offset(THRESHOLD + 15)                        # 75 s since the hold, only 30 since the transition
    assert env.anomalies("held_idle") == [], "age was not measured from the node's last transition"
    env.at_offset(THRESHOLD - 15 + THRESHOLD + 15)       # 60+ s since the transition
    assert [t["node_id"] for t in env.anomalies("held_idle")] == [node["id"]]


# -------------------------------------------------------------------- AN-R3
def test_an_r3_the_same_anomaly_is_not_emitted_again_across_checks(env):
    node, _ = env.stuck_node()
    env.at_offset(0)
    for step in range(1, 8):
        env.at_offset(step * (INTERVAL + 1))
    assert [t["node_id"] for t in env.anomalies("run_stuck")] == [node["id"]]


def test_an_r3_it_comes_back_once_the_anomaly_cleared_and_returned(env):
    node, run_id = env.stuck_node()
    env.at_offset(0)
    env.at_offset(INTERVAL + 1)
    assert len(env.anomalies("run_stuck", node["id"])) == 1
    env.clear_stuck(run_id)
    env.at_offset(2 * (INTERVAL + 1))
    assert len(env.anomalies("run_stuck", node["id"])) == 1     # clearing emits nothing
    env.mark_stuck(run_id, "wall clock: exceeded 3600s")
    env.at_offset(3 * (INTERVAL + 1))
    again = env.anomalies("run_stuck", node["id"])
    assert len(again) == 2 and "wall clock" in evidence(again[1])


def test_an_r3_a_changed_node_state_lets_the_anomaly_be_reported_again(env):
    node = env.h.record()
    env.h.save(node)
    env.hold(node)
    env.at_offset(0)
    env.at_offset(THRESHOLD + 15)
    assert len(env.anomalies("held_idle", node["id"])) == 1
    env.at_offset(THRESHOLD + 40)
    assert len(env.anomalies("held_idle", node["id"])) == 1
    record = env.h.nodes()[node["id"]]
    record.update(state="open", hold=None, revision=record["revision"] + 1)   # released
    env.h.save(record)
    env.at_offset(THRESHOLD + 60)
    env.hold(node)                                  # held again, a new episode
    env.at_offset(THRESHOLD + 80)
    assert len(env.anomalies("held_idle", node["id"])) == 1
    env.at_offset(2 * THRESHOLD + 120)
    assert len(env.anomalies("held_idle", node["id"])) == 2


def test_an_r3_deduplication_is_per_node_and_per_kind(env):
    a, _ = env.stuck_node()
    b, _ = env.stuck_node()
    held = env.h.record()
    env.h.save(held)
    env.hold(held)
    env.at_offset(0)
    env.at_offset(THRESHOLD + 15)           # run_stuck x2 and held_idle all due
    env.at_offset(THRESHOLD + 40)
    env.at_offset(THRESHOLD + 70)
    found = [(t["node_id"], t["detail"]["kind"]) for t in env.anomalies()]
    assert sorted(found) == sorted([(a["id"], "run_stuck"), (b["id"], "run_stuck"),
                                    (held["id"], "held_idle")])


def test_an_r3_a_second_kind_on_the_same_node_is_still_reported(env):
    node, _ = env.stuck_node()
    env.at_offset(0)
    env.at_offset(INTERVAL + 1)
    env.hold(env.h.nodes()[node["id"]])
    env.at_offset(THRESHOLD + 20)
    env.at_offset(2 * THRESHOLD + 40)
    kinds = [t["detail"]["kind"] for t in env.anomalies(node=node["id"])]
    assert kinds.count("run_stuck") == 1 and kinds.count("held_idle") == 1


# -------------------------------------------------------------------- AN-R4
def test_an_r4_an_anomaly_is_delivered_by_wait_for_nodes_as_any_transition(env):
    node, _ = env.stuck_node()
    env.at_offset(0)
    env.at_offset(INTERVAL + 1)
    reply = env.request("wait_for_nodes", {"timeout": 0, "cursor": 0})
    assert reply["ok"]
    found = [t for t in reply["result"]["transitions"] if t["kind"] == "anomaly"]
    assert len(found) == 1
    assert {"seq", "at", "node_id", "kind", "detail"} <= set(found[0])
    seq = found[0]["seq"]
    # The cursor moves past it, so it is delivered once...
    after = env.request("wait_for_nodes", {"timeout": 0, "cursor": reply["result"]["next_cursor"]})
    assert [t for t in after["result"]["transitions"] if t["kind"] == "anomaly"] == []
    assert reply["result"]["next_cursor"] >= seq
    # ...and a node filter selects it.
    only = env.transitions(node_ids=[node["id"]])
    assert [t["kind"] for t in only if t["kind"] == "anomaly"] == ["anomaly"]
    other = env.transitions(node_ids=["nd-00000000"])
    assert [t for t in other if t["kind"] == "anomaly"] == []


def test_an_r4_a_wait_in_flight_returns_on_an_anomaly(env):
    node, _ = env.stuck_node()
    env.at_offset(0)
    cursor = env.request("wait_for_nodes", {"timeout": 0, "cursor": 0})["result"]["next_cursor"]
    box = {}

    def wait():
        started = time.monotonic()
        box["reply"] = env.request("wait_for_nodes", {"timeout": WAIT, "cursor": cursor})
        box["took"] = time.monotonic() - started

    thread = threading.Thread(target=wait, daemon=True)
    thread.start()
    time.sleep(0.3)
    assert thread.is_alive(), "the wait returned with nothing to deliver"
    env.at_offset(INTERVAL + 1)
    thread.join(WAIT + 2)
    assert not thread.is_alive()
    kinds = [(t["kind"], t["node_id"]) for t in box["reply"]["result"]["transitions"]]
    assert ("anomaly", node["id"]) in kinds
    assert box["took"] < WAIT - 0.5, "the wait ran to its timeout instead of returning on the anomaly"


def test_an_r4_the_anomaly_survives_a_restart_of_the_service(env, tmp_path):
    node, _ = env.stuck_node()
    env.at_offset(0)
    env.at_offset(INTERVAL + 1)
    assert len(env.anomalies("run_stuck")) == 1
    again = Service(env.h.world.root, now())          # a new process image on the same store
    again.store.initialize()
    with again.store.transaction(write=False) as db:
        token = again.store.meta(db, "root_token")
    reply = again.request({"op": "wait_for_nodes", "token": token,
                           "args": {"timeout": 0, "cursor": 0}, "request_id": "r-" + uuid.uuid4().hex})
    found = [t for t in reply["result"]["transitions"] if t["kind"] == "anomaly"]
    assert [t["node_id"] for t in found] == [node["id"]]


def test_an_r4_a_restarted_engine_does_not_re_report_an_anomaly_already_delivered(env):
    node, _ = env.stuck_node()
    env.at_offset(0)
    env.at_offset(INTERVAL + 1)
    assert len(env.anomalies("run_stuck")) == 1
    env.engine.loop.close()
    env.engine = Engine(env.service, clock_file=env.clock_file)    # scheduler restart
    env.service.engine = env.engine
    env.h.engine = env.engine
    env.at_offset(3 * (INTERVAL + 1))
    env.at_offset(6 * (INTERVAL + 1))
    assert len(env.anomalies("run_stuck", node["id"])) == 1


# -------------------------------------------------------------------- AN-R5
def test_an_r5_a_check_that_emits_anomalies_changes_no_node_run_or_attempt(env):
    stuck, run_id = env.stuck_node()
    loop, rev = env.unresolved_loop()
    refused = env.refused_node()
    held = env.h.record()
    env.h.save(held)
    env.hold(held)
    env.at_offset(0)                                   # settle: first stamps and bookkeeping
    before = env.snapshot()
    env.at_offset(THRESHOLD + 15)
    kinds = {t["detail"]["kind"] for t in env.anomalies()}
    assert kinds == {"run_stuck", "verdict_unrecorded", "admission_blocked", "held_idle"}, kinds
    assert env.snapshot() == before
    # the round is neither settled nor reopened, and the stuck run is not stopped
    record = env.h.nodes()[loop["id"]]
    assert record["state"] == "held" and record["hold"]["reason"] == "unresolved_round"
    assert all(g["verdict"] is None for g in record["generations"])
    assert "pending_verdict" not in record
    assert env.engine.runner.tree.get(run_id).status == "stuck"
    assert env.h.nodes()[stuck["id"]]["state"] == "running"
    assert env.h.nodes()[refused["id"]]["state"] == "open"
    assert env.h.nodes()[held["id"]]["state"] == "held"


def test_an_r5_repeated_checks_keep_state_identical(env):
    env.stuck_node()
    env.unresolved_loop()
    env.at_offset(0)
    env.at_offset(INTERVAL + 1)
    after_first = env.snapshot()
    for step in range(2, 6):
        env.at_offset(step * (INTERVAL + 1))
    assert env.snapshot() == after_first


# --------------------------------------------------- the host scheduler itself
@pytest.fixture
def real(tmp_path, monkeypatch):
    forget_stale_state(tmp_path / "proj")
    world = ClockWorld(tmp_path, monkeypatch, now=datetime.now(timezone.utc),
                       scheduler={"anomaly_interval_seconds": 5, "anomaly_admission_seconds": 30,
                                  "anomaly_held_seconds": 30, "starvation_after_seconds": HUGE})
    world.pc = world.provider("pcfx", max_concurrent=1)
    world.agent("pcworker", "pcfx")
    yield world
    world.close()                                      # stops the scheduler and its workers
    forget_stale_state(tmp_path / "proj")


def test_an_r2_r4_the_host_scheduler_reports_a_refused_node_to_a_waiting_orchestrator(real):
    real.start_scheduler()
    holder = real.simple("H", "pcworker", fx={"gate": "gH"})
    real.wait_running(holder)
    blocked = real.simple("B", "pcworker")
    real.until(lambda: "admission:provider_concurrency" in blocked_codes(real.get(blocked)),
               what="B refused for provider concurrency")
    real.advance(seconds=10)                           # inside the threshold: nothing yet
    quiet = real.rpc("wait_for_nodes", {"timeout": 0, "cursor": 0})["result"]["transitions"]
    assert [t for t in quiet if t["kind"] == "anomaly"] == []
    cursor = real.rpc("wait_for_nodes", {"timeout": 0, "cursor": 0})["result"]["next_cursor"]
    box = {}

    def wait():
        box["reply"] = real.rpc("wait_for_nodes", {"timeout": WAIT, "cursor": cursor})

    thread = threading.Thread(target=wait, daemon=True)
    thread.start()
    real.advance(seconds=60)                           # past anomaly_admission_seconds
    thread.join(WAIT + 6)
    assert not thread.is_alive()
    found = [t for t in box["reply"]["result"]["transitions"] if t["kind"] == "anomaly"]
    assert [(t["node_id"], t["detail"]["kind"]) for t in found] == [(blocked, "admission_blocked")]
    assert "provider_concurrency" in json.dumps(found[0]["detail"])


# ------------------------------------------------------------- configuration
ANOMALY_KEYS = ["anomaly_interval_seconds", "anomaly_admission_seconds", "anomaly_held_seconds"]
REFUSED = ["0", "-1", "-0.5", "soon", "''", "'60'", "true", "[1]", "~"]


def load_with(tmp_path, monkeypatch, scheduler_yaml, global_yaml=None):
    root = h3.make_git_repo(tmp_path / "cfgproj")
    paths = ProjectPaths(root)
    paths.ensure()
    paths.config.mkdir(parents=True, exist_ok=True)
    gdir = tmp_path / "gconf"
    gdir.mkdir(exist_ok=True)
    monkeypatch.setenv("MULTIAGENTS_CONFIG_DIR", str(gdir))
    if scheduler_yaml is not None:
        (paths.config / "project.yaml").write_text("scheduler:\n  enabled: false\n" + scheduler_yaml)
    if global_yaml is not None:
        (gdir / "project.yaml").write_text(global_yaml)
    return config_mod.load(paths, seed=False).project["scheduler"]


@pytest.mark.parametrize("key", ANOMALY_KEYS)
@pytest.mark.parametrize("value", REFUSED)
def test_an_config_a_non_positive_or_non_numeric_value_is_refused_at_load(tmp_path, monkeypatch, key, value):
    with pytest.raises(SchedulerConfigError) as raised:
        load_with(tmp_path, monkeypatch, f"  {key}: {value}\n")
    message = str(raised.value)
    assert key in message
    assert re.search(r"project\.yaml:3\b", message), message     # the line of the offending key


@pytest.mark.parametrize("key", ANOMALY_KEYS)
def test_an_config_a_refused_value_in_the_global_layer_names_the_global_file(tmp_path, monkeypatch, key):
    with pytest.raises(SchedulerConfigError) as raised:
        load_with(tmp_path, monkeypatch, None, global_yaml=f"scheduler:\n  {key}: 0\n")
    assert re.search(r"gconf/project\.yaml:2\b", str(raised.value)), str(raised.value)


@pytest.mark.parametrize("key", ANOMALY_KEYS)
@pytest.mark.parametrize("value,expected", [("1", 1), ("0.5", 0.5), ("3600", 3600)])
def test_an_config_the_smallest_positive_values_are_accepted_and_kept(tmp_path, monkeypatch, key, value, expected):
    got = load_with(tmp_path, monkeypatch, f"  {key}: {value}\n")
    assert got[key] == expected
