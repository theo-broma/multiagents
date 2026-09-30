"""Adversarial tests for H7 (context/specs/h7-provider-startup.md).

Two groups:

- DEFECTS: tests that fail on the code as merged. Each names the input and
  what goes wrong.
- MUTATION GUARDS: tests that pass today, written because a mutation of the
  named line survived the existing suite (tests/test_h7_provider_startup.py
  and the wider files that touch startup health).
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).parent / "support"))
import c3_harness as h  # noqa: E402
from multiagents import budget as budget_mod  # noqa: E402
from multiagents.config import AgentSpec  # noqa: E402
from multiagents.providers import load_providers  # noqa: E402
from multiagents.runner import Runner  # noqa: E402
from multiagents import startup as startup_mod  # noqa: E402
from multiagents.startup import StartupHealth, StartupUnavailable  # noqa: E402

SHIPPED = yaml.safe_load(
    (Path(__file__).parents[1] / "src/multiagents/defaults/providers.yaml").read_text())


# --------------------------------------------------------------- helpers ---

def _events(r: Runner, kind: str) -> list[dict]:
    if not r.paths.events_file.exists():
        return []
    return [e for line in r.paths.events_file.read_text().splitlines()
            if (e := json.loads(line)).get("kind") == kind]


def _runner(tmp_path, monkeypatch, providers, *, limits=None, agent=None):
    monkeypatch.setattr(budget_mod, "read_all", lambda *a, **k: {
        n: budget_mod.Budget(n, known=True, headroom=1.0) for n in providers})
    r = h.make_runner(tmp_path / "project", monkeypatch, providers=providers,
                      agents={"worker": agent or AgentSpec.from_dict(
                          "worker", {"provider": next(iter(providers)), "model": "m1"})},
                      project={"limits": {"provider_failure_threshold": 100,
                                          "provider_down_cooldown_seconds": 0.15,
                                          **(limits or {})}})
    for name in providers:
        script = r.paths.config / "providers" / f"{name}.sh"
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text("#!/bin/sh\nexit 0\n")
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return r


def _start(r: Runner, **kwargs):
    async def go():
        try:
            result = await r.start("worker", "work", **kwargs)
        except (RuntimeError, PermissionError, ValueError, FileNotFoundError) as exc:
            return exc
        run = r.runs.get(result.get("agent_id"))
        if run:
            await asyncio.wait_for(run.done.wait(), 15)
        return result
    return asyncio.run(go())


def _shipped_stream_cli(tmp_path, provider, events, *, name=None, exit_code=1,
                        stderr="startup exploded\n"):
    """A fake CLI that speaks `provider`'s shipped stream rules."""
    config = h.fake_cli(tmp_path, name or provider, events=events,
                        exit_code=exit_code, stderr=stderr)
    config["stream"] = SHIPPED["providers"][provider]["stream"]
    return config


def _mark_down(health: StartupHealth, provider: str, cooldown: float) -> None:
    token = health.claim(provider, "ag-tripper")
    health.finish(provider, "ag-tripper", token, failed=True, error="boom",
                  threshold=1, cooldown=cooldown)
    assert health.availability(provider)


# The error Claude Code prints when the API call itself fails. Claude Code
# reports it as an assistant message from the model "<synthetic>" (the repo
# already knows this shape: src/multiagents/transcripts.py:99), with zero
# output tokens, followed by a `result` whose `is_error` is true.
CLAUDE_API_ERROR = [
    {"type": "system", "subtype": "init", "session_id": "s"},
    {"type": "assistant", "session_id": "s", "error": "unknown",
     "message": {"id": "msg-synthetic", "model": "<synthetic>", "role": "assistant",
                 "content": [{"type": "text",
                              "text": "API Error: 500 {\"type\":\"error\",\"error\":"
                                      "{\"type\":\"api_error\",\"message\":"
                                      "\"Internal server error\"}}"}],
                 "usage": {"input_tokens": 0, "output_tokens": 0}}},
    {"type": "result", "subtype": "success", "is_error": True, "session_id": "s",
     "result": "API Error: 500", "usage": {"input_tokens": 0, "output_tokens": 0}},
]


# =============================================================== DEFECTS ===

