"""Phase 0, group D — P0-R5: the MCP server never runs on a stale config
silently (`bug-2138e6`).

Contract: `context/specs/phase0-runtime-repairs.md` § P0-R5.

Everything goes through the server's own surface: a real project on disk
(`MULTIAGENTS_PROJECT`), the tool functions called as the MCP host would call
them, config edited by writing the files an operator — or a subagent — would
edit. Agents are fake CLIs (small Python scripts) so a spawn is a real
subprocess without a real provider.

Two things are read off objects rather than tool results, both because the
contract names them:

- `runner().runs[id].supervisor.max_steps` — P0-R5.1 is verified in the
  contract as "the next start_agent constructs its Supervisor with the new
  value", and no tool result reports it.
- `runner().tree`, `runner().runs[id]`, `runner().providers` — P0-R5.6 and
  P0-R5.7 are about object identity ("the same objects before and after",
  "the new spawn uses the new provider object").

The reload announcement (P0-R5.2) and the load-error report (P0-R5.4) have no
agreed field name, so they are asserted as "some key or string value in the
result names the file", and the unchanged case as "nothing does".

Every edit moves the file's mtime forward explicitly. Two writes inside one
filesystem timestamp tick are indistinguishable by mtime, and a fingerprint
built on mtime is a legitimate implementation of P0-R5.5; the tests must not
fail it for a reason no operator could hit.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
import time
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import c3_harness as h  # noqa: E402

from multiagents import config as config_mod  # noqa: E402
from multiagents import server  # noqa: E402

MAX_STEPS_BEFORE = 111
MAX_STEPS_AFTER = 222
MAX_DEPTH_BEFORE = 7          # the shipped default is 3: never fall back to it
MAX_DEPTH_AFTER = 9


# ---------------------------------------------------------------- helpers --

def _fake_agent(tmp_path: Path, name: str, marker: Path, delay: float = 0.5) -> str:
    """An executable stand-in for a provider CLI that leaves `marker` behind."""
    script = tmp_path / f"{name}.py"
    event = json.dumps({"type": "result", "subtype": "success", "result": "done"})
    script.write_text(
        f"#!{sys.executable}\n"
        "import pathlib, sys, time\n"
        f"pathlib.Path({str(marker)!r}).write_text('ran')\n"
        f"time.sleep({delay!r})\n"
        f"print({event!r})\n"
        "sys.stdout.flush()\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


def _provider(bin_path: str) -> dict:
    return {"bin": bin_path, "spawn": {"args": ["--fake-cli"]},
            "stream": {"format": "ndjson",
                       "rules": [{"match": {"type": "result"}, "as": "result",
                                  "fields": {"status": "subtype", "text": "result"}}]}}


class Project:
    """A project on disk whose config layer the test edits."""

    def __init__(self, tmp_path: Path):
        self.tmp = tmp_path
        self.root = tmp_path / "proj"
        h.make_git_repo(self.root)
        self.config = self.root / ".multiagents" / "config"
        (self.config / "agents").mkdir(parents=True)
        self.events = self.root / ".multiagents" / "events.jsonl"
        self._tick = time.time()
        self.marker = tmp_path / "ran-1"
        self.bin = _fake_agent(tmp_path, "fake1", self.marker)
        self.providers = {"fakep": _provider(self.bin)}
        self.agents = {"worker": {"provider": "fakep", "model": "m", "writes": False}}
        self.project = {"team": "",
                        "limits": {"max_depth": MAX_DEPTH_BEFORE,
                                   "max_steps": MAX_STEPS_BEFORE}}
        self.write_providers()
        self.write_agents()
        self.write_project()

    def write(self, rel: str, text: str) -> None:
        path = self.config / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        self._tick += 10
        os.utime(path, (self._tick, self._tick))

    def write_project(self) -> None:
        self.write("project.yaml", yaml.safe_dump(self.project))

    def write_agents(self) -> None:
        self.write("agents.yaml", yaml.safe_dump({"agents": self.agents}))

    def write_providers(self) -> None:
        self.write("providers.yaml", yaml.safe_dump({"providers": self.providers}))

    def event_lines(self) -> list[str]:
        if not self.events.is_file():
            return []
        return [line for line in self.events.read_text().splitlines() if line.strip()]


@pytest.fixture
def project(tmp_path, monkeypatch):
    h.as_root(monkeypatch)
    proj = Project(tmp_path)
    monkeypatch.setenv("MULTIAGENTS_PROJECT", str(proj.root))
    server._reset()
    yield proj
    server._reset()


def _strings(value):
    if isinstance(value, dict):
        for k, v in value.items():
            yield str(k)
            yield from _strings(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _strings(v)
    elif value is not None:
        yield str(value)


def _mentions(result, needle: str) -> bool:
    return any(needle in s for s in _strings(result))


async def _drain(timeout: float = 20) -> None:
    """Let every run this server started finish inside the current loop."""
    for run in list(server.runner().runs.values()):
        await asyncio.wait_for(run.done.wait(), timeout)


async def _start(agent: str = "worker") -> dict:
    result = await server.start_agent(agent, "do the thing")
    return result


def _max_steps_of(agent_id: str) -> int:
    return server.runner().runs[agent_id].supervisor.max_steps


# ---------------------------------------------------------------- P0-R5.1 --

def test_p0_r5_1_list_agents_sees_an_edited_limit(project):
    assert server.list_agents()["max_depth"] == MAX_DEPTH_BEFORE
    project.project["limits"]["max_depth"] = MAX_DEPTH_AFTER
    project.write_project()
    assert server.list_agents()["max_depth"] == MAX_DEPTH_AFTER


def test_p0_r5_1_next_start_agent_builds_its_supervisor_with_the_new_max_steps(project):
    async def go():
        first = await _start()
        assert "agent_id" in first, first
        assert _max_steps_of(first["agent_id"]) == MAX_STEPS_BEFORE
        await _drain()
        project.project["limits"]["max_steps"] = MAX_STEPS_AFTER
        project.write_project()
        second = await _start()
        assert "agent_id" in second, second
        steps = _max_steps_of(second["agent_id"])
        await _drain()
        return steps
    assert asyncio.run(go()) == MAX_STEPS_AFTER


def test_p0_r5_1_an_edited_instruction_file_is_detected(project):
    """The agent instruction files the roster reads are config layer files."""
    project.agents["worker"]["instructions"] = "worker-brief.md"
    project.write_agents()
    project.write("agents/worker-brief.md", "# Worker\n\nFirst version.\n")
    server.list_agents()                                   # load settles
    assert not _mentions(server.list_agents(), "worker-brief.md")
    project.write("agents/worker-brief.md", "# Worker\n\nSecond version.\n")
    result = server.list_agents()
    assert _mentions(result, "worker-brief.md"), (
        f"an edited instruction file must trigger an announced reload: {result}")


# ---------------------------------------------------------------- P0-R5.2 --

def test_p0_r5_2_the_triggering_call_names_the_changed_file(project):
    before = server.list_agents()
    assert not _mentions(before, "project.yaml"), before
    project.project["limits"]["max_depth"] = MAX_DEPTH_AFTER
    project.write_project()
    announced = server.list_agents()
    assert _mentions(announced, "project.yaml"), (
        f"P0-R5.2: the call that triggered a reload must name the changed file: "
        f"{announced}")
    quiet = server.list_agents()
    assert not _mentions(quiet, "project.yaml"), (
        f"the announcement belongs to the triggering call only: {quiet}")


def test_p0_r5_2_names_every_changed_file_and_no_unchanged_one(project):
    server.list_agents()
    project.project["limits"]["max_depth"] = MAX_DEPTH_AFTER
    project.write_project()
    project.agents["worker"]["description"] = "edited"
    project.write_agents()
    announced = server.list_agents()
    assert _mentions(announced, "project.yaml"), announced
    assert _mentions(announced, "agents.yaml"), announced
    assert not _mentions(announced, "providers.yaml"), (
        f"providers.yaml did not change and must not be named: {announced}")


def test_p0_r5_2_start_agent_announces_the_reload_it_triggered(project):
    async def go():
        server.list_agents()
        project.project["limits"]["max_steps"] = MAX_STEPS_AFTER
        project.write_project()
        result = await _start()
        await _drain()
        return result
    result = asyncio.run(go())
    assert "agent_id" in result, result
    assert _mentions(result, "project.yaml"), result


# ---------------------------------------------------------------- P0-R5.3 --

def test_p0_r5_3_a_running_agent_keeps_the_config_it_started_with(project):
    project.bin = _fake_agent(project.tmp, "slow", project.marker, delay=4)
    project.providers["fakep"] = _provider(project.bin)
    project.write_providers()

    async def go():
        old = await _start()
        old_id = old["agent_id"]
        old_run = server.runner().runs[old_id]
        project.project["limits"]["max_steps"] = MAX_STEPS_AFTER
        project.write_project()
        new = await _start()
        assert "agent_id" in new, new
        assert server.runner().tree.get(old_id).status == "running", (
            "the reload happened while the first agent was still running")
        assert old_run.supervisor.max_steps == MAX_STEPS_BEFORE
        assert _max_steps_of(new["agent_id"]) == MAX_STEPS_AFTER
        await _drain()
        return old_id
    old_id = asyncio.run(go())
    assert server.runner().tree.get(old_id).status == "done"


# ---------------------------------------------------------------- P0-R5.4 --

def test_p0_r5_4_invalid_yaml_keeps_the_previous_config_and_reports_every_call(project):
    assert server.list_agents()["max_depth"] == MAX_DEPTH_BEFORE
    project.write("project.yaml", "limits: [max_depth: 9\n  team: {\n")
    for attempt in range(3):
        result = server.list_agents()
        assert _mentions(result, "project.yaml"), (
            f"call {attempt + 1} after the file broke must report the load error: "
            f"{result}")
        limits = server.runner().config.limits
        assert limits.get("max_depth") == MAX_DEPTH_BEFORE, (
            f"the previous config stays in force, never the defaults: {limits}")
        assert limits.get("max_steps") == MAX_STEPS_BEFORE, limits


def test_p0_r5_4_fixing_the_file_ends_the_error_and_applies_the_new_value(project):
    server.list_agents()
    project.write("project.yaml", "limits: [unclosed\n")
    assert _mentions(server.list_agents(), "project.yaml")
    project.project["limits"]["max_depth"] = MAX_DEPTH_AFTER
    project.write_project()
    server.list_agents()                                   # the fixing reload
    healthy = server.list_agents()
    assert healthy["max_depth"] == MAX_DEPTH_AFTER, healthy
    assert not _mentions(healthy, "project.yaml"), (
        f"once fixed, the error is no longer reported: {healthy}")


def test_p0_r5_4_a_spawn_under_a_broken_config_reports_it_and_uses_the_old_limits(project):
    async def go():
        server.list_agents()
        project.write("project.yaml", "limits: {max_steps: 222\n")
        result = await _start()
        steps = _max_steps_of(result["agent_id"]) if "agent_id" in result else None
        await _drain()
        return result, steps
    result, steps = asyncio.run(go())
    assert _mentions(result, "project.yaml"), (
        f"start_agent uses config, so it must report the load error: {result}")
    if steps is not None:                     # proceeding on the old config is allowed
        assert steps == MAX_STEPS_BEFORE, steps


def test_p0_r5_4_a_roster_that_fails_validation_keeps_the_previous_roster(project):
    """Valid YAML, wrong shape: `agents` must be a mapping."""
    server.list_agents()
    project.write("agents.yaml", yaml.safe_dump({"agents": ["worker", "other"]}))
    result = server.list_agents()
    assert _mentions(result, "agents.yaml"), result
    assert "worker" in server.runner().config.agents, (
        "the previous roster stays in force")
    again = server.list_agents()
    assert _mentions(again, "agents.yaml"), again


# ---------------------------------------------------------------- P0-R5.5 --

def test_p0_r5_5_no_load_config_call_when_nothing_changed(project, monkeypatch):
    server.list_agents()                                   # built and loaded once
    calls = []
    real = config_mod.load

    def counting(*args, **kwargs):
        calls.append(args)
        return real(*args, **kwargs)

    monkeypatch.setattr(config_mod, "load", counting)
    monkeypatch.setattr(server, "load_config", counting)

    for _ in range(3):
        server.list_agents()
        server.list_models()
        server.agent_tree()
    assert calls == [], f"load_config ran {len(calls)} times with no file changed"

    # The same counter must see a reload, or the assertion above is vacuous.
    project.project["limits"]["max_depth"] = MAX_DEPTH_AFTER
    project.write_project()
    assert server.list_agents()["max_depth"] == MAX_DEPTH_AFTER
    assert len(calls) >= 1, "a changed file must go through load_config"


# ---------------------------------------------------------------- P0-R5.6 --

def test_p0_r5_6_tree_and_in_flight_runs_are_the_same_objects_after_a_reload(project):
    project.bin = _fake_agent(project.tmp, "slow", project.marker, delay=3)
    project.providers["fakep"] = _provider(project.bin)
    project.write_providers()

    async def go():
        started = await _start()
        agent_id = started["agent_id"]
        run = server.runner()
        tree, in_flight = run.tree, run.runs[agent_id]
        run.tree.defer({"agent": "worker", "task": "later"},
                       retry_after=time.time() + 3600, reason="test")
        deferred_before = server.agent_tree()["deferred"]

        project.project["limits"]["max_depth"] = MAX_DEPTH_AFTER
        project.write_project()
        assert server.list_agents()["max_depth"] == MAX_DEPTH_AFTER   # reloaded

        after = server.runner()
        assert after.tree is tree
        assert after.runs.get(agent_id) is in_flight
        assert server.agent_tree()["deferred"] == deferred_before == 1
        waited = await server.wait_for_agents([agent_id], 20)
        await _drain()
        return agent_id, waited
    agent_id, waited = asyncio.run(go())
    assert [c["agent_id"] for c in waited["changed"]] == [agent_id], waited
    assert waited["changed"][0]["status"] == "done", waited


def test_p0_r5_6_an_in_flight_run_can_still_be_stopped_after_a_reload(project):
    project.bin = _fake_agent(project.tmp, "slow", project.marker, delay=30)
    project.providers["fakep"] = _provider(project.bin)
    project.write_providers()

    async def go():
        started = await _start()
        agent_id = started["agent_id"]
        project.project["limits"]["max_depth"] = MAX_DEPTH_AFTER
        project.write_project()
        assert server.list_agents()["max_depth"] == MAX_DEPTH_AFTER
        stopped = await server.stop_agent(agent_id)
        await _drain(timeout=15)
        return agent_id, stopped
    agent_id, stopped = asyncio.run(go())
    assert "error" not in stopped, stopped
    assert server.runner().tree.get(agent_id).status == "cancelled"


# ---------------------------------------------------------------- P0-R5.7 --

def test_p0_r5_7_a_changed_provider_binary_is_what_the_next_spawn_runs(project):
    new_marker = project.tmp / "ran-2"
    new_bin = _fake_agent(project.tmp, "fake2", new_marker)

    async def go():
        server.list_agents()
        old_provider = server.runner().providers["fakep"]
        project.providers["fakep"] = _provider(new_bin)
        project.write_providers()
        result = await _start()
        assert "agent_id" in result, result
        run = server.runner()
        spawned_with = run.runs[result["agent_id"]].provider
        await _drain()
        return old_provider, spawned_with, run.providers["fakep"]
    old_provider, spawned_with, current = asyncio.run(go())
    assert new_marker.exists(), "the next spawn must execute the new binary"
    assert not project.marker.exists(), "the old binary must not have run"
    assert current is not old_provider
    assert spawned_with is current, "the spawn used the rebuilt provider object"
    assert current.bin == new_bin


def test_p0_r5_7_an_added_provider_and_agent_can_be_spawned(project):
    marker = project.tmp / "ran-q"
    project_bin = _fake_agent(project.tmp, "fakeq", marker)

    async def go():
        server.list_agents()
        project.providers["fakeq"] = _provider(project_bin)
        project.write_providers()
        project.agents["worker2"] = {"provider": "fakeq", "model": "m", "writes": False}
        project.write_agents()
        listed = [a["name"] for a in server.list_agents()["agents"]]
        result = await _start("worker2")
        await _drain()
        return listed, result
    listed, result = asyncio.run(go())
    assert "worker2" in listed
    assert "agent_id" in result and "error" not in result, result
    assert marker.exists()


def test_p0_r5_7_a_removed_agent_can_no_longer_be_spawned(project):
    async def go():
        server.list_agents()
        project.agents = {"other": {"provider": "fakep", "model": "m", "writes": False}}
        project.write_agents()
        listed = [a["name"] for a in server.list_agents()["agents"]]
        result = await _start("worker")
        await _drain()
        return listed, result
    listed, result = asyncio.run(go())
    assert "worker" not in listed and "other" in listed, listed
    assert "error" in result and "agent_id" not in result, result
    assert not project.marker.exists()


# ---------------------------------------------------------------- P0-R5.8 --

def test_p0_r5_8_a_reload_appends_one_event_naming_the_changed_file(project):
    server.list_agents()
    baseline = project.event_lines()
    server.list_agents()
    assert project.event_lines() == baseline, "no change, no event"

    project.project["limits"]["max_depth"] = MAX_DEPTH_AFTER
    project.write_project()
    server.list_agents()
    added = project.event_lines()[len(baseline):]
    assert len(added) == 1, f"one reload, one event: {added}"
    assert "project.yaml" in added[0], added[0]


def test_p0_r5_8_one_event_for_a_reload_of_several_files(project):
    server.list_agents()
    baseline = project.event_lines()
    project.project["limits"]["max_depth"] = MAX_DEPTH_AFTER
    project.write_project()
    project.agents["worker"]["description"] = "edited"
    project.write_agents()
    server.list_agents()
    added = project.event_lines()[len(baseline):]
    assert len(added) == 1, added
    assert "project.yaml" in added[0] and "agents.yaml" in added[0], added[0]


def test_p0_r5_8_a_failed_reload_is_recorded_too(project):
    server.list_agents()
    baseline = project.event_lines()
    project.write("project.yaml", "limits: [unclosed\n")
    server.list_agents()
    added = project.event_lines()[len(baseline):]
    assert len(added) == 1, added
    assert "project.yaml" in added[0], added[0]
    success = baseline + added

    project.project["limits"]["max_depth"] = MAX_DEPTH_AFTER
    project.write_project()
    server.list_agents()
    fixed = project.event_lines()[len(success):]
    assert len(fixed) == 1, fixed

    def outcome(line):                    # the event minus its timestamps
        entry = json.loads(line)
        return {k: v for k, v in entry.items() if not isinstance(v, (int, float))}
    assert outcome(fixed[0]) != outcome(added[0]), (
        "the event must say whether the reload succeeded or failed to load")
