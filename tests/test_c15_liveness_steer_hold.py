"""C15 — docker liveness under load, and a steer hold that stop_agent clears.
Contract: `context/specs/c15-docker-liveness-steer-hold.md` (LV-R1..LV-R5).
Ticket bug-d1731b.

Two halves.

Executor half (LV-R1, LV-R2): a real `DockerExecutor` over the executing fake
docker of `tests/support/sp_harness.py`, whose `docker exec` runs the command
locally. "Under load" and "a probe that hangs" are made with shims on PATH
(the fake docker runs the in-container command with the test's PATH):
- slow tools: `sed`, `awk`, `ps`, `pgrep` sleep before running, so a probe that
  walks every process in the container is slow while a direct check of the
  recorded pids is not;
- a hanging probe: a `sh` that, when it is started for this agent's liveness
  question, blocks with a small tree of descendants and logs their pids.
Both are heuristics about HOW a probe is run (it enters through `sh`, names the
run by its id or run dir, reads with ordinary tools); the assertions are only
about the verdict, the time it took, and which processes are left over.

Runner half (LV-R3..LV-R5): the SR/SF world (`test_sf_review_r3`), steered
through the public `steer_agent` / `stop_agent` tools. The predecessor's
container is a test double at the executor interface (`wrapper_verdict` /
`wrapper_alive` answer True / False / None), reached through the runner's
documented lookup of a RECORDED container, exactly as `test_sr_internal.py`
seams its probes. The real process of the previous turn IS dead (the steer's
stop killed it); what the double controls is what the container says.

Assumption recorded (NEED_INFO): the contract says `stop_agent`'s result "says
which of the two cases applied" without naming a field. These tests read the
vocabulary SF-R3 already gave the explicit-release result:
`predecessor_death_confirmed` (bool).
"""
from __future__ import annotations

import asyncio
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import sp_harness as sp                                         # noqa: E402
import test_sf_review_r3 as h                                   # noqa: E402
import test_sf_review_r5 as previous                            # noqa: E402
import pc_harness as pc                                         # noqa: E402

w = h.w


# ======================================================== executor half ====

AID = "ag-lv1501"
SLOW_TOOL_SECONDS = 10
FAST = 5.0     # a direct check of a pid answers well inside this, loaded or not


def _gone_pid() -> int:
    """A pid that existed a moment ago and is positively absent now."""
    p = subprocess.Popen(["true"])
    p.wait()
    return p.pid


def _reset_dir(path: Path) -> Path:
    shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True)
    return path


