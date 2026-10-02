"""Harness for the refusal-classification contract (RC-R*),
`context/specs/refusal-classification.md`.

`controlled_cli` writes a fake agent CLI whose behaviour is read from a JSON
control file on every launch, so a test can flip one provider between "starts
and answers", "dies at startup" and "is refused by a content filter" between
runs. Every launch appends a line to `<name>.calls`.
"""
from __future__ import annotations

import asyncio
import json
import stat
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import c3_harness as h  # noqa: E402
from multiagents import budget as budget_mod  # noqa: E402
from multiagents.config import AgentSpec  # noqa: E402

CODEX_MESSAGE = "This content was flagged for possible cybersecurity risk. Try rephrasing."

_SCRIPT = r'''#!{python}
import json, sys, time
base = {base!r}
with open(base + ".calls", "a") as f:
    f.write("launch\n")
ctl = json.load(open(base + ".ctl.json"))
time.sleep(ctl.get("delay", 0))
for ev in ctl.get("events", []):
    print(json.dumps(ev)); sys.stdout.flush()
sys.stderr.write(ctl.get("stderr", "")); sys.stderr.flush()
time.sleep(ctl.get("late", 0))
sys.exit(ctl.get("exit", 0))
'''

OK_EVENTS = [{"type": "text", "text": "ok", "session_id": "s1"}]


class Cli:
    def __init__(self, tmp: Path, name: str, markers: list[str] | None = None,
                 **extra: Any):
        self.name = name
        self.base = str(tmp / f"{name}.rc")
        script = tmp / f"{name}-rc.py"
        script.write_text(_SCRIPT.format(python=sys.executable, base=self.base))
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        self.set()
        Path(self.base + ".calls").write_text("")
        self.config: dict[str, Any] = {
            "bin": str(script), "spawn": {"args": ["--fake-cli"]},
            "stream": {"format": "ndjson", "session_id_paths": ["session_id"], "rules": [
                {"match": {"type": "text"}, "as": "text", "fields": {"text": "text"}},
            ]},
            "refusal_markers": list(markers or []),
            **extra,
        }

    def set(self, **ctl: Any) -> None:
        Path(self.base + ".ctl.json").write_text(json.dumps(ctl))

    def ok(self) -> None:
        self.set(events=OK_EVENTS)

    def launches(self) -> int:
        return len(Path(self.base + ".calls").read_text().splitlines())


def runner(tmp_path: Path, monkeypatch, providers: dict[str, Any], *,
           limits: dict | None = None, agent: AgentSpec | None = None):
    monkeypatch.setattr(budget_mod, "read_all", lambda *a, **k: {
        n: budget_mod.Budget(n, known=True, headroom=1.0) for n in providers})
    return h.make_runner(
        tmp_path / "project", monkeypatch, providers=providers,
        agents={"worker": agent or AgentSpec.from_dict(
            "worker", {"provider": next(iter(providers)), "model": "m1"})},
        project={"limits": {"provider_failure_threshold": 100,
                            "provider_down_cooldown_seconds": 0.15,
                            **(limits or {})}})


def start(r, **kwargs):
    """Start `worker`, wait for the run to end. Returns the result dict, or the
    exception a refused start raised."""
    async def go():
        try:
            result = await r.start("worker", "work", **kwargs)
        except (RuntimeError, PermissionError, ValueError, FileNotFoundError) as exc:
            return exc
        run = r.runs.get(result.get("agent_id"))
        if run:
            await asyncio.wait_for(run.done.wait(), 20)
        return result
    return asyncio.run(go())


def steer(r, agent_id: str, message: str = "go on"):
    async def go():
        try:
            result = await r.steer(agent_id, message)
        except (RuntimeError, PermissionError, ValueError, FileNotFoundError) as exc:
            return exc
        run = r.runs.get(agent_id)
        if run and result.get("steered"):
            await asyncio.wait_for(run.done.wait(), 20)
        return result
    return asyncio.run(go())


def status(r, result) -> str:
    assert isinstance(result, dict) and result.get("agent_id"), result
    return r.tree.get(result["agent_id"]).status


def events(r, kind: str) -> list[dict]:
    if not r.paths.events_file.exists():
        return []
    return [e for line in r.paths.events_file.read_text().splitlines()
            if (e := json.loads(line)).get("kind") == kind]


def recorded(r, result) -> str:
    """Everything the host recorded about a run's verdict, except the raw
    stderr tail (which would contain the text being classified and so prove
    nothing): the node's reason, result.json minus `stderr_tail`, and every
    event for the node."""
    agent_id = result["agent_id"]
    parts = [r.tree.get(agent_id).reason or ""]
    path = r.paths.run_dir(agent_id) / "result.json"
    if path.exists():
        data = json.loads(path.read_text())
        data.pop("stderr_tail", None)
        parts.append(json.dumps(data))
    if r.paths.events_file.exists():
        for line in r.paths.events_file.read_text().splitlines():
            e = json.loads(line)
            if agent_id in json.dumps(e):
                parts.append(json.dumps(e))
    return "\n".join(parts)
