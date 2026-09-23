"""Contract SL: a watchdog trip is a label, not a state that outlives the run
(ticket bug-2cebea).

The contract is `context/specs/stuck-lifecycle.md`, SL-R1 to SL-R7 plus
"Decisions from the advisor's read". Black box throughout:

- runs are driven through `Runner.start` / `consult` / `stop` /
  `wait_for_any` / `capacity` with a fake CLI (a small Python script standing
  in for the provider binary — no real CLI is ever spawned);
- what is observed is the node as the tree reports it (`status`, `reason`,
  `ended_at`, `pid`), `tree.active()`, the project's event log, and the
  results the public methods return;
- SL-R7's reconciliation is `multiagents run`'s own pass (`cmd_resume`, with
  `--no-launch`), over a `tree.json` written the way an older install left it.

Timing. The fake CLI is phased by GATE FILES rather than sleeps wherever the
test needs to look at a state in the middle of a run: the script blocks until
the test creates the file, so "stuck and live" is a state the test holds open
for as long as it needs, not a window it has to hit.

A note on SL-R6's providers.yaml key. The contract says a provider "may
declare, in providers.yaml, tools whose reported arguments do not identify
the call" but does not name the key. These tests use the provider-level key
`DECLARED_KEY` below; it is the one name in this file the contract did not
supply (reported as NEED_INFO with the suite).
"""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import c3_harness as h                                # noqa: E402
from multiagents import procs                                      # noqa: E402
from multiagents.tree import Node, TERMINAL                        # noqa: E402

SID = "sess-sl"
SRC = Path(__file__).resolve().parents[1] / "src" / "multiagents"
SHIPPED_PROVIDERS = SRC / "defaults" / "providers.yaml"

# SL-R6: the providers.yaml key under which a provider lists tools whose
# reported arguments do not identify the call. Invented here — see the module
# docstring.
DECLARED_KEY = "opaque_tools"


# ==========================================================================
# The fake CLI
# ==========================================================================

# Plays a scripted sequence of steps. `plans` is a list of step lists: the
# first invocation plays plans[0], the second plans[1], and so on (the last
# plan repeats). Steps:
#   ["emit", {...}]        print one NDJSON line
#   ["sleep", s]           sleep s seconds
#   ["gate", name]         block until <probe>/<name> exists (max 90 s)
#   ["touch", rel, text]   write a file relative to the cwd (the worktree)
#   ["touch_probe", name]  write <probe>/<name>: "every line before this is out"
#   ["stderr", text]       write to stderr
#   ["exit", code]         exit with code
_CLI = r'''#!{python}
import json, os, sys, time
from pathlib import Path

PROBE = Path({probe!r})
PLANS = json.loads({plans!r})
count_file = PROBE / "invocations"
n = int(count_file.read_text()) if count_file.exists() else 0
count_file.write_text(str(n + 1))
(PROBE / f"pid-{{n}}").write_text(str(os.getpid()))
plan = PLANS[min(n, len(PLANS) - 1)]
for step in plan:
    op = step[0]
    if op == "emit":
        print(json.dumps(step[1]))
        sys.stdout.flush()
    elif op == "sleep":
        time.sleep(step[1])
    elif op == "gate":
        deadline = time.time() + 90
        while not (PROBE / step[1]).exists() and time.time() < deadline:
            time.sleep(0.05)
    elif op == "touch":
        Path(step[1]).write_text(step[2])
    elif op == "touch_probe":
        (PROBE / step[1]).write_text("x")
    elif op == "stderr":
        sys.stderr.write(step[1])
        sys.stderr.flush()
    elif op == "exit":
        sys.exit(step[1])
sys.exit(0)
'''


def tool(name="read_it", **args):
    return ["emit", {"type": "tool", "session_id": SID, "name": name,
                     "input": args or {"path": "a.py"}}]


def text(words="all finished"):
    return ["emit", {"type": "text", "session_id": SID, "text": words}]


def step():
    return ["emit", {"type": "step", "session_id": SID}]


def gate(name):
    return ["gate", name]


def fake_provider(base: Path, plans, *, name="p", **extra) -> tuple[dict, Path]:
    """A providers.yaml-shaped dict pointing at a scripted fake CLI, and the
    probe directory the script and the test share."""
    probe = base / f"probe-{name}"
    probe.mkdir(parents=True, exist_ok=True)
    script = base / f"cli-{name}.py"
    script.write_text(_CLI.format(python=sys.executable, probe=str(probe),
                                  plans=json.dumps(plans)))
    script.chmod(0o755)
    provider = {
        "bin": str(script),
        "spawn": {"args": ["--fake"], "resume": ["--resume", "{session_id}"]},
        "stream": {"format": "ndjson", "session_id_paths": ["session_id"],
                   "rules": [
                       {"match": {"type": "tool"}, "as": "tool",
                        "fields": {"name": "name", "args": "input"}},
                       {"match": {"type": "step"}, "as": "step", "fields": {}},
                       {"match": {"type": "text"}, "as": "text",
                        "fields": {"text": "text"}},
                   ]},
        **extra,
    }
    return provider, probe


