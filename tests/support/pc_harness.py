"""Harness for the provider-concurrency contract tests (PC-R*),
`context/specs/provider-concurrency.md`.

Builds on `sc_harness` (a real project on disk, the server's MCP tool
functions, fake opencode-style CLIs). The one addition is a fake CLI that
*blocks until released*, so "a run is holding a slot" is a state a test can
hold open and then end deliberately.

`Gated` writes a script that, on each invocation, appends
`{"argv": [...], "pid": N}` to `<name>.calls`, prints a step, then waits until
its gate exists before printing its final text and exiting 0. The gate is per
provider (`open()` / `close()`), or per task via `hold(substring)`: a run whose
argv contains the substring waits for that tag's own file (`release(tag)`).
No gate set means the run does not block.
"""
from __future__ import annotations

import json
import os
import signal
import stat
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import sc_harness as sc  # noqa: E402
from sc_harness import World, shipped_opencode  # noqa: E402,F401

_SCRIPT = r'''#!{python}
import json, os, sys, time
base, name = {base!r}, {name!r}
argv = sys.argv[1:]
with open(base + ".calls", "a") as f:
    f.write(json.dumps({{"argv": argv, "pid": os.getpid(), "t": time.time()}}) + "\n")
ctl = json.load(open(base + ".ctl.json"))
text = " ".join(argv)
session = argv[argv.index("-s") + 1] if "-s" in argv else "ses_%s_%d" % (name, os.getpid())
def emit(kind, part):
    ev = {{"type": kind, "sessionID": session, "part": dict(part, sessionID=session)}}
    print(json.dumps(ev)); sys.stdout.flush()
emit("step_start", {{"id": "prt_s%d" % os.getpid(), "type": "step-start"}})
emit("step_finish", {{"id": "prt_f%d" % os.getpid(), "type": "step-finish",
                     "reason": "stop", "cost": 0,
                     "tokens": {{"input": 1, "output": 1, "reasoning": 0,
                                "cache": {{"read": 0, "write": 0}}}}}})
if ctl.get("talk_first"):
    emit("text", {{"id": "prt_u%d" % os.getpid(), "type": "text", "text": "working"}})
gate = ctl.get("gate")
for tag, path in ctl.get("holds", {{}}).items():
    if tag in text:
        gate = path
        break
if gate:
    while not os.path.exists(gate):
        time.sleep(0.05)
if ctl.get("sleep"):
    time.sleep(ctl["sleep"])
with open(base + ".done", "a") as f:
    f.write(json.dumps({{"argv": argv, "t": time.time()}}) + "\n")
emit("text", {{"id": "prt_t%d" % os.getpid(), "type": "text", "text": ctl.get("text", "done")}})
sys.exit(ctl.get("exit", 0))
'''


class Gated:
    def __init__(self, tmp: Path, name: str, **extra: Any):
        self.name = name
        self.base = str(tmp / f"{name}.gx")
        self.gate = tmp / f"{name}.gate"
        self.tmp = tmp
        script = tmp / f"{name}-gated.py"
        script.write_text(_SCRIPT.format(python=sys.executable, base=self.base, name=name))
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        Path(self.base + ".calls").write_text("")
        entry = shipped_opencode()
        for key in ("auth", "mcp", "home_links", "bin_search", "models_cmd",
                    "models_parse", "models_include", "notes"):
            entry.pop(key, None)
        entry["bin"] = str(script)
        entry["models_include"] = [f"{name}/*"]
        entry.update(extra)
        self.entry = entry
        self._holds: dict[str, str] = {}
        self._gated = False
        self._talk = False
        self._write()

    def _write(self) -> None:
        Path(self.base + ".ctl.json").write_text(json.dumps({
            "gate": str(self.gate) if self._gated else None,
            "holds": self._holds, "text": "done", "exit": 0,
            "talk_first": self._talk}))

    def close(self) -> None:
        """New runs block until `open()`."""
        self.gate.unlink(missing_ok=True)
        self._gated = True
        self._write()

    def talk_first(self) -> None:
        """Runs print some text before they block, so one that dies while
        blocked has not "said nothing" and is not given the free retry."""
        self._talk = True
        self._write()

    def open(self) -> None:
        self.gate.write_text("open")

    def hold(self, tag: str) -> None:
        """Runs whose argv contains `tag` block until `release(tag)`."""
        path = self.tmp / f"{self.name}.hold.{tag}"
        path.unlink(missing_ok=True)
        self._holds[tag] = str(path)
        self._write()

    def release(self, tag: str) -> None:
        Path(self._holds[tag]).write_text("go")

    def calls(self) -> list[dict]:
        return [json.loads(x) for x in Path(self.base + ".calls").read_text().splitlines() if x]

    def spawns(self) -> int:
        return len(self.calls())

    def pids(self) -> list[int]:
        return [c["pid"] for c in self.calls()]

    def done(self) -> int:
        p = Path(self.base + ".done")
        return len(p.read_text().splitlines()) if p.is_file() else 0

    def argv_text(self, k: int = -1) -> str:
        return " ".join(self.calls()[k]["argv"])


def gated(w: World, name: str, *, max_concurrent: Any = 1, **extra: Any) -> Gated:
    """A blocking provider registered in the world. `max_concurrent=None`
    leaves the key absent; `"null"` writes an explicit null."""
    if max_concurrent == "null":
        extra["max_concurrent"] = None
    elif max_concurrent is not None:
        extra["max_concurrent"] = max_concurrent
    g = Gated(w.p.tmp, name, **extra)
    w.p.fakes[name] = g          # type: ignore[assignment]
    w.p.providers[name] = g.entry
    return g


def set_limit(w: World, provider: str, value: Any) -> None:
    if value is None:
        w.p.providers[provider].pop("max_concurrent", None)
    else:
        w.p.providers[provider]["max_concurrent"] = value
    w.reload()


def wait_until(pred, timeout: float = 20.0, step: float = 0.1) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(step)
    return False


async def await_until(pred, timeout: float = 20.0, step: float = 0.1) -> bool:
    import asyncio
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        await asyncio.sleep(step)
    return False


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:
        return Path(f"/proc/{pid}/stat").read_text().split(")")[-1].split()[0] != "Z"
    except OSError:
        return False


def kill9(pid: int) -> None:
    os.kill(pid, signal.SIGKILL)


def deferred_for_pc(r: Any) -> bool:
    return bool(r.get("deferred")) and "provider_concurrency" in str(r.get("reason", ""))


def pc_events(w: World) -> list[dict]:
    return w.p.events_of("provider_concurrency")
