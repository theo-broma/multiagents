"""bug-30623f: consult replies belong to the turn that produced them (R1–R4).

Replies are correlated on the host side: providers here print plain, unmarked
text and know nothing about request ids.

Most tests go through the public `Runner.consult` with a real fake-provider
subprocess. A *late* reply (R1/R2) and a *late finalization* (R4) cannot be
produced by a real process — a timed-out turn's process is killed and its
reader cancelled — so those tests reproduce the late event on the stale `Run`
object the timed-out attempt left behind. That is the one seam used, and it is
confined to `stale_run`, `publish_stale_reply` and `finalize_late`.
"""
import asyncio
import json
from pathlib import Path
import sys
import time

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))
import c3_harness as h
from multiagents.runner import Runner

BOUND = 8.0        # every wait on the world; red tests must fail well inside it
POLL = 0.02

PROVIDER = '''import json, sys, time
from pathlib import Path
probe = Path(sys.argv[1])
prompt = sys.argv[2]
resume = sys.argv[3:]
n = len(sorted(probe.glob("call-*.json")))
(probe / f"call-{n:03}.json").write_text(json.dumps({"prompt": prompt, "resume": resume}))
end = time.monotonic() + 15
while (probe / f"hold-{n}").exists() and not (probe / f"release-{n}").exists():
    if time.monotonic() > end:
        break
    time.sleep(0.02)
if any(int(c.name.split("-")[1]) <= n for c in probe.glob("crash-*")):
    sys.exit(1)
reply = probe / f"reply-{n}"
text = reply.read_text() if reply.exists() else f"ANSWER {n}"
sid = "session-created-here"
if (probe / "chunks").exists():
    print(json.dumps({"type": "text", "text": "FIRST", "session": sid}))
    text = "SECOND"
if text:
    print(json.dumps({"type": "text", "text": text, "session": sid}))
else:
    # Worked but said nothing: steps count as work, so the turn ends idle.
    for _ in range(12):
        print(json.dumps({"type": "step", "session": sid}))
'''


@pytest.fixture
def world(tmp_path, monkeypatch):
    probe = tmp_path / "probe"
    probe.mkdir()
    script = tmp_path / "provider.py"
    script.write_text(PROVIDER)
    entry = {
        "bin": sys.executable,
        "family": "original",
        "spawn": {"args": [str(script), str(probe), "{prompt}"],
                  "resume": ["--resume", "{session_id}"]},
        "stream": {"format": "ndjson", "session_id_paths": ["session"],
                   "rules": [{"match": {"type": "text"}, "as": "text",
                              "fields": {"text": "text"}},
                             {"match": {"type": "step"}, "as": "step"}]},
    }
    spec = h.AgentSpec("advisor", "fake", "m", conversational=True, writes=False)
    runner = h.make_runner(tmp_path / "project", monkeypatch,
                           agents={"advisor": spec}, providers={"fake": entry})
    monkeypatch.setattr(Runner, "CONSULT_LOCK_SLACK_SECONDS", 0)
    yield runner, probe
    asyncio.run(runner.shutdown(detach=False))


def consult(world, question="question", timeout=10):
    runner, _ = world
    return asyncio.run(runner.consult("advisor", question, timeout=timeout))


def calls(probe):
    return [json.loads(p.read_text()) for p in sorted(probe.glob("call-*.json"))]


def events(runner, kind):
    return [row for line in runner.paths.events_file.read_text().splitlines()
            if (row := json.loads(line)).get("kind") == kind]


async def until(predicate):
    end = time.monotonic() + BOUND
    while not predicate():
        assert time.monotonic() < end, "condition never became true"
        await asyncio.sleep(POLL)


def hold(probe, n):
    (probe / f"hold-{n}").write_text("")


def release(probe, n):
    (probe / f"release-{n}").write_text("")


