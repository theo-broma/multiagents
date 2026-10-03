"""SR-R1..R5: a steered turn is judged by its own exit. Contract:
`context/specs/steer-exit-race.md`, including "Revision after the advisor's
check", which overrides the earlier wording. Ticket bug-dc522a.

Everything is driven through the public surfaces: the MCP tool functions of
`multiagents.server` (start_agent / steer_agent / check_agent / collect_agent /
agent_tree), a project whose config files the test rewrites, and a fake
provider CLI. The CLI runs under the real launch wrapper, so what a "previous
turn's wrapper writing a late exit status" does is the real thing, not a stub.

The one seam: `multiagents.executor.base.stop_wrapped`, the function the local
executor's handle uses to end a wrapped agent. The contract's "verified by"
asks for a test that forces the predecessor's termination, and its status
write, to be delayed (or never to happen) — there is no way to do that from the
outside. A test using it asserts nothing about HOW the stop is attempted; the
assertions are about what the run, its node and the spawned processes do.
If a reimplementation ends its predecessor some other way, the delay tests
degrade to plain steers and pass; they cannot then fail for the wrong reason.

Observable facts used throughout:
* the fake CLI appends `{argv, pid, t, pred_alive}` to a calls file at start;
  `pred_alive` lists the pids of earlier invocations still alive at that
  moment — "launched beside a live predecessor" is a recorded fact;
* a node's status / reason / elapsed_seconds as check_agent reports them.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import stat
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import pc_harness as pc  # noqa: E402
import sc_harness as sc  # noqa: E402

_SCRIPT = r'''#!{python}
import json, os, sys, time
base, name = {base!r}, {name!r}
argv = sys.argv[1:]
prompt = sys.stdin.read()    # C3: the shipped transport delivers the prompt on stdin
def _alive(pid):
    try:
        os.kill(pid, 0)
        return open("/proc/%d/stat" % pid).read().split(")")[-1].split()[0] != "Z"
    except OSError:
        return False
try:
    earlier = [json.loads(x)["pid"] for x in open(base + ".calls").read().splitlines() if x]
except OSError:
    earlier = []
with open(base + ".calls", "a") as f:
    f.write(json.dumps({{"argv": argv, "prompt": prompt, "pid": os.getpid(), "t": time.time(),
                        "pred_alive": [p for p in earlier if _alive(p)]}}) + "\n")
ctl = json.load(open(base + ".ctl.json"))
text = " ".join(argv) + " " + prompt
for key, variant in ctl.get("variants", {{}}).items():
    if key in text:
        ctl = dict(ctl, **variant)
        break
session = argv[argv.index("-s") + 1] if "-s" in argv else "ses_%s_%d" % (name, os.getpid())
def emit(kind, part):
    print(json.dumps({{"type": kind, "sessionID": session,
                      "part": dict(part, sessionID=session)}})); sys.stdout.flush()
for i in range(ctl.get("steps", 1)):
    emit("step_start", {{"id": "prt_s%d_%d" % (os.getpid(), i), "type": "step-start"}})
    emit("step_finish", {{"id": "prt_f%d_%d" % (os.getpid(), i), "type": "step-finish",
                         "reason": "tool-calls", "cost": 0,
                         "tokens": {{"input": 1, "output": 1, "reasoning": 0,
                                    "cache": {{"read": 0, "write": 0}}}}}})
if ctl.get("talk", True):
    emit("text", {{"id": "prt_u%d" % os.getpid(), "type": "text", "text": "working"}})
gate = ctl.get("gate")
if gate:
    while not os.path.exists(gate):
        time.sleep(0.05)
if ctl.get("silent"):
    sys.exit(ctl.get("exit", 0))
emit("text", {{"id": "prt_t%d" % os.getpid(), "type": "text", "text": ctl.get("text", "done")}})
sys.exit(ctl.get("exit", 0))
'''


class Fake:
    """A fake opencode-style CLI: blocks on a gate, then answers and exits."""

    def __init__(self, tmp: Path, name: str):
        self.name = name
        self.base = str(tmp / f"{name}.sr")
        self.gate = tmp / f"{name}.srgate"
        script = tmp / f"{name}-sr.py"
        script.write_text(_SCRIPT.format(python=sys.executable, base=self.base, name=name))
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        Path(self.base + ".calls").write_text("")
        entry = sc.shipped_opencode()
        for key in ("auth", "mcp", "home_links", "bin_search", "models_cmd",
                    "models_parse", "models_include", "notes"):
            entry.pop(key, None)
        entry["bin"] = str(script)
        entry["models_include"] = [f"{name}/*"]
        self.entry = entry
        self.ctl = {"steps": 1, "text": "done", "exit": 0, "variants": {},
                    "gate": str(self.gate)}
        self.close()

    def _write(self) -> None:
        Path(self.base + ".ctl.json").write_text(json.dumps(self.ctl))

    def close(self) -> None:
        self.gate.unlink(missing_ok=True)
        self._write()

    def open(self) -> None:
        self.gate.write_text("open")

    def set(self, **kw) -> None:
        self.ctl.update(kw)
        self._write()

    def variant(self, key: str, **kw) -> None:
        self.ctl["variants"][key] = kw
        self._write()

    def calls(self) -> list[dict]:
        return [json.loads(x) for x in Path(self.base + ".calls").read_text().splitlines() if x]

    def spawns(self) -> int:
        return len(self.calls())

    def pids(self) -> list[int]:
        return [c["pid"] for c in self.calls()]


@pytest.fixture
def w(tmp_path, monkeypatch):
    from multiagents.runner import Runner
    monkeypatch.setattr(Runner, "WATCH_POLL_SECONDS", 0.25)    # production: 5 s
    world = sc.World(tmp_path, monkeypatch)
    fake = Fake(tmp_path, "acme")
    world.p.fakes["acme"] = fake
    world.p.providers["acme"] = fake.entry
    world.g = fake
    yield world
    fake.open()
    for pid in fake.pids():
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    world.down()


def up(w, **agent):
    agent.setdefault("timeout", 900)
    w.agent("worker", "acme", "acme/m1", **agent)
    w.up()


async def running(w, task="job", **kw):
    """A started run, its first CLI alive, its session id captured."""
    aid = await w.started("worker", task, **kw)
    assert await pc.await_until(lambda: w.g.spawns() == 1, 30)
    assert await pc.await_until(lambda: bool(w.node(aid).session_id), 30)
    return aid


def run_dir(w, aid) -> Path:
    return w.runner.paths.run_dir(aid)


def wrapper_pid(w, aid) -> int:
    return int((run_dir(w, aid) / "wrapper.pid").read_text().split()[0])


async def restart(w):
    """A server restart as the next server sees it: the old one lets go of its
    runs without ending them, a new one is built from disk and adopts them."""
    await w.runner.shutdown(detach=True)
    w.restart_server()
    await w.runner.adopt()


def refusal_text(r: dict) -> str:
    return " ".join(str(r.get(k, "")) for k in ("error", "reason")).strip()


def slow_stop(monkeypatch, victim_pid: int, delay: float, kill: bool = True) -> dict:
    """The predecessor's termination is delayed: the stop of a wrapped agent
    returns at once, and `victim_pid` (the agent) gets its SIGTERM `delay`
    seconds later — so the old wrapper writes `-15` that much later. With
    `kill=False` it is never ended at all."""
    from multiagents.executor import base
    box = {"calls": 0}

    def stop(run_dir, pid, start="", grace=3.0):
        box["calls"] += 1
        if kill:
            def later():
                time.sleep(delay)
                try:
                    os.kill(victim_pid, signal.SIGTERM)
                except OSError:
                    pass
            threading.Thread(target=later, daemon=True).start()
        return True
    monkeypatch.setattr(base, "stop_wrapped", stop)
    return box


def alive_wrapper(pid: int) -> bool:
    return pc.alive(pid)


async def stays(pred, seconds: float, step: float = 0.1) -> bool:
    """True when `pred()` held at every look during `seconds`."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if not pred():
            return False
        await asyncio.sleep(step)
    return pred()