class Container:
    """A docker project's executor, its run dir, and the processes made."""

    def __init__(self, tmp_path: Path, monkeypatch):
        self.tmp = tmp_path
        self.monkeypatch = monkeypatch
        self.project = sp.Project(tmp_path, monkeypatch, executor="docker")
        self.executor = self.project.docker_executor()
        self.run_dir = self.executor.paths.run_dir(AID)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.procs: list[subprocess.Popen] = []

    def record(self, wrapper: int | None, agent: int | None = None) -> None:
        for name in ("wrapper.pid", "container.pid"):
            (self.run_dir / name).unlink(missing_ok=True)
        if wrapper is not None:
            (self.run_dir / "wrapper.pid").write_text(f"{wrapper}\n")
        if agent is not None:
            ns = os.readlink("/proc/self/ns/pid")
            (self.run_dir / "container.pid").write_text(f"{agent} {ns}\n")

    def live_session_leader(self) -> subprocess.Popen:
        p = subprocess.Popen(["sleep", "600"], start_new_session=True)
        self.procs.append(p)
        return p

    def shim_dir(self) -> Path:
        d = self.tmp / "shims"
        d.mkdir(exist_ok=True)
        return d

    def slow_tools(self, seconds: int = SLOW_TOOL_SECONDS) -> None:
        d = self.shim_dir()
        for tool in ("sed", "awk", "ps", "pgrep"):
            real = shutil.which(tool)
            if not real:
                continue
            f = d / tool
            f.write_text(f'#!/bin/sh\n/bin/sleep {seconds}\nexec {real} "$@"\n')
            f.chmod(0o755)
        self.monkeypatch.setenv("PATH", f"{d}{os.pathsep}{os.environ['PATH']}")

    def hanging_probe(self) -> Path:
        """A `sh` that hangs, with descendants, when it is started for this
        agent's liveness question; any other `sh` is the real one. Returns
        the file the hanging tree's pids are logged to."""
        d = self.shim_dir()
        log = self.tmp / "hung.pids"
        log.write_text("")
        f = d / "sh"
        f.write_text(
            "#!/bin/sh\n"
            f'case "$*" in *{AID}*) case "$*" in *kill*) ;; *)\n'
            f'  echo $$ >> {log}\n'
            f'  ( /bin/sleep 600 & echo $! >> {log}; wait ) &\n'
            f'  echo $! >> {log}\n'
            f'  /bin/sleep 600 & echo $! >> {log}; wait ;; esac ;; esac\n'
            'exec /bin/sh "$@"\n')
        f.chmod(0o755)
        self.monkeypatch.setenv("PATH", f"{d}{os.pathsep}{os.environ['PATH']}")
        return log

    def transport_failing_docker(self) -> None:
        """Every `docker exec` fails to reach the container (exit 1, no output);
        the daemon still says the container is running."""
        d = self.tmp / "failbin"
        d.mkdir(exist_ok=True)
        real = shutil.which("docker")
        f = d / "docker"
        f.write_text(
            "#!/bin/sh\n"
            'if [ "$1" = exec ]; then echo "transport failed" >&2; exit 1; fi\n'
            f'exec {real} "$@"\n')
        f.chmod(0o755)
        self.monkeypatch.setenv("PATH", f"{d}{os.pathsep}{os.environ['PATH']}")

    def close(self) -> None:
        for p in self.procs:
            try:
                p.kill()
            except OSError:
                pass
            p.wait()


@pytest.fixture
def box(tmp_path, monkeypatch):
    c = Container(tmp_path, monkeypatch)
    yield c
    c.close()


def _timed(fn, *args):
    t0 = time.monotonic()
    out = fn(*args)
    return out, time.monotonic() - t0


def test_lv_r1_recorded_pids_that_are_gone_give_a_dead_verdict_even_when_scanning_is_slow(box):
    box.record(_gone_pid(), _gone_pid())
    box.slow_tools()
    verdict, took = _timed(box.executor.wrapper_verdict, AID)
    assert verdict is False, f"absent recorded pids were not confirmed dead: {verdict!r}"
    assert took < FAST, f"the dead verdict took {took:.1f}s: it waited on a scan"


def test_lv_r1_a_live_recorded_pid_gives_alive(box):
    live = box.live_session_leader()
    box.record(live.pid, live.pid)
    assert box.executor.wrapper_verdict(AID) is True


def test_lv_r1_a_live_recorded_pid_gives_alive_even_when_scanning_is_slow(box):
    live = box.live_session_leader()
    box.record(live.pid, live.pid)
    box.slow_tools()
    verdict, took = _timed(box.executor.wrapper_verdict, AID)
    assert verdict is True
    assert took < FAST, f"the alive verdict took {took:.1f}s: it waited on a scan"


def test_lv_r1_a_recorded_live_agent_under_a_dead_wrapper_is_alive(box):
    """A dead wrapper is not a dead run: the agent it started lives on."""
    child_file = box.tmp / "agent.pid"
    leader = subprocess.Popen(
        ["sh", "-c", f"sleep 600 & echo $! > {child_file}; exit 0"],
        start_new_session=True)
    leader.wait()
    for _ in range(100):
        if child_file.exists() and child_file.read_text().strip():
            break
        time.sleep(0.05)
    agent = int(child_file.read_text())
    try:
        box.record(leader.pid, agent)
        assert box.executor.wrapper_verdict(AID) is True
    finally:
        os.kill(agent, signal.SIGKILL)


