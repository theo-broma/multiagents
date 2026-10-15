"""bug-d3bf10: timers survive failed probes and orphaned tool calls."""
import asyncio
import contextlib
import threading
from types import SimpleNamespace

import pytest

from multiagents import runner as runner_module
from multiagents import supervisor as supervisor_module
from multiagents.providers import Event
from multiagents.runner import Runner
from multiagents.supervisor import Supervisor


@pytest.fixture
def clock(monkeypatch):
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(supervisor_module, "time", SimpleNamespace(monotonic=lambda: clock.now))
    return clock


def supervisor(clock, **kwargs):
    return Supervisor(started=clock.now, last_event=clock.now, silence_timeout=10,
                      wall_timeout=kwargs.pop("wall_timeout", 1000), **kwargs)


@pytest.mark.parametrize("mode", ["raises", "blocks", "empty"])
@pytest.mark.parametrize("target", ["worktree", "descendants"])
def test_r1_r2_watchdog_checks_timers_despite_probe_failure(monkeypatch, clock, tmp_path, mode, target):
    """A blocked probe stays single-flight across ticks; failures grant no life."""
    release = threading.Event()
    calls = []

    def progress(*args):
        calls.append(True)
        if mode == "blocks":
            release.wait(5)
        if mode == "empty":
            return ""
        raise TimeoutError("git status timed out")

    monkeypatch.setattr(runner_module, "_worktree_state",
                        progress if target == "worktree" else lambda *args: "unchanged")

    async def check():
        sleepers = asyncio.Queue()

        async def sleep(seconds):
            future = asyncio.get_running_loop().create_future()
            await sleepers.put(future)
            await future

        class AsyncioView:
            def __getattr__(self, name):
                return getattr(asyncio, name)
        view = AsyncioView()
        view.sleep = sleep
        monkeypatch.setattr(runner_module, "asyncio", view)
        events = []
        sup = supervisor(clock, wall_timeout=30)
        run = SimpleNamespace(supervisor=sup, node_id="worker", spec=None)
        tree = SimpleNamespace(get=lambda _: SimpleNamespace(worktree=str(tmp_path)),
                               emit=lambda _, kind, **data: events.append((kind, data)),
                               set_status=lambda *args: None)
        runner = SimpleNamespace(tree=tree, paths=SimpleNamespace(root=tmp_path),
                                 WATCH_POLL_SECONDS=.02,
                                 executor=lambda _: SimpleNamespace(has_live_descendants=(
                                     progress if target == "descendants" else lambda _: False)),
                                 _shutdown_preserves=lambda _: False, _trip_notice=lambda *args: None)
        task = asyncio.create_task(Runner._watch_timers(runner, run))
        waiter = await asyncio.wait_for(sleepers.get(), 2)

        async def tick(now):
            nonlocal waiter
            clock.now = now
            waiter.set_result(None)
            waiter = await asyncio.wait_for(sleepers.get(), 2)

        try:
            await tick(11)
            await tick(12)
            assert [data["reason"] for kind, data in events if kind == "stuck"] == ["silence"]
            await tick(31)
            assert [data["reason"] for kind, data in events if kind == "stuck"] == ["silence", "timeout"]
            assert sup.last_event == 0
            if mode == "blocks":
                assert len(calls) == 1
        finally:
            release.set()
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
    asyncio.run(check())


def test_r1_wall_check_precedes_all_probes(monkeypatch, clock, tmp_path):
    async def check():
        sup = supervisor(clock, wall_timeout=1)
        clock.now = 2
        seen = []
        run = SimpleNamespace(supervisor=sup, node_id="worker", spec=None)
        runner = SimpleNamespace(
            tree=SimpleNamespace(get=lambda _: SimpleNamespace(worktree=str(tmp_path)),
                                 set_status=lambda *args: None,
                                 emit=lambda _, kind, **data: seen.append(data.get("reason"))),
            paths=SimpleNamespace(root=tmp_path), WATCH_POLL_SECONDS=.001,
            _shutdown_preserves=lambda _: False,
            _trip_notice=lambda *args: None)

        at_probe = []

        def executor(_):
            at_probe.append(list(seen))
            raise asyncio.CancelledError
        runner.executor = executor
        with pytest.raises(asyncio.CancelledError):
            await Runner._watch_timers(runner, run)
        assert at_probe == [["timeout"]]
    asyncio.run(check())


