"""Agents survive a restart of the orchestrator's CLI — SV-R1..SV-R11.

Contract: `context/specs/agent-survival.md`. Written red, before any
implementation (ag-0e7618).

Black box throughout. The server is a real `python -m multiagents.server`
subprocess spoken to over MCP stdio; it is ended the ways the contract names
(stdin EOF, SIGTERM, SIGHUP, SIGKILL, SIGSTOP). The agent is a real stub
process printing opencode-shaped NDJSON. The CLI is run as a subprocess. See
`tests/support/sv_harness.py`.

What these tests read, and why each is fair game:

- `tree.json` / `events.jsonl` through `Tree`: the node's status and reason,
  usage, steps and event count, and the `detached` / `adopted` events the
  contract names.
- `runs/<id>/exit_status`: named by SV-R2.
- The stub's own markers, pid files and heartbeat files: whether the agent is
  alive, finished, or was killed — the observable fact the contract is about.
- Where the contract names no file (the output file of SV-R1, the lock of
  SV-R5, the offset of SV-R7) no file name is assumed: SV-R1 searches every
  file under `runs/<id>/` for the lines, SV-R5 and SV-R7 are tested by their
  consequences.

Docker variants (SV-R1, SV-R4) are opt-in: `SV_TEST_DOCKER=1` with a working
docker daemon and project container. They skip otherwise.
"""

from __future__ import annotations

import json
import re
import signal
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import sv_harness as h  # noqa: E402

SID = "sv-sess-7c1e"
SERVER_EXIT = 10.0      # a server told to go must be gone by then
ADOPT_WITHIN = 15.0     # a root server adopts within this of starting
GRACE = 6.0             # SV-R4's "small grace" past the timeout, TERM->KILL included
DOCKER_SKIP = h.docker_ready()

EXECUTORS = [
    "local",
    pytest.param("docker", marks=pytest.mark.skipif(bool(DOCKER_SKIP),
                                                    reason=DOCKER_SKIP or "docker")),
]


@pytest.fixture
def project(tmp_path):
    made: list[h.Project] = []

    def build(**kw) -> h.Project:
        base = tmp_path / f"p{len(made)}"
        base.mkdir()
        p = h.Project(base, **kw)
        made.append(p)
        return p

    yield build
    for p in made:
        p.cleanup()


# ---------------------------------------------------------------------------
# Shared steps
# ---------------------------------------------------------------------------

def adopted(p: h.Project, agent_id: str) -> bool:
    return bool(p.events(agent_id, "adopted"))


