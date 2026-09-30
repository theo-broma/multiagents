import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent / "support"))
import c3_harness as h
from multiagents.config import AgentSpec
from multiagents.paths import ProjectPaths
from multiagents.runner import Runner
from multiagents.tree import now
from multiagents.authority import HostAuthority

# How long a dummy provider needs to sleep to comfortably trigger a 1s timeout
# (Must be > 5s because _watch_timers polls every 5s)
OUTLIVES_TIMEOUT = 8.0

def _budgets(monkeypatch, **headroom):
    import multiagents.budget
    readings = {name: multiagents.budget.Budget(name, known=True, headroom=room)
                for name, room in headroom.items()}
    monkeypatch.setattr(multiagents.budget, "read_all", lambda *a, **kw: readings)
    for k, v in headroom.items():
        monkeypatch.setenv(f"{k.upper()}_API_KEY", "fake")

def _runner(tmp_path, monkeypatch, *, agent=None, project=None, providers=None):
    if agent is None:
        agent = AgentSpec(name="worker", provider="acme", model="m1")
    return h.make_runner(tmp_path / "project", monkeypatch,
                         agents={"worker": agent}, providers=providers or {},
                         project=project or {})

def _start(runner, name="worker", **kwargs):
    async def go():
        result = await runner.start(name, "hello", **kwargs)
        run = runner.runs.get(result.get("agent_id"))
        if run:
            await asyncio.wait_for(run.done.wait(), 60)
        return result
    return asyncio.run(go())

def _events(r: Runner, kind: str) -> list[dict]:
    path = r.paths.events_file
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        try:
            ev = json.loads(line)
            if ev.get("kind") == kind:
                out.append(ev)
        except ValueError:
            pass
    return out

def test_d1_finding8_steer_keeps_original_call_value_despite_forged_tree(tmp_path, monkeypatch):
    _budgets(monkeypatch, acme=1.0)
    count = tmp_path / "invocations.txt"
    script = tmp_path / "two-turns.py"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, pathlib, time\n"
        f"p = pathlib.Path({str(count)!r})\n"
        "n = int(p.read_text()) + 1 if p.exists() else 1\n"
        "p.write_text(str(n))\n"
        f"if n > 1: time.sleep({OUTLIVES_TIMEOUT})\n"
        "print(json.dumps({'type': 'text', 'text': 'answer', 'session': 's1'}), flush=True)\n"
    )
    script.chmod(0o755)
    provider = {"bin": str(script),
                "spawn": {"args": ["--model", "{model}"],
                          "resume": ["--resume", "{session_id}"]},
                "stream": {"format": "ndjson", "session_id_paths": ["session"],
                           "rules": [{"match": {"type": "text"}, "as": "text",
                                      "fields": {"text": "text"}}]}}
    r = _runner(tmp_path, monkeypatch, providers={"acme": provider},
                project={"limits": {"default_timeout": 10}})
    first = _start(r, timeout=1)
    agent_id = first["agent_id"]
    
    # The container-writable tree is forged between turns
    with r.tree.transaction() as data:
        data["nodes"][agent_id]["limits"] = {"timeout": {"value": 999, "source": {"layer": "call"}}}

    async def steer_and_wait():
        steered = await r.steer(agent_id, "continue")
        run = r.runs.get(agent_id)
        if run:
            await asyncio.wait_for(run.done.wait(), 60)
        return steered

    steered = asyncio.run(steer_and_wait())
    assert steered.get("steered") is True, steered
    
    # Should still trip on the original 1s limit, not the forged 999
    trips = _events(r, "stuck")
    assert any(ev.get("agent") == agent_id for ev in trips), f"No trips! steered={steered}, stuck events={trips}"
    hit = _events(r, "limit_hit")
    timeout_hits = [ev for ev in hit if ev.get("key") == "limits.default_timeout" and ev.get("scope") == agent_id]
    assert timeout_hits
    assert timeout_hits[-1].get("value") == 1