# ================================================================= SR-R1 ====

# Longer than the two 2 s settle windows today's stop waits through, so the old
# wrapper's write lands after a launch that did not wait for it.
DELAY = 6.5


def test_sr_r1_a_late_exit_status_of_the_previous_turn_does_not_finalize_the_new_turn(w, monkeypatch):
    up(w)
    box = {}

    async def go():
        aid = await running(w)
        old_agent, old_wrapper = w.g.pids()[0], wrapper_pid(w, aid)
        box["stop"] = slow_stop(monkeypatch, old_agent, DELAY)
        r = await w.server.steer_agent(aid, "next: carry on")
        assert r.get("steered") is True, r
        # the previous turn's wrapper writes its -15 at some point from here
        assert await pc.await_until(lambda: not alive_wrapper(old_wrapper), 30), \
            "the previous turn's wrapper never ended"
        held = await stays(lambda: w.status(aid) in ("running", "pending"), 3.0)
        return aid, held, w.server.check_agent(aid)
    aid, held, view = asyncio.run(go())
    assert held, f"the new turn was ended by the previous turn's exit: {view}"
    assert "-15" not in str(view.get("reason", "")), view


def test_sr_r1_the_new_turn_finishes_on_its_own_exit_after_the_late_write(w, monkeypatch):
    up(w)
    w.g.variant("own: ", exit=5)

    async def go():
        aid = await running(w)
        old_wrapper = wrapper_pid(w, aid)
        slow_stop(monkeypatch, w.g.pids()[0], DELAY)
        r = await w.server.steer_agent(aid, "own: go")
        assert r.get("steered") is True, r
        assert await pc.await_until(lambda: not alive_wrapper(old_wrapper), 30)
        await asyncio.sleep(2.0)
        w.g.open()
        states = await w.until(aid, timeout=30)
        return aid, states[aid]
    aid, state = asyncio.run(go())
    reason = str(w.server.check_agent(aid).get("reason", ""))
    assert state == "failed", (state, reason)
    assert "-15" not in reason, f"judged by the predecessor's exit: {reason}"
    assert "5" in reason, f"the new turn's own exit code is not reported: {reason}"


