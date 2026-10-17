"""A session id recorded under one provider family is never resumed under another.

Observed (run ag-5d0962, exit 20): a conversation agent's turn 1 ran on an
opencode-format provider and left a `ses_...` id; a later turn was routed to a
codex-format provider, which was handed that id and died with
"session id is not UUID-shaped". Per the bug-30623f decision the turn must
start fresh with reason `provider_changed`, whether or not the node pins a
provider or model. Same-family turns keep resuming.

Everything goes through the public `Runner.consult` with real fake-provider
subprocesses. The "codex" fake refuses a non-UUID resume exactly as the real
CLI does, so a wrongly resumed id fails the turn instead of passing quietly.
"""
import asyncio
import json
from pathlib import Path
import sys
import uuid

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))
import c3_harness as h
from multiagents.runner import Runner

OC_ID = "ses_eb866f7eeffeNLvdUOQTwH0TZJ"
CX_ID = "7d1c2b9e-4f0a-4c53-9a58-3b0e6f1d2a44"

PROVIDER = '''import json, sys, uuid
from pathlib import Path
probe = Path(sys.argv[1]); kind = sys.argv[2]; prompt = sys.argv[3]; resume = sys.argv[4:]
n = len(sorted(probe.glob(kind + "-*.json")))
(probe / f"{kind}-{n:03}.json").write_text(json.dumps({"prompt": prompt, "resume": resume}))
if kind == "cx" and resume:
    try:
        uuid.UUID(resume[-1])
    except ValueError:
        sys.stderr.write(f"ValueError: session id is not UUID-shaped: {resume[-1]!r}\\n")
        sys.exit(20)
sid = {"oc": "%s", "cx": "%s"}[kind]
print(json.dumps({"type": "text", "text": f"{kind} ANSWER {n}", "session": sid}))
''' % (OC_ID, CX_ID)


def entry(script, probe, kind, family):
    return {
        "bin": sys.executable, "family": family,
        "spawn": {"args": [str(script), str(probe), kind, "{prompt}"],
                  "resume": ["--resume", "{session_id}"]},
        "stream": {"format": "ndjson", "session_id_paths": ["session"],
                   "rules": [{"match": {"type": "text"}, "as": "text",
                              "fields": {"text": "text"}}]},
    }


@pytest.fixture
def world(tmp_path, monkeypatch):
    probe = tmp_path / "probe"
    probe.mkdir()
    script = tmp_path / "provider.py"
    script.write_text(PROVIDER)
    providers = {
        "oc": entry(script, probe, "oc", "oc-family"),
        "oc2": entry(script, probe, "oc", "oc-family"),     # same family as oc
        "cx": entry(script, probe, "cx", "cx-family"),
    }
    spec = h.AgentSpec("advisor", "oc", "m", conversational=True, writes=False,
                       models={"cx": "m", "oc2": "m"})
    runner = h.make_runner(tmp_path / "project", monkeypatch,
                           agents={"advisor": spec}, providers=providers)
    monkeypatch.setattr(Runner, "CONSULT_LOCK_SLACK_SECONDS", 0)
    yield runner, probe
    asyncio.run(runner.shutdown(detach=False))


def consult(world, question="question", timeout=10):
    runner, _ = world
    return asyncio.run(runner.consult("advisor", question, timeout=timeout))


def calls(probe, kind):
    return [json.loads(p.read_text()) for p in sorted(probe.glob(kind + "-*.json"))]


def events(runner, kind):
    return [row for line in runner.paths.events_file.read_text().splitlines()
            if (row := json.loads(line)).get("kind") == kind]


def move_agent_to(runner, monkeypatch, provider, keep=()):
    """The roster now routes the agent to `provider` (a config change); the
    providers in `keep` stay listed as fallbacks, the others are dropped."""
    monkeypatch.setitem(runner.config.agents, "advisor",
                        h.AgentSpec("advisor", provider, "m", conversational=True,
                                    writes=False, models={k: "m" for k in keep}))