def open_gate(probe: Path, name: str) -> None:
    (probe / name).write_text("go")


# A loop threshold of 2: two identical calls with nothing changed on disk trip
# doom_loop. Kept small so a trip needs only a handful of stream lines.
LOOP = {"doom_loop_repeats": 2}


def make(tmp_path, monkeypatch, providers: dict, *, limits=None, agents=None, **spec_kw):
    """A Runner over a fresh project. `providers` is {name: dict}; the default
    agent `worker` runs on the first one."""
    first = next(iter(providers))
    agents = agents or {"worker": h.AgentSpec("worker", first, "m", **spec_kw)}
    return h.make_runner(tmp_path / "proj", monkeypatch, agents=agents,
                         providers=providers,
                         project={"limits": {**LOOP, **(limits or {})}})


# ==========================================================================
# Observation
# ==========================================================================

def events(r) -> list[dict]:
    path = r.paths.events_file
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def trips(r, agent_id) -> list[str]:
    """Trip kinds recorded for this agent in the event stream, in order."""
    return [e.get("reason") for e in events(r)
            if e.get("kind") == "stuck" and e.get("agent") == agent_id]


def status(r, agent_id) -> str:
    node = r.tree.get(agent_id)
    return node.status if node else ""


def active_ids(r) -> set[str]:
    return {n.id for n in r.tree.active()}


async def until(predicate, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.05)
    return predicate()


async def finish(r, agent_id, timeout=30):
    """Wait for the run to be over, however it ends."""
    run = r.runs.get(agent_id)
    if run is not None:
        await asyncio.wait_for(run.done.wait(), timeout)


async def stop_all(r):
    for agent_id in list(r.runs):
        run = r.runs[agent_id]
        if run.task and not run.task.done():
            try:
                await r.stop(agent_id)
            except Exception:
                pass


def live_process():
    """A real process that stays alive, with the identity procs.alive checks."""
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"],
                         start_new_session=True)
    for _ in range(100):
        if procs.start_time(p.pid):
            break
        time.sleep(0.01)
    return p, procs.start_time(p.pid)


def dead_process():
    """The pid and start time of a process that has since exited."""
    p, start = live_process()
    p.kill()
    p.wait()
    return p.pid, start


def put_node(tree, agent_id, status, reason, *, pid=None, pid_start="",
             agent="worker", provider="p"):
    """Write a node into tree.json through the tree's own public API, as a
    run would have left it."""
    tree.add(Node(id=agent_id, agent=agent, provider=provider, model="m",
                  parent=None, depth=1, task="t"))
    tree.set_status(agent_id, "running")
    tree.update(agent_id, pid=pid, pid_start=pid_start)
    if status != "running":
        tree.set_status(agent_id, status, reason)


# ==========================================================================
# SL-R1 — a run that ends gets its terminal status, tripped or not
# SL-R2 — the trip is not lost (asserted inside the SL-R1 tests, as the
#         contract's "Verified by" says)
# ==========================================================================

def test_sl_r1_tripped_run_that_exits_cleanly_is_done(tmp_path, monkeypatch):
    prov, _ = fake_provider(tmp_path, [[tool(), tool(), tool(), text("result"), ["exit", 0]]])
    r = make(tmp_path, monkeypatch, {"p": prov})

    async def go():
        res = await r.start("worker", "go")
        await finish(r, res["agent_id"])
        return res["agent_id"]
    agent = asyncio.run(go())

    assert trips(r, agent) == ["doom_loop"], "fixture: the run must have tripped"
    node = r.tree.get(agent)
    assert node.status == "done", (node.status, node.reason)
    assert node.ended_at is not None
    assert agent not in active_ids(r)
    # SL-R2: the trip kind is still visible on the node.
    assert "doom_loop" in node.reason, node.reason


def test_sl_r1_tripped_run_that_exits_nonzero_is_failed_with_both_reasons(tmp_path, monkeypatch):
    """Said something (so no free retry applies), then exited 3."""
    prov, _ = fake_provider(tmp_path, [[tool(), tool(), text("half way"), ["exit", 3]]])
    r = make(tmp_path, monkeypatch, {"p": prov})

    async def go():
        res = await r.start("worker", "go")
        await finish(r, res["agent_id"])
        return res["agent_id"]
    agent = asyncio.run(go())

    assert trips(r, agent) == ["doom_loop"]
    node = r.tree.get(agent)
    assert node.status == "failed", (node.status, node.reason)
    assert node.ended_at is not None
    assert agent not in active_ids(r)
    # SL-R2: both the trip and the classification's own reason are present.
    assert "doom_loop" in node.reason, node.reason
    assert "3" in node.reason and "exit" in node.reason.lower(), node.reason


