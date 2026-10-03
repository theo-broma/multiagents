"""C7: an adopted run keeps the `max_steps` it was launched with.

Contract: `context/specs/phase6-closing-fixes.md`, item C7, including "Revision
after the advisor's check", which overrides the earlier wording. Ids: C7-R1,
C7-R1a, C7-R2, C7-R3, C7-R3a. (C7-R4, "the existing suites stay green", is not
a test of its own.)

Driven as test_sr_internal.py drives adoption: a real runner, a fake CLI that
stays alive across a server restart (`h.restart`), then a new runner adopting
it. The fake here differs from the SR one in one way: it says one word, then
waits for a "pre-gate" before emitting any step, so a test can restart the
server while the turn has counted nothing, and decide afterwards how many steps
the adopted turn sees. Every step is one `step_start` event, i.e. one step.

What is observed: whether the adopted turn is `stuck` with a `runaway_steps`
trip (and at which step), the `limit_hit` notice of that trip, and `check_agent`.
Wording the contract only paraphrases is matched loosely: "restored" for a
value read back from the launch record, "fallback" for one taken from the
current config.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import pc_harness as pc  # noqa: E402
import sc_harness as sc  # noqa: E402
import test_sr_steer_exit_race as h  # noqa: E402

_LOOP = 'for i in range(ctl.get("steps", 1)):'
_TAIL = 'if ctl.get("talk", True):'
_PRE = r'''emit("text", {{"id": "prt_h%d" % os.getpid(), "type": "text", "text": "hello"}})
pre = ctl.get("pregate")
if pre:
    while not os.path.exists(pre):
        time.sleep(0.05)
for i in range(ctl.get("steps", 1)):
    emit("step_start", {{"id": "prt_s%d_%d" % (os.getpid(), i), "type": "step-start"}})
with open(base + ".emitted", "a") as f:
    f.write("x")
'''
assert _LOOP in h._SCRIPT and _TAIL in h._SCRIPT
_SCRIPT = h._SCRIPT[:h._SCRIPT.index(_LOOP)] + _PRE + h._SCRIPT[h._SCRIPT.index(_TAIL):]

SETTLE = 1.5          # the watch polls every 0.25 s in these tests


@pytest.fixture
def w(tmp_path, monkeypatch):
    from multiagents.runner import Runner
    monkeypatch.setattr(Runner, "WATCH_POLL_SECONDS", 0.25)
    monkeypatch.setattr(h, "_SCRIPT", _SCRIPT)
    world = sc.World(tmp_path, monkeypatch, at=None)   # real clock: the wrapper's deadline is real
    fake = h.Fake(tmp_path, "acme")
    world.p.fakes["acme"] = fake
    world.p.providers["acme"] = fake.entry
    world.g = fake
    fake.pregate = tmp_path / "acme.pregate"
    fake.set(pregate=str(fake.pregate))
    yield world
    fake.open()
    fake.pregate.write_text("open")
    for pid in fake.pids():
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    world.down()


# ---------------------------------------------------------------- helpers --

def launch_line(w, rel: str, key: str = "max_steps") -> int:
    """The 1-based line of `key` in a config file as it is right now."""
    lines = (w.p.config / rel).read_text().splitlines()
    return next(i + 1 for i, line in enumerate(lines) if line.strip().startswith(key + ":"))


def emitted(w) -> int:
    path = Path(w.g.base + ".emitted")
    return len(path.read_text()) if path.exists() else 0


def trips(w, aid) -> list[str]:
    return [e.get("reason") for e in w.p.event_records()
            if e.get("kind") == "stuck" and e.get("agent") == aid]


def runaway_notices(w, aid) -> list[dict]:
    return [e for e in w.p.event_records()
            if e.get("kind") == "limit_hit" and e.get("node") == aid
            and str(e.get("key", "")).endswith("max_steps")]


def wording(thing) -> str:
    """The words of a notice or a limit entry, without its file paths (a
    pytest directory is named after the test, and so can contain "restored")."""
    text = json.dumps(thing)
    for path in set(re.findall(r"/[^\s\"():]+", text)):
        text = text.replace(path, "")
    return text.lower()


async def adopted(w, *, steps, edit=None, tamper=None, agent_kwargs=None):
    """Launch the run, tamper with host or run state, change the config,
    restart and adopt, then let the adopted turn see `steps` steps."""
    w.g.set(steps=steps, bare=True)
    aid = await h.running(w)
    if tamper:
        tamper(aid)
    if edit:
        edit()
        w.reload()
    await h.restart(w)
    assert w.status(aid) == "running", w.server.check_agent(aid)
    w.g.pregate.write_text("open")
    assert await pc.await_until(lambda: emitted(w) >= 1, 30), "the fake never emitted its steps"
    await asyncio.sleep(SETTLE)
    return aid


def run(coro):
    return asyncio.run(coro)


def set_agent(w, value):
    if value is None:
        w.p.agents["worker"].pop("max_steps", None)
    else:
        w.p.agents["worker"]["max_steps"] = value


def set_project(w, value):
    if value is None:
        w.p.project["limits"].pop("max_steps", None)
    else:
        w.p.project["limits"]["max_steps"] = value


def drop_from_ledger(w, aid, *, whole_record=False, keep=("timeout",)):
    """The host's launch record as an older server, or an eviction, left it."""
    ledger = w.runner.launch_limits
    with ledger.locked() as records:
        if whole_record:
            records.pop(aid)
        else:
            records[aid]["limits"].pop("max_steps", None)
        ledger.commit(records)