def test_control_same_family_turn_two_resumes(world, monkeypatch):
    runner, probe = world
    first = consult(world)
    assert not first.get("error"), first
    move_agent_to(runner, monkeypatch, "oc2")
    second = consult(world)
    assert not second.get("error"), second
    assert second["agent_id"] == first["agent_id"]
    assert calls(probe, "oc")[1]["resume"] == ["--resume", OC_ID]
    assert second.get("reason") != "provider_changed"


def test_control_same_provider_turn_two_resumes(world):
    _, probe = world
    first = consult(world)
    second = consult(world)
    assert not second.get("error"), second
    assert second["agent_id"] == first["agent_id"]
    assert calls(probe, "oc")[1]["resume"] == ["--resume", OC_ID]


def test_roster_move_to_another_family_starts_fresh(world, monkeypatch):
    runner, probe = world
    first = consult(world)
    assert not first.get("error"), first
    move_agent_to(runner, monkeypatch, "cx")
    second = consult(world)
    assert not second.get("error"), second
    assert calls(probe, "cx")[0]["resume"] == []
    assert second["session"] == "fresh" and second["reason"] == "provider_changed"
    assert events(runner, "conversation_replaced")[-1]["reason"] == "provider_changed"


def test_the_foreign_session_id_is_not_kept_on_the_conversation_node(world, monkeypatch):
    runner, _ = world
    first = consult(world)
    move_agent_to(runner, monkeypatch, "cx")
    second = consult(world)
    assert not second.get("error"), second
    node = runner.tree.get(second["agent_id"])
    assert node.session_id == CX_ID
    assert node.session_provider_family == "cx-family"


def test_switch_survives_a_restart_with_no_live_run(world, monkeypatch):
    runner, probe = world
    consult(world)
    runner.runs.clear()
    move_agent_to(runner, monkeypatch, "cx")
    second = consult(world)
    assert not second.get("error"), second
    assert calls(probe, "cx")[0]["resume"] == []
    assert second["reason"] == "provider_changed"


def test_legacy_node_without_recorded_family_still_never_crosses_families(world, monkeypatch):
    runner, probe = world
    first = consult(world)
    runner.tree.update(first["agent_id"], session_provider_family="")
    runner.runs.clear()
    move_agent_to(runner, monkeypatch, "cx")
    second = consult(world)
    assert not second.get("error"), second
    assert calls(probe, "cx")[0]["resume"] == []
    assert second["reason"] == "provider_changed"


def test_after_the_family_switch_the_new_family_resumes_normally(world, monkeypatch):
    runner, probe = world
    consult(world)
    move_agent_to(runner, monkeypatch, "cx")
    consult(world)
    third = consult(world)
    assert not third.get("error"), third
    assert calls(probe, "cx")[1]["resume"] == ["--resume", CX_ID]
    assert third.get("reason") != "provider_changed"


def test_switching_back_to_the_first_family_does_not_resurrect_its_old_session(world, monkeypatch):
    runner, probe = world
    consult(world)
    move_agent_to(runner, monkeypatch, "cx")
    consult(world)
    move_agent_to(runner, monkeypatch, "oc")
    back = consult(world)
    assert not back.get("error"), back
    assert calls(probe, "oc")[1]["resume"] == []
    assert back["reason"] == "provider_changed"


# ---------------------------------------------------------------------------
# The exact sequence of run ag-5d0962 (bug-535139): quota on the first family,
# handover to a second family whose segment fails after naming its own session,
# then the next turn on the first family.
# ---------------------------------------------------------------------------

MARKER = "QH fixture quota exhausted"