def test_sr_r1_a_new_turn_that_exits_zero_is_done_not_failed_by_the_old_status(w, monkeypatch):
    up(w)

    async def go():
        aid = await running(w)
        old_wrapper = wrapper_pid(w, aid)
        slow_stop(monkeypatch, w.g.pids()[0], DELAY)
        r = await w.server.steer_agent(aid, "next: carry on")
        assert r.get("steered") is True, r
        assert await pc.await_until(lambda: not alive_wrapper(old_wrapper), 30)
        await asyncio.sleep(2.0)
        w.g.open()
        return aid, (await w.until(aid, timeout=30))[aid]
    aid, state = asyncio.run(go())
    assert state == "done", (state, w.server.check_agent(aid))


def test_sr_r1_the_late_write_after_a_server_restart_does_not_finalize_the_adopted_turn(w, monkeypatch):
    """The identity of a turn survives a restart and the adopt/follow path."""
    up(w)

    async def go():
        aid = await running(w)
        old_wrapper = wrapper_pid(w, aid)
        slow_stop(monkeypatch, w.g.pids()[0], DELAY)
        r = await w.server.steer_agent(aid, "next: carry on")
        assert r.get("steered") is True, r
        await restart(w)
        assert await pc.await_until(lambda: not alive_wrapper(old_wrapper), 30)
        held = await stays(lambda: w.status(aid) in ("running", "pending"), 3.0)
        view = w.server.check_agent(aid)
        w.g.open()
        final = (await w.until(aid, timeout=30))[aid]
        return held, view, final
    held, view, final = asyncio.run(go())
    assert held, f"the adopted turn was ended by the previous turn's exit: {view}"
    assert final == "done", final