def test_claude_api_error_message_is_not_startup_progress():
    """PS-R4: error events never count. Claude's API-error message is an
    error; it only looks like assistant text."""
    claude = load_providers(SHIPPED["providers"])["claude"]
    event = claude.parse_line(json.dumps(CLAUDE_API_ERROR[1]))
    assert not event.startup_progress, event


def test_claude_run_that_only_reports_an_api_error_counts_as_a_startup_failure(
        tmp_path, monkeypatch):
    cli = _shipped_stream_cli(tmp_path, "claude", CLAUDE_API_ERROR)
    r = _runner(tmp_path, monkeypatch, {"claude": cli},
                limits={"startup_failure_threshold": 1})
    _start(r)
    assert _events(r, "startup_down"), "an API-error-only run reset the count instead"


def test_claude_probe_that_only_reports_an_api_error_does_not_recover(tmp_path, monkeypatch):
    """PS-R5: a probe with a startup failure is re-marked, never cleared."""
    cli = _shipped_stream_cli(tmp_path, "claude", CLAUDE_API_ERROR)
    r = _runner(tmp_path, monkeypatch, {"claude": cli},
                limits={"startup_failure_threshold": 1,
                        "provider_down_cooldown_seconds": 0.2})
    _mark_down(r.startup, "claude", cooldown=0.2)
    time.sleep(0.3)
    probe = _start(r, model="m1")
    assert isinstance(probe, dict) and probe.get("provider") == "claude", probe
    assert not _events(r, "provider_recovered")
    assert r.startup.availability("claude"), "the failed probe cleared startup_down"


def test_steer_of_a_live_unpinned_run_on_a_tripped_provider_does_not_kill_it(
        tmp_path, monkeypatch):
    """A healthy, progressing run is steered after OTHER runs tripped its
    provider (the runner's own wrap-up does this automatically). Today the
    steer stops the live run first, the relaunch's claim is refused, and the
    node ends `failed: Provider 'primary' is startup_down`."""
    primary = h.fake_cli(tmp_path, "primary",
                         events=[{"type": "text", "text": "working", "session_id": "sess-1"}])
    binary = Path(primary["bin"])
    binary.write_text(binary.read_text().replace("sys.exit(0)", "time.sleep(20)\nsys.exit(0)"))
    primary["stream"]["session_id_paths"] = ["session_id"]
    primary["spawn"]["resume"] = ["--resume", "{session_id}"]
    r = _runner(tmp_path, monkeypatch, {"primary": primary},
                limits={"startup_failure_threshold": 1,
                        "provider_down_cooldown_seconds": 60})

    async def go():
        started = await r.start("worker", "work")
        agent_id = started["agent_id"]
        try:
            for _ in range(200):
                if r.runs[agent_id].startup_progress:
                    break
                await asyncio.sleep(0.05)
            assert r.runs[agent_id].startup_progress
            _mark_down(r.startup, "primary", cooldown=60)
            steered = await r.steer(agent_id, "also do X")
            node = r.tree.get(agent_id)
            return steered, node.status, node.reason
        finally:
            with contextlib.suppress(Exception):
                await r.stop(agent_id)

    steered, status, reason = asyncio.run(go())
    if not steered.get("steered"):
        assert status == "running", (steered, status, reason)


def test_unpinned_start_that_loses_the_probe_race_is_not_given_a_pin_refusal(
        tmp_path, monkeypatch):
    """Two servers share the host-state dir. Between this server's routing
    decision (primary half-open, probe free) and its claim, the other server
    takes the probe. The start had no `model`, yet it returns
    `{"reason": "startup_down", "error": "... Omitting model lets the router
    choose ..."}`: not routed to the agent's fallback, not deferred."""
    primary = h.fake_cli(tmp_path, "primary", exit_code=1, stderr="startup exploded")
    sibling = h.fake_cli(tmp_path, "sibling", events=[{"type": "text", "text": "ok"}])
    agent = AgentSpec.from_dict("worker", {"provider": "primary", "model": "m1",
                                            "models": {"sibling": "s1"}})
    r = _runner(tmp_path, monkeypatch, {"primary": primary, "sibling": sibling},
                agent=agent, limits={"startup_failure_threshold": 1,
                                     "provider_down_cooldown_seconds": 0.2})
    _mark_down(r.startup, "primary", cooldown=0.2)
    time.sleep(0.3)
    other_server = StartupHealth(r.paths)
    real_claim = r.startup.claim

    def claim_after_other_server(provider, run_id):
        if provider == "primary":
            other_server.claim("primary", "ag-other-servers-probe")
        return real_claim(provider, run_id)

    r.startup.claim = claim_after_other_server
    result = _start(r)
    assert isinstance(result, dict), result
    assert result.get("agent_id") or result.get("deferred"), result


