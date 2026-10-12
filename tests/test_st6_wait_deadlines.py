"""ST6-R1: bounded waits, validation, and non-consuming node delivery.

Only MCP replies are asserted. Time is injected at standard clock/timer
dependencies; the scheduler RPC server, store, runner and tools are real.
The node server is embedded so its monotonic clock can be controlled without
waiting 1500 seconds. Agents are launched by a real scheduler before it is
stopped (NC-R57), and remain gated while the embedded server handles reads.

Decision 8 at 0f71fbc9 specifies effective_timeout as the clamped value in
seconds; alternative field names do not satisfy the contract.
"""
from __future__ import annotations

import asyncio
import threading
import time
from contextlib import contextmanager
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from nc_fixture.world import World, call_tool  # noqa: E402

from multiagents import runner as runner_module, server
from multiagents.scheduler import rpc
from multiagents.scheduler.store import Store, root_capability, transport_directory

WAIT_BOUND = 8
RETURN_BOUND = 2
CAP = 1500


class Clock:
    def __init__(self):
        self.value = time.monotonic()
        self.lock = threading.Lock()
        self.node_blocked = threading.Event()
        self.agent_blocked = threading.Event()
        self.loops = []

    def monotonic(self):
        with self.lock:
            return self.value

    def advance(self, seconds):
        with self.lock:
            self.value += seconds
        for loop in self.loops:
            if not loop.is_closed():
                loop.call_soon_threadsafe(lambda: None)


class ClockTime:
    def __init__(self, clock):
        self.monotonic = clock.monotonic

    def __getattr__(self, name):
        return getattr(time, name)


class ClockAsyncio:
    def __init__(self, clock):
        self.clock = clock

    async def sleep(self, seconds):
        self.clock.agent_blocked.set()
        await asyncio.sleep(seconds)

    def __getattr__(self, name):
        return getattr(asyncio, name)