@pytest.mark.skip(reason="needs a docker daemon: the docker executor's status-file unlink "
                         "(executor/docker.py ~2504) and its container-side wrapper cannot "
                         "be driven without one; the contract (SR-R1 Docker) is the same "
                         "scenario as the local tests above")
def test_sr_r1_docker_late_status_of_the_previous_turn_is_ignored():
    pass


# ================================================================= SR-R2 ====

def test_sr_r2_when_steer_returns_the_predecessor_is_dead_or_the_steer_was_refused(w, monkeypatch):
    up(w)

    async def go():
        aid = await running(w)
        old = w.g.pids()[0]
        slow_stop(monkeypatch, old, DELAY)
        r = await w.server.steer_agent(aid, "next: carry on")
        return old, r, pc.alive(old)
    old, r, old_alive = asyncio.run(go())
    if r.get("steered") is True:
        assert not old_alive, "steer returned success beside a live predecessor"
        assert w.g.calls()[1]["pred_alive"] == [], "the new turn started beside a live predecessor"
    else:
        assert refusal_text(r), f"a refused steer says why: {r}"
        assert w.g.spawns() == 1, "a refused steer launched anyway"


def test_sr_r2_the_new_turn_never_launches_beside_a_live_predecessor(w, monkeypatch):
    up(w)

    async def go():
        aid = await running(w)
        slow_stop(monkeypatch, w.g.pids()[0], DELAY)
        r = await w.server.steer_agent(aid, "next: carry on")
        await asyncio.sleep(1.0)
        return r
    r = asyncio.run(go())
    for call in w.g.calls()[1:]:
        assert call["pred_alive"] == [], f"launched beside {call['pred_alive']}"


def test_sr_r2_a_predecessor_that_cannot_be_ended_gets_a_refusal_not_a_second_process(w, monkeypatch):
    up(w)

    async def go():
        aid = await running(w)
        old = w.g.pids()[0]
        slow_stop(monkeypatch, old, 0, kill=False)       # the stop never takes effect
        r = await asyncio.wait_for(w.server.steer_agent(aid, "next: carry on"), 120)
        return aid, old, r
    aid, old, r = asyncio.run(go())
    if r.get("steered") is True:
        # a bounded grace elapsed and the steer itself killed them
        assert not pc.alive(old), "steered beside a predecessor that is still alive"
        assert w.g.calls()[1]["pred_alive"] == []
    else:
        assert refusal_text(r), f"a refusal names its reason: {r}"
        assert w.g.spawns() == 1, "refused, yet a second process was launched"
        assert pc.alive(old), "refused, but the predecessor was left half-killed"
        assert w.status(aid) in ("running", "pending", "stuck"), w.status(aid)


def test_sr_r2_a_refused_steer_does_not_leave_a_phantom_slot(w, monkeypatch):
    """The refusal goes through the existing release path (SF): nothing it
    claimed is left held."""
    up(w)

    async def go():
        aid = await running(w)
        old = w.g.pids()[0]
        slow_stop(monkeypatch, old, 0, kill=False)
        r = await asyncio.wait_for(w.server.steer_agent(aid, "next: carry on"), 120)
        return r
    r = asyncio.run(go())
    if r.get("steered") is True:
        pytest.skip("this implementation killed the straggler itself (bounded grace)")
    assert w.runner.startup.availability("acme") is None
    assert w.runner.provider_slots()["acme"]["in_use"] <= 1


@pytest.mark.skip(reason="needs a docker daemon: 'death' there means the container-side wrapper "
                         "and agent, not the host `docker exec` client")
def test_sr_r2_docker_predecessor_death_is_the_container_side_process():
    pass


@pytest.mark.skip(reason="contract: unknown liveness means refusal — a liveness probe that "
                         "cannot answer needs a fault injected into the executor's probe, "
                         "which has no public seam")