QH_PROVIDER = '''import json, sys, uuid
from pathlib import Path
probe = Path(sys.argv[1]); kind = sys.argv[2]; state = Path(sys.argv[3])
prompt = sys.argv[4]; resume = sys.argv[5:]
plan = json.loads(state.read_text())
n = len(sorted(probe.glob(kind + "-*.json")))
(probe / f"{kind}-{n:03}.json").write_text(json.dumps({"prompt": prompt, "resume": resume}))
if kind == "cx" and resume:
    try:
        uuid.UUID(resume[-1])
    except ValueError:
        sys.stderr.write(f"ValueError: session id is not UUID-shaped: {resume[-1]!r}\\n")
        sys.exit(20)
sid = {"oc": "%s", "cx": "%s"}[kind]
if plan.get("fail"):   # the first family's quota is over by the time of the retry
    other = state.parent / "cx-state.json"
    other.write_text(json.dumps({"quota": False}))
store = Path(sys.argv[3]).parent / ("store-" + kind); store.mkdir(exist_ok=True)
text = "%s" if plan.get("quota") else f"{kind} ANSWER {n}"
(store / (sid + ".jsonl")).write_text(json.dumps({"text": text}) + "\\n")
print(json.dumps({"type": "text", "text": text, "session": sid}), flush=True)
sys.exit(1 if plan.get("quota") or plan.get("fail") else 0)
''' % (OC_ID, CX_ID, MARKER)


@pytest.fixture
def qh_world(tmp_path, monkeypatch):
    from multiagents import budget
    from multiagents.budget import Budget
    probe = tmp_path / "probe"
    probe.mkdir()
    script = tmp_path / "provider.py"
    script.write_text(QH_PROVIDER)
    states = {}
    providers = {}
    for kind, family in (("cx", "cx-family"), ("oc", "oc-family")):
        state = tmp_path / f"{kind}-state.json"
        state.write_text(json.dumps({"quota": kind == "cx", "fail": kind == "oc"}))
        states[kind] = state
        e = entry(script, probe, kind, family)
        e["spawn"]["args"] = [str(script), str(probe), kind, str(state), "{prompt}"]
        e["handover_mode"] = {"local": "none", "docker": "none"}
        e["transcript"] = {"dir": str(tmp_path / f"store-{kind}"), "glob": "*.jsonl",
                           "limit_markers": [{"match": MARKER, "detail": "quota", "resets": True}]}
        providers[kind] = e

    def plan(kind):
        return json.loads(states[kind].read_text())

    monkeypatch.setattr(budget, "read_all", lambda *a, **kw: {
        k: Budget(k, known=True, headroom=0.8) for k in states})
    monkeypatch.setattr(budget, "read_provider", lambda name, *a, **kw: Budget(
        name, known=True, headroom=0.0 if plan(name).get("quota") else 0.8))
    spec = h.AgentSpec("advisor", "cx", "m", conversational=True, writes=False,
                       models={"oc": "m"})
    runner = h.make_runner(tmp_path / "project", monkeypatch, agents={"advisor": spec},
                           providers=providers,
                           project={"quota_handover": {"enabled": True},
                                    "budget": {"reserve": False},
                                    "limits": {"retry_silent_failure_under_seconds": 0},
                                    "scheduler": {"retries": {"budget": 1,
                                                              "backoff_seconds": [0]}}})
    monkeypatch.setattr(Runner, "CONSULT_LOCK_SLACK_SECONDS", 0)

    def set_plan(kind, **kw):
        states[kind].write_text(json.dumps({**plan(kind), **kw}))
    yield runner, probe, set_plan
    asyncio.run(runner.shutdown(detach=False))


def test_bug_535139_quota_handover_then_failed_segment_never_leaks_the_foreign_id(qh_world):
    """One turn: cx hits quota -> handover to oc -> oc names ses_... and fails
    -> the turn is retried on the first family (its quota is over by then).
    cx must never be handed the oc id: it resumes its own UUID session or
    starts fresh."""
    runner, probe, _ = qh_world
    result = asyncio.run(runner.consult("advisor", "question", timeout=20))
    assert len(calls(probe, "oc")) >= 1, "the quota handover to oc never happened"
    cx_calls = calls(probe, "cx")
    assert len(cx_calls) >= 2, ("the turn was never retried on cx", cx_calls, result)
    for call in cx_calls:
        assert OC_ID not in call["resume"], f"cx was handed the oc session id: {call}"
    resumed = cx_calls[-1]["resume"]
    assert resumed in ([], ["--resume", CX_ID]), resumed
    assert not result.get("error"), result