async def timed_out_first_turn(runner, probe):
    """Turn 0 hangs and its consult times out; returns (result, stale Run)."""
    hold(probe, 0)
    stale = []

    async def capture():
        await until(lambda: any(r.node_id for r in runner.runs.values()))
        stale.append(next(iter(runner.runs.values())))

    grab = asyncio.create_task(capture())
    first = await runner.consult("advisor", "first question", timeout=1)
    await grab
    return first, stale[0]


async def second_turn_pending(runner, probe, question="second question", n=1, timeout=10):
    """Start the next consult with turn `n` held; returns its task."""
    hold(probe, n)
    task = asyncio.create_task(runner.consult("advisor", question, timeout=timeout))
    await until(lambda: len(calls(probe)) > n)
    return task


def adopt(runner, stale):
    """The stale Run surfaces under the conversation id the pending turn uses.

    A timed-out consult leaves its node cancelled and the next consult starts
    a conversation under a new id, so the late attempt is pointed at that id.
    Returns the active (turn 2) Run.
    """
    active = next(r for r in runner.runs.values() if r is not stale and r.node_id != stale.node_id)
    stale.node_id = active.node_id
    return active


# ---------------------------------------------------------------- R1 / shape

def test_r1_ids_context_and_existing_result_shape(world):
    first = consult(world, "first question")
    second = consult(world, "second question")
    assert not first.get("error"), first
    assert not second.get("error"), second
    assert first["request_id"] != second["request_id"]
    assert first["agent_id"] == second["agent_id"]
    assert first["turn"] == 1 and second["turn"] == 2
    assert first["reply"] == "ANSWER 0" and second["reply"] == "ANSWER 1"
    assert second["status"] == "idle"
    assert {"usage", "note", "commit", "base_commit"} <= second.keys()
    _, probe = world
    assert calls(probe)[1]["resume"] == ["--resume", "session-created-here"]
    assert calls(probe)[1]["prompt"].endswith("second question")


def test_r1_plain_unmarked_reply_is_accepted_verbatim(world):
    _, probe = world
    (probe / "reply-0").write_text("just some advice, no ids anywhere")
    result = consult(world)
    assert not result.get("error"), result
    assert result["reply"] == "just some advice, no ids anywhere"


def test_r1_prompt_carries_no_correlation_envelope(world):
    # Correlation is the host's business; the provider is not asked to echo anything.
    result = consult(world, "plain question")
    _, probe = world
    assert not result.get("error"), result
    assert result["request_id"] not in calls(probe)[0]["prompt"]


def test_r1_reply_can_arrive_in_multiple_stream_events(world):
    _, probe = world
    (probe / "chunks").write_text("")
    result = consult(world)
    assert not result.get("error"), result
    assert result["reply"] == "FIRST\nSECOND"


def test_r1_validation_errors_keep_the_request_id(world):
    runner, _ = world
    with pytest.raises(KeyError) as exc:
        asyncio.run(runner.consult("missing", "question"))
    assert len(exc.value.consult_request_id) == 32


def test_r1_late_reply_of_timed_out_turn_is_never_returned_for_next_request(world):
    runner, probe = world

    async def go():
        first, stale = await timed_out_first_turn(runner, probe)
        assert first["timed_out"], first
        (probe / "reply-1").write_text("CURRENT ANSWER")
        task = await second_turn_pending(runner, probe)
        # The timed-out turn's output arrives late, while turn 2 is pending,
        # and the older Run is republished under the conversation id.
        adopt(runner, stale)
        stale.text_parts.append("OLD ANSWER")
        runner.runs[stale.node_id] = stale
        release(probe, 1)
        return first, await asyncio.wait_for(task, BOUND)

    first, second = asyncio.run(go())
    assert first["request_id"] != second["request_id"]
    assert "OLD ANSWER" not in json.dumps(second)
    assert not second.get("error"), second
    assert second["reply"] == "CURRENT ANSWER"
    discarded = events(runner, "consult_reply_discarded")
    assert discarded, "the discarded late reply must be recorded"
    last = discarded[-1]
    assert last["request_id"] == second["request_id"]
    assert first["request_id"] in last["observed_request_ids"]


