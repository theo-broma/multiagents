"""TB-R7 — a standing conversation follows a roster model change
(context/specs/tooling-batch-2026-10.md, package C).

Contract: a conversation records the effective launch fingerprint (provider,
model, effort, provider options such as variant). On the next consult, if the
current roster resolves to a different fingerprint, a fresh session is started
instead of resuming and a `conversation_replaced` event records the old and
new values; the same fingerprint resumes as today. The comparison happens
under the conversation lock, before any launch side effect. An explicit
`steer_agent` keeps resuming with the recorded model. A conversation recorded
before this change (no fingerprint) is compared on provider and model only.

Black box: a real first consult records the conversation; the roster is then
changed by replacing the agent in the loaded config (what a reload does), and
the next consult is observed through the argv the fake CLI ran with, the
reply's node, the tree and the event log. Seams are those of
`test_fo_fallback_options.py` / `test_conversation_provider_change.py`.

Silences, stated rather than invented:
- The event's field names for old/new values are not specified; the tests
  look for the distinctive old and new values anywhere in the event.
- A conversation resumed on a `models:` fallback route (not the agent's own
  provider): what "the current roster resolves to" means there is not
  specified, so it is not tested.
- A "legacy record" is a node with no recorded fingerprint, which is what a
  node hand-built the way the CX-C28 tests build theirs is.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from multiagents.config import AgentSpec  # noqa: E402
from multiagents.tree import Node  # noqa: E402
from test_conversation_provider_change import _calls, _events  # noqa: E402
from test_fo_fallback_options import (  # noqa: E402
    _opt, _providers, _runner, _start, _steer)

CONSULT_SECONDS = 30


def _setup(tmp_path, monkeypatch, **data):
    providers, probes = _providers(tmp_path, "acme")
    base = {"provider": "acme", "model": "model-old", "conversational": True}
    base.update(data)
    runner = _runner(tmp_path, monkeypatch, base, providers)
    return runner, probes["acme"], base


def _consult(runner, message="next"):
    return asyncio.run(runner.consult("worker", message, timeout=CONSULT_SECONDS))


def _change_roster(runner, base, **changes):
    """The roster after an edit: `changes` over the old data; a value of None
    removes the key."""
    data = {**base, **changes}
    data = {k: v for k, v in data.items() if v is not None}
    runner.config.agents["worker"] = AgentSpec.from_dict("worker", data)


def _replaced(runner):
    return _events(runner, "conversation_replaced")


def _assert_resumed(runner, probe, first, second, n_calls=2):
    calls = _calls(probe)
    assert len(calls) == n_calls, calls
    assert _opt(calls[-1], "--resume") == "sess-acme-new", calls[-1]
    assert second.get("agent_id") == first.get("agent_id")
    assert not _replaced(runner)


def _assert_replaced(runner, probe, first, second):
    calls = _calls(probe)
    assert len(calls) == 2, f"exactly one launch per consult: {calls}"
    assert "--resume" not in calls[1], f"a changed fingerprint must not resume: {calls[1]}"
    assert not second.get("error"), second
    assert second.get("agent_id") and second["agent_id"] != first["agent_id"], (first, second)
    node = runner.tree.get(second["agent_id"])
    assert node is not None and node.conversation
    assert len(_replaced(runner)) == 1, _replaced(runner)
    return calls[1]


# ---------------------------------------------------------------------------
# a changed fingerprint starts a fresh session
# ---------------------------------------------------------------------------

def test_tb_r7_same_provider_changed_model_starts_a_fresh_session(tmp_path, monkeypatch):
    runner, probe, base = _setup(tmp_path, monkeypatch)
    first = _consult(runner, "one")
    _change_roster(runner, base, model="model-new")

    second = _consult(runner, "two")

    argv = _assert_replaced(runner, probe, first, second)
    assert _opt(argv, "--model") == "model-new"
    assert "sess-acme-new" not in argv, "the old session id travelled"


def test_tb_r7_a_changed_variant_starts_a_fresh_session(tmp_path, monkeypatch):
    runner, probe, base = _setup(tmp_path, monkeypatch, variant="var-old")
    first = _consult(runner, "one")
    assert _opt(_calls(probe)[0], "--variant") == "var-old"
    _change_roster(runner, base, variant="var-new")

    second = _consult(runner, "two")

    argv = _assert_replaced(runner, probe, first, second)
    assert _opt(argv, "--variant") == "var-new"
    assert _opt(argv, "--model") == "model-old"


def test_tb_r7_a_removed_variant_starts_a_fresh_session_without_the_flag(tmp_path, monkeypatch):
    runner, probe, base = _setup(tmp_path, monkeypatch, variant="var-old")
    first = _consult(runner, "one")
    _change_roster(runner, base, variant=None)

    second = _consult(runner, "two")

    argv = _assert_replaced(runner, probe, first, second)
    assert "--variant" not in argv


def test_tb_r7_a_changed_effort_starts_a_fresh_session(tmp_path, monkeypatch):
    runner, probe, base = _setup(tmp_path, monkeypatch, effort="low")
    first = _consult(runner, "one")
    assert _opt(_calls(probe)[0], "--effort") == "low"
    _change_roster(runner, base, effort="high")

    second = _consult(runner, "two")

    argv = _assert_replaced(runner, probe, first, second)
    assert _opt(argv, "--effort") == "high"


def test_tb_r7_a_changed_option_in_the_primary_models_entry_starts_a_fresh_session(
        tmp_path, monkeypatch):
    # The fingerprint is the EFFECTIVE launch: the entry's variant is the one
    # the conversation ran with.
    runner, probe, base = _setup(
        tmp_path, monkeypatch, variant="top",
        models={"acme": {"model": "model-old", "variant": "var-old"}})
    first = _consult(runner, "one")
    assert _opt(_calls(probe)[0], "--variant") == "var-old"
    _change_roster(runner, base, models={"acme": {"model": "model-old", "variant": "var-new"}})

    second = _consult(runner, "two")

    argv = _assert_replaced(runner, probe, first, second)
    assert _opt(argv, "--variant") == "var-new"


# ---------------------------------------------------------------------------
# the event records the old and the new values
# ---------------------------------------------------------------------------

def test_tb_r7_the_event_records_the_old_and_new_model(tmp_path, monkeypatch):
    runner, probe, base = _setup(tmp_path, monkeypatch)
    first = _consult(runner, "one")
    _change_roster(runner, base, model="model-new")

    second = _consult(runner, "two")

    events = _replaced(runner)
    assert len(events) == 1, (events, second)
    text = json.dumps(events[0])
    assert "model-old" in text and "model-new" in text, text
    assert first["agent_id"] in text, "the event names the replaced conversation"


def test_tb_r7_the_event_records_the_old_and_new_variant(tmp_path, monkeypatch):
    runner, probe, base = _setup(tmp_path, monkeypatch, variant="var-old")
    _consult(runner, "one")
    _change_roster(runner, base, variant="var-new")

    _consult(runner, "two")

    events = _replaced(runner)
    assert len(events) == 1, events
    text = json.dumps(events[0])
    assert "var-old" in text and "var-new" in text, text


def test_tb_r7_replacing_is_a_one_time_event_the_next_consult_resumes_the_new_one(
        tmp_path, monkeypatch):
    runner, probe, base = _setup(tmp_path, monkeypatch)
    _consult(runner, "one")
    _change_roster(runner, base, model="model-new")
    second = _consult(runner, "two")

    third = _consult(runner, "three")

    calls = _calls(probe)
    assert len(calls) == 3, calls
    assert _opt(calls[2], "--resume") == "sess-acme-new"
    assert _opt(calls[2], "--model") == "model-new"
    assert third.get("agent_id") == second.get("agent_id")
    assert len(_replaced(runner)) == 1


# ---------------------------------------------------------------------------
# the same fingerprint resumes as today
# ---------------------------------------------------------------------------

def test_tb_r7_an_unchanged_roster_resumes(tmp_path, monkeypatch):
    runner, probe, base = _setup(tmp_path, monkeypatch, variant="var-old", effort="low")
    first = _consult(runner, "one")

    second = _consult(runner, "two")

    _assert_resumed(runner, probe, first, second)


def test_tb_r7_a_reloaded_identical_roster_resumes(tmp_path, monkeypatch):
    runner, probe, base = _setup(tmp_path, monkeypatch, variant="var-old")
    first = _consult(runner, "one")
    _change_roster(runner, base)            # a new spec object, same values

    second = _consult(runner, "two")

    _assert_resumed(runner, probe, first, second)


def test_tb_r7_a_roster_edit_that_leaves_the_effective_launch_unchanged_resumes(
        tmp_path, monkeypatch):
    # The top-level variant moves, but the primary's entry overrides it with
    # the same effective value: provider, model, effort, variant all equal.
    runner, probe, base = _setup(
        tmp_path, monkeypatch, variant="top-one",
        models={"acme": {"model": "model-old", "variant": "eff"}})
    first = _consult(runner, "one")
    _change_roster(runner, base, variant="top-two")

    second = _consult(runner, "two")

    _assert_resumed(runner, probe, first, second)


# ---------------------------------------------------------------------------
# a legacy record (no fingerprint): provider and model only
# ---------------------------------------------------------------------------

def _legacy(tmp_path, monkeypatch, **data):
    runner, probe, base = _setup(tmp_path, monkeypatch, **data)
    worktree = runner.paths.worktree("ag-1e9ac1")
    worktree.mkdir(parents=True)
    runner.tree.add(Node(id="ag-1e9ac1", agent="worker", provider="acme", model="model-old",
                         parent=None, depth=1, status="idle", session_id="sess-acme-new",
                         worktree=str(worktree), conversation=True, turns=1))
    return runner, probe, base


def test_tb_r7_a_legacy_record_with_the_same_provider_and_model_resumes(tmp_path, monkeypatch):
    runner, probe, base = _legacy(tmp_path, monkeypatch)

    second = _consult(runner)

    calls = _calls(probe)
    assert len(calls) == 1, calls
    assert _opt(calls[0], "--resume") == "sess-acme-new"
    assert second.get("agent_id") == "ag-1e9ac1"
    assert not _replaced(runner)


def test_tb_r7_a_legacy_record_is_not_replaced_for_a_variant_or_effort_the_roster_now_has(
        tmp_path, monkeypatch):
    # Nothing was recorded about variant or effort, so they cannot differ.
    runner, probe, base = _legacy(tmp_path, monkeypatch, variant="var-new", effort="high")

    second = _consult(runner)

    calls = _calls(probe)
    assert len(calls) == 1, calls
    assert _opt(calls[0], "--resume") == "sess-acme-new"
    assert second.get("agent_id") == "ag-1e9ac1"
    assert not _replaced(runner)


def test_tb_r7_a_legacy_record_with_a_different_model_is_replaced(tmp_path, monkeypatch):
    runner, probe, base = _legacy(tmp_path, monkeypatch, model="model-new")

    second = _consult(runner)

    calls = _calls(probe)
    assert len(calls) == 1, calls
    assert "--resume" not in calls[0] and "sess-acme-new" not in calls[0], calls[0]
    assert _opt(calls[0], "--model") == "model-new"
    assert second.get("agent_id") != "ag-1e9ac1"
    events = _replaced(runner)
    assert len(events) == 1, events
    text = json.dumps(events[0])
    assert "model-old" in text and "model-new" in text and "ag-1e9ac1" in text, text


# ---------------------------------------------------------------------------
# the lock, and the side effects
# ---------------------------------------------------------------------------

def test_tb_r7_concurrent_consults_after_a_roster_change_replace_exactly_once(
        tmp_path, monkeypatch):
    runner, probe, base = _setup(tmp_path, monkeypatch)
    first = _consult(runner, "one")
    _change_roster(runner, base, model="model-new")

    async def both():
        return await asyncio.gather(
            runner.consult("worker", "a", timeout=CONSULT_SECONDS),
            runner.consult("worker", "b", timeout=CONSULT_SECONDS))
    a, b = asyncio.run(both())

    assert not a.get("error") and not b.get("error"), (a, b)
    assert len(_replaced(runner)) == 1, _replaced(runner)
    assert a["agent_id"] == b["agent_id"] != first["agent_id"]
    calls = _calls(probe)
    assert len(calls) == 3, calls
    fresh = [c for c in calls[1:] if "--resume" not in c]
    resumed = [c for c in calls[1:] if "--resume" in c]
    assert len(fresh) == 1 and len(resumed) == 1, calls
    assert _opt(resumed[0], "--model") == "model-new"


def test_tb_r7_the_old_session_is_never_launched_after_the_roster_changed(
        tmp_path, monkeypatch):
    runner, probe, base = _setup(tmp_path, monkeypatch)
    _consult(runner, "one")
    _change_roster(runner, base, model="model-new")

    _consult(runner, "two")

    for argv in _calls(probe)[1:]:
        assert _opt(argv, "--model") == "model-new", f"the old model was launched: {argv}"
        assert "--resume" not in argv


# ---------------------------------------------------------------------------
# steer_agent keeps today's semantics
# ---------------------------------------------------------------------------

def test_tb_r7_a_steer_resumes_with_the_recorded_model_after_a_roster_change(
        tmp_path, monkeypatch):
    providers, probes = _providers(tmp_path, "acme")
    base = {"provider": "acme", "model": "model-old", "variant": "var-old"}
    runner = _runner(tmp_path, monkeypatch, base, providers)
    started = _start(runner)
    _change_roster(runner, base, model="model-new")

    result = _steer(runner, started["agent_id"])

    assert not result.get("error"), result
    calls = _calls(probes["acme"])
    assert len(calls) == 2, calls
    assert "--resume" in calls[1], "a steer resumes the run's session"
    assert _opt(calls[1], "--model") == "model-old"
    assert not _replaced(runner), "a steer never replaces anything"
