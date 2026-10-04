"""The clock seam of NC-R82 for the M5 window tests.

`ClockWorld` is a `World` whose scheduler is started with
`scheduler start --clock-file <path>`: "now" is one ISO-8601 timezone-aware
timestamp in that file, re-read at every evaluation. A test moves time by
rewriting the file; nothing sleeps on wall time for a window boundary. The only
real waiting is for the scheduler's own evaluation (`tick_seconds`, 1 s here)
to have seen the new instant.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from nc_fixture.world import CLI, World, blocked_codes

UTC = timezone.utc
PARIS = ZoneInfo("Europe/Paris")
ALL_DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def local(y, mo, d, h=0, mi=0, s=0, tz: str | ZoneInfo = PARIS) -> datetime:
    """A wall-clock instant in `tz` (fold=0), as an aware datetime."""
    zone = ZoneInfo(tz) if isinstance(tz, str) else tz
    return datetime(y, mo, d, h, mi, s, tzinfo=zone)


def utc(y, mo, d, h=0, mi=0, s=0) -> datetime:
    return datetime(y, mo, d, h, mi, s, tzinfo=UTC)


class ClockWorld(World):
    def __init__(self, tmp_path: Path, monkeypatch, *, now: datetime, **kw):
        super().__init__(tmp_path, monkeypatch, **kw)
        self.clock_file = tmp_path / "clock.iso"
        self.now = now
        self.write_clock(now)

    # -------------------------------------------------------------- the clock
    def write_clock(self, instant: datetime) -> None:
        assert instant.tzinfo is not None
        tmp = self.clock_file.with_suffix(".tmp")
        tmp.write_text(instant.isoformat() + "\n")
        os.replace(tmp, self.clock_file)
        self.now = instant

    def set_clock(self, instant: datetime, settle: float = 1.4) -> None:
        """Move "now" and give the scheduler one tick (tick_seconds is 1) to
        evaluate it."""
        self.write_clock(instant)
        time.sleep(settle)

    def advance(self, **delta) -> None:
        self.set_clock(self.now + timedelta(**delta))

    # ------------------------------------------------------------- scheduler
    def start_scheduler(self, timeout: float = 20) -> int:
        self.write_config()
        self.write_clock(self.now)
        proc = subprocess.Popen(
            [sys.executable, "-c", CLI, "--path", str(self.root), "scheduler", "start",
             "--clock-file", str(self.clock_file)],
            cwd=self.root, env=self._env(), stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, start_new_session=True)
        self._popen.append(proc)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                status = self.status()
                if status.get("pid"):
                    return int(status["pid"])
            except (OSError, ValueError, KeyError, ImportError, AssertionError):
                pass
            if proc.poll() not in (None, 0):
                break
            time.sleep(0.1)
        out = proc.stdout.read() if proc.poll() is not None and proc.stdout else ""
        raise AssertionError(
            f"the scheduler did not come up with --clock-file (exit {proc.poll()}): {out[-1500:]}")

    # ------------------------------------------------------------- observing
    def codes(self, node_id: str) -> list[str]:
        return blocked_codes(self.get(node_id))

    def is_open(self, node_id: str) -> bool:
        """Is the node's effective window open, as `get_node` shows it: the
        node is `ready` (NC-R52) and not blocked by `window`/`empty_window`."""
        node = self.get(node_id)
        codes = blocked_codes(node)
        win = [c for c in codes if c in ("window", "empty_window")]
        assert bool(node.get("ready")) == (not win), \
            f"`ready`={node.get('ready')!r} disagrees with blocked={codes} for {node_id}"
        return not win

    def hold_lock(self, lock: str = "L") -> str:
        """A gated running node on `lock`: whatever else names `lock` is
        `ready` but never launches, so its `ready` can be read without it
        racing into `running`."""
        holder = self.simple("HOLDER", locks=[lock], fx={"gate": "holder"})
        self.wait_running(holder)
        return holder

    def count(self, transition: str, node_id: str) -> int:
        return self.transitions(node_id).count(transition)
