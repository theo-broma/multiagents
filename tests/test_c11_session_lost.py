"""C11 — a conversation that cannot be resumed is reported, not replaced
silently (context/specs/phase6-closing-fixes.md, C11 and its revision).

Seen live: `consult(advisor)` resumed a codex session the native CLI could no
longer find; the turn failed with `codex: resume failed: requested '<id>',
observed ''`, and the next consult started a cold advisor with nothing said.

The "explicit resume-mismatch signal" of C11-R1a is, today, the shipped codex
adapter's resume check. The contract does not say how a provider signals it
in general, so this suite drives the real codex adapter, through the real
Runner, against a fake *native* codex (tests/support/codex_harness.py). Nothing
here looks at how the runner records or detects the loss: only at `check`,
`collect`, `consult` and `steer` results, the event log, the fake CLI's argv
log and the filesystem.

Ids: C11-R1 / R1a (status, reason, scope, no relaunch), R2 / R2a (the next
consult says so), R3 / R3a (steer refuses). C11-R4 is the existing suites
staying green and is not a test of its own.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import c3_harness as h  # noqa: E402
import codex_harness as ch  # noqa: E402
from multiagents.tree import Node  # noqa: E402

ORIGINAL = "0199a213-81c0-7800-8aa1-bbab2a035a53"
OTHER = "0199a213-81c0-7800-8aa1-bbab2a035a99"
LOST = "session_lost"


def _events(thread=ORIGINAL, text="done"):
    return [
        {"type": "thread.started", "thread_id": thread},
        {"type": "turn.started"},
        {"type": "item.completed",
         "item": {"id": "a", "type": "agent_message", "text": text}},
        {"type": "turn.completed",
         "usage": {"input_tokens": 1, "output_tokens": 1}},
    ]


class Advisor:
    """A conversational `advisor` on the shipped codex provider, whose native
    CLI is a fake. `lose()` makes the next resume fail the two ways the
    adapter's check distinguishes: no session at all, or a different one."""

    def __init__(self, tmp_path, monkeypatch):
        self.tmp = tmp_path
        self.monkeypatch = monkeypatch
        self.fake = ch.FakeCodex(tmp_path)
        self.fake.set(events=_events())
        profile = tmp_path / "profile"
        profile.mkdir()
        (profile / "auth.json").write_text("{}")
        monkeypatch.setenv("MULTIAGENTS_CODEX_PROFILE", str(profile))
        block = dict(ch.block())
        block["bin"] = str(self.fake.path)
        self.block = block
        self.runner = self.restart()

    def restart(self):
        """A new Runner over the same project state — a server restart."""
        spec = h.AgentSpec("advisor", "codex", "gpt-x", conversational=True)
        self.runner = h.make_runner(self.tmp / "proj", self.monkeypatch,
                                    agents={"advisor": spec},
                                    providers={"codex": self.block})
        return self.runner

    def consult(self, message="question"):
        return asyncio.run(self.runner.consult("advisor", message, timeout=60))

    def steer(self, agent_id, message="again"):
        """The steer result; a refusal raised counts as a refusal returned."""
        try:
            return asyncio.run(self.runner.steer(agent_id, message))
        except (ValueError, RuntimeError, PermissionError) as exc:
            return {"steered": False, "error": str(exc), "raised": True}

    def lose(self, how="empty"):
        if how == "empty":
            self.fake.set(resume_gone="error")      # no rollout; no session
        else:
            self.fake.set(resume_gone="fresh")      # resumes as another thread

    def heal(self):
        self.fake.set(resume_gone=None)

    def execs(self):
        return self.fake.exec_calls()

    def resumes(self):
        return [c for c in self.execs() if "resume" in c["argv"]]

    def events(self, kind):
        path = self.runner.paths.events_file
        if not path.exists():
            return []
        out = []
        for line in path.read_text().splitlines():
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if entry.get("kind") == kind:
                out.append(entry)
        return out

    def established(self):
        """Turn 1 done; returns the conversation's agent id."""
        first = self.consult("first")
        assert first["status"] == "idle" and first["agent_id"], first
        return first["agent_id"]

    def lost(self, how="empty"):
        """Turn 1, then a resume the provider cannot find. Returns the id."""
        agent_id = self.established()
        self.lose(how)
        self.consult("second")      # the lost turn; its own return is not pinned
        self.heal()
        return agent_id