def test_sr_r2_unknown_liveness_is_a_refusal():
    pass


# ================================================================= SR-R3 ====

def limits_reported(w, r: dict, since: int) -> dict:
    """The `effective_limits` a steer reported: in its result, else the last
    one in an event recorded since `since`."""
    found = sc.find_key(r, "effective_limits")
    for record in w.p.event_records()[since:]:
        found.extend(sc.find_key(record, "effective_limits"))
    assert found, f"neither the steer result nor its events report effective_limits: {r}"
    return found[-1]


async def steer_after_config(w, edit, message="next: go"):
    aid = await running(w)
    before = len(w.p.event_records())
    edit()
    w.reload()
    r = await w.server.steer_agent(aid, message)
    assert r.get("steered") is True, r
    return aid, r, before


def test_sr_r3_timeout_is_re_resolved_from_the_agent_config_at_the_steer(w):
    up(w, timeout=900)

    def edit():
        w.p.agents["worker"]["timeout"] = 2700
    _, r, before = asyncio.run(steer_after_config(w, edit))
    limits = limits_reported(w, r, before)
    assert limits["timeout"]["value"] == 2700, limits
    assert limits["timeout"]["source"] == "agent", limits


def test_sr_r3_silence_timeout_is_re_resolved_at_the_steer(w):
    up(w, silence_timeout=100)

    def edit():
        w.p.agents["worker"]["silence_timeout"] = 250
    _, r, before = asyncio.run(steer_after_config(w, edit))
    assert limits_reported(w, r, before)["silence_timeout"]["value"] == 250


def test_sr_r3_the_layering_is_agent_then_project_then_default(w):
    up(w, timeout=900)
    w.p.project["limits"]["default_timeout"] = 1200

    def edit():
        del w.p.agents["worker"]["timeout"]          # the agent no longer sets it
    _, r, before = asyncio.run(steer_after_config(w, edit))
    limits = limits_reported(w, r, before)
    assert limits["timeout"]["value"] == 1200, limits
    assert limits["timeout"]["source"] == "project", limits


def test_sr_r3_a_limit_the_config_no_longer_sets_falls_back_to_the_default(w):
    up(w, timeout=900)

    def edit():
        del w.p.agents["worker"]["timeout"]
    _, r, before = asyncio.run(steer_after_config(w, edit))
    limits = limits_reported(w, r, before)
    assert limits["timeout"]["value"] == 900, limits       # the built-in default
    assert limits["timeout"]["source"] in ("builtin", "default"), limits


def test_sr_r3_an_explicit_timeout_given_to_start_agent_keeps_priority_over_config(w):
    """Contract: 'a per-run timeout given explicitly to start_agent keeps
    priority over config'. Read as: it survives the steer."""
    up(w, timeout=900)

    async def go():
        aid = await running(w, timeout=60)
        before = len(w.p.event_records())
        w.p.agents["worker"]["timeout"] = 2700
        w.reload()
        r = await w.server.steer_agent(aid, "next: go")
        assert r.get("steered") is True, r
        return r, before
    r, before = asyncio.run(go())
    limits = limits_reported(w, r, before)
    assert limits["timeout"]["value"] == 60, limits
    assert limits["timeout"]["source"] == "call", limits


def test_sr_r3_a_raised_timeout_governs_the_steered_turn_and_its_clock_starts_at_the_steer(w):
    """Behaviourally: the run was stopped by its 3 s wall clock; the config now
    says 6 s. The node is by then older than 6 s, so a clock from the node's
    start trips at once, and the old 3 s trips after 3 s. Neither may happen
    within 4.5 s of the steer."""
    up(w, timeout=3)

    async def go():
        aid = await running(w)
        await w.until(aid, timeout=30, states={"stuck"})
        await asyncio.sleep(5)                       # age now well past 6 s
        w.p.agents["worker"]["timeout"] = 6
        w.reload()
        r = await w.server.steer_agent(aid, "next: go")
        assert r.get("steered") is True, r
        quiet = await stays(lambda: w.status(aid) in ("running", "pending"), 4.5)
        return quiet, w.server.check_agent(aid)
    quiet, view = asyncio.run(go())
    assert quiet, f"the steered turn tripped before its own 6 s: {view}"