def test_lv_r1_a_live_descendant_of_a_dead_wrapper_is_not_dead_when_no_agent_pid_is_recorded(box):
    """The fallback scan's reason to exist (SR-R2): the wrapper is gone, the
    agent pid was never recorded, a member of the wrapper's session lives."""
    child_file = box.tmp / "member.pid"
    leader = subprocess.Popen(
        ["sh", "-c", f"sleep 600 & echo $! > {child_file}; exit 0"],
        start_new_session=True)
    leader.wait()
    for _ in range(100):
        if child_file.exists() and child_file.read_text().strip():
            break
        time.sleep(0.05)
    member = int(child_file.read_text())
    try:
        box.record(leader.pid, None)
        assert box.executor.wrapper_verdict(AID) is True
    finally:
        os.kill(member, signal.SIGKILL)


def test_lv_r1_a_missing_pid_file_is_unknown_never_dead(box):
    box.record(None, None)
    assert box.executor.wrapper_verdict(AID) is None


def test_lv_r1_a_transport_failure_is_unknown_even_for_absent_pids(box):
    box.record(_gone_pid(), _gone_pid())
    box.transport_failing_docker()
    assert box.executor.wrapper_verdict(AID) is None


def test_lv_r1_a_timed_out_probe_is_unknown(box):
    box.record(_gone_pid(), _gone_pid())
    box.hanging_probe()
    verdict, took = _timed(box.executor.wrapper_verdict, AID)
    assert took > 1.0, "the probe was never hung: the seam did not engage"
    assert verdict is None, f"a timed-out probe answered {verdict!r}"


def _alive_pid(pid: int) -> bool:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    return stat.rsplit(")", 1)[1].split()[0] not in ("Z", "X")


def test_lv_r2_a_probe_forced_to_time_out_leaves_no_process_behind(box):
    box.record(_gone_pid(), _gone_pid())
    log = box.hanging_probe()
    try:
        box.executor.wrapper_verdict(AID)
        pids = [int(x) for x in log.read_text().split()]
        assert len(pids) >= 3, f"the hanging probe did not start: {pids}"
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and any(_alive_pid(p) for p in pids):
            time.sleep(0.1)
        leaked = [p for p in pids if _alive_pid(p)]
        assert not leaked, f"probe processes survived the timeout: {leaked}"
    finally:
        for token in log.read_text().split():
            try:
                os.kill(int(token), signal.SIGKILL)
            except (OSError, ValueError):
                pass


def test_lv_r2_wrapper_alive_probe_that_times_out_leaves_no_process_behind(box):
    box.record(_gone_pid(), _gone_pid())
    log = box.hanging_probe()
    try:
        assert box.executor.wrapper_alive(AID) is None
        pids = [int(x) for x in log.read_text().split()]
        assert len(pids) >= 3, f"the hanging probe did not start: {pids}"
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and any(_alive_pid(p) for p in pids):
            time.sleep(0.1)
        leaked = [p for p in pids if _alive_pid(p)]
        assert not leaked, f"probe processes survived the timeout: {leaked}"
    finally:
        for token in log.read_text().split():
            try:
                os.kill(int(token), signal.SIGKILL)
            except (OSError, ValueError):
                pass


# ========================================================== runner half ====

CONTAINER = "lv-fake-container"


class FakeContainerExecutor:
    """The recorded container's executor, as the runner looks it up. What it
    answers about liveness is `answer` (True alive, False dead, None unknown)."""
    kind = "docker"
    container = CONTAINER

    def __init__(self):
        self.answer = None

    def inside(self) -> bool:
        return False

    def wrapper_verdict(self, agent_id):
        return self.answer

    def wrapper_alive(self, agent_id):
        return self.answer

    def kill_detached(self, agent_id, grace=3.0):
        return True

    def stop(self, remove=False):
        return {"ok": True, "acted_on": {}}


def container_world(w, monkeypatch):
    """A running agent with a captured session, recorded as run in a docker
    container whose liveness answers are the test's."""
    g = h.steer_world(w)
    monkeypatch.setattr("multiagents.runner.LAUNCH_CONFIRM_SECONDS", 0.3)
    fake = FakeContainerExecutor()
    real_for = w.runner._docker_for
    monkeypatch.setattr(w.runner, "_docker_for",
                        lambda c: fake if c == CONTAINER else real_for(c))
    return g, fake