def test_d1_finding8_deleted_host_record_falls_back_to_current_config(tmp_path, monkeypatch):
    _budgets(monkeypatch, acme=1.0)
    count = tmp_path / "invocations2.txt"
    script = tmp_path / "two-turns.py"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, pathlib, time\n"
        f"p = pathlib.Path({str(count)!r})\n"
        "n = int(p.read_text()) + 1 if p.exists() else 1\n"
        "p.write_text(str(n))\n"
        f"if n > 1: time.sleep({OUTLIVES_TIMEOUT})\n"
        "print(json.dumps({'type': 'text', 'text': 'answer', 'session': 's1'}), flush=True)\n"
    )
    script.chmod(0o755)
    provider = {"bin": str(script),
                "spawn": {"args": ["--model", "{model}"],
                          "resume": ["--resume", "{session_id}"]},
                "stream": {"format": "ndjson", "session_id_paths": ["session"],
                           "rules": [{"match": {"type": "text"}, "as": "text",
                                      "fields": {"text": "text"}}]}}
    r = _runner(tmp_path, monkeypatch, providers={"acme": provider},
                project={"limits": {"default_timeout": 10}})
    
    first = _start(r, timeout=5)
    agent_id = first["agent_id"]
    if agent_id in r.runs:
        asyncio.run(r.shutdown(detach=False))

    # Delete the host-owned limits file
    limits_file = HostAuthority.directory_for(r.paths) / "launch-limits.json"
    if limits_file.exists():
        limits_file.unlink()

    # Now we relaunch with a new runner that has a different config default
    r2 = _runner(tmp_path, monkeypatch, providers={"acme": provider},
                 project={"limits": {"default_timeout": 1}})
    
    async def steer_and_wait():
        steered = await r2.steer(agent_id, "continue")
        run = r2.runs.get(agent_id)
        if run:
            await asyncio.wait_for(run.done.wait(), 20)
        return steered

    steered = asyncio.run(steer_and_wait())
    assert steered.get("steered") is True, steered

    all_events = path.read_text() if (path := r2.paths.events_file).exists() else ""
    stream_content = r2.paths.run_dir(agent_id).joinpath("stream.jsonl").read_text() if r2.paths.run_dir(agent_id).joinpath("stream.jsonl").exists() else ""

    trips = [ev for ev in _events(r2, "stuck") if ev.get("agent") == agent_id]
    assert trips, f"No trips! steered={steered}\nEVENTS:\n{all_events}\nSTREAM:\n{stream_content}"
    hit = [ev for ev in _events(r2, "limit_hit") if ev.get("key") == "limits.default_timeout" and ev.get("scope") == agent_id]
    assert hit
    assert hit[-1].get("value") == 1
    # Ensure provenance says project config, not call
    assert hit[-1].get("source", {}).get("layer") == "project"

def test_d1_adopted_run_past_deadline(tmp_path, monkeypatch):
    _budgets(monkeypatch, acme=1.0)
    script = tmp_path / "long-run.py"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, time\n"
        "while True: time.sleep(1)\n"
    )
    script.chmod(0o755)
    provider = {"bin": str(script),
                "spawn": {"args": ["--model", "{model}"]},
                "stream": {"format": "ndjson", "session_id_paths": ["session"],
                           "rules": [{"match": {"type": "text"}, "as": "text",
                                      "fields": {"text": "text"}}]}}
    r = _runner(tmp_path, monkeypatch, providers={"acme": provider},
                project={"limits": {"default_timeout": 5}})
    
    first = asyncio.run(r.start("worker", "hello"))
    agent_id = first["agent_id"]
    
    # Forge the launch limits to be deep in the past
    with r.launch_limits.locked() as records:
        records[agent_id]["launched_at"] -= 100
        r.launch_limits.commit(records)
        
    asyncio.run(r.shutdown(detach=True))
    
    r2 = _runner(tmp_path, monkeypatch, providers={"acme": provider},
                 project={"limits": {"default_timeout": 5}})
    
    async def try_adopt():
        node = r2.tree.get(agent_id)
        return await r2._adopt_one(node)
        
    adopted = asyncio.run(try_adopt())
    assert adopted is False