def test_sr_r3_a_raised_max_steps_governs_the_steered_turn(w):
    up(w, max_steps=2)
    w.g.set(steps=5)

    async def go():
        aid = await running(w)
        await w.until(aid, timeout=30, states={"stuck"})
        w.p.agents["worker"]["max_steps"] = 80
        w.reload()
        r = await w.server.steer_agent(aid, "next: go")
        assert r.get("steered") is True, r
        quiet = await stays(lambda: w.status(aid) in ("running", "pending"), 4.0)
        return quiet, w.server.check_agent(aid)
    quiet, view = asyncio.run(go())
    assert quiet, f"the steered turn kept the old max_steps: {view}"


def test_sr_r3_model_and_provider_stay_as_the_run_was_started(w):
    up(w)

    async def go():
        aid = await running(w)
        w.p.agents["worker"]["model"] = "acme/m2"
        w.reload()
        r = await w.server.steer_agent(aid, "next: go")
        assert r.get("steered") is True, r
        assert await pc.await_until(lambda: w.g.spawns() == 2, 30)
    asyncio.run(go())
    argv = w.g.calls()[1]["argv"]
    assert "acme/m1" in argv and "acme/m2" not in argv, argv


def test_sr_r3_model_stays_frozen_across_a_server_restart(w):
    up(w)

    async def go():
        aid = await running(w)
        await restart(w)
        w.p.agents["worker"]["model"] = "acme/m2"
        w.reload()
        r = await w.server.steer_agent(aid, "next: go")
        assert r.get("steered") is True, r
        assert await pc.await_until(lambda: w.g.spawns() == 2, 30)
    asyncio.run(go())
    argv = w.g.calls()[1]["argv"]
    assert "acme/m1" in argv and "acme/m2" not in argv, \
        f"an adopted run was rebuilt from the current config: {argv}"


def _assert_refused_and_untouched(w, aid, old, r):
    assert r.get("steered") is not True, r
    assert refusal_text(r), f"a refusal says why: {r}"
    assert pc.alive(old), "the predecessor was stopped for a steer that was refused"
    assert w.g.spawns() == 1, "a replacement was spawned"


def test_sr_r3_an_invalid_config_refuses_the_steer_before_the_predecessor_is_stopped(w):
    up(w)

    async def go():
        aid = await running(w)
        old = w.g.pids()[0]
        w.p.write_raw("agents.yaml", "agents: [unclosed\n  worker: {")
        r = await w.server.steer_agent(aid, "next: go")
        await asyncio.sleep(1.0)
        _assert_refused_and_untouched(w, aid, old, r)
        assert w.status(aid) in ("running", "pending"), w.status(aid)
    asyncio.run(go())


def test_sr_r3_a_missing_config_file_refuses_the_steer_before_the_predecessor_is_stopped(w):
    up(w)

    async def go():
        aid = await running(w)
        old = w.g.pids()[0]
        (w.p.config / "agents.yaml").unlink()
        r = await w.server.steer_agent(aid, "next: go")
        await asyncio.sleep(1.0)
        _assert_refused_and_untouched(w, aid, old, r)
    asyncio.run(go())


def test_sr_r3_an_agent_the_config_no_longer_defines_refuses_the_steer_untouched(w):
    up(w)

    async def go():
        aid = await running(w)
        old = w.g.pids()[0]
        w.p.agents.pop("worker")
        w.reload()
        r = await w.server.steer_agent(aid, "next: go")
        await asyncio.sleep(1.0)
        _assert_refused_and_untouched(w, aid, old, r)
    asyncio.run(go())