# =================================================================== R1 ====

def test_c7_r1_a_run_launched_with_100_is_not_tripped_at_100_after_the_config_drops_to_50(w):
    h.up(w, max_steps=100)

    async def go():
        aid = await adopted(w, steps=100, edit=lambda: set_agent(w, 50))
        return aid, w.status(aid), trips(w, aid)
    aid, status, tripped = run(go())
    assert status == "running" and tripped == [], \
        f"the adopted turn was governed by the new config (50): {status} {tripped}"


def test_c7_r1a_governed_by_100_trips_at_step_101_and_names_100(w):
    h.up(w, max_steps=100)

    async def go():
        aid = await adopted(w, steps=101, edit=lambda: set_agent(w, 50))
        return aid, w.status(aid), trips(w, aid), runaway_notices(w, aid)
    aid, status, tripped, notices = run(go())
    assert status == "stuck" and tripped == ["runaway_steps"], (status, tripped)
    assert [n.get("value") for n in notices] == [100], notices
    assert "101 steps exceeds max_steps=100" in json.dumps(w.p.event_records()), \
        "the trip detail names the limit that governed the turn"


def test_c7_r1_the_converse_launched_with_50_stays_governed_by_50_after_the_config_rises_to_100(w):
    h.up(w, max_steps=50)

    async def go():
        aid = await adopted(w, steps=51, edit=lambda: set_agent(w, 100))
        return aid, w.status(aid), trips(w, aid), runaway_notices(w, aid)
    aid, status, tripped, notices = run(go())
    assert status == "stuck" and tripped == ["runaway_steps"], \
        f"the adopted turn took the raised config value: {status} {tripped}"
    assert [n.get("value") for n in notices] == [50], notices


def test_c7_r1a_the_converse_does_not_trip_at_exactly_50(w):
    h.up(w, max_steps=50)

    async def go():
        aid = await adopted(w, steps=50, edit=lambda: set_agent(w, 100))
        return aid, w.status(aid), trips(w, aid)
    aid, status, tripped = run(go())
    assert status == "running" and tripped == [], (status, tripped)


def test_c7_r1a_a_value_set_at_project_level_is_restored_too(w):
    h.up(w)
    set_project(w, 100)
    w.reload()

    async def go():
        aid = await adopted(w, steps=100, edit=lambda: set_project(w, 50))
        quiet = (w.status(aid), trips(w, aid))
        return aid, quiet
    aid, quiet = run(go())
    assert quiet == ("running", []), f"project-level max_steps 100 was not restored: {quiet}"


def test_c7_r1a_a_project_level_value_trips_at_101_and_names_100(w):
    h.up(w)
    set_project(w, 100)
    w.reload()

    async def go():
        aid = await adopted(w, steps=101, edit=lambda: set_project(w, 50))
        return aid, trips(w, aid), runaway_notices(w, aid)
    aid, tripped, notices = run(go())
    assert tripped == ["runaway_steps"]
    assert [n.get("value") for n in notices] == [100], notices