@pytest.fixture
def adv(tmp_path, monkeypatch):
    return Advisor(tmp_path, monkeypatch)


def _text(value):
    return json.dumps(value, default=str)


# ---------------------------------------------------------------------------
# C11-R1 / R1a — a lost session is its own outcome
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("how", ["empty", "different-id"])
def test_c11_r1_check_agent_reports_session_lost_and_the_requested_id(adv, how):
    agent_id = adv.lost(how)

    result = adv.runner.check(agent_id)

    assert result["status"] == "failed", result
    assert result["reason"] == LOST, (
        f"a lost session must be reported as reason {LOST!r}, not the plain "
        f"failure text: {result['reason']!r}")
    assert result.get("requested_session") == ORIGINAL, result


@pytest.mark.parametrize("how", ["empty", "different-id"])
def test_c11_r1_collect_agent_reports_session_lost_and_the_requested_id(adv, how):
    agent_id = adv.lost(how)

    result = adv.runner.collect(agent_id)

    assert result["status"] == "failed", result
    assert result["reason"] == LOST, result["reason"]
    assert result.get("requested_session") == ORIGINAL, result


def test_c11_r1a_the_resume_was_really_attempted_with_the_recorded_id(adv):
    """Guards the fixture: the loss above comes from resuming ORIGINAL."""
    adv.lost("empty")
    resumes = adv.resumes()
    assert len(resumes) == 1, adv.execs()
    argv = resumes[0]["argv"]
    assert argv[argv.index("resume") + 1] == ORIGINAL


def test_c11_r1a_an_ordinary_first_turn_failure_keeps_its_ordinary_reason(
        adv, tmp_path):
    """No session was requested, so none can be lost: a first turn that dies
    with no session id is not `session_lost`."""
    adv.fake.set(events=[{"type": "turn.failed",
                          "error": {"message": "boom"}}], exit=1)

    result = adv.consult("first")

    node = adv.runner.check(result["agent_id"])
    assert node["status"] == "failed", node
    assert node["reason"] != LOST, node
    assert "requested_session" not in node or not node["requested_session"], node
    assert LOST not in _text(adv.runner.collect(result["agent_id"]))


def test_c11_r1a_a_resumed_turn_that_fails_for_another_reason_is_not_lost(adv):
    """The session was found (re-announced) and the turn then failed on a rate
    limit: the ordinary quota reason, nothing about a lost session."""
    agent_id = adv.established()
    adv.fake.set(events=[{"type": "thread.started", "thread_id": ORIGINAL},
                         {"type": "turn.failed",
                          "error": {"message": "rate limit"}}], exit=1)

    adv.consult("second")

    node = adv.runner.check(agent_id)
    assert node["status"] == "failed", node
    assert node["reason"] != LOST, node
    assert LOST not in _text(adv.runner.collect(agent_id))


def test_c11_r1a_a_provider_without_the_signal_never_reports_session_lost(
        tmp_path, monkeypatch):
    """Generic, not by provider name: a plain declarative provider whose
    resumed turn yields another session id and exits nonzero has signalled
    nothing explicit, so its failure stays an ordinary `failed`."""
    script = tmp_path / "plain-cli"
    script.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "--resume" ]; then\n'
        "  echo '{\"type\":\"text\",\"text\":\"x\",\"session\":\"s-other\"}'\n"
        "  exit 1\n"
        "fi\n"
        "echo '{\"type\":\"text\",\"text\":\"ok\",\"session\":\"s-1\"}'\n"
        "exit 0\n")
    script.chmod(0o755)
    provider = {
        "bin": str(script),
        "spawn": {"args": ["--cwd", "{workdir}"],
                  "resume": ["--resume", "{session_id}"]},
        "stream": {"format": "ndjson", "session_id_paths": ["session"],
                   "rules": [{"match": {"type": "text"}, "as": "text",
                              "fields": {"text": "text"}}]},
    }
    spec = h.AgentSpec("advisor", "plain", "m", conversational=True)
    runner = h.make_runner(tmp_path / "proj", monkeypatch,
                           agents={"advisor": spec}, providers={"plain": provider})
    first = asyncio.run(runner.consult("advisor", "q1", timeout=60))
    asyncio.run(runner.consult("advisor", "q2", timeout=60))

    node = runner.check(first["agent_id"])
    assert node["reason"] != LOST, node


