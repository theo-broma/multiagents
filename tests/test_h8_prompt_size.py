"""H8 prompt-size contract: an argv element past the kernel's 128 KiB
per-argument limit is refused before launch, never truncated."""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "support"))
import c3_harness as h  # noqa: E402
from multiagents import budget as budget_mod  # noqa: E402
from multiagents.config import AgentSpec  # noqa: E402
from multiagents.providers import MAX_ARG_STRLEN  # noqa: E402

LIMIT = MAX_ARG_STRLEN  # 131072 bytes, trailing NUL included


def _runner(tmp_path, monkeypatch):
    provider = h.fake_cli(tmp_path, "p", events=[{"type": "text", "text": "ok"}])
    # The prompt reaches a real CLI as one argv element via the {prompt}
    # placeholder; fake_cli's own spawn args omit it.
    provider["spawn"]["args"] = ["--fake-cli", "{prompt}"]
    monkeypatch.setattr(budget_mod, "read_all", lambda *a, **k: {
        "p": budget_mod.Budget("p", known=True, headroom=1.0)})
    r = h.make_runner(tmp_path / "project", monkeypatch,
                      providers={"p": provider},
                      agents={"worker": AgentSpec.from_dict(
                          "worker", {"provider": "p", "model": "m1"})},
                      project={"limits": {"provider_failure_threshold": 100,
                                          "provider_down_cooldown_seconds": 0.15,
                                          "startup_failure_threshold": 100}})
    (r.paths.config / "providers").mkdir(parents=True, exist_ok=True)
    (r.paths.config / "providers" / "p.sh").write_text("#!/bin/sh\nexit 0\n")
    (r.paths.config / "providers" / "p.sh").chmod(0o755)
    return r


def _start(r, prompt):
    async def go():
        result = await r.start("worker", prompt)
        run = r.runs.get(result.get("agent_id"))
        if run:
            await asyncio.wait_for(run.done.wait(), 15)
        return result
    return asyncio.run(go())


def _overhead(r) -> int:
    """Bytes compose_prompt adds around the task, learned from one probe run."""
    probe = "T"
    result = _start(r, probe)
    assert isinstance(result, dict) and result.get("agent_id"), result
    run_dir = r.paths.run_dir(result["agent_id"])
    prompt = (run_dir / "prompt.md").read_text()
    return len(prompt.encode()) - len(probe.encode())


def _argv(r, result) -> list[str]:
    run_dir = r.paths.run_dir(result["agent_id"])
    return json.loads((run_dir / "command.json").read_text())["argv"]


def test_h8_just_under_limit_launches_unchanged(tmp_path, monkeypatch):
    r = _runner(tmp_path, monkeypatch)
    overhead = _overhead(r)
    task = "x" * (LIMIT - 1 - overhead)  # composed + NUL == LIMIT, allowed
    result = _start(r, task)
    assert isinstance(result, dict) and result.get("agent_id"), result
    argv = _argv(r, result)
    assert any(len(element.encode()) + 1 == LIMIT for element in argv), \
        [len(e) for e in argv]


def test_h8_exact_boundary_launches(tmp_path, monkeypatch):
    r = _runner(tmp_path, monkeypatch)
    overhead = _overhead(r)
    task = "x" * (LIMIT - 1 - overhead)
    assert len(task.encode()) + overhead + 1 == LIMIT
    result = _start(r, task)
    assert isinstance(result, dict) and result.get("agent_id"), result


def test_h8_just_over_limit_refused_before_start(tmp_path, monkeypatch):
    r = _runner(tmp_path, monkeypatch)
    overhead = _overhead(r)
    task = "x" * (LIMIT - overhead)  # composed + NUL == LIMIT + 1
    result = _start(r, task)
    message = str(result.get("error", ""))
    assert str(LIMIT + 1) in message, message
    assert "p" in message and "128 KiB" in message and str(LIMIT) in message, message
    assert "shorter" in message.lower(), message
    # Refused before launch: no command was recorded for the run.
    assert not (r.paths.run_dir(result["agent_id"]) / "command.json").exists()


def test_h8_multibyte_counted_in_bytes(tmp_path, monkeypatch):
    r = _runner(tmp_path, monkeypatch)
    overhead = _overhead(r)
    # Characters comfortably under the limit, bytes over it once composed.
    n = (LIMIT - overhead) // 2 + 10
    assert overhead + n < LIMIT
    task = "é" * n
    result = _start(r, task)
    message = str(result.get("error", ""))
    assert str(overhead + 2 * n + 1) in message, message
    assert not (r.paths.run_dir(result["agent_id"]) / "command.json").exists()