def record_in_container(w, aid):
    w.tree().update(aid, exec_identity={"kind": "docker", "container": CONTAINER})


def refused_for_liveness(r) -> bool:
    return (r.get("steered") is False and "confirmed dead" in r.get("error", "")
            and "still pending" not in r.get("error", ""))


def refused_as_pending(r) -> bool:
    return r.get("steered") is False and "still pending" in r.get("error", "")


def test_lv_r3_unknown_cleanup_then_stop_agent_then_dead_lets_the_next_steer_proceed(w, monkeypatch):
    g, fake = container_world(w, monkeypatch)

    async def go():
        aid = await h.running_with_session(w, g)
        record_in_container(w, aid)
        fake.answer = None
        first = await w.server.steer_agent(aid, "steerhold: one")
        assert refused_for_liveness(first), first
        assert g.spawns() == 1
        fake.answer = False
        stopped = await w.server.stop_agent(aid)
        second = await w.server.steer_agent(aid, "steerhold: two")
        return aid, first, stopped, second
    aid, first, stopped, second = asyncio.run(go())
    assert not refused_as_pending(second), \
        f"stop_agent left a hold that rejects steers as pending: {second}"
    assert second.get("steered") is True, (stopped, second)
    assert g.spawns() == 2
    assert stopped.get("predecessor_death_confirmed") is True, stopped


def test_lv_r3_unknown_cleanup_then_stop_agent_still_unknown_refuses_with_the_not_confirmed_dead_reason(w, monkeypatch):
    g, fake = container_world(w, monkeypatch)

    async def go():
        aid = await h.running_with_session(w, g)
        record_in_container(w, aid)
        fake.answer = None
        first = await w.server.steer_agent(aid, "steerhold: one")
        assert refused_for_liveness(first), first
        stopped = await w.server.stop_agent(aid)
        second = await w.server.steer_agent(aid, "steerhold: two")
        return aid, stopped, second
    aid, stopped, second = asyncio.run(go())
    assert refused_for_liveness(second), \
        f"after stop_agent with liveness unknown the refusal must be SR-R2's: {second}"
    assert g.spawns() == 1, "a run was launched beside a predecessor not confirmed dead"
    assert stopped.get("predecessor_death_confirmed") is False, stopped


def test_lv_r3_stop_result_reports_death_unconfirmed_when_the_container_says_alive(w, monkeypatch):
    g, fake = container_world(w, monkeypatch)

    async def go():
        aid = await h.running_with_session(w, g)
        record_in_container(w, aid)
        fake.answer = None
        assert refused_for_liveness(await w.server.steer_agent(aid, "steerhold: one"))
        fake.answer = True
        stopped = await w.server.stop_agent(aid)
        second = await w.server.steer_agent(aid, "steerhold: two")
        return stopped, second
    stopped, second = asyncio.run(go())
    assert stopped.get("predecessor_death_confirmed") is False, stopped
    assert refused_for_liveness(second), second
    assert g.spawns() == 1


def test_lv_r3_a_stop_that_found_it_unknown_is_not_a_permanent_hold(w, monkeypatch):
    """Unknown at the stop, dead later: the steer after that proceeds."""
    g, fake = container_world(w, monkeypatch)

    async def go():
        aid = await h.running_with_session(w, g)
        record_in_container(w, aid)
        fake.answer = None
        assert refused_for_liveness(await w.server.steer_agent(aid, "steerhold: one"))
        await w.server.stop_agent(aid)
        assert refused_for_liveness(await w.server.steer_agent(aid, "steerhold: two"))
        fake.answer = False
        return await w.server.steer_agent(aid, "steerhold: three")
    third = asyncio.run(go())
    assert third.get("steered") is True, third
    assert g.spawns() == 2