# ======================================================= MUTATION GUARDS ===

def test_progress_resets_the_consecutive_count(tmp_path, monkeypatch):
    """Survived: deleting `record["count"] = 0` in StartupHealth.progress."""
    health = StartupHealth(h.make_paths(tmp_path / "project"))

    def run(run_id, progress, failed):
        token = health.claim("p", run_id)
        if progress:
            health.progress("p", run_id, token)
        return health.finish("p", run_id, token, failed=failed, error="e",
                             threshold=2, cooldown=60)

    assert run("a", False, True) is None
    assert run("b", True, False) is None
    assert run("c", False, True) is None
    assert health.availability("p") is None


def test_startup_down_event_names_count_and_first_error_line(tmp_path):
    """Survived: the event carrying the whole error instead of its first line."""
    health = StartupHealth(h.make_paths(tmp_path / "project"))
    for run_id in ("a", "b"):
        token = health.claim("p", run_id)
        event = health.finish("p", run_id, token, failed=True,
                              error="first line\nsecond line", threshold=2, cooldown=60)
    assert event == {"provider": "p", "count": 2, "error": "first line"}


_CLAIM_IN_ANOTHER_SERVER = """
import sys
from pathlib import Path
from multiagents.paths import ProjectPaths
from multiagents.startup import StartupHealth
paths = ProjectPaths(Path(sys.argv[1]))
print(StartupHealth(paths).claim("p", "ag-probe"))
"""


def _other_server_claims(paths) -> str:
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")}
    out = subprocess.run([sys.executable, "-c", _CLAIM_IN_ANOTHER_SERVER, str(paths.root)],
                         capture_output=True, text=True, env=env, check=True)
    return out.stdout.strip()


class _FrozenClock:
    """`startup`'s clock, this process only: `time()` stands still until
    moved, so the 10 ms cooldown cannot run out between marking the provider
    down and `_mark_down`'s check that it is down. Measured under `-n auto`
    with the basetemp on disk: `_write`'s two fsyncs took longer than 10 ms
    and both tests below failed on that check. Starts at the real time, so a
    subprocess on the real clock still sees the cooldown expire after a real
    sleep past it."""

    def __init__(self):
        self.now = time.time()

    def time(self):
        return self.now

    def __getattr__(self, name):
        return getattr(time, name)


def _past_the_cooldown(clock: _FrozenClock, seconds: float) -> None:
    clock.now += seconds          # for this process
    time.sleep(seconds)           # for another server, on the real clock


def test_probe_claim_of_a_dead_server_is_released_on_reconcile(tmp_path, monkeypatch):
    """Survived: `_reconcile` keeping record["probe"] after deleting the run."""
    clock = _FrozenClock()
    monkeypatch.setattr(startup_mod, "time", clock)
    paths = h.make_paths(tmp_path / "project")
    health = StartupHealth(paths)
    _mark_down(health, "p", cooldown=0.01)
    _past_the_cooldown(clock, 0.05)
    assert _other_server_claims(paths)          # that server has now exited
    assert health.availability("p") is None
    assert health.claim("p", "ag-next-probe")


def test_probe_claim_held_while_the_host_lives_even_if_the_child_is_gone(tmp_path,
                                                                        monkeypatch):
    """Survived: `and` -> `or` in `_reconcile`. The host still has to drain
    and classify an exited child, so its claim stays."""
    clock = _FrozenClock()
    monkeypatch.setattr(startup_mod, "time", clock)
    health = StartupHealth(h.make_paths(tmp_path / "project"))
    _mark_down(health, "p", cooldown=0.01)
    _past_the_cooldown(clock, 0.05)
    token = health.claim("p", "ag-probe")
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    assert health.bind("p", "ag-probe", token, child.pid, "1")
    assert health.availability("p") == {"reason": "startup_down"}
    with pytest.raises(StartupUnavailable):
        health.claim("p", "ag-second-probe")