def test_sl_r1_tripped_run_the_cli_truncated_is_truncated(tmp_path, monkeypatch):
    prov, _ = fake_provider(tmp_path, [[tool(), tool(), text("partial"),
                                        ["stderr", "turn CUT SHORT here"], ["exit", 0]]],
                            truncation_markers=["cut short"])
    r = make(tmp_path, monkeypatch, {"p": prov})

    async def go():
        res = await r.start("worker", "go")
        await finish(r, res["agent_id"])
        return res["agent_id"]
    agent = asyncio.run(go())

    node = r.tree.get(agent)
    assert node.status == "truncated", (node.status, node.reason)
    assert node.ended_at is not None
    assert "doom_loop" in node.reason, node.reason


def test_sl_r1_a_later_trip_kind_is_the_one_named(tmp_path, monkeypatch):
    """runaway_steps, not doom_loop: the classification works for any kind,
    and the reason names the trip that actually happened."""
    prov, _ = fake_provider(tmp_path, [[step(), step(), step(), step(),
                                        text("result"), ["exit", 0]]])
    r = make(tmp_path, monkeypatch, {"p": prov}, max_steps=2)

    async def go():
        res = await r.start("worker", "go")
        await finish(r, res["agent_id"])
        return res["agent_id"]
    agent = asyncio.run(go())

    assert trips(r, agent) == ["runaway_steps"]
    node = r.tree.get(agent)
    assert node.status == "done", (node.status, node.reason)
    assert "runaway_steps" in node.reason, node.reason
    assert agent not in active_ids(r)


def test_sl_r1_tripped_conversation_turn_parks_idle(tmp_path, monkeypatch):
    """A conversation's clean turn ends idle, tripped or not."""
    prov, _ = fake_provider(tmp_path, [[tool(), tool(), tool(), text("answer"), ["exit", 0]]])
    spec = h.AgentSpec("advisor", "p", "m", conversational=True)
    r = make(tmp_path, monkeypatch, {"p": prov}, agents={"advisor": spec})

    result = asyncio.run(r.consult("advisor", "question?", timeout=30))
    agent = result.get("agent_id") or result.get("node_id")
    assert agent, result
    assert trips(r, agent) == ["doom_loop"], "fixture: the turn must have tripped"
    node = r.tree.get(agent)
    assert node.status == "idle", (node.status, node.reason)
    assert agent not in active_ids(r)
    assert "doom_loop" in node.reason, node.reason


def test_sl_r1_tripped_silent_early_death_still_gets_its_free_retry(tmp_path, monkeypatch):
    """Decision: the free retry for a cheap death applies as it would had the
    run never tripped, and the status the retried run ends with is the one
    recorded. First attempt: trips, says nothing, exits 1 at once. Second:
    answers and exits 0."""
    prov, probe = fake_provider(tmp_path, [
        [tool(), tool(), tool(), ["exit", 1]],
        [text("second attempt worked"), ["exit", 0]],
    ])
    r = make(tmp_path, monkeypatch, {"p": prov})

    async def go():
        res = await r.start("worker", "go")
        agent = res["agent_id"]
        assert await until(lambda: status(r, agent) in TERMINAL, 20), status(r, agent)
        return agent
    agent = asyncio.run(go())

    assert (probe / "invocations").read_text() == "2", "the retry never ran"
    node = r.tree.get(agent)
    assert node.status == "done", (node.status, node.reason)
    assert node.ended_at is not None


# ==========================================================================
# SL-R2 — the trip's event is unchanged; explicit operator actions win
# ==========================================================================

def test_sl_r2_trip_event_is_unchanged_by_the_ending(tmp_path, monkeypatch):
    prov, probe = fake_provider(tmp_path, [[tool(), tool(), gate("end"),
                                            text("result"), ["exit", 0]]])
    r = make(tmp_path, monkeypatch, {"p": prov})

    async def go():
        res = await r.start("worker", "go")
        agent = res["agent_id"]
        assert await until(lambda: trips(r, agent), 20)
        before = [e for e in events(r) if e.get("kind") == "stuck" and e.get("agent") == agent]
        open_gate(probe, "end")
        await finish(r, agent)
        after = [e for e in events(r) if e.get("kind") == "stuck" and e.get("agent") == agent]
        return agent, before, after
    agent, before, after = asyncio.run(go())

    assert after == before, "the trip's event was rewritten or dropped"
    assert status(r, agent) == "done"