def test_c7_r1a_the_project_level_converse_stays_at_50(w):
    h.up(w)
    set_project(w, 50)
    w.reload()

    async def go():
        aid = await adopted(w, steps=51, edit=lambda: set_project(w, 100))
        return aid, trips(w, aid), runaway_notices(w, aid)
    aid, tripped, notices = run(go())
    assert tripped == ["runaway_steps"], f"the raised project value governed the turn: {tripped}"
    assert [n.get("value") for n in notices] == [50], notices


def test_c7_r1_a_value_launched_from_the_agent_survives_the_agent_losing_it(w):
    """Launched under agent-level 100; the config now sets 50 only for the
    project. The record says 100, wherever the config now puts the limit."""
    h.up(w, max_steps=100)

    def edit():
        set_agent(w, None)
        set_project(w, 50)

    async def go():
        aid = await adopted(w, steps=100, edit=edit)
        return w.status(aid), trips(w, aid)
    assert run(go()) == ("running", [])


def test_c7_r1_two_restarts_keep_the_launch_value(w):
    h.up(w, max_steps=100)

    async def go():
        w.g.set(steps=100, bare=True)
        aid = await h.running(w)
        set_agent(w, 50)
        w.reload()
        await h.restart(w)
        await h.restart(w)
        w.g.pregate.write_text("open")
        assert await pc.await_until(lambda: emitted(w) >= 1, 30)
        await asyncio.sleep(SETTLE)
        return w.status(aid), trips(w, aid)
    assert run(go()) == ("running", [])


# ------------------------------------------------- R1a: where provenance shows

def test_c7_r1a_the_runaway_notice_says_restored_and_keeps_the_launch_file_and_line(w):
    h.up(w, max_steps=100)
    line = launch_line(w, "agents.yaml")
    agents_file = str((w.p.config / "agents.yaml").resolve())

    def edit():
        # The limit moves in the file: the line it had at launch is now elsewhere.
        set_agent(w, None)
        set_project(w, 50)

    async def go():
        aid = await adopted(w, steps=101, edit=edit)
        return runaway_notices(w, aid)
    notices = run(go())
    assert len(notices) == 1, notices
    note = notices[0]
    assert note["value"] == 100, note
    assert note["key"] == "agents.worker.max_steps", note
    source = note["source"]
    assert source.get("file") == agents_file and source.get("line") == line, \
        f"provenance must keep the original launch source, file and line: {source}"
    assert "restored" in wording(note), f"not marked as restored: {note}"
    assert "fallback" not in wording(note), note


def test_c7_r1a_a_project_level_launch_keeps_its_source_in_the_notice(w):
    h.up(w)
    set_project(w, 100)
    w.reload()
    line = launch_line(w, "project.yaml")
    project_file = str((w.p.config / "project.yaml").resolve())

    async def go():
        aid = await adopted(w, steps=101, edit=lambda: set_project(w, 50))
        return runaway_notices(w, aid)
    notices = run(go())
    assert len(notices) == 1, notices
    note = notices[0]
    assert note["key"] == "limits.max_steps" and note["value"] == 100, note
    assert note["source"].get("file") == project_file, note
    assert note["source"].get("line") == line, note
    assert "restored" in wording(note), note


def test_c7_r1a_check_agent_reports_the_restored_max_steps_with_its_provenance(w):
    h.up(w, max_steps=100)

    async def go():
        w.g.set(steps=0, bare=True)
        aid = await h.running(w)
        set_agent(w, 50)
        w.reload()
        await h.restart(w)
        return w.server.check_agent(aid)
    view = run(go())
    entries = [e for e in sc.find_key(view, "max_steps") if isinstance(e, dict)]
    assert entries, f"check_agent reports no max_steps limit for an adopted run: {view}"
    assert any(e.get("value") == 100 and "restored" in wording(e)
               for e in entries), entries


# =================================================================== R2 ====