@pytest.mark.skip(reason="the `models.<route>` limit layer needs a run routed to a fallback "
                         "provider; the contract names the layer but no public way to force "
                         "the route in a fake world without the routing suite's budget stubs")
def test_sr_r3_a_limit_in_the_models_route_entry_is_part_of_the_resolution():
    pass


@pytest.mark.skip(reason="'queueing and cleanup before the launch do not count against the "
                         "wall clock' has no observable at second granularity without "
                         "injecting a delay inside steer")
def test_sr_r3_the_clock_starts_when_the_turn_launches_not_when_the_steer_is_queued():
    pass


# ================================================================= SR-R4 ====

AGE = 7


async def aged_then_steered_to_failure(w):
    aid = await running(w)
    assert await pc.await_until(
        lambda: w.server.check_agent(aid).get("elapsed_seconds", 0) >= AGE, AGE + 20, 0.25)
    # whatever starts from here on dies at once, saying nothing (the free
    # retry of such a death dies the same way)
    w.g.set(gate=None, exit=3, silent=True, talk=False)
    r = await w.server.steer_agent(aid, "failfast: now")
    return aid, r


def test_sr_r4_a_steered_turn_that_fails_at_once_reports_seconds_not_the_nodes_age(w):
    up(w)

    async def go():
        aid, _ = await aged_then_steered_to_failure(w)
        await w.until(aid, timeout=30, states={"failed"})
        return aid, w.server.check_agent(aid), w.server.collect_agent(aid)
    aid, view, collected = asyncio.run(go())
    assert view["elapsed_seconds"] <= 4, f"the node's age, not the turn's: {view}"
    assert collected["elapsed_seconds"] <= 4, collected


def test_sr_r4_the_no_output_text_describes_the_turn(w):
    up(w)

    async def go():
        aid, _ = await aged_then_steered_to_failure(w)
        await w.until(aid, timeout=30, states={"failed"})
        collected = w.server.collect_agent(aid)
        result = run_dir(w, aid) / "result.json"
        return [*sc.strings(collected), result.read_text() if result.is_file() else ""]
    texts = asyncio.run(go())
    found = [int(m.group(1)) for t in texts for m in re.finditer(r"\[no output\][^\n]*? after (\d+)s", t)]
    assert found, f"no '[no output] ... after Ns' text found: {texts}"
    assert all(n <= 4 for n in found), f"the text reports the node's age: {found}"


def test_sr_r4_the_nodes_age_is_reported_under_its_own_name(w):
    up(w)

    async def go():
        aid, _ = await aged_then_steered_to_failure(w)
        await w.until(aid, timeout=30, states={"failed"})
        return w.server.check_agent(aid), w.server.collect_agent(aid)
    view, collected = asyncio.run(go())
    for where, data in (("check_agent", view), ("collect_agent", collected)):
        assert data.get("node_elapsed_seconds", 0) >= AGE, (where, data)


def test_sr_r4_a_running_steered_turn_reports_its_own_elapsed_and_the_node_age_separately(w):
    up(w)

    async def go():
        aid = await running(w)
        assert await pc.await_until(
            lambda: w.server.check_agent(aid).get("elapsed_seconds", 0) >= AGE, AGE + 20, 0.25)
        r = await w.server.steer_agent(aid, "next: go")
        assert r.get("steered") is True, r
        return aid, w.server.check_agent(aid), w.server.agent_tree()
    aid, view, tree = asyncio.run(go())
    assert view["elapsed_seconds"] <= 4, view
    assert view["node_elapsed_seconds"] >= AGE, view
    row = next(n for n in tree["active"] if n["agent_id"] == aid)
    assert row.get("node_elapsed_seconds", 0) >= AGE, f"listing lacks the node's age: {row}"
    assert row.get("elapsed_seconds", 0) <= 4 or "elapsed_seconds" not in row, row