def test_sl_r2_stop_on_a_stuck_agent_keeps_stops_own_status(tmp_path, monkeypatch):
    """Decision: stop_agent sets its own status and reason, as today; the trip
    remains in the event stream."""
    prov, _ = fake_provider(tmp_path, [[tool(), tool(), gate("never"), ["exit", 0]]])
    r = make(tmp_path, monkeypatch, {"p": prov})

    async def go():
        res = await r.start("worker", "go")
        agent = res["agent_id"]
        assert await until(lambda: status(r, agent) == "stuck", 20)
        out = await r.stop(agent)
        return agent, out
    agent, out = asyncio.run(go())

    node = r.tree.get(agent)
    assert out["status"] == "cancelled"
    assert node.status == "cancelled", (node.status, node.reason)
    assert node.reason == "stopped by parent", node.reason
    assert trips(r, agent) == ["doom_loop"]
    assert agent not in active_ids(r)


# ==========================================================================
# SL-R3 — `stuck` clears when the agent visibly moves on
# ==========================================================================

def _r3_run(tmp_path, monkeypatch, plan, *, after_trip, **spec_kw):
    """Start a run whose plan trips, blocks on gate `a`, then plays on to gate
    `b` (where it stays alive). Returns (status after the trip, status after
    the progress, pids seen, invocations, trip kinds)."""
    prov, probe = fake_provider(tmp_path, [plan])
    r = make(tmp_path, monkeypatch, {"p": prov}, **spec_kw)

    async def go():
        res = await r.start("worker", "go")
        agent = res["agent_id"]
        try:
            assert await until(lambda: status(r, agent) == "stuck", 30), status(r, agent)
            pid_before = r.tree.get(agent).pid
            stuck_status = status(r, agent)
            open_gate(probe, "a")
            await until(lambda: (probe / "b-reached").exists(), 20)
            await after_trip(r, agent)
            later = status(r, agent)
            pid_after = r.tree.get(agent).pid
            alive = r.runs[agent].task is not None and not r.runs[agent].task.done()
            return (stuck_status, later, (pid_before, pid_after), alive,
                    (probe / "invocations").read_text(), trips(r, agent))
        finally:
            await stop_all(r)
    return asyncio.run(go())


async def _settle(r, agent, seconds=3.0):
    """Give the runner time to act on what was just streamed."""
    await until(lambda: status(r, agent) == "running", seconds)


def test_sl_r3_a_different_tool_call_clears_stuck(tmp_path, monkeypatch):
    plan = [tool(), tool(), gate("a"), tool("other_tool", q="x"), gate("b")]
    stuck, later, pids, alive, invocations, kinds = _r3_run_marked(
        tmp_path, monkeypatch, plan)
    assert stuck == "stuck"
    assert later == "running", later
    assert alive, "clearing must not end the run"
    assert pids[0] == pids[1] and invocations == "1", \
        "clearing must not restart or steer the agent"
    assert kinds == ["doom_loop"], "the trip stays recorded as an event"


def test_sl_r3_the_same_repeated_call_stays_stuck(tmp_path, monkeypatch):
    """One more identical call after the trip — not the re-arm point (that is
    the 4th at repeats=2) — is not progress."""
    plan = [tool(), tool(), gate("a"), tool(), gate("b")]
    stuck, later, _pids, alive, _inv, kinds = _r3_run_marked(tmp_path, monkeypatch, plan)
    assert stuck == "stuck"
    assert later == "stuck", later
    assert kinds == ["doom_loop"]


def test_sl_r3_a_worktree_change_clears_stuck(tmp_path, monkeypatch):
    """After the trip the agent changes a file in its worktree and — past the
    progress sampling interval — repeats the SAME call. Only the change on
    disk can explain the recovery."""
    plan = [tool(), tool(), gate("a"), ["touch", "progress.txt", "work\n"],
            ["sleep", 4], tool(), gate("b")]
    stuck, later, pids, alive, invocations, kinds = _r3_run_marked(
        tmp_path, monkeypatch, plan)
    assert stuck == "stuck"
    assert later == "running", later
    assert alive and pids[0] == pids[1] and invocations == "1"
    assert kinds == ["doom_loop"]


def test_sl_r3_any_stream_event_clears_a_silence_trip(tmp_path, monkeypatch):
    """silence_timeout=1; the timer loop needs two looks at a still tree, so
    the trip lands within ~10-15 s. Then a plain text event arrives."""
    plan = [text("starting"), gate("a"), text("back again"), gate("b")]
    stuck, later, pids, alive, invocations, kinds = _r3_run_marked(
        tmp_path, monkeypatch, plan, silence_timeout=1)
    assert stuck == "stuck"
    assert kinds[:1] == ["silence"], kinds
    assert later == "running", later
    assert alive and pids[0] == pids[1] and invocations == "1"