def test_r1_late_reply_arriving_after_own_reply_still_not_mixed_in(world):
    runner, probe = world

    async def go():
        first, stale = await timed_out_first_turn(runner, probe)
        (probe / "reply-1").write_text("CURRENT ANSWER")
        task = await second_turn_pending(runner, probe)
        active = adopt(runner, stale)
        release(probe, 1)
        result = await asyncio.wait_for(task, BOUND)
        stale.node_id = active.node_id
        stale.text_parts.append("OLD ANSWER")     # after the fact: next turn must stay clean
        runner.runs[stale.node_id] = stale
        (probe / "reply-2").write_text("THIRD ANSWER")
        third = await runner.consult("advisor", "third question", timeout=10)
        return result, third

    second, third = asyncio.run(go())
    assert second["reply"] == "CURRENT ANSWER"
    assert not third.get("error"), third
    assert third["reply"] == "THIRD ANSWER"
    assert "OLD ANSWER" not in json.dumps(third)


# ---------------------------------------------------------------------- R2

def test_r2_own_empty_turn_never_returns_another_turns_text(world):
    runner, probe = world

    async def go():
        first, stale = await timed_out_first_turn(runner, probe)
        (probe / "reply-1").write_text("")             # turn 2 says nothing
        task = await second_turn_pending(runner, probe)
        adopt(runner, stale)
        stale.text_parts.append("OLD ANSWER")
        runner.runs[stale.node_id] = stale
        release(probe, 1)
        return await asyncio.wait_for(task, BOUND)

    result = asyncio.run(go())
    assert "OLD ANSWER" not in json.dumps(result)
    if not result.get("error"):
        # A turn that legitimately finished idle and empty keeps its answer.
        assert result["reply"] == "" and result["status"] == "idle", result


def test_r2_own_turn_crash_never_returns_another_turns_text(world):
    runner, probe = world

    async def go():
        first, stale = await timed_out_first_turn(runner, probe)
        (probe / "crash-1").write_text("")             # turn 2 and its free retry die
        task = await second_turn_pending(runner, probe, timeout=2)
        adopt(runner, stale)
        stale.text_parts.append("OLD ANSWER")
        runner.runs[stale.node_id] = stale
        release(probe, 1)
        return await asyncio.wait_for(task, BOUND)

    result = asyncio.run(go())
    assert "OLD ANSWER" not in json.dumps(result)
    assert result.get("error"), result
    assert not result.get("reply")


def test_r2_legitimate_empty_idle_reply_is_not_an_error(world):
    _, probe = world
    (probe / "reply-0").write_text("")
    result = consult(world)
    assert not result.get("error"), result
    assert result["reply"] == ""
    assert result["status"] == "idle"


def test_r2_empty_idle_reply_on_a_later_turn_is_not_an_error(world):
    _, probe = world
    first = consult(world)
    (probe / "reply-1").write_text("")
    second = consult(world)
    assert first["reply"] == "ANSWER 0"
    assert not second.get("error"), second
    assert second["reply"] == "" and second["status"] == "idle"
    assert second["request_id"] != first["request_id"]


# ---------------------------------------------------------------------- R3

def test_r3_session_records_provider_family_provenance(world):
    first = consult(world)
    runner, _ = world
    assert runner.tree.get(first["agent_id"]).session_provider_family == "original"


def test_r3_changed_provider_family_starts_fresh_and_explains(world, monkeypatch):
    first = consult(world)
    runner, probe = world
    # A route changed after the session was created, and the server restarted:
    # the family recorded at creation must win over today's provider object.
    monkeypatch.setattr(runner.providers["fake"], "family", "different")
    runner.runs.clear()
    second = consult(world)
    assert not second.get("error"), second
    assert second["agent_id"] != first["agent_id"]
    assert second["session"] == "fresh"
    assert second["reason"] == "provider_changed"
    assert calls(probe)[1]["resume"] == []
    assert events(runner, "conversation_replaced")[-1]["reason"] == "provider_changed"
    assert runner.tree.get(second["agent_id"]).session_provider_family == "different"