@pytest.mark.parametrize("content", [
    "not json", "[]", json.dumps({"p": {"generation": 1}}),
    json.dumps({"p": {"generation": "g", "count": "2", "down": False,
                      "until": 0, "probe": None, "runs": {}}}),
])
def test_malformed_host_state_fails_closed(tmp_path, content):
    """Survived: availability/claim returning open on an unreadable state."""
    health = StartupHealth(h.make_paths(tmp_path / "project"))
    health.file.write_text(content)
    assert health.availability("p") == {"reason": "startup_down"}
    with pytest.raises(StartupUnavailable):
        health.claim("p", "ag-x")


def test_missing_host_state_file_fails_closed(tmp_path):
    health = StartupHealth(h.make_paths(tmp_path / "project"))
    health.file.unlink()
    assert health.availability("p") == {"reason": "startup_down"}
    with pytest.raises(StartupUnavailable):
        health.claim("p", "ag-x")


def test_host_state_directory_is_private(tmp_path):
    """Survived: dropping `self.directory.chmod(0o700)`."""
    paths = h.make_paths(tmp_path / "project")
    from multiagents.authority import HostAuthority
    target = HostAuthority.directory_for(paths)
    target.mkdir(parents=True, exist_ok=True)
    target.chmod(0o755)
    StartupHealth(paths)
    assert stat.S_IMODE(target.stat().st_mode) == 0o700


@pytest.mark.parametrize("tree_value", [
    {"p": True},
    {"p": {"down": True, "until": 9e12, "count": 99}},
    "garbage",
])
def test_forged_tree_startup_state_does_not_block_a_healthy_provider(
        tmp_path, monkeypatch, tree_value):
    cli = h.fake_cli(tmp_path, "p", events=[{"type": "text", "text": "ok"}])
    r = _runner(tmp_path, monkeypatch, {"p": cli})
    r.tree.set_cooldown("p", 0, "seed the tree file")
    raw = json.loads(r.paths.tree_file.read_text())
    raw["cooldowns"] = {}
    raw["startup_down"] = tree_value
    raw["startup_failures"] = {"p": 99}
    r.paths.tree_file.write_text(json.dumps(raw))
    result = _start(Runner(r.paths, r.config), model="m1")
    assert isinstance(result, dict) and result.get("provider") == "p", result


def _progress(provider_name: str, payload: dict) -> bool:
    provider = load_providers(SHIPPED["providers"])[provider_name]
    return provider.parse_line(json.dumps(payload)).startup_progress


@pytest.mark.parametrize("provider,payload,expected", [
    # claude
    ("claude", {"type": "system", "subtype": "init", "session_id": "s"}, False),
    ("claude", {"type": "user", "message": {"role": "user",
                "content": [{"type": "text", "text": "echoed prompt"}]}}, False),
    ("claude", {"type": "assistant", "message": {"id": "m", "content": [
        {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}]}}, True),
    ("claude", {"type": "result", "subtype": "success",
                "usage": {"output_tokens": 0}}, False),
    ("claude", {"type": "result", "subtype": "success",
                "usage": {"output_tokens": 7}}, True),
    ("claude", {"type": "rate_limit_event"}, False),
    # codex adapter stream
    ("codex", {"kind": "step", "type": "turn.started"}, False),
    ("codex", {"kind": "error", "text": "Reconnecting... 1/5"}, False),
    ("codex", {"kind": "result", "status": "failed", "text": "stream disconnected"}, False),
    ("codex", {"kind": "result", "status": "success",
               "tokens": {"output_tokens": 0}}, False),
    ("codex", {"kind": "result", "status": "success",
               "tokens": {"output_tokens": 3}}, True),
    ("codex", {"kind": "text", "text": ""}, False),
    ("codex", {"kind": "tool", "name": "command_execution", "args": {"command": "ls"}}, True),
    # opencode and opencode-zai (extends opencode)
    *[(name, payload, expected) for name in ("opencode", "opencode-zai")
      for payload, expected in [
          ({"type": "step_start", "sessionID": "s"}, False),
          ({"type": "error", "sessionID": "s", "error": {"name": "UnknownError",
            "data": {"message": "Unexpected server error"}}}, False),
          ({"type": "text", "part": {"text": "   "}}, False),
          ({"type": "text", "part": {"text": "hi"}}, True),
          ({"type": "step_finish", "part": {"tokens": {"input": 10, "output": 0},
                                            "reason": "stop"}}, False),
          ({"type": "step_finish", "part": {"tokens": {"input": 10, "output": 4},
                                            "reason": "stop"}}, True),
          ({"type": "tool_use", "part": {"tool": "bash",
                                         "state": {"input": {"c": 1}}}}, True),
      ]],
])
def test_startup_progress_classification(provider, payload, expected):
    """Survived: counting failed-result text, zero usage, blank text; and
    dropping tool calls or output usage as progress."""
    assert _progress(provider, payload) is expected