def test_c7_r2_a_steer_picks_up_the_lowered_config_value(w):
    h.up(w, max_steps=100)

    async def go():
        w.g.set(steps=51, bare=True)
        aid = await h.running(w)
        set_agent(w, 50)
        w.reload()
        r = await w.server.steer_agent(aid, "next: go")
        assert r.get("steered") is True, r
        w.g.pregate.write_text("open")
        assert await pc.await_until(lambda: emitted(w) >= 1, 30)
        await asyncio.sleep(SETTLE)
        return aid, w.status(aid), trips(w, aid), runaway_notices(w, aid)
    aid, status, tripped, notices = run(go())
    assert status == "stuck" and tripped == ["runaway_steps"], (status, tripped)
    assert [n.get("value") for n in notices] == [50], notices


def test_c7_r2_a_steer_after_a_restart_still_picks_up_the_fresh_value(w):
    """The restored value governs the adopted turn only; the turn a steer
    launches resolves again."""
    h.up(w, max_steps=100)

    async def go():
        w.g.set(steps=51, bare=True)
        aid = await h.running(w)
        set_agent(w, 50)
        w.reload()
        await h.restart(w)
        r = await w.server.steer_agent(aid, "next: go")
        assert r.get("steered") is True, r
        w.g.pregate.write_text("open")
        assert await pc.await_until(lambda: emitted(w) >= 1, 30)
        await asyncio.sleep(SETTLE)
        return aid, trips(w, aid), runaway_notices(w, aid)
    aid, tripped, notices = run(go())
    assert tripped == ["runaway_steps"], tripped
    assert [n.get("value") for n in notices] == [50], notices


def test_c7_r2_a_steer_picks_up_a_raised_value_where_the_launch_value_would_trip(w):
    h.up(w, max_steps=50)

    async def go():
        w.g.set(steps=60, bare=True)
        aid = await h.running(w)
        set_agent(w, 100)
        w.reload()
        await h.restart(w)
        r = await w.server.steer_agent(aid, "next: go")
        assert r.get("steered") is True, r
        w.g.pregate.write_text("open")
        assert await pc.await_until(lambda: emitted(w) >= 1, 30)
        await asyncio.sleep(SETTLE)
        return aid, w.status(aid), trips(w, aid)
    aid, status, tripped = run(go())
    assert status == "running" and tripped == [], (status, tripped)


# =================================================================== R3 ====

def test_c7_r3_a_record_without_max_steps_falls_back_to_the_config_value(w):
    h.up(w, max_steps=100)

    async def go():
        aid = await adopted(w, steps=51, edit=lambda: set_agent(w, 50),
                            tamper=lambda a: drop_from_ledger(w, a))
        return aid, trips(w, aid), runaway_notices(w, aid)
    aid, tripped, notices = run(go())
    assert tripped == ["runaway_steps"], tripped
    assert [n.get("value") for n in notices] == [50], notices


def test_c7_r3_a_record_without_max_steps_does_not_trip_inside_the_config_value(w):
    h.up(w, max_steps=100)

    async def go():
        aid = await adopted(w, steps=50, edit=lambda: set_agent(w, 50),
                            tamper=lambda a: drop_from_ledger(w, a))
        return w.status(aid), trips(w, aid)
    assert run(go()) == ("running", [])


def test_c7_r3a_the_fallback_is_marked_as_a_fallback_not_as_restored(w):
    h.up(w, max_steps=100)
    agents_file = str((w.p.config / "agents.yaml").resolve())

    async def go():
        aid = await adopted(w, steps=51, edit=lambda: set_agent(w, 50),
                            tamper=lambda a: drop_from_ledger(w, a))
        return runaway_notices(w, aid)
    notices = run(go())
    assert len(notices) == 1, notices
    note = notices[0]
    text = wording(note)
    assert "fallback" in text, f"a fallback is not marked as one: {note}"
    assert "restored" not in text, note
    assert note["source"].get("file") == agents_file, \
        f"a fallback points at the current config: {note}"
    assert note["source"].get("line") == launch_line(w, "agents.yaml"), note


def test_c7_r3_a_missing_record_falls_back_and_says_so(w):
    h.up(w, max_steps=100)

    async def go():
        aid = await adopted(w, steps=51, edit=lambda: set_agent(w, 50),
                            tamper=lambda a: drop_from_ledger(w, a, whole_record=True))
        return trips(w, aid), runaway_notices(w, aid)
    tripped, notices = run(go())
    assert tripped == ["runaway_steps"], tripped
    assert [n.get("value") for n in notices] == [50], notices
    assert "fallback" in wording(notices[0]), notices