def test_r3_same_family_resumes_after_server_restart(world):
    first = consult(world)
    runner, probe = world
    runner.runs.clear()
    second = consult(world)
    assert not second.get("error"), second
    assert second["agent_id"] == first["agent_id"]
    assert calls(probe)[1]["resume"] == ["--resume", "session-created-here"]
    assert second.get("reason") != "provider_changed"
    assert second.get("session") != "fresh"


def test_r3_roster_moves_to_another_family_without_reusing_session(world, monkeypatch):
    first = consult(world)
    runner, probe = world
    entry = {**runner.config.providers["fake"], "family": "foreign"}
    monkeypatch.setitem(runner.providers, "other", h.make_provider("other", **entry))
    monkeypatch.setitem(runner.config.agents, "advisor",
                        h.AgentSpec("advisor", "other", "m", conversational=True, writes=False))
    second = consult(world)
    assert not second.get("error"), second
    assert second["agent_id"] != first["agent_id"]
    assert second["session"] == "fresh" and second["reason"] == "provider_changed"
    assert calls(probe)[1]["resume"] == []


def test_r3_fresh_session_after_family_change_is_then_resumed_normally(world, monkeypatch):
    consult(world)
    runner, probe = world
    monkeypatch.setattr(runner.providers["fake"], "family", "different")
    runner.runs.clear()
    second = consult(world)
    third = consult(world)
    assert second["reason"] == "provider_changed"
    assert not third.get("error"), third
    assert third["agent_id"] == second["agent_id"]
    assert third.get("reason") != "provider_changed"
    assert calls(probe)[2]["resume"] == ["--resume", "session-created-here"]


# ---------------------------------------------------------------------- R4

def test_r4_late_finalization_does_not_overwrite_node_or_start_free_retry(world):
    runner, probe = world

    async def go():
        first, stale = await timed_out_first_turn(runner, probe)
        assert first["timed_out"], first
        (probe / "reply-1").write_text("CURRENT ANSWER")
        task = await second_turn_pending(runner, probe)
        active = adopt(runner, stale)
        node_id = active.node_id
        stale.node_id = node_id
        before = runner.tree.get(node_id)
        usage_before = dict(before.usage or {})
        # The timed-out attempt is finalized late: a fast empty exit that,
        # for a live run, would earn the one free retry.
        await runner._finalize(stale, 1, {"input_tokens": 999_999}, "stale-session")
        during = runner.tree.get(node_id)
        state = (during.status, during.session_id, dict(during.usage or {}),
                 runner.runs[node_id] is active, len(calls(probe)))
        release(probe, 1)
        result = await asyncio.wait_for(task, BOUND)
        return before, usage_before, state, result, node_id

    before, usage_before, state, result, node_id = asyncio.run(go())
    status, session_id, usage, still_active, ncalls = state
    assert status == before.status, "late finalization rewrote the node's state"
    assert session_id != "stale-session"
    assert session_id == before.session_id
    assert usage == usage_before and 999_999 not in usage.values()
    assert still_active, "late finalization replaced the active run"
    assert ncalls == 2, "late finalization started a free retry"
    assert not result.get("error"), result
    assert result["reply"] == "CURRENT ANSWER"
    final = runner.tree.get(node_id)
    assert final.session_id == "session-created-here"
    assert 999_999 not in (final.usage or {}).values()


def test_r4_conversation_still_usable_after_timeout_and_late_finalization(world):
    runner, probe = world

    async def go():
        first, stale = await timed_out_first_turn(runner, probe)
        await runner._finalize(stale, 1, {}, "stale-session")
        return await runner.consult("advisor", "second question", timeout=10)

    result = asyncio.run(go())
    assert not result.get("error"), result
    assert result["reply"] == "ANSWER 1"
    assert "stale-session" not in json.dumps(calls(probe))