def test_sr_r4_an_unsteered_run_reports_the_same_for_both(w):
    up(w)

    async def go():
        aid = await running(w)
        await asyncio.sleep(2)
        return w.server.check_agent(aid)
    view = asyncio.run(go())
    assert abs(view["elapsed_seconds"] - view["node_elapsed_seconds"]) <= 1, view


def test_sr_r4_a_finished_turns_duration_is_frozen_and_survives_a_restart(w):
    up(w)

    async def go():
        aid, _ = await aged_then_steered_to_failure(w)
        await w.until(aid, timeout=30, states={"failed"})
        first = w.server.check_agent(aid)
        await asyncio.sleep(3.2)
        second = w.server.check_agent(aid)
        await restart(w)
        third = w.server.check_agent(aid)
        return first, second, third
    first, second, third = asyncio.run(go())
    for key in ("elapsed_seconds", "node_elapsed_seconds"):
        assert abs(second[key] - first[key]) <= 1, (key, first, second)
        assert abs(third[key] - first[key]) <= 1, (key, first, third)


@pytest.mark.skip(reason="consult sessions also resume through `_launch` (SR-R1/R2/R4 apply); "
                         "a conversational consult needs its own harness world and is left "
                         "to the consult suite's fixtures — not written here")
def test_sr_r4_consult_reports_the_turns_elapsed():
    pass


# ================================================================= SR-R5 ====
# These pass before and after the change: behaviour apart from SR-R3's limits
# is as it was. The SF refusal path is covered by tests/test_sf_followups.py.

def test_sr_r5_steering_a_done_run_resumes_it(w):
    up(w)
    w.g.open()

    async def go():
        aid = await running(w)
        await w.until(aid, timeout=30, states={"done"})
        r = await w.server.steer_agent(aid, "next: again")
        assert r.get("steered") is True, r
        assert await pc.await_until(lambda: w.g.spawns() == 2, 30)
        return aid, (await w.until(aid, timeout=30, states={"done"}))[aid]
    aid, state = asyncio.run(go())
    assert state == "done"
    assert "next: again" in w.g.calls()[1]["prompt"]


def test_sr_r5_steering_a_failed_run_resumes_it(w):
    up(w)
    w.g.set(gate=None, exit=3, silent=True, talk=False)

    async def go():
        aid = await running(w)
        await w.until(aid, timeout=30, states={"failed"})
        w.g.set(gate=None, exit=0, silent=False)
        r = await w.server.steer_agent(aid, "next: retry")
        assert r.get("steered") is True, r
        return aid, (await w.until(aid, timeout=30, states={"done"}))[aid]
    aid, state = asyncio.run(go())
    assert state == "done"


def test_sr_r5_a_need_info_answer_resumes_the_run_with_the_answer(w):
    up(w)
    w.g.set(gate=None, text="NEED_INFO: which database?")

    async def go():
        aid = await running(w)
        await w.until(aid, timeout=30)
        assert w.server.collect_agent(aid)["need_info"], "the question was not surfaced"
        w.g.set(gate=None, text="ok")
        r = await w.server.steer_agent(aid, "next: postgres")
        assert r.get("steered") is True, r
        assert await pc.await_until(lambda: w.g.spawns() == 2, 30)
        return aid, (await w.until(aid, timeout=30, states={"done"}))[aid]
    aid, state = asyncio.run(go())
    assert state == "done"
    assert "next: postgres" in w.g.calls()[1]["prompt"]


def test_sr_r5_steering_a_live_run_still_ends_its_predecessor_and_resumes_the_session(w):
    up(w)

    async def go():
        aid = await running(w)
        old = w.g.pids()[0]
        r = await w.server.steer_agent(aid, "next: go")
        assert r.get("steered") is True, r
        assert await pc.await_until(lambda: not pc.alive(old), 30)
        w.g.open()
        return aid, (await w.until(aid, timeout=30, states={"done"}))[aid]
    aid, state = asyncio.run(go())
    assert state == "done"
    argv = w.g.calls()[1]["argv"]
    assert w.node(aid).session_id in argv, argv