def test_stopped_silent_run_is_not_a_startup_failure(tmp_path, monkeypatch):
    cli = h.fake_cli(tmp_path, "p", events=[], delay=10, exit_code=1)
    r = _runner(tmp_path, monkeypatch, {"p": cli},
                limits={"startup_failure_threshold": 1})

    async def go():
        started = await r.start("worker", "work")
        await asyncio.sleep(0.3)
        await r.stop(started["agent_id"])
        await asyncio.wait_for(r.runs[started["agent_id"]].done.wait(), 15)
    asyncio.run(go())
    assert not _events(r, "startup_down")
    assert r.startup.availability("p") is None


def test_pinned_start_refused_when_provider_is_known_unauthenticated(tmp_path, monkeypatch):
    """Survived: skipping the `_auth_ok(...) is False` check in `_pin_health`."""
    marker = tmp_path / "launched"
    cli = h.fake_cli(tmp_path, "p", events=[{"type": "text", "text": "ok"}])
    binary = Path(cli["bin"])
    binary.write_text(binary.read_text().replace(
        "import json, sys, time", f"import json, sys, time\nopen({str(marker)!r}, 'w').close()"))
    r = _runner(tmp_path, monkeypatch, {"p": cli})
    monkeypatch.setattr(r, "_auth_ok", lambda name: False)
    refused = _start(r, model="m1")
    assert isinstance(refused, dict) and refused.get("reason") and not refused.get("agent_id")
    assert not refused.get("deferred") and not refused.get("paused"), refused
    assert not marker.exists()
    monkeypatch.setattr(r, "_auth_ok", lambda name: None)
    allowed = _start(r, model="m1")
    assert isinstance(allowed, dict) and allowed.get("provider") == "p", allowed


def _pinned_finished_run(tmp_path, monkeypatch):
    cli = h.fake_cli(tmp_path, "p", events=[{"type": "text", "text": "ok", "session_id": "s-1"}])
    cli["stream"]["session_id_paths"] = ["session_id"]
    cli["spawn"]["resume"] = ["--resume", "{session_id}"]
    r = _runner(tmp_path, monkeypatch, {"p": cli},
                limits={"startup_failure_threshold": 1,
                        "provider_down_cooldown_seconds": 60})
    started = _start(r, model="pinned-model")
    assert isinstance(started, dict) and started.get("model") == "pinned-model", started
    return r, started["agent_id"]


def test_pinned_steer_on_a_tripped_provider_is_a_structured_refusal(tmp_path, monkeypatch):
    """Survived: removing the `node.model_pinned` health check in steer."""
    r, agent_id = _pinned_finished_run(tmp_path, monkeypatch)
    before = r.tree.get(agent_id).status
    _mark_down(r.startup, "p", cooldown=60)
    fresh = Runner(r.paths, r.config)
    result = asyncio.run(fresh.steer(agent_id, "more"))
    assert result.get("steered") is False and result.get("reason") == "startup_down", result
    assert "omitting" in str(result).lower()
    assert r.tree.get(agent_id).status == before


def test_pinned_steer_after_restart_keeps_the_pinned_model(tmp_path, monkeypatch):
    """Survived: `_spec_of` ignoring `node.model_pinned`."""
    r, agent_id = _pinned_finished_run(tmp_path, monkeypatch)
    fresh = Runner(r.paths, r.config)

    async def go():
        result = await fresh.steer(agent_id, "more")
        run = fresh.runs.get(agent_id)
        if run:
            await asyncio.wait_for(run.done.wait(), 15)
        return result
    result = asyncio.run(go())
    assert result.get("steered") is not False or "session" in str(result), result
    run_dir = fresh.paths.run_dir(agent_id)
    commands = [json.loads(p.read_text()) for p in run_dir.glob("command*.json")]
    assert commands and all(c.get("model") == "pinned-model" for c in commands), commands
