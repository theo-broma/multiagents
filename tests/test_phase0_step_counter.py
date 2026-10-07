"""Phase 0, group B — the step counter counts model turns, not stream lines.
Contract: `context/specs/phase0-runtime-repairs.md`, P0-R4.1–R4.5
(R4.2 as amended in 9e12929).

Fixtures:
  claude-stream-turns.jsonl  a REAL `claude -p --output-format stream-json
                             --verbose` capture (haiku, 2026-09-22): 4 tool
                             calls, 5 assistant message ids. Runs of
                             system/thinking_tokens lines were cut to 3 and
                             bulky keys removed from `init`; nothing else edited.
  agy-stream.jsonl,          CONSTRUCTED from the shipped stream rules; no raw
  opencode-stream.jsonl      capture of either exists in the repo. They pin
                             today's step counts for P0-R4.5.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path

import yaml

from multiagents.providers import Event, Provider
from multiagents.supervisor import Supervisor

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import c3_harness as h  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
SHIPPED = yaml.safe_load(
    (ROOT / "src" / "multiagents" / "defaults" / "providers.yaml").read_text())["providers"]
BIG = 10 ** 9


def _shipped(name: str) -> Provider:
    return Provider.from_dict(name, SHIPPED[name])


def _sup(**kw) -> Supervisor:
    kw.setdefault("silence_timeout", BIG)
    kw.setdefault("wall_timeout", BIG)
    kw.setdefault("max_steps", BIG)
    kw.setdefault("loop_repeats", BIG)
    return Supervisor(**kw)


def _replay(provider: Provider, fixture: str, **kw) -> tuple[Supervisor, list[Event]]:
    sup, events = _sup(**kw), []
    for line in (FIXTURES / fixture).read_text().splitlines():
        event = provider.parse_line(line)
        if event is not None:
            events.append(event)
            sup.observe(event)
    return sup, events


def _lines(fixture: str) -> list[dict]:
    return [json.loads(l) for l in (FIXTURES / fixture).read_text().splitlines() if l.strip()]


def _custom(rules) -> Provider:
    return Provider.from_dict("x", {"bin": "x", "spawn": {"args": ["x"]},
                                    "stream": {"format": "ndjson", "rules": rules}})


def _ev(kind="step", turn="", step=None, **kw) -> Event:
    # `turn` is passed only when set, so the untagged cases also run against
    # an Event that predates the field.
    if turn:
        kw["turn"] = turn
    return Event(kind=kind, step=step, **kw)


# ===========================================================================
# P0-R4.1 — `fields: {turn: <path>}` is lifted onto the parsed event
# ===========================================================================

def test_p0_r4_1_claude_assistant_line_carries_its_message_id():
    claude = _shipped("claude")
    raw = next(l for l in _lines("claude-stream-turns.jsonl")
               if l["type"] == "assistant"
               and any(b["type"] == "tool_use" for b in l["message"]["content"]))
    event = claude.parse_line(json.dumps(raw))
    assert event.kind == "tool"
    assert event.turn == raw["message"]["id"]


def test_p0_r4_1_every_claude_assistant_line_is_tagged_with_its_id():
    """thinking-only, text and tool_use lines alike."""
    claude = _shipped("claude")
    for raw in _lines("claude-stream-turns.jsonl"):
        if raw["type"] == "assistant":
            assert claude.parse_line(json.dumps(raw)).turn == raw["message"]["id"], raw


def test_p0_r4_1_claude_user_line_has_empty_turn():
    claude = _shipped("claude")
    raw = next(l for l in _lines("claude-stream-turns.jsonl") if l["type"] == "user")
    assert claude.parse_line(json.dumps(raw)).turn == ""


def test_p0_r4_1_turn_path_absent_from_payload_gives_empty_string():
    p = _custom([{"match": {"type": "a"}, "as": "step", "fields": {"turn": "m.id"}}])
    assert p.parse_line('{"type": "a", "m": {"id": "t-1"}}').turn == "t-1"
    assert p.parse_line('{"type": "a"}').turn == ""
    assert p.parse_line('{"type": "a", "m": {}}').turn == ""


def test_p0_r4_1_rule_without_turn_and_unmatched_lines_have_empty_turn():
    p = _custom([{"match": {"type": "a"}, "as": "step", "fields": {}}])
    assert p.parse_line('{"type": "a", "m": {"id": "t-1"}}').turn == ""
    assert p.parse_line('{"type": "zzz"}').turn == ""


def test_p0_r4_1_turn_is_a_string():
    p = _custom([{"match": {"type": "a"}, "as": "step", "fields": {"turn": "id"}}])
    assert isinstance(p.parse_line('{"type": "a", "id": "t-1"}').turn, str)


# ===========================================================================
# P0-R4.2 (amended 2026-09-22) — precedence: step index; else, if the
# provider's rules declare `turn` anywhere, only the first sight of each
# non-empty turn counts and untagged events NEVER count; else +1 per `step`.
#
# Whether a provider declares `turn` reaches the Supervisor through the runner
# under a name the contract leaves to the implementer, so the declared-turn
# branches are driven end to end: a fake CLI prints lines, a provider block
# classifies them, and the step count is read off the finished node.
# ===========================================================================

TURN_RULES = [
    {"match": {"type": "t"}, "as": "tool", "fields": {"name": "name", "turn": "mid"}},
    {"match": {"type": "s"}, "as": "step", "fields": {"turn": "mid", "step": "idx"}},
]


def _node_steps(tmp_path, monkeypatch, lines, rules=TURN_RULES, **spec_kw):
    """Run a fake CLI printing `lines`; return (node.steps, stuck reasons)."""
    tmp_path.mkdir(exist_ok=True)
    # a closing result line, so an otherwise silent run is not retried
    done = {"type": "done-marker", "result": "ok"}
    prov = h.fake_cli(tmp_path, "p", events=lines + [done])
    prov["stream"] = {"format": "ndjson", "rules": list(rules) + [
        {"match": {"type": "done-marker"}, "as": "result", "fields": {"text": "result"}}]}
    spec = h.AgentSpec("worker", "p", "m", **spec_kw)
    r = h.make_runner(tmp_path, monkeypatch, agents={"worker": spec},
                      providers={"p": prov})

    async def go():
        res = await r.start("worker", "go")
        await r.runs[res["agent_id"]].done.wait()
        return res["agent_id"]
    agent = asyncio.run(asyncio.wait_for(go(), 20))
    stuck = [json.loads(l).get("reason") for l in r.paths.events_file.read_text().splitlines()
             if l.strip() and json.loads(l).get("kind") == "stuck"]
    return r.tree.get(agent).steps, stuck


def _s(mid=None, idx=None):
    line = {"type": "s"}
    if mid is not None:
        line["mid"] = mid
    if idx is not None:
        line["idx"] = idx
    return line


def test_p0_r4_2_ten_events_sharing_one_turn_count_as_one(tmp_path, monkeypatch):
    lines = [_s("m1") if i % 2 else {"type": "t", "name": "Read", "mid": "m1"} for i in range(10)]
    assert _node_steps(tmp_path, monkeypatch, lines)[0] == 1


def test_p0_r4_2_a_turn_counts_only_the_first_time_it_is_seen(tmp_path, monkeypatch):
    lines = [_s(m) for m in ("m1", "m1", "m2", "m1", "m3", "m2")]
    assert _node_steps(tmp_path, monkeypatch, lines)[0] == 3


def test_p0_r4_2_untagged_events_never_count_when_turn_is_declared(tmp_path, monkeypatch):
    """Including those before the first turn — the amendment."""
    lines = [_s(), _s(), _s(), _s("m1"), _s(), _s(), _s("m2"), _s()]
    assert _node_steps(tmp_path, monkeypatch, lines)[0] == 2


def test_p0_r4_2_step_index_takes_precedence_over_turn(tmp_path, monkeypatch):
    lines = [_s("a", 0), _s("b", 0), _s("c", 0), _s("d", 6)]
    assert _node_steps(tmp_path, monkeypatch, lines)[0] == 7


def test_p0_r4_2_max_steps_is_measured_in_turns(tmp_path, monkeypatch):
    """max_steps=2: fifty lines on each of two turns stay inside it."""
    lines = [_s("m1")] * 50 + [_s("m2")] * 50
    steps, stuck = _node_steps(tmp_path / "a", monkeypatch, lines, max_steps=2)
    assert (steps, stuck) == (2, [])
    steps, stuck = _node_steps(tmp_path / "b", monkeypatch, lines + [_s("m3")], max_steps=2)
    assert stuck == ["runaway_steps"]


def test_p0_r4_2_provider_declaring_neither_keeps_todays_count(tmp_path, monkeypatch):
    rules = [{"match": {"type": "s"}, "as": "step", "fields": {}},
             {"match": {"type": "t"}, "as": "tool", "fields": {"name": "name"}}]
    lines = [_s("ignored")] * 7 + [{"type": "t", "name": "Read"}] * 3
    assert _node_steps(tmp_path, monkeypatch, lines, rules=rules)[0] == 7


def test_p0_r4_2_supervisor_without_turns_counts_as_today():
    """+1 per `step` event, nothing for other kinds, empty turn is untagged."""
    sup = _sup()
    for kind in ["step"] * 7 + ["tool", "text", "tool", "result", "raw"]:
        sup.observe(_ev(kind=kind, name="t" if kind == "tool" else ""))
    assert sup.steps == 7


# ===========================================================================
# P0-R4.3 — replaying a real claude stream counts distinct message ids
# ===========================================================================

def _claude_replay(tmp_path, monkeypatch):
    """The real capture, through the runner, classified by the SHIPPED claude
    stream rules (only `stream` is taken from the block; the fake CLI stands
    in for the binary)."""
    return _node_steps(tmp_path, monkeypatch, _lines("claude-stream-turns.jsonl"),
                       rules=SHIPPED["claude"]["stream"]["rules"])[0]


def test_p0_r4_3_claude_replay_counts_distinct_message_ids(tmp_path, monkeypatch):
    ids = {l["message"]["id"] for l in _lines("claude-stream-turns.jsonl")
           if l["type"] == "assistant"}
    assert len(ids) == 5, "fixture sanity"
    steps = _claude_replay(tmp_path, monkeypatch)
    assert steps == 5, f"{steps} steps for 5 turns (today: 28)"


def test_p0_r4_3_claude_replay_is_within_2x_of_tool_calls(tmp_path, monkeypatch):
    tools = sum(1 for e in _replay(_shipped("claude"), "claude-stream-turns.jsonl")[1]
                if e.kind == "tool")
    assert tools == 4, "fixture sanity"
    steps = _claude_replay(tmp_path, monkeypatch)
    assert tools / 2 <= steps <= tools * 2, f"{steps} steps for {tools} tool calls"


def test_p0_r4_3_claude_replay_still_classifies_every_line():
    """Whatever the rules become, no line of the real capture may fall to raw
    (the property test_core's older fixture test protects)."""
    _, events = _replay(_shipped("claude"), "claude-stream-turns.jsonl")
    assert not [e for e in events if e.kind == "raw"]


# ===========================================================================
# P0-R4.4 — no provider switch in the supervisor or the parser
# ===========================================================================

# Lines mentioning a provider name today (case-insensitive). They are comments;
# this work may not add any.
PROVIDER_NAME_LINES = {"supervisor.py": 2, "providers.py": 7, "runner.py": 10}


def test_p0_r4_4_no_new_provider_names_in_supervisor_parser_or_runner():
    pattern = re.compile(r"claude|agy|opencode", re.IGNORECASE)
    for name, today in PROVIDER_NAME_LINES.items():
        text = (ROOT / "src" / "multiagents" / name).read_text()
        count = sum(1 for line in text.splitlines() if pattern.search(line))
        assert count <= today, f"{name}: {count} lines name a provider (was {today})"


def test_p0_r4_4_the_turn_path_lives_in_providers_yaml_not_python():
    for name in ("supervisor.py", "providers.py", "runner.py"):
        text = (ROOT / "src" / "multiagents" / name).read_text()
        assert "message.id" not in text, name
    rules = SHIPPED["claude"]["stream"]["rules"]
    assert any((r.get("fields") or {}).get("turn") for r in rules), \
        "the claude block declares turn"


# ===========================================================================
# P0-R4.5 — agy and opencode count exactly as today
# ===========================================================================

def test_p0_r4_5_agy_replay_count_is_unchanged():
    sup, _ = _replay(_shipped("agy"), "agy-stream.jsonl")
    assert sup.steps == 7          # pinned 2026-09-22 against pre-change code


def test_p0_r4_5_opencode_replay_count_is_unchanged():
    sup, _ = _replay(_shipped("opencode"), "opencode-stream.jsonl")
    assert sup.steps == 8          # pinned 2026-09-22 against pre-change code