def test_sl_r3_a_later_trip_sets_stuck_again(tmp_path, monkeypatch):
    plan = [tool(), tool(), gate("a"), tool("other_tool", q="x"), gate("b"),
            tool("third", q="y"), tool("third", q="y"), gate("c")]
    prov, probe = fake_provider(tmp_path, [_with_markers(plan)])
    r = make(tmp_path, monkeypatch, {"p": prov})

    async def go():
        res = await r.start("worker", "go")
        agent = res["agent_id"]
        try:
            assert await until(lambda: status(r, agent) == "stuck", 30)
            open_gate(probe, "a")
            assert await until(lambda: (probe / "b-reached").exists(), 20)
            cleared = await until(lambda: status(r, agent) == "running", 5)
            open_gate(probe, "b")
            assert await until(lambda: (probe / "c-reached").exists(), 20)
            again = await until(lambda: status(r, agent) == "stuck", 5)
            return cleared, again, trips(r, agent)
        finally:
            await stop_all(r)
    cleared, again, kinds = asyncio.run(go())
    assert cleared, "the different call did not clear stuck"
    assert again, "a new loop after clearing did not set stuck again"
    assert kinds == ["doom_loop", "doom_loop"]


def _with_markers(plan):
    """Before each gate after the first, drop a `<gate>-reached` file in the
    probe so the test knows every line before it has been printed."""
    out = []
    for s in plan:
        if s[0] == "gate" and s[1] != "a":
            out.append(["touch_probe", f"{s[1]}-reached"])
        out.append(s)
    return out


def _r3_run_marked(tmp_path, monkeypatch, plan, **spec_kw):
    async def after(r, agent):
        await _settle(r, agent)
    return _r3_run(tmp_path, monkeypatch, _with_markers(plan), after_trip=after, **spec_kw)


# ==========================================================================
# SL-R4 — one definition of "occupies a slot"
# ==========================================================================

def test_sl_r4_only_live_stuck_occupies_a_slot(tmp_path, monkeypatch):
    """A: trips and stays alive. B: trips and then finishes. With
    max_concurrent=2 one slot is free, so a third start is allowed."""
    prov_a, probe_a = fake_provider(tmp_path, [[tool(), tool(), gate("never")]], name="pa")
    prov_b, _ = fake_provider(tmp_path, [[tool(), tool(), text("ok"), ["exit", 0]]], name="pb")
    prov_c, _ = fake_provider(tmp_path, [[text("c done"), ["exit", 0]]], name="pc")
    agents = {"a": h.AgentSpec("a", "pa", "m"), "b": h.AgentSpec("b", "pb", "m"),
              "c": h.AgentSpec("c", "pc", "m")}
    r = make(tmp_path, monkeypatch, {"pa": prov_a, "pb": prov_b, "pc": prov_c},
             agents=agents, limits={"max_concurrent": 2})

    async def go():
        try:
            a = (await r.start("a", "go"))["agent_id"]
            assert await until(lambda: status(r, a) == "stuck", 20)
            b = (await r.start("b", "go"))["agent_id"]
            await finish(r, b)
            assert trips(r, b) == ["doom_loop"], "fixture: B must have tripped"
            cap = r.capacity()
            waited = await r.wait_for_any([a], timeout=1)
            started = await r.start("c", "go")
            await finish(r, started["agent_id"])
            return a, b, cap, waited, started
        finally:
            await stop_all(r)
    a, b, cap, waited, started = asyncio.run(go())

    assert cap["running"] == 1, cap
    assert cap["free_slots"] == 1, cap
    # wait_for_agents agrees with capacity().
    assert waited["capacity"]["running"] == 1, waited
    assert started.get("agent_id"), started


def test_sl_r4_finished_stuck_nodes_do_not_block_start(tmp_path, monkeypatch):
    """max_concurrent=1 and one node on disk that tripped and whose process is
    gone: start_agent is allowed."""
    prov, _ = fake_provider(tmp_path, [[text("fine"), ["exit", 0]]])
    r = make(tmp_path, monkeypatch, {"p": prov}, limits={"max_concurrent": 1})
    pid, start = dead_process()
    put_node(r.tree, "ag-dead01", "stuck", "doom_loop: x called 2x", pid=pid, pid_start=start)

    assert r.capacity()["running"] == 0, r.capacity()

    async def go():
        res = await r.start("worker", "go")
        await finish(r, res["agent_id"])
        return res
    assert asyncio.run(go()).get("agent_id")


