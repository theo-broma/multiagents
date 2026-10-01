"""TS-R3a: production seams and exact deadline points missed by Run B mutants.

These tests pass on unmutated Run B and fail against the independent mutants
described in context/ts/run-b/robustness-independent.md. No source is mutated
by these tests.
"""

import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from multiagents import driver, runner, supervisor, viewer
from multiagents.runner import Runner
from multiagents.supervisor import Supervisor


def test_ts_r3a_watchdog_uses_the_production_five_second_poll(monkeypatch):
    intervals = []

    class FirstPoll(BaseException):
        pass

    async def sleep(seconds):
        intervals.append(seconds)
        raise FirstPoll

    monkeypatch.setattr(runner, "asyncio", SimpleNamespace(sleep=sleep))
    r = object.__new__(Runner)
    r.tree = SimpleNamespace(get=lambda _: None)
    run = SimpleNamespace(node_id="ag-111111", supervisor=Supervisor())
    with pytest.raises(FirstPoll):
        asyncio.run(r._watch_timers(run))
    assert intervals == [5.0], "the production watchdog cadence changed"


def test_ts_r3a_consult_keeps_the_production_sixty_second_slack():
    assert Runner.CONSULT_LOCK_SLACK_SECONDS == 60.0


@pytest.mark.parametrize("condition", ["timeout", "silence"])
def test_ts_r3a_watchdog_before_at_and_after_deadline(monkeypatch, condition):
    now = [1000.0]
    monkeypatch.setattr(supervisor, "time", SimpleNamespace(monotonic=lambda: now[0]))
    sup = Supervisor(
        started=1000.0, last_event=1000.0,
        wall_timeout=1.0 if condition == "timeout" else 10000,
        silence_timeout=1.0 if condition == "silence" else 10000,
        current_progress="same", progress_when_last_quiet="same",
    )
    for elapsed in (0.875, 1.0):
        now[0] = 1000.0 + elapsed
        assert sup.check_timers() is None, f"{condition} fired at {elapsed}s"
    now[0] = 1001.125
    trip = sup.check_timers()
    assert trip is not None and trip.reason == condition, (
        f"{condition} did not fire immediately after its deadline: {trip}"
    )
    assert sup.check_timers() is None, "one quiet period was reported twice"


def test_ts_r3a_tm_r3_follower_still_follows_at_exactly_sixty_seconds(
    tmp_path, monkeypatch, capsys,
):
    aid = "ag-111111"
    run_dir = tmp_path / "runs" / aid
    run_dir.mkdir(parents=True)
    (run_dir / "stream.jsonl").write_text(json.dumps({"kind": "text", "text": "end"}) + "\n")
    paths = SimpleNamespace(
        run_dir=lambda _: run_dir, tree_file=tmp_path / "tree.json",
        events_file=tmp_path / "events.jsonl",
    )
    monkeypatch.setattr(viewer, "Tree", lambda *args: SimpleNamespace(
        get=lambda _: {"status": "done"},
    ))

    class Clock:
        now = 0.0

        def __init__(self):
            self.slept_at = []

        def time(self):
            return self.now

        def sleep(self, seconds):
            self.slept_at.append(self.now)
            self.now = round(self.now + seconds, 6)
            assert self.now <= 61, "the follower never finished its linger"

    clock = Clock()
    monkeypatch.setattr(viewer, "time", clock)
    viewer.view_stream(paths, aid, follow=True)
    assert 59.8 in clock.slept_at, "the view stopped before the deadline"
    assert 60.0 in clock.slept_at, "the view stopped at the exact deadline"
    assert clock.now == 60.2, "the view did not stop on the first poll after the deadline"
    assert "final status: done" in capsys.readouterr().out


@pytest.mark.parametrize("prefix_length", [64, 128, 256])
def test_ts_r3a_follower_detects_truncate_and_regrow_with_the_same_head(
    tmp_path, monkeypatch, capsys, prefix_length,
):
    aid = "ag-111111"
    run_dir = tmp_path / "runs" / aid
    run_dir.mkdir(parents=True)
    stream = run_dir / "stream.jsonl"
    prefix = "A" * prefix_length
    stream.write_text(json.dumps({"kind": "text", "text": prefix + "old"}) + "\n")
    paths = SimpleNamespace(
        run_dir=lambda _: run_dir, tree_file=tmp_path / "tree.json",
        events_file=tmp_path / "events.jsonl",
    )
    monkeypatch.setattr(viewer, "Tree", lambda *args: SimpleNamespace(
        get=lambda _: {"status": "done"},
    ))
    monkeypatch.setattr(viewer, "LINGER_SECONDS", 1)

    class Clock:
        now = 0.0

        def time(self):
            return self.now

        def sleep(self, seconds):
            if self.now == 0:
                # Same inode, larger size, identical first fingerprint bytes.
                stream.write_text(json.dumps({
                    "kind": "text", "text": prefix + "replacement" + "B" * 80,
                }) + "\n")
            self.now = round(self.now + seconds, 6)
            assert self.now <= 2, "the view did not finish its linger"

    monkeypatch.setattr(viewer, "time", Clock())
    viewer.view_stream(paths, aid, follow=True)
    output = capsys.readouterr().out
    assert "--- stream truncated ---" in output, output
    assert "replacement" in output, "the rewritten record was silently dropped"


@pytest.mark.parametrize("elapsed,launches", [(59.875, 1), (60.0, 2), (60.125, 2)])
def test_ts_r3a_p0_r8f_20_crash_retry_before_at_and_after_default(
    tmp_path, monkeypatch, capsys, elapsed, launches,
):
    from test_phase0_r8f_leftovers import _fake, _run
    from test_phase0_interactive_compact import Session

    s = Session(tmp_path, monkeypatch, capsys, compact_at=0,
                limits={"restart_on_crash": True,
                        "restart_min_runtime_seconds": float("inf")})
    s.script.write_text(_fake())

    class Clock:
        now = 1000.0

        def monotonic(self):
            return self.now

        def __getattr__(self, name):
            return getattr(time, name)

    clock = Clock()
    attached = driver._run_attached

    def attach(*args, **kwargs):
        code = attached(*args, **kwargs)
        clock.now += elapsed
        return code

    monkeypatch.setattr(driver, "time", clock)
    monkeypatch.setattr(driver, "_run_attached", attach)
    code, output = _run(s, [{"exit": 1}, {"exit": 0}])
    assert len(s.calls("launch")) == launches, (
        f"crash after exactly {elapsed}s launched {s.seq()}:\n{output}"
    )
    assert code == (1 if launches == 1 else 0), output