def test_lv_r3_stop_agent_twice_after_an_unknown_cleanup_reports_the_same_and_does_not_wedge(w, monkeypatch):
    g, fake = container_world(w, monkeypatch)

    async def go():
        aid = await h.running_with_session(w, g)
        record_in_container(w, aid)
        fake.answer = None
        assert refused_for_liveness(await w.server.steer_agent(aid, "steerhold: one"))
        first = await w.server.stop_agent(aid)
        second = await w.server.stop_agent(aid)
        fake.answer = False
        steered = await w.server.steer_agent(aid, "steerhold: two")
        return first, second, steered
    first, second, steered = asyncio.run(go())
    assert first.get("predecessor_death_confirmed") is False, first
    assert second.get("predecessor_death_confirmed") is False, second
    assert steered.get("steered") is True, steered


def test_lv_r4_a_steer_refused_for_unknown_liveness_leaves_no_hold_once_it_is_known_dead(w, monkeypatch):
    g, fake = container_world(w, monkeypatch)

    async def go():
        aid = await h.running_with_session(w, g)
        record_in_container(w, aid)
        fake.answer = None
        first = await w.server.steer_agent(aid, "steerhold: one")
        fake.answer = False
        second = await w.server.steer_agent(aid, "steerhold: two")
        return first, second
    first, second = asyncio.run(go())
    assert refused_for_liveness(first), first
    assert not refused_as_pending(second), \
        f"the transient unknown left a hold that blocks the retry: {second}"
    assert second.get("steered") is True, second
    assert g.spawns() == 2


def test_lv_r4_a_refusal_for_unknown_liveness_launches_nothing(w, monkeypatch):
    g, fake = container_world(w, monkeypatch)

    async def go():
        aid = await h.running_with_session(w, g)
        record_in_container(w, aid)
        fake.answer = None
        first = await w.server.steer_agent(aid, "steerhold: one")
        again = await w.server.steer_agent(aid, "steerhold: again")
        return first, again
    first, again = asyncio.run(go())
    assert refused_for_liveness(first), first
    assert again.get("steered") is False, again
    assert g.spawns() == 1


def test_lv_r5_a_live_predecessor_still_refuses_the_steer(w, monkeypatch):
    g, fake = container_world(w, monkeypatch)

    async def go():
        aid = await h.running_with_session(w, g)
        record_in_container(w, aid)
        fake.answer = True
        first = await w.server.steer_agent(aid, "steerhold: one")
        second = await w.server.steer_agent(aid, "steerhold: two")
        return first, second
    first, second = asyncio.run(go())
    assert first.get("steered") is False, first
    assert "confirmed dead" in first.get("error", ""), first
    assert second.get("steered") is False, second
    assert g.spawns() == 1


def test_lv_r5_stop_agent_never_makes_a_live_predecessor_steerable(w, monkeypatch):
    g, fake = container_world(w, monkeypatch)

    async def go():
        aid = await h.running_with_session(w, g)
        record_in_container(w, aid)
        fake.answer = True
        assert (await w.server.steer_agent(aid, "steerhold: one")).get("steered") is False
        stopped = await w.server.stop_agent(aid)
        after = await w.server.steer_agent(aid, "steerhold: two")
        return stopped, after
    stopped, after = asyncio.run(go())
    assert after.get("steered") is False, after
    assert "confirmed dead" in after.get("error", ""), after
    assert g.spawns() == 1
    assert stopped.get("predecessor_death_confirmed") is False, stopped


def test_lv_r5_a_plain_steer_of_a_dead_predecessor_proceeds(w, monkeypatch):
    g, fake = container_world(w, monkeypatch)

    async def go():
        aid = await h.running_with_session(w, g)
        record_in_container(w, aid)
        fake.answer = False
        return await w.server.steer_agent(aid, "steerhold: one")
    r = asyncio.run(go())
    assert r.get("steered") is True, r
    assert g.spawns() == 2


def test_lv_r3_stop_agent_on_a_run_with_no_steer_hold_is_unchanged(w, monkeypatch):
    g, fake = container_world(w, monkeypatch)

    async def go():
        aid = await h.running_with_session(w, g)
        return aid, await w.server.stop_agent(aid)
    aid, stopped = asyncio.run(go())
    assert stopped.get("status") == "cancelled", stopped
    assert w.node(aid).status == "cancelled"