def test_sl_r4_live_stuck_node_still_blocks_a_full_house(tmp_path, monkeypatch):
    """The other side of the boundary: a stuck node whose process IS alive
    occupies its slot, so with max_concurrent=1 a start is refused."""
    prov, _ = fake_provider(tmp_path, [[text("fine"), ["exit", 0]]])
    r = make(tmp_path, monkeypatch, {"p": prov}, limits={"max_concurrent": 1})
    proc, start = live_process()
    try:
        put_node(r.tree, "ag-live01", "stuck", "doom_loop: x called 2x",
                 pid=proc.pid, pid_start=start)
        assert r.capacity()["running"] == 1, r.capacity()
        with pytest.raises(RuntimeError, match="max_concurrent"):
            asyncio.run(r.start("worker", "go"))
    finally:
        proc.kill()
        proc.wait()


def test_sl_r4_recycled_pid_is_not_live(tmp_path, monkeypatch):
    """Liveness is pid AND pid_start: a stuck node whose pid now belongs to a
    different process (start time disagrees) does not occupy a slot."""
    prov, _ = fake_provider(tmp_path, [[text("fine"), ["exit", 0]]])
    r = make(tmp_path, monkeypatch, {"p": prov}, limits={"max_concurrent": 1})
    proc, start = live_process()
    try:
        put_node(r.tree, "ag-recy01", "stuck", "silence: no stream event for 200s",
                 pid=proc.pid, pid_start=str(int(start) + 12345))
        assert r.capacity()["running"] == 0, r.capacity()
    finally:
        proc.kill()
        proc.wait()


def test_sl_r4_running_agent_is_counted_as_before(tmp_path, monkeypatch):
    """Guard against over-correcting: an ordinary live running agent still
    occupies its slot, and a second start past the limit is refused."""
    prov, probe = fake_provider(tmp_path, [[text("working"), gate("end"), ["exit", 0]]])
    r = make(tmp_path, monkeypatch, {"p": prov}, limits={"max_concurrent": 1})

    async def go():
        a = (await r.start("worker", "go"))["agent_id"]
        try:
            assert await until(lambda: status(r, a) == "running", 10)
            cap = r.capacity()
            refused = None
            try:
                await r.start("worker", "again")
            except RuntimeError as exc:
                refused = exc
            return cap, refused
        finally:
            open_gate(probe, "end")
            await finish(r, a)
    cap, refused = asyncio.run(go())
    assert cap["running"] == 1, cap
    assert refused is not None and "max_concurrent" in str(refused)


# ==========================================================================
# SL-R5 — wait_for_agents waits on a live stuck agent
# ==========================================================================

def test_sl_r5_a_live_stuck_agent_is_waited_on_until_it_finishes(tmp_path, monkeypatch):
    """(a) stuck before the call, then finishes: the wait returns on the
    finish, not at once, and never lists it under already_finished."""
    prov, probe = fake_provider(tmp_path, [[tool(), tool(), gate("end"),
                                            text("result"), ["exit", 0]]])
    r = make(tmp_path, monkeypatch, {"p": prov})

    async def go():
        agent = (await r.start("worker", "go"))["agent_id"]
        try:
            assert await until(lambda: status(r, agent) == "stuck", 20)
            waiting = asyncio.create_task(r.wait_for_any([agent], timeout=40))
            await asyncio.sleep(2.5)
            early = waiting.done()
            open_gate(probe, "end")
            result = await waiting
            return agent, early, result
        finally:
            await stop_all(r)
    agent, early, result = asyncio.run(go())

    assert not early, f"returned before the agent finished: {result}"
    assert not result.get("timed_out"), result
    finished = [c["agent_id"] for c in result.get("already_finished") or []]
    assert agent not in finished, result
    changed = {c["agent_id"]: c for c in result["changed"]}
    assert agent in changed, result
    assert changed[agent]["status"] == "done", changed[agent]


def test_sl_r5_a_trip_during_the_wait_is_returned(tmp_path, monkeypatch):
    """(b) running when the call starts, trips during it: returned as changed."""
    prov, probe = fake_provider(tmp_path, [[text("working"), gate("go"),
                                            tool(), tool(), gate("never")]])
    r = make(tmp_path, monkeypatch, {"p": prov})

    async def go():
        agent = (await r.start("worker", "go"))["agent_id"]
        try:
            assert await until(lambda: status(r, agent) == "running", 10)
            waiting = asyncio.create_task(r.wait_for_any([agent], timeout=40))
            await asyncio.sleep(1.5)
            open_gate(probe, "go")
            result = await waiting
            return agent, result
        finally:
            await stop_all(r)
    agent, result = asyncio.run(go())

    assert not result.get("timed_out"), result
    changed = {c["agent_id"]: c for c in result["changed"]}
    assert agent in changed, result
    assert changed[agent]["status"] == "stuck"
    assert "doom_loop" in changed[agent]["reason"]