@pytest.mark.parametrize("how", ["empty", "different-id"])
def test_c11_r1a_no_relaunch_after_a_lost_session(adv, how):
    """The silent-failure free retry and the commit-fix resume must not fire:
    the turn that lost its session runs the native CLI exactly once."""
    agent_id = adv.lost(how)

    assert len(adv.resumes()) == 1, adv.execs()
    # Anything that relaunched would have had time to by now; look once more
    # after the node settles.
    node = adv.runner.check(agent_id)
    assert node["status"] == "failed", node
    assert len(adv.execs()) == 2, (
        f"expected the first turn and one resume, got {len(adv.execs())}: "
        f"{[c['argv'] for c in adv.execs()]}")


def test_c11_r1a_a_lost_session_keeps_the_worktree_and_run_dir(adv):
    agent_id = adv.lost("empty")
    assert adv.runner.paths.worktree(agent_id).is_dir()
    assert adv.runner.paths.run_dir(agent_id).is_dir()


# ---------------------------------------------------------------------------
# C11-R2 / R2a — the next consult says the conversation was replaced
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("how", ["empty", "different-id"])
def test_c11_r2_the_next_consult_starts_a_new_conversation_and_says_so(adv, how):
    old = adv.lost(how)
    adv.fake.set(events=_events(OTHER, text="fresh answer"))

    reply = adv.consult("third")

    assert not reply.get("error"), reply
    assert reply["agent_id"] != old, reply
    assert reply["status"] == "idle", reply
    assert reply.get("conversation_replaced") == {
        "previous_agent_id": old,
        "reason": LOST,
        "requested_session": ORIGINAL,
    }, reply
    # The new conversation really is new: its first turn resumed nothing.
    assert len(adv.resumes()) == 1, (
        f"only the lost turn may have resumed: {[c['argv'] for c in adv.execs()]}")


def test_c11_r2_the_reply_text_carries_a_prefix_beside_the_answer(adv):
    old = adv.lost("empty")
    adv.fake.set(events=_events(OTHER, text="fresh answer"))

    reply = adv.consult("third")

    text = reply.get("reply") or ""
    assert "fresh answer" in text, reply
    prefix = text.split("fresh answer")[0]
    assert prefix.strip(), f"no note before the answer: {text!r}"
    assert old in prefix, f"the note must name the old conversation: {prefix!r}"


def test_c11_r2_the_replacement_is_recorded_as_an_event(adv):
    old = adv.lost("empty")

    adv.consult("third")

    replaced = adv.events("conversation_replaced")
    assert len(replaced) == 1, replaced
    assert old in _text(replaced[0]), replaced[0]


def test_c11_r2_replacing_is_a_one_time_event_the_new_conversation_resumes(adv):
    old = adv.lost("empty")
    adv.fake.set(events=_events(OTHER, text="fresh answer"))
    second = adv.consult("third")
    new_id = second["agent_id"]

    third = adv.consult("fourth")

    assert third["agent_id"] == new_id, third
    assert "conversation_replaced" not in third, third
    assert "fresh answer" in (third.get("reply") or "")
    last = adv.resumes()[-1]["argv"]
    assert last[last.index("resume") + 1] == OTHER, last
    assert len(adv.events("conversation_replaced")) == 1
    assert adv.runner.check(old)["reason"] == LOST    # the old one stays lost


def test_c11_r2_a_consult_that_resumes_normally_carries_no_such_field(adv):
    adv.established()
    reply = adv.consult("second")
    assert reply["status"] == "idle", reply
    assert "conversation_replaced" not in reply, reply
    assert not adv.events("conversation_replaced")


def test_c11_r2a_an_older_lost_conversation_does_not_displace_a_newer_healthy_one(adv):
    old = adv.lost("empty")
    adv.fake.set(events=_events(OTHER, text="fresh answer"))
    newer = adv.consult("third")["agent_id"]
    assert newer != old

    reply = adv.consult("fourth")

    assert reply["agent_id"] == newer, reply
    assert "conversation_replaced" not in reply, reply