def test_c7_r3a_an_evicted_record_counts_as_a_missing_one(w):
    """256 later launches push this node out of the bounded ledger."""
    h.up(w, max_steps=100)

    def evict(aid):
        ledger = w.runner.launch_limits
        for i in range(ledger.KEEP):
            ledger.record(f"ag-evict{i:03d}", {}, 1.0)
        assert aid not in ledger.read()

    async def go():
        aid = await adopted(w, steps=51, edit=lambda: set_agent(w, 50), tamper=evict)
        return trips(w, aid), runaway_notices(w, aid)
    tripped, notices = run(go())
    assert tripped == ["runaway_steps"], tripped
    assert [n.get("value") for n in notices] == [50], notices
    assert "fallback" in wording(notices[0]), notices


def test_c7_r3a_a_partial_record_restores_the_timeout_and_falls_back_for_max_steps(w):
    """The entry keeps its timeout (900, from the agent) but not max_steps. The
    config now says timeout 3 and max_steps 50: the timeout is the launch's, so
    nothing times out; max_steps is the config's, so 51 steps trip."""
    h.up(w, max_steps=100, timeout=900)

    def edit():
        set_agent(w, 50)
        w.p.agents["worker"]["timeout"] = 3

    async def go():
        aid = await adopted(w, steps=51, edit=edit, tamper=lambda a: drop_from_ledger(w, a))
        await asyncio.sleep(4)                      # past the config's 3 s
        return trips(w, aid), runaway_notices(w, aid)
    tripped, notices = run(go())
    assert tripped == ["runaway_steps"], \
        f"the timeout was not restored from the partial record (or max_steps was): {tripped}"
    assert [n.get("value") for n in notices] == [50], notices
    assert "fallback" in wording(notices[0]), notices


def test_c7_r3_a_forged_max_steps_in_command_json_has_no_effect_on_a_restored_value(w):
    h.up(w, max_steps=100)

    def forge(aid):
        path = h.run_dir(w, aid) / "command.json"
        command = json.loads(path.read_text())
        command["max_steps"] = 60000
        path.write_text(json.dumps(command))

    async def go():
        aid = await adopted(w, steps=101, edit=lambda: set_agent(w, 50), tamper=forge)
        return trips(w, aid), runaway_notices(w, aid)
    tripped, notices = run(go())
    assert tripped == ["runaway_steps"], f"a forged command.json extended the budget: {tripped}"
    assert [n.get("value") for n in notices] == [100], notices


def test_c7_r3_a_forged_max_steps_in_command_json_has_no_effect_on_a_fallback(w):
    h.up(w, max_steps=100)

    def forge(aid):
        path = h.run_dir(w, aid) / "command.json"
        command = json.loads(path.read_text())
        command["max_steps"] = 60000
        path.write_text(json.dumps(command))
        drop_from_ledger(w, aid)

    async def go():
        aid = await adopted(w, steps=51, edit=lambda: set_agent(w, 50), tamper=forge)
        return trips(w, aid), runaway_notices(w, aid)
    tripped, notices = run(go())
    assert tripped == ["runaway_steps"], f"a forged command.json extended the budget: {tripped}"
    assert [n.get("value") for n in notices] == [50], notices


def test_c7_r3_a_forged_limits_block_in_tree_json_has_no_effect(w):
    h.up(w, max_steps=100)
    forged = {"max_steps": {"value": 60000, "source": "agent",
                            "source_detail": {"layer": "agent", "file": "x", "line": 1}}}

    def forge(aid):
        w.runner.tree.update(aid, limits=forged)
        drop_from_ledger(w, aid)

    async def go():
        aid = await adopted(w, steps=51, edit=lambda: set_agent(w, 50), tamper=forge)
        return trips(w, aid), runaway_notices(w, aid)
    tripped, notices = run(go())
    assert tripped == ["runaway_steps"], f"a forged tree.json limit extended the budget: {tripped}"
    assert [n.get("value") for n in notices] == [50], notices