def test_sl_r5_the_result_names_the_stuck_agents_still_waited_on(tmp_path, monkeypatch):
    """A wait that times out on a live stuck agent says it is still waiting on
    that agent and shows why it is stuck. The field name is the developer's:
    asserted as "somewhere in the result, outside changed/already_finished"."""
    prov, _ = fake_provider(tmp_path, [[tool(), tool(), gate("never")]])
    r = make(tmp_path, monkeypatch, {"p": prov})

    async def go():
        agent = (await r.start("worker", "go"))["agent_id"]
        try:
            assert await until(lambda: status(r, agent) == "stuck", 20)
            started = time.monotonic()
            result = await r.wait_for_any([agent], timeout=2)
            return agent, result, time.monotonic() - started
        finally:
            await stop_all(r)
    agent, result, took = asyncio.run(go())

    assert took >= 1.5, f"returned after {took:.1f}s instead of waiting: {result}"
    assert result.get("changed") == [], result
    finished = [c["agent_id"] for c in result.get("already_finished") or []]
    assert agent not in finished, result
    rest = {k: v for k, v in result.items() if k not in ("changed", "already_finished")}
    blob = json.dumps(rest, default=str)
    assert agent in blob, result
    assert "doom_loop" in blob, f"the stuck reason is not reported: {result}"


def test_sl_r5_waiting_on_everything_includes_a_live_stuck_agent(tmp_path, monkeypatch):
    """wait_for_any(None): B finishes while A is live and stuck. The result
    reports B as changed and A not as finished."""
    prov_a, _ = fake_provider(tmp_path, [[tool(), tool(), gate("never")]], name="pa")
    prov_b, probe_b = fake_provider(tmp_path, [[text("b"), gate("end"), ["exit", 0]]], name="pb")
    agents = {"a": h.AgentSpec("a", "pa", "m"), "b": h.AgentSpec("b", "pb", "m")}
    r = make(tmp_path, monkeypatch, {"pa": prov_a, "pb": prov_b}, agents=agents)

    async def go():
        try:
            a = (await r.start("a", "go"))["agent_id"]
            assert await until(lambda: status(r, a) == "stuck", 20)
            b = (await r.start("b", "go"))["agent_id"]
            assert await until(lambda: status(r, b) == "running", 10)
            waiting = asyncio.create_task(r.wait_for_any(None, timeout=40))
            await asyncio.sleep(1.5)
            early = waiting.done()
            open_gate(probe_b, "end")
            return a, b, early, await waiting
        finally:
            await stop_all(r)
    a, b, early, result = asyncio.run(go())

    assert not early, f"returned before anything finished: {result}"
    changed = [c["agent_id"] for c in result["changed"]]
    assert changed == [b], result
    finished = [c["agent_id"] for c in result.get("already_finished") or []]
    assert a not in finished, result


# ==========================================================================
# SL-R6 — reads the stream cannot tell apart are not a doom loop
# ==========================================================================

def _r6(tmp_path, monkeypatch, plan, *, declared, **spec_kw):
    extra = {DECLARED_KEY: declared} if declared is not None else {}
    prov, _ = fake_provider(tmp_path, [plan], **extra)
    r = make(tmp_path, monkeypatch, {"p": prov}, **spec_kw)

    async def go():
        res = await r.start("worker", "go")
        await finish(r, res["agent_id"])
        return res["agent_id"]
    agent = asyncio.run(go())
    return r, agent


def test_sl_r6_repeats_of_a_declared_tool_do_not_trip(tmp_path, monkeypatch):
    """Six identical calls, three times the threshold, nothing on disk."""
    plan = [tool("peek", path="a.py")] * 6 + [text("read it all"), ["exit", 0]]
    r, agent = _r6(tmp_path, monkeypatch, plan, declared=["peek"])
    assert "doom_loop" not in trips(r, agent), trips(r, agent)
    node = r.tree.get(agent)
    assert node.status == "done" and "doom_loop" not in node.reason, (node.status, node.reason)


def test_sl_r6_an_undeclared_tool_in_the_same_provider_still_trips(tmp_path, monkeypatch):
    """Tools not declared behave exactly as today."""
    plan = [tool("grep_it", q="x")] * 2 + [text("done"), ["exit", 0]]
    r, agent = _r6(tmp_path, monkeypatch, plan, declared=["peek"])
    assert trips(r, agent) == ["doom_loop"]


def test_sl_r6_the_same_repeats_trip_without_the_declaration(tmp_path, monkeypatch):
    """Control: the plan that did not trip above does trip for a provider that
    declares nothing."""
    plan = [tool("peek", path="a.py")] * 6 + [text("read it all"), ["exit", 0]]
    r, agent = _r6(tmp_path, monkeypatch, plan, declared=None)
    assert "doom_loop" in trips(r, agent)


