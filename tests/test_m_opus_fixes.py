"""Regression tests for the round-10 review of batch M (reviewer ag-f27608),
fixed at the root in the opus take-over (context/specs/m-routing-fixes.md,
RM-R1c and RM-R1d):

1. P1: a node carrying a launch-cleanup hold occupies a slot in ANY status —
   `stop()` marking it `cancelled` does not free it — in `start()`'s
   admission, a resumed consult's admission and `capacity()` alike.
2. P1: a tree that cannot be written (ENOSPC) never stops the cleanup from
   stopping the process; the hold is still honoured in the owning Runner and
   reaches `tree.json` on a later pass.
3. P1: an unconfirmed cleanup has a recovery path. The owner re-checks death
   on its next admission / capacity / adoption pass and ends the hold with
   every release it owns; a hold whose owner crashed is lifted by any Runner
   once the held process is positively dead, and only then.
4. P2: a relaunch whose prologue fails releases the supervision flock the
   previous turn kept for it.

Plus: a held node cannot be relaunched (steer, resumed consult) while its
previous process is not confirmed dead.

Reuses the black-box seams of tests/test_m_routing_fixes.py.
"""
from __future__ import annotations

import asyncio
import errno
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent / "support"))
from multiagents import procs, runner as runner_mod  # noqa: E402
from multiagents.config import AgentSpec  # noqa: E402
from multiagents.executor import docker as docker_mod  # noqa: E402
from multiagents.executor.base import running as _running  # noqa: E402
from multiagents.executor.base import session_alive  # noqa: E402
from multiagents.executor.docker import DockerExecutor  # noqa: E402
from multiagents.tree import Node  # noqa: E402
from test_m_routing_fixes import (  # noqa: E402
    _budgets, _events, _fakes, _project,
)


_PROVIDERS: dict[str, dict] = {}


def _make(tmp_path, monkeypatch, *, cap=1, conversational=False):
    """A Runner on the project at `tmp_path`; a second call is a second
    Runner (another server) on the same project, sharing its fake CLI."""
    key = str(tmp_path)
    if key not in _PROVIDERS:
        _PROVIDERS[key] = _fakes(tmp_path, "acme")[0]
    providers = _PROVIDERS[key]
    agents = [AgentSpec("worker", "acme", "acme-large")]
    if conversational:
        agents.append(AgentSpec("advisor", "acme", "acme-large",
                                conversational=True))
    import c3_harness as h
    runner = h.make_runner(tmp_path / "project", monkeypatch,
                           agents={a.name: a for a in agents},
                           providers=providers,
                           project={"limits": {"max_concurrent": cap}})
    _budgets(monkeypatch, acme=1.0)
    return runner


def _unconfirmable_launch(runner, monkeypatch):
    """The first start faults after its process is up (the `running` write),
    and the container probe cannot answer — so the cleanup stops the process
    but cannot CONFIRM its death, and holds. `probe["alive"]` is what the
    raw probe answers from then on; `handles` collects the launched ones."""
    monkeypatch.setattr(runner_mod, "LAUNCH_CONFIRM_SECONDS", 0.4)
    probe = {"alive": None}
    handles = []
    executor_cls = type(runner.executor())
    real_start = executor_cls.start

    async def grabbing_start(self, *args, **kwargs):
        handle = await real_start(self, *args, **kwargs)
        handles.append(handle)
        return handle

    monkeypatch.setattr(executor_cls, "start", grabbing_start)
    monkeypatch.setattr(executor_cls, "wrapper_alive",
                        lambda self, agent_id: probe["alive"], raising=False)
    real_set_status = runner.tree.set_status
    state = {"raised": False}

    def set_status(agent_id, status, reason=""):
        if not state["raised"] and status == "running":
            state["raised"] = True
            raise RuntimeError("injected status fault")
        return real_set_status(agent_id, status, reason)

    monkeypatch.setattr(runner.tree, "set_status", set_status)
    return probe, handles


def _held_start(runner, monkeypatch):
    probe, handles = _unconfirmable_launch(runner, monkeypatch)
    result = asyncio.run(runner.start("worker", "q"))
    assert "injected status fault" in (result.get("error") or ""), result
    node_id = result["agent_id"]
    return node_id, probe, handles


def _hold_of(runner, node_id):
    return (runner.tree.read()["nodes"].get(node_id) or {}).get("cleanup_hold")


def _startup_runs(runner):
    with runner.startup._lock():
        return (runner.startup._read().get("acme") or {}).get("runs") or {}