class ObservedCondition(threading.Condition):
    def __init__(self, clock, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.clock = clock

    def wait(self, timeout=None):
        self.clock.node_blocked.set()
        return super().wait(timeout)


class ClockThreading:
    def __init__(self, clock):
        self.clock = clock

    def Condition(self, *args, **kwargs):
        return ObservedCondition(self.clock, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(threading, name)


class InFlight:
    def __init__(self, fn):
        self.result = None
        self.error = None
        self.done = threading.Event()

        def delivered(result):
            self.result = result
            self.done.set()

        def run():
            try:
                delivered(fn(delivered))
            except BaseException as exc:
                self.error = exc
            finally:
                self.done.set()

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()

    def reply(self):
        assert self.done.wait(RETURN_BOUND), "wait did not return at its total deadline"
        if self.error:
            raise self.error
        return self.result


class WaitWorld:
    def __init__(self, world, clock):
        self.world, self.clock = world, clock
        self.flights = []

    def wait(self, tool, timeout, **fields):
        self.clock.node_blocked.clear()
        self.clock.agent_blocked.clear()

        def invoke(delivered):
            if tool == "wait_for_nodes":
                return call_tool(self.world, tool, timeout=timeout, **fields)

            def loop_factory():
                loop = asyncio.new_event_loop()
                loop.time = self.clock.monotonic
                self.clock.loops.append(loop)
                return loop

            server._reset()  # the existing fixture's MCP construction seam

            async def tool_reply():
                result = await server.wait_for_agents(timeout=timeout, **fields)
                # The observable deadline ends when the public tool returns.
                # asyncio.run's subsequent executor teardown can wait for a
                # cancelled RPC thread; that is fixture cleanup, not the reply.
                delivered(result)
                return result

            return asyncio.run(tool_reply(), loop_factory=loop_factory)

        flight = InFlight(invoke)
        self.flights.append(flight)
        return flight


@contextmanager
def embedded(world, monkeypatch):
    clock = Clock()
    with monkeypatch.context() as patch:
        patch.setattr(rpc, "time", ClockTime(clock))
        patch.setattr(rpc, "threading", ClockThreading(clock))
        patch.setattr(runner_module, "time", ClockTime(clock))
        patch.setattr(runner_module, "asyncio", ClockAsyncio(clock))
        Store(world.root).initialize()
        root_capability(world.root)
        sock = transport_directory(world.root) / "rpc.sock"
        sock.parent.mkdir(parents=True, exist_ok=True)
        sock.unlink(missing_ok=True)
        service = rpc.Service(world.root, "fixture")
        transport = rpc.Server(str(sock), rpc.Handler)
        transport.service = service
        thread = threading.Thread(target=transport.serve_forever,
                                  kwargs={"poll_interval": 0.02}, daemon=True)
        thread.start()
        wait_world = WaitWorld(world, clock)
        try:
            yield wait_world
        finally:
            # A red wait is explicitly released, so no 3000-second RPC or
            # asyncio executor thread survives fixture teardown.
            for loop in list(clock.loops):
                if not loop.is_closed():
                    try:
                        loop.call_soon_threadsafe(
                            lambda loop=loop: [task.cancel() for task in asyncio.all_tasks(loop)])
                    except RuntimeError:
                        pass
            service.stopping = True
            with service.changed:
                service.changed.notify_all()
            for flight in wait_world.flights:
                flight.thread.join(RETURN_BOUND)
            transport.shutdown()
            transport.server_close()
            thread.join(RETURN_BOUND)
            sock.unlink(missing_ok=True)
            server._reset()


@pytest.fixture
def nodes(tmp_path, monkeypatch):
    world = World(tmp_path, monkeypatch)
    world.write_config()
    with embedded(world, monkeypatch) as waits:
        yield waits
    world.close()


@pytest.fixture
def agents(tmp_path, monkeypatch):
    world = World(tmp_path, monkeypatch, tick_seconds=0.1)
    world.start_scheduler(timeout=WAIT_BOUND)
    node = world.simple("live", fx={"gate": "live"})
    running = world.wait_running(node, timeout=WAIT_BOUND)
    world.wait_spawn("live", timeout=WAIT_BOUND)
    world.stop_scheduler(timeout=WAIT_BOUND)
    try:
        with embedded(world, monkeypatch) as waits:
            waits.run_id = running["runs"][-1]["run_id"]
            yield waits
    finally:
        world.gate("live")
        world.close()


@pytest.mark.parametrize("timeout", [1500.001, 1800, 3000])
def test_st6_r1_node_wait_above_the_cap_returns_at_1500_with_clamp_fields(nodes, timeout):
    flight = nodes.wait("wait_for_nodes", timeout)
    assert nodes.clock.node_blocked.wait(RETURN_BOUND), "node wait never began"
    nodes.clock.advance(CAP)
    reply = flight.reply()
    assert not reply.get("error"), reply
    assert reply.get("timeout_clamped") is True, reply
    assert reply.get("effective_timeout") == CAP, reply
    assert reply["transitions"] == [], reply


def test_st6_r1_agent_wait_above_the_cap_returns_at_1500_with_clamp_fields(agents):
    flight = agents.wait("wait_for_agents", 3000, agent_ids=[agents.run_id])
    assert agents.clock.agent_blocked.wait(RETURN_BOUND), "agent wait never began"
    agents.clock.advance(CAP)
    reply = flight.reply()
    assert reply.get("timed_out") is True, reply
    assert reply.get("timeout_clamped") is True, reply
    assert reply.get("effective_timeout") == CAP, reply
    assert agents.run_id in reply.get("still_running", []), reply


@pytest.mark.parametrize("timeout", [0, 300, 1500])
def test_st6_r1_node_timeout_at_or_below_cap_is_unchanged(nodes, timeout):
    flight = nodes.wait("wait_for_nodes", timeout)
    if timeout:
        assert nodes.clock.node_blocked.wait(RETURN_BOUND), "node wait never began"
        nodes.clock.advance(timeout - 1)
        assert not flight.done.wait(0.05), "node timeout shortened by the clamp"
        nodes.clock.advance(1)
    reply = flight.reply()
    assert not reply.get("error"), reply
    assert not reply.get("timeout_clamped"), reply
    assert reply["transitions"] == [], reply


def test_st6_r1_agent_timeout_of_300_is_unchanged(agents):
    flight = agents.wait("wait_for_agents", 300, agent_ids=[agents.run_id])
    assert agents.clock.agent_blocked.wait(RETURN_BOUND), "agent wait never began"
    agents.clock.advance(299)
    assert not flight.done.wait(0.05), "agent timeout shortened by the clamp"
    agents.clock.advance(1)
    reply = flight.reply()
    assert reply.get("timed_out") is True, reply
    assert not reply.get("timeout_clamped"), reply
    assert agents.run_id in reply.get("still_running", []), reply


@pytest.mark.parametrize("tool", ["wait_for_nodes", "wait_for_agents"])
@pytest.mark.parametrize("timeout", [-1, "invalid", None])
def test_st6_r1_invalid_timeouts_are_refused_before_clamping(nodes, tool, timeout):
    # A tool may refuse with its structured invalid result or its established
    # Python ValueError/TypeError boundary. Neither may become a clamped wait.
    flight = nodes.wait(tool, timeout)
    assert flight.done.wait(RETURN_BOUND), "invalid timeout entered a blocking wait"
    if flight.error is not None:
        assert isinstance(flight.error, (ValueError, TypeError)), flight.error
        return
    reply = flight.result
    assert reply.get("error") == "invalid", reply
    assert not reply.get("timeout_clamped"), reply


def test_st6_r1_a_clamped_read_does_not_consume_node_transitions_before_ack(nodes):
    node = nodes.world.simple("selected")
    other = nodes.world.simple("unrelated")
    before = call_tool(nodes.world, "wait_for_nodes", timeout=0)
    assert {node, other} <= {t["node_id"] for t in before["transitions"]}, before
    # Wait beyond those transitions with an explicit cursor, without acking
    # them. Neither clamping nor a filtered timed-out read may advance ack.
    flight = nodes.wait("wait_for_nodes", 3000, cursor=before["next_cursor"], node_ids=[node])
    assert nodes.clock.node_blocked.wait(RETURN_BOUND), "node wait never began"
    nodes.clock.advance(CAP)
    capped = flight.reply()
    assert capped.get("timeout_clamped") is True, capped
    assert capped.get("effective_timeout") == CAP, capped
    first = call_tool(nodes.world, "wait_for_nodes", timeout=0)
    second = call_tool(nodes.world, "wait_for_nodes", timeout=0)
    assert first["transitions"] == before["transitions"], (before, first)
    assert second["transitions"] == first["transitions"], (first, second)
    assert second["next_cursor"] == first["next_cursor"], (first, second)
    ack = call_tool(nodes.world, "ack_nodes", second["next_cursor"])
    assert not ack.get("error"), ack
    after = call_tool(nodes.world, "wait_for_nodes", node_ids=[node], timeout=0)
    assert after["transitions"] == [], after


@pytest.mark.parametrize("timeout", [300, 3000])
def test_st6_r1_combined_agent_and_node_wait_has_one_total_deadline(agents, timeout):
    child = agents.world.simple("unscheduled")
    reply = agents.world.create({"kind": "group", "children": [child]})
    assert reply.get("ok"), reply
    group = reply["result"]["id"]
    cursor = call_tool(agents.world, "wait_for_nodes", timeout=0)["next_cursor"]
    call_tool(agents.world, "ack_nodes", cursor)
    flight = agents.wait("wait_for_agents", timeout, agent_ids=[agents.run_id, group])
    assert agents.clock.agent_blocked.wait(RETURN_BOUND), "combined wait never began"
    deadline = min(timeout, CAP)
    agents.clock.advance(deadline - 1)
    assert not flight.done.wait(0.05), "combined deadline expired too early"
    agents.clock.advance(1)
    result = flight.reply()
    assert result.get("timed_out") is True, result
    if timeout > CAP:
        assert result.get("timeout_clamped") is True, result
        assert result.get("effective_timeout") == CAP, result
    else:
        assert not result.get("timeout_clamped"), result
    assert agents.run_id in result.get("still_running", []), result