def test_sl_r6_runaway_steps_still_bounds_a_declared_tool(tmp_path, monkeypatch):
    plan = ([tool("peek", path="a.py"), step()] * 4) + [text("done"), ["exit", 0]]
    r, agent = _r6(tmp_path, monkeypatch, plan, declared=["peek"], max_steps=2)
    assert "runaway_steps" in trips(r, agent), trips(r, agent)
    assert "doom_loop" not in trips(r, agent)


def test_sl_r6_silence_still_bounds_a_declared_tool(tmp_path, monkeypatch):
    plan = [tool("peek", path="a.py")] * 3 + [["sleep", 14], text("done"), ["exit", 0]]
    r, agent = _r6(tmp_path, monkeypatch, plan, declared=["peek"], silence_timeout=1)
    assert "silence" in trips(r, agent), trips(r, agent)
    assert "doom_loop" not in trips(r, agent)


def test_sl_r6_no_provider_tool_name_in_the_package_source():
    """Providers are plugins: no provider tool name — the one this ticket is
    about, or any a shipped provider declares — appears in src/multiagents."""
    names = {"view_file"}
    shipped = yaml.safe_load(SHIPPED_PROVIDERS.read_text()) or {}
    for block in (shipped.get("providers") or {}).values():
        if isinstance(block, dict):
            names.update(str(n) for n in (block.get(DECLARED_KEY) or []))
    offenders = []
    for path in sorted(SRC.glob("*.py")):
        source = path.read_text()
        for name in names:
            if re.search(rf"\b{re.escape(name)}\b", source):
                offenders.append(f"{path.name}: {name}")
    assert not offenders, offenders


# ==========================================================================
# SL-R7 — nodes already stuck on disk are healed
# ==========================================================================

def _old_stuck_node(tree, agent_id="ag-old001"):
    """A node as a pre-fix install left it: stuck, process gone, no ended_at."""
    pid, start = dead_process()
    put_node(tree, agent_id, "stuck",
             "doom_loop: read_it called 5x with identical arguments and nothing "
             "changed on disk", pid=pid, pid_start=start)
    node = tree.get(agent_id)
    assert node.status == "stuck" and node.ended_at is None, "fixture"
    return agent_id


def test_sl_r7_old_stuck_node_frees_its_slot(tmp_path, monkeypatch):
    prov, _ = fake_provider(tmp_path, [[text("fine"), ["exit", 0]]])
    r = make(tmp_path, monkeypatch, {"p": prov}, limits={"max_concurrent": 1})
    _old_stuck_node(r.tree)
    # A fresh Runner over the same tree.json: what a restarted server sees.
    fresh = h.Runner(r.paths, r.config)
    assert fresh.capacity()["running"] == 0, fresh.capacity()

    async def go():
        res = await fresh.start("worker", "go")
        await asyncio.wait_for(fresh.runs[res["agent_id"]].done.wait(), 30)
        return res
    assert asyncio.run(go()).get("agent_id")


def test_sl_r7_wait_does_not_hang_on_an_old_stuck_node(tmp_path, monkeypatch):
    prov, _ = fake_provider(tmp_path, [[text("fine"), ["exit", 0]]])
    r = make(tmp_path, monkeypatch, {"p": prov})
    agent = _old_stuck_node(r.tree)

    started = time.monotonic()
    result = asyncio.run(r.wait_for_any([agent], timeout=10))
    assert time.monotonic() - started < 5, f"waited on a dead node: {result}"
    assert not result.get("timed_out"), result


def test_sl_r7_reconciliation_ends_it_and_keeps_the_trip(tmp_path, monkeypatch):
    import argparse
    import multiagents.cli as cli
    from multiagents.tree import Tree

    for k, v in {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
                 "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}.items():
        monkeypatch.setenv(k, v)
    h.as_root(monkeypatch)
    monkeypatch.setattr(cli, "_confirm", lambda *a, **k: True)
    project = tmp_path / "proj"
    project.mkdir()
    cli.cmd_init(argparse.Namespace(path=str(project), force=False, nested=False))
    paths = cli._resolve(str(project))
    tree = Tree(paths.tree_file, paths.events_file)
    agent = _old_stuck_node(tree)

    monkeypatch.setattr(cli, "_executor_problems", lambda *a: [])
    cli.cmd_resume(argparse.Namespace(path=str(project), no_launch=True, resume=True,
                                      wait=False, unattended=0, team="", supervise=True))

    node = Tree(paths.tree_file, paths.events_file).get(agent)
    assert node.status in TERMINAL, (node.status, node.reason)
    assert node.ended_at is not None
    assert "doom_loop" in node.reason, node.reason