def _dead_pid() -> tuple[int, str]:
    """A pid identity that existed and is now positively gone."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    start = procs.start_time(proc.pid) or "1"
    proc.wait()
    return proc.pid, start


# ---------------------------------------------------------------------------
# Finding 1 (P1): a hold occupies in any status
# ---------------------------------------------------------------------------

def test_a_cancelled_node_still_holds_its_slot_while_held(tmp_path, monkeypatch):
    runner = _make(tmp_path, monkeypatch)
    node_id, probe, _ = _held_start(runner, monkeypatch)
    assert _hold_of(runner, node_id), "no durable hold after an unconfirmed cleanup"

    asyncio.run(runner.stop(node_id))
    assert runner.tree.get(node_id).status == "cancelled"

    assert runner.capacity()["running"] == 1, runner.capacity()
    with pytest.raises(RuntimeError, match="max_concurrent"):
        asyncio.run(runner.start("worker", "q"))
    assert runner.tree.get(node_id).status == "cancelled", (
        "the hold's end must not overwrite the stop's verdict")


def test_a_terminal_held_node_counts_for_a_second_runner(tmp_path, monkeypatch):
    """The hold is tree state: a Runner that never owned it still counts a
    `cancelled` node carrying one, while its owner (this live process, a
    different Runner) has not confirmed death."""
    runner = _make(tmp_path, monkeypatch)
    runner.tree.add(Node(id="ag-held", agent="worker", provider="acme",
                         model="acme-large", parent=None, depth=1,
                         status="cancelled", pid=None))
    runner.tree.update("ag-held", cleanup_hold={
        "since": 1.0, "owner_pid": os.getpid(),
        "owner_start": procs.start_time(os.getpid()), "owner": "another-runner",
        "pid": None, "pid_start": ""})
    assert runner.capacity()["running"] == 1, runner.capacity()
    with pytest.raises(RuntimeError, match="max_concurrent"):
        asyncio.run(runner.start("worker", "q"))


def test_a_resumed_consult_counts_a_terminal_held_node(tmp_path, monkeypatch):
    runner = _make(tmp_path, monkeypatch, conversational=True)
    runner.tree.add(Node(id="ag-held", agent="worker", provider="acme",
                         model="acme-large", parent=None, depth=1,
                         status="failed"))
    runner.tree.update("ag-held", cleanup_hold={
        "since": 1.0, "owner_pid": os.getpid(),
        "owner_start": procs.start_time(os.getpid()), "owner": "another-runner"})
    conv = Node(id="ag-conv", agent="advisor", provider="acme",
                model="acme-large", parent=None, depth=1, status="idle",
                conversation=True, session_id="s", turns=1)
    runner.tree.add(conv)
    with pytest.raises(RuntimeError, match="max_concurrent"):
        runner._admission_reserved(runner.config.agent("advisor"), conv)
    assert runner.tree.get("ag-conv").status == "idle"


# ---------------------------------------------------------------------------
# Finding 2 (P1, ag-f27608) and 3 (P1, ag-43f57f): the reservation is durable
# before the process exists, and a tree write failing after the start never
# skips the stop
# ---------------------------------------------------------------------------

def _enospc_on_hold(runner, monkeypatch, state):
    real_update = runner.tree.update

    def update(agent_id, **fields):
        if "cleanup_hold" in fields and state["full"]:
            raise OSError(errno.ENOSPC, "No space left on device")
        return real_update(agent_id, **fields)

    monkeypatch.setattr(runner.tree, "update", update)


def test_the_reservation_is_durable_before_the_process_starts(tmp_path, monkeypatch):
    runner = _make(tmp_path, monkeypatch)
    seen = []
    executor_cls = type(runner.executor())
    real_start = executor_cls.start

    async def looking_start(self, argv, cwd, env, **kwargs):
        seen.append(runner.tree.get(env["MULTIAGENTS_AGENT_ID"]).cleanup_hold)
        return await real_start(self, argv, cwd, env, **kwargs)

    monkeypatch.setattr(executor_cls, "start", looking_start)
    result = asyncio.run(runner.start("worker", "q"))
    assert result.get("agent_id") and not result.get("error"), result
    assert seen and seen[0] and seen[0]["executor"]["kind"] == "local", seen
    assert runner.tree.get(result["agent_id"]).cleanup_hold is None, (
        "the reservation outlived an established supervision")


def test_a_reservation_that_cannot_be_written_launches_nothing(tmp_path, monkeypatch):
    runner = _make(tmp_path, monkeypatch)
    started = []
    executor_cls = type(runner.executor())

    async def never(self, *args, **kwargs):
        started.append(1)
        raise AssertionError("launched without a durable reservation")

    monkeypatch.setattr(executor_cls, "start", never)
    _enospc_on_hold(runner, monkeypatch, {"full": True})

    with pytest.raises(OSError):
        asyncio.run(runner.start("worker", "q"))
    assert started == []
    assert not runner._locks and not _startup_runs(runner)
    assert all(n.get("status") == "failed"
               for n in runner.tree.read()["nodes"].values())


def _fail_the_pid_write(runner, monkeypatch):
    """The transaction recording the new pid (and its hold) hits ENOSPC."""
    real = runner._record_launched

    def failing(node_id, hold, handle):
        real_transaction = runner.tree.transaction

        def full():
            raise OSError(errno.ENOSPC, "No space left on device")

        runner.tree.transaction = full
        try:
            real(node_id, hold, handle)
        finally:
            runner.tree.transaction = real_transaction

    monkeypatch.setattr(runner, "_record_launched", failing)


def test_a_failed_pid_write_still_stops_the_process(tmp_path, monkeypatch):
    runner = _make(tmp_path, monkeypatch)
    probe, handles = _unconfirmable_launch(runner, monkeypatch)
    probe["alive"] = False                  # death CAN be confirmed here
    _fail_the_pid_write(runner, monkeypatch)

    with pytest.raises(OSError):
        asyncio.run(runner.start("worker", "q"))
    handle = handles[0]
    assert not _running(handle.pid, getattr(handle, "pid_start", "")), (
        "the cleanup never stopped the process")
    assert not runner._locks and not _startup_runs(runner)
    nodes = runner.tree.read()["nodes"]
    assert all((n.get("status"), n.get("cleanup_hold")) == ("failed", None)
               for n in nodes.values()), [
        (n.get("status"), n.get("reason"), n.get("cleanup_hold"))
        for n in nodes.values()]


def test_a_relaunch_whose_pid_write_fails_is_held_for_every_runner(
        tmp_path, monkeypatch):
    """The reviewer's case: a relaunch of a node whose durable record names
    no live process (a steered idle conversation here; a retry's `running`
    node names its dead predecessor). The pid write fails and death cannot
    be confirmed: a SECOND Runner still counts the node, through the
    reservation written before the launch."""
    runner = _make(tmp_path, monkeypatch, conversational=True)
    node_id = _idle_conversation(runner)
    probe, _ = _unconfirmable_launch(runner, monkeypatch)
    _fail_the_pid_write(runner, monkeypatch)

    with pytest.raises(OSError):
        asyncio.run(runner.steer(node_id, "continue"))
    assert _hold_of(runner, node_id), "no durable reservation"

    other = _make(tmp_path, monkeypatch, conversational=True)
    assert other.capacity()["running"] == 1, other.capacity()
    with pytest.raises(RuntimeError, match="max_concurrent"):
        asyncio.run(other.start("worker", "q"))


# ---------------------------------------------------------------------------
# Finding 3 (P1): an unconfirmed hold is recovered
# ---------------------------------------------------------------------------

def test_the_owner_ends_its_hold_once_death_is_confirmed(tmp_path, monkeypatch):
    runner = _make(tmp_path, monkeypatch)
    node_id, probe, handles = _held_start(runner, monkeypatch)
    assert node_id in runner._locks and _startup_runs(runner)
    assert runner.capacity()["running"] == 1

    probe["alive"] = True                   # still alive: nothing moves
    assert runner.capacity()["running"] == 1
    assert _hold_of(runner, node_id) and node_id in runner._locks

    probe["alive"] = False                  # now positively dead
    assert runner.capacity()["running"] == 0, runner.capacity()
    assert not _hold_of(runner, node_id)
    assert node_id not in runner._locks, "the flock outlived the hold"
    assert not _startup_runs(runner), "the startup claim outlived the hold"
    node = runner.tree.get(node_id)
    assert node.status == "failed" and "injected status fault" in node.reason, (
        f"the deferred launch failure was not applied: {node.status} {node.reason}")


def test_adoption_passes_settle_holds(tmp_path, monkeypatch):
    runner = _make(tmp_path, monkeypatch)
    node_id, probe, _ = _held_start(runner, monkeypatch)
    probe["alive"] = False
    asyncio.run(runner.adopt())
    assert not _hold_of(runner, node_id)


LOCAL = {"kind": "local", "container": ""}


def _orphan_hold(runner, *, pid, pid_start, status="pending", then=None,
                 identity=LOCAL):
    owner, owner_start = _dead_pid()
    runner.tree.add(Node(id="ag-orphan", agent="worker", provider="acme",
                         model="acme-large", parent=None, depth=1,
                         status=status, pid=pid, pid_start=pid_start))
    runner.tree.update("ag-orphan", cleanup_hold={
        "since": 1.0, "owner_pid": owner, "owner_start": owner_start,
        "owner": "crashed-runner", "pid": pid, "pid_start": pid_start,
        "executor": identity, "then": then})


def test_a_crashed_owners_hold_is_lifted_once_its_process_is_dead(
        tmp_path, monkeypatch):
    runner = _make(tmp_path, monkeypatch)
    pid, start = _dead_pid()
    _orphan_hold(runner, pid=pid, pid_start=start)

    result = asyncio.run(runner.start("worker", "q"))
    assert result.get("agent_id") and not result.get("error"), result
    orphan = runner.tree.get("ag-orphan")
    assert orphan.cleanup_hold is None
    assert orphan.status == "failed", orphan.status


def test_a_crashed_owners_deferred_status_is_applied(tmp_path, monkeypatch):
    runner = _make(tmp_path, monkeypatch)
    pid, start = _dead_pid()
    _orphan_hold(runner, pid=pid, pid_start=start, then=["idle", ""])
    runner.capacity()
    assert runner.tree.get("ag-orphan").status == "idle"


def test_a_crashed_owners_hold_stays_while_its_process_lives(tmp_path, monkeypatch):
    runner = _make(tmp_path, monkeypatch)
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        _orphan_hold(runner, pid=proc.pid,
                     pid_start=procs.start_time(proc.pid) or "")
        with pytest.raises(RuntimeError, match="max_concurrent"):
            asyncio.run(runner.start("worker", "q"))
        assert runner.tree.get("ag-orphan").cleanup_hold
    finally:
        proc.kill()
        proc.wait()
    runner.capacity()
    assert runner.tree.get("ag-orphan").cleanup_hold is None


def test_an_unanswerable_container_keeps_a_crashed_owners_hold(
        tmp_path, monkeypatch):
    runner = _make(tmp_path, monkeypatch)
    monkeypatch.setattr(DockerExecutor, "wrapper_alive",
                        lambda self, agent_id: None)
    pid, start = _dead_pid()
    _orphan_hold(runner, pid=pid, pid_start=start,
                 identity={"kind": "docker", "container": "held-box"})
    assert runner.capacity()["running"] == 1
    assert runner.tree.get("ag-orphan").cleanup_hold


def test_a_hold_without_an_execution_identity_is_never_lifted(
        tmp_path, monkeypatch):
    runner = _make(tmp_path, monkeypatch)
    pid, start = _dead_pid()
    _orphan_hold(runner, pid=pid, pid_start=start, identity=None)
    assert runner.capacity()["running"] == 1
    assert runner.tree.get("ag-orphan").cleanup_hold


# ---------------------------------------------------------------------------
# Finding 4 (P1, ag-43f57f): recovery probes the recorded execution identity
# ---------------------------------------------------------------------------

def test_recovery_asks_the_container_the_hold_recorded(tmp_path, monkeypatch):
    """The project now runs agents locally, but the held run was launched in
    container `held-box`: its dead `docker exec` client is no proof, and
    only that container is asked."""
    runner = _make(tmp_path, monkeypatch)
    assert runner.executor().kind == "local"
    asked = []
    answer = {"alive": True}

    def wrapper_alive(self, agent_id):
        asked.append(self.container)
        return answer["alive"]

    monkeypatch.setattr(DockerExecutor, "wrapper_alive", wrapper_alive)
    pid, start = _dead_pid()
    _orphan_hold(runner, pid=pid, pid_start=start,
                 identity={"kind": "docker", "container": "held-box"})
    assert runner.capacity()["running"] == 1
    assert runner.tree.get("ag-orphan").cleanup_hold
    assert asked and set(asked) == {"held-box"}, asked

    answer["alive"] = False
    assert runner.capacity()["running"] == 0
    assert runner.tree.get("ag-orphan").cleanup_hold is None


# ---------------------------------------------------------------------------
# Finding 5 (P1, ag-43f57f): a crashed owner's hold is taken over, never
# adopted, and ends with the owner's release sequence
# ---------------------------------------------------------------------------

def _crash_owner(runner, node_id):
    """What the kernel and time do to a crashed owner: its flock is gone and
    its pid identity is dead. The hold still names it."""
    runner._release(node_id)
    runner._holds.pop(node_id, None)
    owner, owner_start = _dead_pid()
    with runner.tree.transaction() as data:
        data["nodes"][node_id]["cleanup_hold"].update(
            owner_pid=owner, owner_start=owner_start, owner="crashed-runner")


def test_a_taken_over_hold_ends_with_the_owners_releases(tmp_path, monkeypatch):
    runner = _make(tmp_path, monkeypatch)
    node_id, probe, _ = _held_start(runner, monkeypatch)
    assert _startup_runs(runner)
    _crash_owner(runner, node_id)
    # Launched in a container that cannot answer yet: the heir must ask it.
    with runner.tree.transaction() as data:
        data["nodes"][node_id]["cleanup_hold"]["executor"] = {
            "kind": "docker", "container": "held-box"}
    monkeypatch.setattr(DockerExecutor, "wrapper_alive",
                        lambda self, agent_id: probe["alive"])

    heir = _make(tmp_path, monkeypatch)
    heir.capacity()                          # takes the hold over; unknown still
    assert node_id in heir._locks, "the heir does not own the held node"
    assert _hold_of(heir, node_id)["owner"] == heir._hold_owner
    assert _startup_runs(heir), "the claim was released before confirmation"

    probe["alive"] = False                   # now positively dead
    assert heir.capacity()["running"] == 0
    assert not _hold_of(heir, node_id)
    assert node_id not in heir._locks, "the flock outlived the hold"
    assert not _startup_runs(heir), "the claim outlived the hold"
    assert heir.tree.get(node_id).status == "failed"


def test_adoption_never_follows_a_held_node(tmp_path, monkeypatch):
    runner = _make(tmp_path, monkeypatch)
    monkeypatch.setattr(DockerExecutor, "wrapper_alive",
                        lambda self, agent_id: None)
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        _orphan_hold(runner, pid=proc.pid, status="running",
                     pid_start=procs.start_time(proc.pid) or "",
                     identity={"kind": "docker", "container": "held-box"})
        # Followable in every other respect: live, with output to read.
        run_dir = runner.paths.run_dir("ag-orphan")
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "output.ndjson").write_text("")
        asyncio.run(runner.adopt())
        assert "ag-orphan" not in runner.runs, "a held node was adopted"
        assert runner.tree.get("ag-orphan").cleanup_hold
        assert runner.tree.get("ag-orphan").status == "running"
    finally:
        proc.kill()
        proc.wait()


# ---------------------------------------------------------------------------
# Findings 1 and 2 (P1, ag-43f57f): death is positive, and covers the session
# ---------------------------------------------------------------------------

def _orphaned_session_child():
    """A session leader (a wrapper) that has exited, and its setpgrp'd
    child (the agent) still running in its session: (leader, child)."""
    code = ("import os, subprocess, sys; p = subprocess.Popen([sys.executable, "
            "'-c', 'import time; time.sleep(60)'], preexec_fn=os.setpgrp); "
            "print(p.pid, flush=True)")
    leader = subprocess.Popen([sys.executable, "-c", code], text=True,
                              stdout=subprocess.PIPE, start_new_session=True)
    child = int(leader.stdout.readline())
    leader.wait()
    leader.stdout.close()
    return leader.pid, child


def _kill_and_wait(pid, leader):
    os.kill(pid, 9)
    for _ in range(200):
        if session_alive(leader) is False:
            return
        time.sleep(0.02)
    raise AssertionError("the session never emptied")


def _alive_code(path):
    return subprocess.run(["sh", "-c", docker_mod._ALIVE_SCRIPT, str(path)]
                          ).returncode


def test_a_missing_or_malformed_wrapper_pid_is_unknown(tmp_path):
    path = tmp_path / "wrapper.pid"
    assert _alive_code(path) == 2
    path.write_text("garbage\n")
    assert _alive_code(path) == 2


def test_a_dead_wrapper_with_a_live_agent_is_alive_to_the_container_probe(tmp_path):
    leader, child = _orphaned_session_child()
    path = tmp_path / "wrapper.pid"
    path.write_text(f"{leader}\n")
    try:
        assert _alive_code(path) == 0, "a live agent in the session read as dead"
    finally:
        _kill_and_wait(child, leader)
    assert _alive_code(path) == 1


def test_wrapper_alive_maps_unknown_to_none(tmp_path, monkeypatch):
    runner = _make(tmp_path, monkeypatch)
    executor = DockerExecutor({}, runner.paths)
    monkeypatch.setattr(DockerExecutor, "inside", lambda self: True)
    assert executor.wrapper_alive("ag-none") is None


def test_a_dead_wrapper_with_a_live_agent_is_not_death(tmp_path):
    leader, child = _orphaned_session_child()
    try:
        assert not runner_mod._positively_ended(leader, "", None), (
            "the agent outlived its wrapper and the run was called dead")
    finally:
        _kill_and_wait(child, leader)
    assert runner_mod._positively_ended(leader, "", None)


def test_no_pid_and_no_probe_is_unknown():
    assert not runner_mod._positively_ended(None, "", None)


# ---------------------------------------------------------------------------
# Finding 6 (P2, ag-43f57f): a cancelled first consult gives its slot back
# ---------------------------------------------------------------------------

def test_a_first_consult_cancelled_after_start_gives_the_slot_back(
        tmp_path, monkeypatch):
    runner = _make(tmp_path, monkeypatch, conversational=True)
    real_track = runner._track_container_run
    state = {"cancelled": False}

    async def track_once(run, executor):
        if not state["cancelled"]:
            state["cancelled"] = True
            raise asyncio.CancelledError
        return await real_track(run, executor)

    monkeypatch.setattr(runner, "_track_container_run", track_once)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(runner.consult("advisor", "hello", timeout=60))
    nodes = runner.tree.read()["nodes"]
    assert nodes and all(n.get("status") == "failed" and not n.get("cleanup_hold")
                         for n in nodes.values()), nodes
    assert runner.capacity()["running"] == 0


# ---------------------------------------------------------------------------
# Finding 4 (P2, ag-f27608): a relaunch's prologue failure releases the kept
# flock
# ---------------------------------------------------------------------------

def _idle_conversation(runner):
    worktree = runner.paths.worktree("ag-steered")
    worktree.mkdir(parents=True)
    runner.tree.add(Node(id="ag-steered", agent="advisor", provider="acme",
                         model="acme-large", parent=None, depth=1,
                         status="idle", session_id="sess-steered",
                         worktree=str(worktree), conversation=True, turns=1))
    return "ag-steered"


@pytest.mark.parametrize("fault", ["prepare_home", "supervisor"])
def test_a_steer_prologue_failure_releases_the_kept_flock(
        tmp_path, monkeypatch, fault):
    runner = _make(tmp_path, monkeypatch, conversational=True)
    node_id = _idle_conversation(runner)
    assert runner._claim(node_id)           # what the previous turn kept

    def boom(*a, **kw):
        raise OSError(f"{fault} exploded")

    if fault == "prepare_home":
        monkeypatch.setattr(runner_mod, "prepare_home", boom)
    else:
        monkeypatch.setattr(runner, "_supervisor", boom)

    with pytest.raises(OSError, match="exploded"):
        asyncio.run(runner.steer(node_id, "continue"))
    assert node_id not in runner._locks, "the previous turn's flock leaked"
    assert not _startup_runs(runner)


# ---------------------------------------------------------------------------
# A held node is not relaunched
# ---------------------------------------------------------------------------

def test_steer_refuses_a_held_node_before_taking_or_stopping_anything(
        tmp_path, monkeypatch):
    runner = _make(tmp_path, monkeypatch, conversational=True)
    node_id = _idle_conversation(runner)
    runner.tree.update(node_id, cleanup_hold={
        "since": 1.0, "owner_pid": os.getpid(),
        "owner_start": procs.start_time(os.getpid()), "owner": "another-runner"})
    stops = []

    async def stop(agent_id, *, internal=False):
        stops.append(agent_id)
        return {}

    monkeypatch.setattr(runner, "stop", stop)
    result = asyncio.run(runner.steer(node_id, "continue"))
    assert result.get("steered") is False and "confirmed dead" in result["error"]
    assert stops == [] and not _startup_runs(runner)


def test_a_resumed_consult_refuses_a_held_conversation(tmp_path, monkeypatch):
    runner = _make(tmp_path, monkeypatch, cap=4, conversational=True)
    node_id = _idle_conversation(runner)
    runner.tree.update(node_id, cleanup_hold={
        "since": 1.0, "owner_pid": os.getpid(),
        "owner_start": procs.start_time(os.getpid()), "owner": "another-runner"})
    node = runner.tree.get(node_id)
    with pytest.raises(RuntimeError, match="confirmed dead"):
        runner._admission_reserved(runner.config.agent("advisor"), node)
    assert runner.tree.get(node_id).status == "idle"


def test_the_hold_is_reported(tmp_path, monkeypatch):
    runner = _make(tmp_path, monkeypatch)
    _held_start(runner, monkeypatch)
    assert [e for e in _events(runner) if e.get("kind") == "launch_cleanup_failed"]


# ---------------------------------------------------------------------------
# Review ag-598c45, finding 1 (P1): an unreadable session member is unknown
# ---------------------------------------------------------------------------

def _unreadable(monkeypatch, pid, exc):
    real = Path.read_text
    target = f"/proc/{pid}/stat"

    def read_text(self, *a, **kw):
        if str(self) == target:
            raise exc
        return real(self, *a, **kw)

    monkeypatch.setattr(Path, "read_text", read_text)


def test_an_unreadable_session_member_is_not_death(monkeypatch):
    leader, child = _orphaned_session_child()
    try:
        _unreadable(monkeypatch, child, PermissionError(errno.EACCES, "denied"))
        assert session_alive(leader) is None
        assert not runner_mod._positively_ended(leader, "", None), (
            "a live agent whose /proc entry could not be read was called dead")
    finally:
        monkeypatch.undo()
        _kill_and_wait(child, leader)


def test_a_vanished_entry_is_gone(monkeypatch):
    leader, child = _orphaned_session_child()
    _kill_and_wait(child, leader)
    # Some other process vanishing mid-scan says nothing about this session.
    _unreadable(monkeypatch, os.getpid(), ProcessLookupError(errno.ESRCH, "gone"))
    assert session_alive(leader) is False


def test_the_container_probe_calls_an_unreadable_member_unknown(tmp_path):
    leader, child = _orphaned_session_child()
    path = tmp_path / "wrapper.pid"
    path.write_text(f"{leader}\n")
    shim = tmp_path / "bin"
    shim.mkdir()
    (shim / "cat").write_text(
        "#!/bin/sh\n"
        f'[ "$1" = /proc/{child}/stat ] && {{ echo denied >&2; exit 1; }}\n'
        'exec /bin/cat "$@"\n')
    (shim / "cat").chmod(0o755)
    env = dict(os.environ, PATH=f"{shim}:{os.environ.get('PATH', '')}")
    try:
        code = subprocess.run(["sh", "-c", docker_mod._ALIVE_SCRIPT, str(path)],
                              env=env).returncode
        assert code == 2, f"an unreadable live member answered {code}"
    finally:
        _kill_and_wait(child, leader)


# ---------------------------------------------------------------------------
# Review ag-598c45, finding 2 (P2): the hold outlives a failed claim release
# ---------------------------------------------------------------------------

def test_a_claim_release_that_fails_keeps_the_hold_and_is_retried(
        tmp_path, monkeypatch):
    runner = _make(tmp_path, monkeypatch)
    node_id, probe, _ = _held_start(runner, monkeypatch)
    assert _startup_runs(runner)
    state = {"full": True}
    real_write = runner.startup._write

    def write(records):
        if state["full"]:
            raise OSError(errno.ENOSPC, "No space left on device")
        return real_write(records)

    monkeypatch.setattr(runner.startup, "_write", write)
    probe["alive"] = False                   # death is confirmed now
    assert runner.capacity()["running"] == 1, "the hold went with the claim stuck"
    assert _hold_of(runner, node_id) and _startup_runs(runner)

    state["full"] = False                    # storage recovers
    assert runner.capacity()["running"] == 0
    assert not _hold_of(runner, node_id)
    assert not _startup_runs(runner), "the claim was never released"
    assert node_id not in runner._locks


# ---------------------------------------------------------------------------
# Review ag-598c45, finding 3 (P2): a takeover rebinds the occupancy record
# ---------------------------------------------------------------------------

def test_a_takeover_rebinds_the_occupancy_record(tmp_path, monkeypatch):
    runner = _make(tmp_path, monkeypatch)
    executor_cls = type(runner.executor())
    monkeypatch.setattr(executor_cls, "oom_kill_count", lambda self: 7,
                        raising=False)
    monkeypatch.setattr(executor_cls, "container", "held-box", raising=False)
    node_id, probe, _ = _held_start(runner, monkeypatch)
    assert _hold_of(runner, node_id)["occupancy"] == "held-box"
    _crash_owner(runner, node_id)
    with runner.tree.transaction() as data:
        data["nodes"][node_id]["cleanup_hold"]["executor"] = {
            "kind": "docker", "container": "held-box"}
    dead, dead_start = _dead_pid()
    with runner.occupancy.locked() as records:          # the crashed owner's
        records["held-box"][node_id].update(owner_pid=dead, owner_start=dead_start)
        runner.occupancy.commit(records)
    monkeypatch.setattr(DockerExecutor, "wrapper_alive",
                        lambda self, agent_id: probe["alive"])

    heir = _make(tmp_path, monkeypatch)
    heir.capacity()                          # taken over; death still unknown
    assert node_id in heir._locks
    entry = heir.occupancy.read()["held-box"][node_id]
    assert entry.get("ended") is None, "the held run's occupancy was pruned"
    assert entry["owner_pid"] == os.getpid(), entry

    probe["alive"] = False
    assert heir.capacity()["running"] == 0
    assert heir.occupancy.read().get("held-box", {}).get(node_id, {}).get(
        "ended") is not None or node_id not in heir.occupancy.read().get(
        "held-box", {})


# ---------------------------------------------------------------------------
# Review ag-5189c2 / RM-R1e, finding 1: two empty scans, a short gap apart
# ---------------------------------------------------------------------------

def test_an_empty_session_is_confirmed_by_a_second_scan(monkeypatch):
    pid, start = _dead_pid()
    answers = iter([False, True])
    calls = []

    def scan(sid):
        calls.append(sid)
        return next(answers)

    monkeypatch.setattr(runner_mod, "session_alive", scan)
    assert not runner_mod._positively_ended(pid, start, None), (
        "a member seen by the second scan was ignored")
    assert calls == [pid, pid]

    monkeypatch.setattr(runner_mod, "session_alive", lambda sid: False)
    assert runner_mod._positively_ended(pid, start, None)


def test_the_container_probe_rescans_before_calling_a_session_empty(tmp_path):
    """The container probe's second scan sees a member the first did not
    (forged through a `cat` shim on the second pass over one entry)."""
    leader, child = _orphaned_session_child()
    _kill_and_wait(child, leader)
    path = tmp_path / "wrapper.pid"
    path.write_text(f"{leader}\n")
    counter = tmp_path / "count"
    target = f"/proc/{os.getpid()}/stat"
    shim = tmp_path / "bin"
    shim.mkdir()
    (shim / "cat").write_text(
        "#!/bin/sh\n"
        f'if [ "$1" = {target} ]; then\n'
        f'  n=$(/bin/cat {counter} 2>/dev/null || echo 0); n=$((n+1)); '
        f'echo $n > {counter}\n'
        f'  [ "$n" -ge 2 ] && {{ echo "1 (forked) S 1 1 {leader} 0"; exit 0; }}\n'
        'fi\n'
        'exec /bin/cat "$@"\n')
    (shim / "cat").chmod(0o755)
    env = dict(os.environ, PATH=f"{shim}:{os.environ.get('PATH', '')}")
    code = subprocess.run(["sh", "-c", docker_mod._ALIVE_SCRIPT, str(path)],
                          env=env).returncode
    assert code == 0, f"the second scan's member was missed: {code}"
    counter.write_text("-5\n")               # neither scan sees it now
    assert subprocess.run(["sh", "-c", docker_mod._ALIVE_SCRIPT, str(path)],
                          env=env).returncode == 1


# ---------------------------------------------------------------------------
# RM-R1e, finding 2: a claim read that failed is unknown, not "no claim"
# ---------------------------------------------------------------------------

def _docker_crash(runner, node_id, probe, monkeypatch):
    _crash_owner(runner, node_id)
    with runner.tree.transaction() as data:
        data["nodes"][node_id]["cleanup_hold"]["executor"] = {
            "kind": "docker", "container": "held-box"}
    monkeypatch.setattr(DockerExecutor, "wrapper_alive",
                        lambda self, agent_id: probe["alive"])
    dead, dead_start = _dead_pid()
    with runner.occupancy.locked() as records:   # the crashed owner's entry
        entry = (records.get("held-box") or {}).get(node_id)
        if entry is not None:
            entry.update(owner_pid=dead, owner_start=dead_start)
            runner.occupancy.commit(records)


def test_a_failed_claim_read_defers_the_takeover(tmp_path, monkeypatch):
    runner = _make(tmp_path, monkeypatch)
    node_id, probe, _ = _held_start(runner, monkeypatch)
    _docker_crash(runner, node_id, probe, monkeypatch)
    probe["alive"] = False

    heir = _make(tmp_path, monkeypatch)
    state = {"broken": True}
    real_read = heir.startup._read

    def read():
        if state["broken"]:
            raise OSError(errno.EIO, "I/O error")
        return real_read()

    monkeypatch.setattr(heir.startup, "_read", read)
    assert heir.capacity()["running"] == 1, "taken over with its claim unknown"
    assert node_id not in heir._locks
    assert _hold_of(heir, node_id)["owner"] == "crashed-runner"

    state["broken"] = False
    assert heir.capacity()["running"] == 0
    assert not _startup_runs(heir), "the recovered claim was never released"


# ---------------------------------------------------------------------------
# RM-R1e, finding 3: the hold names the container before registration
# ---------------------------------------------------------------------------

def _containerised(runner, monkeypatch):
    executor_cls = type(runner.executor())
    monkeypatch.setattr(executor_cls, "oom_kill_count", lambda self: 7,
                        raising=False)
    monkeypatch.setattr(executor_cls, "container", "held-box", raising=False)


def test_the_hold_names_the_container_before_registration(tmp_path, monkeypatch):
    runner = _make(tmp_path, monkeypatch)
    _containerised(runner, monkeypatch)
    seen = []
    real_register = runner.occupancy.register

    def register(container, node_id, pid, pid_start):
        seen.append((runner.tree.get(node_id).cleanup_hold or {}).get("occupancy"))
        return real_register(container, node_id, pid, pid_start)

    monkeypatch.setattr(runner.occupancy, "register", register)
    result = asyncio.run(runner.start("worker", "q"))
    assert result.get("agent_id") and not result.get("error"), result
    assert seen == ["held-box"], seen


def _grab_handles(runner, monkeypatch):
    handles = []
    executor_cls = type(runner.executor())
    real_start = executor_cls.start

    async def grabbing_start(self, *args, **kwargs):
        handle = await real_start(self, *args, **kwargs)
        handles.append(handle)
        return handle

    monkeypatch.setattr(executor_cls, "start", grabbing_start)
    return handles


def _assert_failed_into_cleanup(runner, result, handles):
    """RM-R1e (review ag-2792f3): an unconfirmed registration fails the
    launch into the one cleanup task — stopped, every release done, and
    the reservation lifted — never a run left going unregistered."""
    assert "container" in (result.get("error") or ""), result
    handle = handles[0]
    assert not _running(handle.pid, getattr(handle, "pid_start", "")), (
        "the unregistered run was left going")
    node = runner.tree.get(result["agent_id"])
    assert node.status == "failed" and node.cleanup_hold is None, node
    assert not runner._locks and not _startup_runs(runner)
    assert result["agent_id"] not in runner.runs
    assert [e for e in _events(runner) if e.get("kind") == "occupancy_unrecorded"]


def test_no_registration_without_the_hold_naming_it(tmp_path, monkeypatch):
    runner = _make(tmp_path, monkeypatch)
    _containerised(runner, monkeypatch)
    handles = _grab_handles(runner, monkeypatch)
    real_update = runner.tree.update

    def update(agent_id, **fields):
        if (fields.get("cleanup_hold") or {}).get("occupancy"):
            raise OSError(errno.ENOSPC, "No space left on device")
        return real_update(agent_id, **fields)

    monkeypatch.setattr(runner.tree, "update", update)
    result = asyncio.run(runner.start("worker", "q"))
    _assert_failed_into_cleanup(runner, result, handles)
    assert result["agent_id"] not in runner.occupancy.read().get("held-box", {})


def test_an_unconfirmed_registration_fails_the_launch_into_cleanup(
        tmp_path, monkeypatch):
    """The reviewer's case: `register` falls back to memory and the durable
    `ensure_live` write fails. Another server's store would see no
    occupant, so the run must not go on unregistered."""
    runner = _make(tmp_path, monkeypatch)
    _containerised(runner, monkeypatch)
    handles = _grab_handles(runner, monkeypatch)
    state = _flaky_occupancy(runner, monkeypatch)
    state["full"] = True
    result = asyncio.run(runner.start("worker", "q"))
    _assert_failed_into_cleanup(runner, result, handles)

    state["full"] = False                    # storage back: launches work
    other = _make(tmp_path, monkeypatch)
    result = asyncio.run(other.start("worker", "q"))
    assert result.get("agent_id") and not result.get("error"), result
    assert _file_entry(other, result["agent_id"]).get("ended") is None


# ---------------------------------------------------------------------------
# RM-R1e, finding 4: occupancy writes count only when durable
# ---------------------------------------------------------------------------

def _flaky_occupancy(runner, monkeypatch):
    state = {"full": False}
    occupancy = runner.occupancy
    real_commit = occupancy.commit

    def commit(records, strict=False):
        if state["full"]:
            if strict:
                raise OSError(errno.ENOSPC, "No space left on device")
            occupancy.memory = records      # the silent fallback
            return None
        return real_commit(records, strict)

    monkeypatch.setattr(occupancy, "commit", commit)
    return state


def _file_entry(runner, node_id):
    return (runner.occupancy._read_file().get("held-box") or {}).get(node_id)


def test_an_occupancy_end_held_in_memory_keeps_the_hold(tmp_path, monkeypatch):
    runner = _make(tmp_path, monkeypatch)
    _containerised(runner, monkeypatch)
    node_id, probe, _ = _held_start(runner, monkeypatch)
    assert _file_entry(runner, node_id).get("ended") is None
    state = _flaky_occupancy(runner, monkeypatch)

    state["full"] = True
    probe["alive"] = False
    assert runner.capacity()["running"] == 1, "lifted on a memory-only forget"
    assert _hold_of(runner, node_id)
    assert _file_entry(runner, node_id).get("ended") is None

    state["full"] = False
    assert runner.capacity()["running"] == 0
    assert not _hold_of(runner, node_id)
    entry = _file_entry(runner, node_id)
    assert entry is None or entry.get("ended") is not None


def test_a_takeover_waits_for_a_durable_rebind(tmp_path, monkeypatch):
    runner = _make(tmp_path, monkeypatch)
    _containerised(runner, monkeypatch)
    node_id, probe, _ = _held_start(runner, monkeypatch)
    _docker_crash(runner, node_id, probe, monkeypatch)

    heir = _make(tmp_path, monkeypatch)
    state = _flaky_occupancy(heir, monkeypatch)
    state["full"] = True
    heir.capacity()
    assert node_id not in heir._locks, "taken over on a memory-only rebind"
    assert _hold_of(heir, node_id)["owner"] == "crashed-runner"

    state["full"] = False
    heir.capacity()
    assert node_id in heir._locks
    assert _file_entry(heir, node_id)["owner_pid"] == os.getpid()