def test_c11_r2a_the_most_recent_lost_conversation_wins_over_an_older_healthy_one(adv):
    """Most recent, not best: an older conversation that is still resumable is
    not revived behind the back of a newer one that was lost."""
    healthy = adv.established()
    newest = Node(
        id="ag-10575e", agent="advisor", provider="codex", model="gpt-x",
        parent=None, depth=1, status="failed", reason=LOST,
        session_id="", worktree=str(adv.tmp / "lost-worktree"),
        conversation=True, turns=2, created_at=9_999_999_999.0)
    Path(newest.worktree).mkdir()
    adv.runner.tree.add(newest)
    before = len(adv.execs())

    reply = adv.consult("again")

    assert reply["agent_id"] not in (healthy, newest.id), reply
    assert not any("resume" in c["argv"] for c in adv.execs()[before:]), (
        "the older healthy session was resumed behind the lost newer one")
    assert reply.get("conversation_replaced", {}).get("previous_agent_id") == newest.id, reply


def test_c11_r2a_the_loss_survives_a_server_restart(adv):
    old = adv.lost("empty")
    adv.restart()
    adv.fake.set(events=_events(OTHER, text="fresh answer"))

    reply = adv.consult("third")

    assert reply["agent_id"] != old, reply
    assert reply.get("conversation_replaced") == {
        "previous_agent_id": old, "reason": LOST, "requested_session": ORIGINAL,
    }, reply
    assert adv.runner.check(old)["reason"] == LOST


def test_c11_r2a_the_restarted_server_keeps_a_newer_healthy_conversation(adv):
    adv.lost("empty")
    adv.fake.set(events=_events(OTHER, text="fresh answer"))
    newer = adv.consult("third")["agent_id"]
    adv.restart()

    reply = adv.consult("fourth")

    assert reply["agent_id"] == newer, reply
    assert "conversation_replaced" not in reply, reply


def test_c11_r2a_an_ordinary_failure_is_not_replaced_with_a_notice(adv):
    """A failed conversation that did not lose its session is not announced
    as replaced with reason session_lost."""
    adv.established()
    adv.fake.set(events=[{"type": "thread.started", "thread_id": ORIGINAL},
                         {"type": "turn.failed",
                          "error": {"message": "rate limit"}}], exit=1)
    adv.consult("second")
    adv.fake.set(events=_events(OTHER))

    try:
        reply = adv.consult("third")
    except (ValueError, RuntimeError, FileNotFoundError):
        return      # a refusal is today's behaviour and stays allowed
    replaced = reply.get("conversation_replaced")
    assert not replaced or replaced.get("reason") != LOST, reply


# ---------------------------------------------------------------------------
# C11-R3 / R3a — steer refuses a lost session and says what to do
# ---------------------------------------------------------------------------

def test_c11_r3_steer_on_a_lost_session_is_refused_not_resumed(adv):
    agent_id = adv.lost("empty")
    execs_before = len(adv.execs())

    result = adv.steer(agent_id, "carry on")

    assert result.get("steered") is not True, result
    assert len(adv.execs()) == execs_before, (
        f"steer launched a turn on a lost session: "
        f"{[c['argv'] for c in adv.execs()[execs_before:]]}")


def test_c11_r3a_the_refusal_names_the_loss_the_run_dir_and_the_way_forward(adv):
    agent_id = adv.lost("empty")

    result = adv.steer(agent_id, "carry on")

    message = _text(result)
    assert LOST in message or "session is lost" in message.lower() \
        or "session lost" in message.lower(), result
    run_dir = str(adv.runner.paths.run_dir(agent_id))
    assert run_dir in message, f"the refusal must name {run_dir}: {result}"
    assert "fresh" in message.lower() or "new run" in message.lower(), (
        f"the refusal must say to start a fresh run: {result}")


def test_c11_r3a_the_refusal_preserves_worktree_run_dir_and_state(adv):
    agent_id = adv.lost("empty")

    adv.steer(agent_id, "carry on")

    assert adv.runner.paths.worktree(agent_id).is_dir()
    assert adv.runner.paths.run_dir(agent_id).is_dir()
    node = adv.runner.check(agent_id)
    assert node["status"] == "failed" and node["reason"] == LOST, node


def test_c11_r3a_repeating_the_refused_steer_gives_the_same_answer(adv):
    agent_id = adv.lost("empty")
    first = adv.steer(agent_id, "carry on")
    second = adv.steer(agent_id, "carry on")
    assert first.get("steered") is not True and second.get("steered") is not True
    assert LOST in _text(second) or "lost" in _text(second).lower()
    assert len(adv.resumes()) == 1, adv.execs()