def wait_adopted(p: h.Project, server: h.Server, agent_id: str,
                 timeout: float = ADOPT_WITHIN) -> bool:
    """Adoption, while keeping the adopting server busy with a harmless call so
    an implementation that adopts on a tool call rather than at startup is not
    penalised for when it chose to do it."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if adopted(p, agent_id):
            return True
        try:
            server.call("agent_tree", timeout=10)
        except h.McpError:
            pass
        time.sleep(0.2)
    return adopted(p, agent_id)


def start_and_detach(p: h.Project, steps: list, *, how: str = "eof",
                     session: str = h.ORCH_SESSION, ready: str = "started",
                     timeout: int = 0) -> tuple[h.Server, str]:
    """A root server starts the stub, waits until it has reached `ready`, then
    goes away `how` (eof / sigkill). Returns the dead server and the node id."""
    a = p.server(session=session)
    agent_id = a.start("survive " + h.plan_token(steps), timeout=timeout)
    assert h.wait_until(p.marker(ready).is_file, 15), (
        f"the stub never reached {ready!r}: {p.describe(agent_id)}\n{a.stderr()}")
    if how == "eof":
        a.eof()
    else:
        a.signal(signal.SIGKILL)
    assert a.exited(SERVER_EXIT), f"the root server did not exit after {how}"
    return a, agent_id


def live_steps(sid: str, p: h.Project, *, before: str = "before-detach",
               after: str = "after-adopt") -> list:
    """Say something, then wait for `go`, then finish cleanly."""
    return ([["pidfile", str(p.marker("pid"))],
             h.step_start(sid), h.text(sid, before), h.step_finish(sid, 100, 10, 0.01),
             ["touch", str(p.marker("started"))],
             ["wait_for", str(p.marker("go")), 60],
             h.step_start(sid), h.text(sid, after), h.step_finish(sid, 100, 10, 0.01),
             ["touch", str(p.marker("finished"))],
             ["exit", 0]])


def go(p: h.Project) -> None:
    p.marker("go").touch()


def terminal(p: h.Project, agent_id: str, timeout: float = 20.0) -> str:
    h.wait_until(lambda: p.status(agent_id) not in
                 ("pending", "running", "stuck", "detached", "absent"), timeout)
    return p.status(agent_id)


def run_text(p: h.Project, agent_id: str) -> str:
    """Every byte of every file under runs/<id>/, however it is laid out."""
    out = []
    for f in sorted(p.run_dir(agent_id).rglob("*")):
        if f.is_file():
            try:
                out.append(f.read_text(errors="replace"))
            except OSError:
                pass
    return "\n".join(out)


def timed_out(p: h.Project, agent_id: str) -> bool:
    """Classified as a timeout, not as the agent's own exit. The contract names
    the classification `timeout` without saying whether it is the node status
    or the reason of a `failed` node; either is accepted (NEED_INFO filed)."""
    node = p.node(agent_id)
    if node is None or node.status in ("done", "running", "detached", "pending"):
        return False
    return node.status == "timeout" or "timeout" in (node.reason or "").lower() \
        or "timed out" in (node.reason or "").lower()


# ===========================================================================
# SV-R1 — output goes to files
# ===========================================================================

@pytest.mark.parametrize("executor", EXECUTORS)
def test_sv_r1_every_line_reaches_a_run_file_even_after_the_server_is_sigkilled(
        project, executor):
    p = project(executor=executor)
    lines = [json.dumps({"type": "text", "sessionID": SID,
                         "part": {"text": f"sv-r1-line-{i}"}, "nonce": f"n{i}-4b2f"})
             for i in range(10)]
    steps: list = []
    for i, line in enumerate(lines):
        steps += [["line", line], ["sleep", 0.3]]
        if i == 2:
            steps.append(["touch", str(p.marker("started"))])
    steps += [["touch", str(p.marker("finished"))], ["exit", 0]]

    _, agent_id = start_and_detach(p, steps, how="sigkill")

    assert h.wait_until(p.marker("finished").is_file, 15), (
        "the agent did not finish after its server was SIGKILLed: its output "
        "went to a pipe nobody reads any more (SIGPIPE / EPIPE)")
    content = run_text(p, agent_id)
    missing = [line for line in lines if line not in content]
    assert not missing, (f"{len(missing)} of {len(lines)} lines are in no file under "
                         f"runs/{agent_id}/, first: {missing[0]}")


def test_sv_r1_a_supervised_run_reads_the_same_results_from_the_file(project):
    """What the server consumed from the pipe it consumes from the file, with
    the same results: usage, session id, steps and the result text."""
    p = project()
    s = p.server()
    agent_id = s.start("plain " + h.plan_token(h.turns(SID, 3, label="r1") + [["exit", 0]]))
    assert terminal(p, agent_id) == "done", p.describe(agent_id)
    node = p.node(agent_id)
    assert node.usage.get("input") == 300 and node.usage.get("output") == 30, node.usage
    assert round(node.usage.get("cost_usd", 0), 6) == 0.03, node.usage
    assert node.session_id == SID
    assert node.steps == 6, "3 step_start + 3 step_finish"
    collected = s.call("collect_agent", agent_id=agent_id, mode="full")
    assert all(f"r1-{i}" in json.dumps(collected) for i in range(3)), collected


# ===========================================================================
# SV-R2 — the exit status is recorded without a server
# ===========================================================================

def test_sv_r2_exit_3_with_no_server_attached_leaves_3(project):
    p = project()
    steps = [["touch", str(p.marker("started"))],
             ["wait_for", str(p.marker("go")), 60], ["exit", 3]]
    _, agent_id = start_and_detach(p, steps, how="sigkill")
    go(p)
    status = p.exit_status(agent_id)
    assert h.wait_until(status.is_file, 10), (
        f"no runs/{agent_id}/exit_status after the agent exited with no server")
    assert status.read_text().strip() == "3"


@pytest.mark.parametrize("code", [0, 3])
def test_sv_r2_exit_status_is_written_while_supervised_too(project, code):
    p = project()
    s = p.server()
    agent_id = s.start("x " + h.plan_token(h.turns(SID, 1) + [["exit", code]]))
    terminal(p, agent_id)
    status = p.exit_status(agent_id)
    assert h.wait_until(status.is_file, 10), "exit_status is written whether or not a server is alive"
    assert status.read_text().strip() == str(code)


# ===========================================================================
# SV-R3 — a root server leaves agents running when it exits
# ===========================================================================

def _two_live_stubs(p: h.Project, server: h.Server) -> list[tuple[str, int]]:
    out = []
    for n in range(2):
        pid_file = p.marker(f"pid{n}")
        steps = [["pidfile", str(pid_file)], h.step_start(SID), h.text(SID, f"a{n}"),
                 ["wait_for", str(p.marker("go")), 60], ["exit", 0]]
        agent_id = server.start(f"stub {n} " + h.plan_token(steps))
        out.append((agent_id, h.read_pid(pid_file)))
    return out


@pytest.mark.parametrize("ending", ["eof", "sigterm", "sighup"])
def test_sv_r3_a_root_server_exits_and_leaves_its_agents_detached(project, ending):
    p = project()
    s = p.server()
    agents = _two_live_stubs(p, s)
    {"eof": s.eof, "sigterm": lambda: s.signal(signal.SIGTERM),
     "sighup": lambda: s.signal(signal.SIGHUP)}[ending]()
    assert s.exited(SERVER_EXIT), f"the root server did not exit on {ending}"
    for agent_id, pid in agents:
        assert h.alive(pid), f"{agent_id} was killed when its root server exited on {ending}"
        assert p.status(agent_id) == "detached", p.describe(agent_id)
        marks = p.events(agent_id, "detached")
        assert len(marks) == 1, f"one `detached` event per node, saw {len(marks)}"
        assert marks[0].get("t"), "the detached event carries the time"


def test_sv_r3_a_depth_one_server_still_cancels_its_children(project):
    """Kept behaviour, so this may pass today: a regression guard."""
    p = project()
    parent = "ag-par001"
    p.tree.add(h.Node(id=parent, agent="spawner", provider="svstub", model="m",
                      parent=None, depth=1, status="running", task="parent",
                      session=h.ORCH_SESSION, started_at=h.tree_now()))
    s = p.server(agent_id=parent, depth=1)
    steps = [["pidfile", str(p.marker("pid"))], ["wait_for", str(p.marker("go")), 60],
             ["exit", 0]]
    child = s.start("child " + h.plan_token(steps))
    pid = h.read_pid(p.marker("pid"))
    s.eof()
    assert s.exited(SERVER_EXIT)
    assert h.wait_until(lambda: not h.alive(pid), 10), "a depth-1 server's child survived it"
    assert p.status(child) == "cancelled", p.describe(child)
    assert not p.events(child, "detached")


# ===========================================================================
# SV-R4 — a detached agent is bounded without a server
# ===========================================================================

@pytest.mark.parametrize("executor", EXECUTORS)
@pytest.mark.parametrize("stubborn", [False, True], ids=["obeys-term", "ignores-term"])
def test_sv_r4_the_wrapper_kills_the_process_group_at_the_timeout(project, executor, stubborn):
    p = project(executor=executor)
    beat, child_beat = p.marker("beat"), p.marker("child-beat")
    steps = ([["ignore_term"]] if stubborn else []) + [
        ["child_heartbeat", str(child_beat)], ["touch", str(p.marker("started"))],
        ["heartbeat", str(beat)]]
    began = time.monotonic()
    _, agent_id = start_and_detach(p, steps, how="sigkill", timeout=3)
    assert h.growing(beat) or time.monotonic() - began > 3, "the stub was not running"

    deadline = began + 3 + GRACE
    assert h.stopped_growing(beat, max(0.5, deadline - time.monotonic())), (
        f"the agent outlived its 3 s timeout + {GRACE} s with no server")
    assert h.stopped_growing(child_beat, max(0.5, deadline - time.monotonic())), (
        "the agent's process group was not killed: its child outlived the timeout")
    assert h.wait_until(p.exit_status(agent_id).is_file, 5), (
        "the timeout kill left no exit_status (SV-R2)")

    # SV-R2: the recorded status reads as a timeout to the next server.
    p.server()
    assert h.wait_until(lambda: timed_out(p, agent_id), ADOPT_WITHIN), (
        f"a timeout kill was not classified as timeout: {p.describe(agent_id)}")


# ===========================================================================
# SV-R5 — ownership is per node, and exclusive
# ===========================================================================

def test_sv_r5_a_node_supervised_by_a_live_server_is_not_adopted_by_another(project):
    p = project()
    a = p.server()
    agent_id = a.start("owned " + h.plan_token(live_steps(SID, p)))
    assert h.wait_until(p.marker("started").is_file, 15)
    b = p.server()
    for _ in range(5):
        b.call("agent_tree")
        time.sleep(0.3)
    assert not adopted(p, agent_id), "B adopted a node A holds"
    go(p)
    assert terminal(p, agent_id) == "done", p.describe(agent_id)
    node = p.node(agent_id)
    assert node.usage.get("input") == 200, f"counted twice or lost: {node.usage}"
    assert not adopted(p, agent_id)


def test_sv_r5_after_the_owner_is_sigkilled_another_live_server_adopts(project):
    """When a running server adopts is not fixed by the contract (SV-R6 says
    "at startup"; SV-R5 says B adopts after A dies). B is given ADOPT_WITHIN
    seconds and tool calls; see the NEED_INFO in the result."""
    p = project()
    a = p.server()
    agent_id = a.start("owned " + h.plan_token(live_steps(SID, p)))
    assert h.wait_until(p.marker("started").is_file, 15)
    b = p.server()
    b.call("agent_tree")
    a.kill()
    assert wait_adopted(p, b, agent_id), (
        f"nobody adopted {agent_id} after its owner was SIGKILLed: {p.describe(agent_id)}")
    go(p)
    assert terminal(p, agent_id) == "done", p.describe(agent_id)


def test_sv_r5_a_suspended_owner_keeps_its_nodes(project):
    p = project()
    a = p.server()
    agent_id = a.start("owned " + h.plan_token(live_steps(SID, p)))
    assert h.wait_until(p.marker("started").is_file, 15)
    a.signal(signal.SIGSTOP)
    try:
        b = p.server()
        for _ in range(5):
            b.call("agent_tree")
            time.sleep(0.3)
        assert not adopted(p, agent_id), "a suspended server's node was adopted"
    finally:
        a.signal(signal.SIGCONT)
    go(p)
    assert terminal(p, agent_id) == "done", p.describe(agent_id)
    assert p.node(agent_id).usage.get("input") == 200, p.node(agent_id).usage


# ===========================================================================
# SV-R6 — the next root server adopts what is left
# ===========================================================================

def test_sv_r6_a_detached_live_agent_is_adopted_and_collected_done(project):
    p = project()
    _, agent_id = start_and_detach(p, live_steps(SID, p, before="BEFORE-5d1", after="AFTER-9e4"))
    pid = h.read_pid(p.marker("pid"))
    assert h.alive(pid), "the agent died with its server (SV-R3)"
    b = p.server()
    assert wait_adopted(p, b, agent_id), p.describe(agent_id)
    assert p.status(agent_id) == "running", p.describe(agent_id)
    go(p)
    b.call("wait_for_agents", 60, args={"agent_ids": [agent_id], "timeout": 30})
    assert terminal(p, agent_id) == "done", p.describe(agent_id)
    result = json.dumps(b.call("collect_agent", agent_id=agent_id, mode="full"))
    assert "BEFORE-5d1" in result and "AFTER-9e4" in result, (
        f"the full result spans the gap: {result[:600]}")
    assert p.node(agent_id).session_id == SID


@pytest.mark.parametrize("ending", ["eof", "sigkill"])
def test_sv_r6_an_agent_that_finished_in_the_gap_is_finalised_done_with_its_usage(
        project, ending):
    p = project()
    steps = ([["touch", str(p.marker("started"))], ["wait_for", str(p.marker("go")), 60]]
             + h.turns(SID, 3, label="gap") + [["touch", str(p.marker("finished"))],
                                                ["exit", 0]])
    _, agent_id = start_and_detach(p, steps, how=ending)
    go(p)
    assert h.wait_until(p.marker("finished").is_file, 15), "the agent did not run to its end"
    h.wait_until(p.exit_status(agent_id).is_file, 5)
    p.server()
    assert terminal(p, agent_id, ADOPT_WITHIN) == "done", p.describe(agent_id)
    usage = p.node(agent_id).usage
    assert (usage.get("input"), usage.get("output")) == (300, 30), usage
    assert round(usage.get("cost_usd", 0), 6) == 0.03, usage
    assert p.node(agent_id).session_id == SID


def test_sv_r6_an_agent_that_failed_in_the_gap_is_finalised_failed(project):
    p = project()
    steps = ([["touch", str(p.marker("started"))], ["wait_for", str(p.marker("go")), 60]]
             + h.turns(SID, 1) + [["touch", str(p.marker("finished"))], ["exit", 2]])
    _, agent_id = start_and_detach(p, steps, how="sigkill")
    go(p)
    assert h.wait_until(p.marker("finished").is_file, 15)
    h.wait_until(p.exit_status(agent_id).is_file, 5)
    p.server()
    assert terminal(p, agent_id, ADOPT_WITHIN) == "failed", p.describe(agent_id)


def test_sv_r6_a_node_with_neither_output_nor_status_is_orphaned(project):
    import subprocess
    p = project()
    gone = subprocess.Popen(["true"])
    gone.wait()
    p.tree.add(h.Node(id="ag-nofile1", agent="worker", provider="svstub", model="m",
                      parent=None, depth=1, status="running", task="t", pid=gone.pid,
                      session=h.ORCH_SESSION, started_at=h.tree_now()))
    p.server()
    assert p.wait_status("ag-nofile1", {"orphaned"}, ADOPT_WITHIN) == "orphaned", (
        p.describe("ag-nofile1"))


def test_sv_r6_a_node_of_the_other_session_role_is_left_alone(project):
    p = project()
    _, agent_id = start_and_detach(p, live_steps(SID, p), session=h.INIT_SESSION)
    pid = h.read_pid(p.marker("pid"))
    b = p.server(session=h.ORCH_SESSION)
    for _ in range(8):
        b.call("agent_tree")
        time.sleep(0.25)
    assert h.alive(pid), "the orchestrator's server touched the initializer's agent"
    assert not adopted(p, agent_id)
    assert p.status(agent_id) == "detached", p.describe(agent_id)


@pytest.mark.parametrize("with_result", [True, False], ids=["final-result", "no-result"])
def test_sv_r6_a_dead_process_without_exit_status_is_judged_from_its_stream(
        project, with_result):
    """Decided: no exit_status is judged from the stream. The missing status is
    produced by deleting the file the contract names, after the agent ended."""
    p = project()
    steps = [["touch", str(p.marker("started"))], ["wait_for", str(p.marker("go")), 60],
             h.step_start(SID), h.text(SID, "judged")]
    if with_result:
        steps.append(["emit", {"type": "result", "subtype": "success",
                               "sessionID": SID, "result": "all done"}])
    steps += [["touch", str(p.marker("finished"))], ["exit", 0]]
    _, agent_id = start_and_detach(p, steps, how="sigkill")
    go(p)
    assert h.wait_until(p.marker("finished").is_file, 15)
    h.wait_until(p.exit_status(agent_id).is_file, 5)
    time.sleep(0.3)
    p.exit_status(agent_id).unlink(missing_ok=True)
    p.server()
    status = terminal(p, agent_id, ADOPT_WITHIN)
    if with_result:
        assert status == "done", p.describe(agent_id)
    else:
        assert status == "failed", p.describe(agent_id)
        assert "process ended without an exit status" in p.node(agent_id).reason


def test_sv_r6_run_does_not_reap_a_live_agent_it_could_adopt(project):
    p = project()
    _, agent_id = start_and_detach(p, live_steps(SID, p), how="sigkill")
    pid = h.read_pid(p.marker("pid"))
    done = p.cli("run", "--no-launch")
    assert h.alive(pid), f"`multiagents run` reaped an adoptable agent:\n{done.stdout}"
    assert p.status(agent_id) != "orphaned", p.describe(agent_id)


# ===========================================================================
# SV-R7 — nothing is counted twice or lost
# ===========================================================================

def _totals(p: h.Project, agent_id: str) -> tuple:
    n = p.node(agent_id)
    return (n.usage.get("input"), n.usage.get("output"),
            round(n.usage.get("cost_usd", 0), 6), n.steps, n.events)


def test_sv_r7_a_gap_mid_stream_gives_the_totals_of_an_unbroken_run(project):
    p = project()
    s = p.server()
    first, second = h.turns(SID, 3, gap=0.1, label="u"), h.turns(SID, 3, gap=0.1, label="v")
    reference = s.start("ref " + h.plan_token(first + second + [["exit", 0]]))
    assert terminal(p, reference) == "done"
    s.close()
    for f in ("started", "go"):
        p.marker(f).unlink(missing_ok=True)

    steps = (first + [["touch", str(p.marker("started"))],
                      ["wait_for", str(p.marker("go")), 60]]
             + second + [["touch", str(p.marker("finished"))], ["exit", 0]])
    _, agent_id = start_and_detach(p, steps, how="eof")
    go(p)
    assert h.wait_until(p.marker("finished").is_file, 15)
    p.server()
    assert terminal(p, agent_id, ADOPT_WITHIN) == "done", p.describe(agent_id)
    assert _totals(p, agent_id) == _totals(p, reference), (
        "(input, output, cost, steps, events) after a gap differ from an unbroken run")


def test_sv_r7_a_server_killed_during_replay_loses_and_doubles_nothing(project):
    p = project(agent_overrides={"max_steps": 100000})
    s = p.server()
    body = h.turns(SID, 150, label="big")
    reference = s.start("ref " + h.plan_token(body + [["exit", 0]]))
    assert terminal(p, reference, 30) == "done"
    s.close()

    steps = ([["touch", str(p.marker("started"))], ["wait_for", str(p.marker("go")), 60]]
             + body + [["touch", str(p.marker("finished"))], ["exit", 0]])
    _, agent_id = start_and_detach(p, steps, how="sigkill")
    go(p)
    assert h.wait_until(p.marker("finished").is_file, 30)
    for delay in (0.3, 0.8, 1.5):            # some of these land mid-replay
        b = p.server(handshake=False)
        time.sleep(delay)
        b.kill()
    p.server()
    assert terminal(p, agent_id, 30) == "done", p.describe(agent_id)
    assert _totals(p, agent_id) == _totals(p, reference), (
        "(input, output, cost, steps, events) after killed replays differ from an "
        "unbroken run")


# ===========================================================================
# SV-R8 — the watchdogs are fair across the gap
# ===========================================================================

def test_sv_r8_the_servers_downtime_is_not_silence(project):
    """silence_timeout 6 s. Last output 1 s before the server goes; the gap
    is over 2x the timeout; after adoption the agent stays quiet for 5.5 s —
    under the timeout counted from adoption, far over it counted from the
    last output — then finishes."""
    p = project(agent_overrides={"silence_timeout": 6})
    steps = [h.step_start(SID), h.text(SID, "spoke"), ["touch", str(p.marker("started"))],
             ["wait_for", str(p.marker("go")), 90],
             h.step_finish(SID, 1, 1, 0), ["exit", 0]]
    a = p.server()
    agent_id = a.start("quiet " + h.plan_token(steps))
    assert h.wait_until(p.marker("started").is_file, 15)
    time.sleep(1)
    a.eof()
    assert a.exited(SERVER_EXIT)
    time.sleep(12)
    b = p.server()
    assert wait_adopted(p, b, agent_id), p.describe(agent_id)
    time.sleep(5.5)
    stuck = [e for e in p.events(agent_id, "stuck")]
    assert p.status(agent_id) != "stuck" and not stuck, (
        f"reported stuck after adoption: {p.describe(agent_id)} {stuck}")
    go(p)
    assert terminal(p, agent_id) == "done", p.describe(agent_id)


def test_sv_r8_an_agent_past_its_wall_clock_at_adoption_ends_as_timeout(project):
    p = project()
    steps = [["touch", str(p.marker("started"))], ["heartbeat", str(p.marker("beat"))]]
    _, agent_id = start_and_detach(p, steps, how="eof", timeout=3)
    time.sleep(4)
    p.server()
    assert h.wait_until(lambda: timed_out(p, agent_id), ADOPT_WITHIN), p.describe(agent_id)
    assert h.stopped_growing(p.marker("beat"), 5)


# ===========================================================================
# SV-R9 — an adopted node is an ordinary node
# ===========================================================================

def _adopt_live(p: h.Project, **kw) -> tuple[h.Server, str]:
    _, agent_id = start_and_detach(p, kw.pop("steps", None) or live_steps(SID, p), how="eof")
    b = p.server()
    assert wait_adopted(p, b, agent_id), p.describe(agent_id)
    return b, agent_id


def test_sv_r9_wait_for_agents_without_ids_returns_when_the_adopted_agent_finishes(project):
    p = project()
    b, agent_id = _adopt_live(p)
    threading.Timer(1.0, go, args=(p,)).start()
    began = time.monotonic()
    result = b.call("wait_for_agents", 60, args={"timeout": 40})
    assert time.monotonic() - began < 30, "wait_for_agents did not return on the finish"
    assert agent_id in json.dumps(result), result
    assert terminal(p, agent_id) == "done", p.describe(agent_id)


def test_sv_r9_check_agent_on_an_adopted_node_shows_events_from_before_the_gap(project):
    p = project()
    b, agent_id = _adopt_live(p)
    seen = json.dumps(b.call("check_agent", agent_id=agent_id))
    assert "before-detach" in seen, seen[:800]
    go(p)


def test_sv_r9_stop_agent_kills_an_adopted_agent(project):
    p = project()
    steps = [["pidfile", str(p.marker("pid"))], ["child", str(p.marker("child"))],
             h.step_start(SID), h.text(SID, "hi"), ["touch", str(p.marker("started"))],
             ["wait_for", str(p.marker("go")), 60], ["exit", 0]]
    b, agent_id = _adopt_live(p, steps=steps)
    pid, child = h.read_pid(p.marker("pid")), h.read_pid(p.marker("child"))
    b.call("stop_agent", agent_id=agent_id)
    assert h.wait_until(lambda: not h.alive(pid) and not h.alive(child), 10), (
        "stop_agent left the adopted agent or its child alive")
    assert p.status(agent_id) == "cancelled", p.describe(agent_id)


def test_sv_r9_steering_a_finished_adopted_agent_resumes_its_session(project):
    p = project()
    b, agent_id = _adopt_live(p)
    go(p)
    assert terminal(p, agent_id) == "done", p.describe(agent_id)
    before = len(p.invocations())
    b.call("steer_agent", agent_id=agent_id, message="one more thing")
    assert h.wait_until(lambda: len(p.invocations()) > before, 15), "the steer launched nothing"
    argv = p.invocations()[-1]["argv"]
    assert "--resume" in argv and argv[argv.index("--resume") + 1] == SID, argv
    assert terminal(p, agent_id) == "done", p.describe(agent_id)


@pytest.mark.parametrize("action", ["merge_agent", "discard_agent"])
def test_sv_r9_merge_and_discard_work_on_an_adopted_node(project, action):
    p = project()
    steps = ([["write", "sv-r9-work.txt", "made across a gap\n"]]
             + live_steps(SID, p))
    b, agent_id = _adopt_live(p, steps=steps)
    go(p)
    assert terminal(p, agent_id) == "done", p.describe(agent_id)
    # Discarding an agent's work is the deliberate act, so it is asked for with
    # force: the adopted node's work is committed, and SV-R9 is about the node
    # being actionable, not about the unmerged-commit guard.
    kw = {"force": True} if action == "discard_agent" else {}
    result = b.call(action, agent_id=agent_id, **kw)
    assert "error" not in json.dumps(result).lower(), result
    if action == "merge_agent":
        assert (p.root / "sv-r9-work.txt").is_file(), f"not merged: {result}"
        assert p.status(agent_id) == "merged", p.describe(agent_id)
    else:
        assert p.status(agent_id) == "discarded", f"{p.describe(agent_id)} {result}"
        assert not (p.root / "sv-r9-work.txt").exists(), f"discarded work landed: {result}"


# ===========================================================================
# SV-R10 — the user can see and undo it
# ===========================================================================

def _detached_pair(p: h.Project, *, stubborn: bool = True) -> list[tuple[str, int, int]]:
    s = p.server()
    out = []
    for n in range(2):
        steps = [["ignore_term"]] * stubborn + [["pidfile", str(p.marker(f"pid{n}"))],
                 ["child", str(p.marker(f"child{n}"))], h.step_start(SID),
                 ["wait_for", str(p.marker("go")), 60], ["exit", 0]]
        agent_id = s.start(f"pair {n} " + h.plan_token(steps))
        out.append((agent_id, h.read_pid(p.marker(f"pid{n}")),
                    h.read_pid(p.marker(f"child{n}"))))
    s.eof()
    if not s.exited(SERVER_EXIT):   # SV-R3's failure, not this requirement's
        s.kill()
    return out


def test_sv_r10_stop_all_ends_detached_agents_with_no_server(project):
    p = project()
    pair = _detached_pair(p)
    done = p.cli("stop", "--all")
    assert done.returncode == 0, done.stderr
    for agent_id, pid, child in pair:
        assert h.wait_until(lambda: not h.alive(pid) and not h.alive(child), 10), (
            f"{agent_id} (or its child) survived `multiagents stop --all`")
        assert p.status(agent_id) == "cancelled", p.describe(agent_id)


def test_sv_r10_stop_all_ends_a_running_agent_whose_server_died(project):
    p = project()
    _, agent_id = start_and_detach(p, live_steps(SID, p), how="sigkill")
    pid = h.read_pid(p.marker("pid"))
    done = p.cli("stop", "--all")
    assert done.returncode == 0, done.stderr
    assert h.wait_until(lambda: not h.alive(pid), 10)
    assert p.status(agent_id) == "cancelled", p.describe(agent_id)


def test_sv_r10_stop_one_id_ends_only_that_agent(project):
    p = project()
    (first, pid1, child1), (second, pid2, child2) = _detached_pair(p)
    done = p.cli("stop", first)
    assert done.returncode == 0, done.stderr
    assert h.wait_until(lambda: not h.alive(pid1) and not h.alive(child1), 10)
    assert p.status(first) == "cancelled", p.describe(first)
    assert h.alive(pid2) and h.alive(child2), "stop <id> ended another agent"
    assert p.status(second) == "detached", p.describe(second)


def test_sv_r10_run_names_the_agents_left_running(project):
    p = project()
    pair = _detached_pair(p, stubborn=False)
    out = p.cli("run", "--no-launch").stdout
    for agent_id, pid, _ in pair:
        assert agent_id in out, out
        assert h.alive(pid), f"`run` ended {agent_id} instead of reporting it:\n{out}"
        assert p.status(agent_id) not in ("orphaned", "cancelled"), p.describe(agent_id)
    counted = [line for line in out.splitlines()
               if re.search(r"(?<![\d.$])2(?![\d.])", line) and "running" in line.lower()
               and not re.search(r"reap|reclaim|branch", line.lower())]
    assert counted, f"no line states that 2 agents are still running:\n{out}"


def test_sv_r10_agent_tree_marks_an_adopted_agent(project):
    p = project()
    b, agent_id = _adopt_live(p)
    tree = b.call("agent_tree")
    entries = [e for e in tree.get("active", []) if e.get("agent_id") == agent_id]
    assert entries, f"the adopted agent is not in agent_tree's active list: {tree}"
    assert "adopted" in json.dumps(entries[0]).lower(), entries[0]
    go(p)


# ===========================================================================
# SV-R11 — compaction may fire with agents running
# ===========================================================================

@pytest.fixture
def compact_session(tmp_path, monkeypatch, capsys):
    import test_phase0_interactive_compact as r8f
    return r8f, (lambda **kw: r8f.Session(tmp_path, monkeypatch, capsys, **kw))


@pytest.mark.parametrize("status", ["running", "detached", "stuck", "pending"])
def test_sv_r11_an_agent_without_an_unseen_result_does_not_block_compaction(
        compact_session, status):
    """The R8f harness, with an agent in flight. It has produced no result, so
    nothing the orchestrator has not seen exists, and compaction fires."""
    r8f, build = compact_session
    s = build()
    s.reading(r8f.OVER)
    s.node(status)
    s.run(r8f.stopped_then())
    r8f.assert_stop_compact_resume(s)