@pytest.mark.parametrize("terminal", [Event(kind="result", status="failed"),
                                      Event(kind="result", status="ERROR"),
                                      Event(kind="error", status="failed"),
                                      Event(kind="error", state="failed"),
                                      Event(kind="result", status="success")])
def test_r3_terminal_turn_clears_all_pending_tools(clock, terminal):
    sup = supervisor(clock)
    for tool_id in ("one", "two", "", ""):
        sup.observe(Event(kind="tool", name="command", state="in_progress", tool_id=tool_id))
    clock.now = 11
    assert sup.check_timers() is None
    assert sup.activity_evidence
    clock.now = 12
    sup.observe(terminal)
    assert not sup.activity_evidence
    clock.now = 23
    assert sup.check_timers() is None
    assert sup.check_timers().reason == "silence"


def test_r3_failure_clears_only_its_tagged_turn(clock):
    sup = supervisor(clock)
    for turn in ("a", "b"):
        sup.observe(Event(kind="tool", name="command", state="running", tool_id=turn, turn=turn))
    sup.observe(Event(kind="result", status="failed", turn="a"))
    assert sup.activity_evidence == "open tool call (b)"
    sup.observe(Event(kind="result", status="failed", turn="b"))
    assert not sup.activity_evidence


@pytest.mark.parametrize("tool_id", ["command", ""])
def test_r4_pending_tool_expires_after_silence_plus_bounded_grace(clock, tool_id):
    sup = supervisor(clock)
    event = Event(kind="tool", name="command", state="running", tool_id=tool_id)
    sup.observe(event)
    clock.now = 309
    assert sup.check_timers() is None
    assert sup.activity_evidence
    clock.now = 311
    assert not sup.activity_evidence
    assert sup.check_timers() is None
    assert sup.check_timers().reason == "silence"


def test_r4_pending_tool_window_capped_by_wall_budget(clock):
    sup = supervisor(clock, wall_timeout=20)
    sup.observe(Event(kind="tool", name="command", state="running", tool_id="a"))
    clock.now = 21
    assert not sup.activity_evidence
    assert sup.check_timers().reason == "timeout"


def test_r4_lifecycle_updates_do_not_renew_pending_window(clock):
    sup = supervisor(clock)
    event = Event(kind="tool", name="command", state="running", tool_id="a")
    sup.observe(event)
    clock.now = 300
    sup.observe(event)
    clock.now = 311
    assert not sup.activity_evidence


@pytest.mark.parametrize("kind", ["raw", "text", "step", "error", "result", "tool"])
def test_r5_every_parsed_event_resets_silence(clock, kind):
    sup = supervisor(clock)
    clock.now = 9
    sup.observe(Event(kind=kind))
    clock.now = 18
    assert sup.check_timers() is None
    assert sup.last_event == 9


def test_r5_running_tool_and_retry_notice_preserve_normal_suppression(clock):
    sup = supervisor(clock)
    sup.observe(Event(kind="tool", name="command", state="in_progress", tool_id="a"))
    sup.observe(Event(kind="tool", name="command", state="in_progress", tool_id="b"))
    clock.now = 50
    sup.observe(Event(kind="error", state="failed", tool_id="b"))
    sup.observe(Event(kind="error", text="retrying connection"))
    clock.now = 100
    assert sup.check_timers() is None
    assert sup.activity_evidence


def test_r3_codex_turn_failed_without_tool_id_releases_pending_command(clock):
    import json
    from pathlib import Path

    import yaml
    from multiagents.defaults.providers.codex import Normalizer
    from multiagents.providers import Provider

    data = yaml.safe_load((Path(runner_module.__file__).parent /
                           "defaults/providers.yaml").read_text())
    provider = Provider.from_dict("codex", data["providers"]["codex"])
    normalizer = Normalizer()
    sup = supervisor(clock)
    for obj in ({"type": "turn.started"},
                {"type": "item.started", "item": {"type": "command_execution",
                 "id": "suite", "command": "pytest", "status": "in_progress"}}):
        sup.observe(provider.parse_line(json.dumps(normalizer.event(obj))))
    assert sup.activity_evidence
    failure = provider.parse_line(json.dumps(normalizer.event(
        {"type": "turn.failed", "error": {"message": "disconnected"}})))
    assert not failure.tool_id
    sup.observe(failure)
    assert not sup.activity_evidence
    clock.now = 11
    assert sup.check_timers() is None
    assert sup.check_timers().reason == "silence"
