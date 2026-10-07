"""Phase 0, group B — a long-running tool call must still count as one step.
Contract: `context/specs/phase0-runtime-repairs.md`, P0-R4.1-R4.5.

Companion to test_phase0_step_counter.py, which this file does not modify.
That suite's claude-stream-turns.jsonl fixture was deliberately truncated
(system/thinking_tokens runs cut to 3 lines) and predates the `tool_progress`
heartbeat event the claude CLI now emits during a long-running tool call —
so it cannot show either failure mode below at a realistic scale.

Fixture:
  claude-stream-long-toolcall.jsonl   A REAL `claude -p --output-format
                                       stream-json --verbose` capture
                                       (2026-09-24): one Bash call that runs
                                       long enough to produce an untruncated,
                                       193-line run of system/thinking_tokens
                                       lines and one tool_progress heartbeat
                                       between two tool_use turns, plus a
                                       closing turn and result line. 3
                                       distinct assistant message ids, 227
                                       lines total. Nothing edited beyond
                                       trimming bulky keys from `init` and
                                       shortening one Bash command/output.

Regression being guarded against: P0-R4's turn-based counting
(4faa975) was correct in the shipped defaults from the day it landed, but a
project- or global-level providers.yaml copy predating that fix silently
shadows it — config layering replaces the `stream.rules` list wholesale
rather than merging entry-by-entry, so a stale copy with no `turn:` field
anywhere turns `declares_turn` back off. test_stale_pre_turn_claude_rules_*
below reconstructs that exact stale rule set (assistant/user/system/
rate_limit_event all `as: step`, no `turn:`) to make the mechanism visible
in-repo, not just in an incident report.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_phase0_step_counter import SHIPPED, _custom, _replay, _shipped  # noqa: E402
from multiagents.runner import _declares_turn  # noqa: E402

FIXTURE = "claude-stream-long-toolcall.jsonl"


def _replay_as_runner_would(provider, fixture):
    """Like _replay, but derives declares_turn from the provider's own rules
    the same way runner.py does -- rather than the test hardcoding it -- so a
    provider that stops declaring turn (or a reconstruction that never did)
    is exercised exactly as a real run would be."""
    return _replay(provider, fixture, declares_turn=_declares_turn(provider))


def test_long_toolcall_replay_still_classifies_every_line():
    """Every line matches a rule — nothing falls to `raw`, including the
    tool_progress heartbeat. Fails before the tool_progress rule is added."""
    _, events = _replay(_shipped("claude"), FIXTURE)
    assert not [e for e in events if e.kind == "raw"]


def test_long_toolcall_replay_counts_turns_not_lines():
    """3 assistant message ids -> 3 steps, not 222 (the `step`-kind line
    count) and not 227 (the raw line count)."""
    sup, events = _replay_as_runner_would(_shipped("claude"), FIXTURE)
    ids = {e.turn for e in events if e.kind in ("tool", "text", "step") and e.turn}
    assert len(ids) == 3, "fixture sanity"
    step_kind_lines = sum(1 for e in events if e.kind == "step")
    assert step_kind_lines > 200, "fixture sanity: the burst is realistically large"
    assert sup.steps == 3, f"{sup.steps} steps for 3 turns and {step_kind_lines} step-kind lines"


def test_long_toolcall_tool_progress_event_is_untagged_like_system():
    """The heartbeat carries no message id, so it is excluded from step
    counting exactly like system/user/rate_limit_event — not because it is
    special-cased, but because none of them declare `turn`."""
    claude = _shipped("claude")
    lines = (Path(__file__).resolve().parent / "fixtures" / FIXTURE).read_text().splitlines()
    raw = next(json.loads(l) for l in lines if l.strip()
               and json.loads(l)["type"] == "tool_progress")
    event = claude.parse_line(json.dumps(raw))
    assert event.kind == "step"
    assert event.turn == ""


# ===========================================================================
# The regression mechanism itself: a stale, pre-P0-R4 project/global
# providers.yaml copy (no `turn:` field anywhere) shadows the corrected
# shipped defaults, because config layering replaces stream.rules wholesale.
# ===========================================================================

_STALE_PRE_TURN_CLAUDE_RULES = [
    {"match": {"type": "assistant",
               "message.content[type=tool_use].type": "tool_use"},
     "as": "tool",
     "fields": {"name": "message.content[type=tool_use].name",
                "args": "message.content[type=tool_use].input"}},
    {"match": {"type": "assistant", "message.content[type=text].type": "text"},
     "as": "text",
     "fields": {"text": "message.content[type=text].text"}},
    {"match": {"type": "result"}, "as": "result",
     "fields": {"status": "subtype", "tokens": "usage", "cost": "total_cost_usd"}},
    {"match": {"type": "assistant"}, "as": "step", "fields": {}},
    {"match": {"type": "user"}, "as": "step", "fields": {}},
    {"match": {"type": "system"}, "as": "step", "fields": {}},
    {"match": {"type": "rate_limit_event"}, "as": "step", "fields": {}},
]


def test_stale_pre_turn_claude_rules_declare_no_turn_field():
    """Sanity check on the reconstruction itself, so the test below is known
    to exercise the `declares_turn=False` path for the right reason."""
    assert not any((r.get("fields") or {}).get("turn")
                   for r in _STALE_PRE_TURN_CLAUDE_RULES)
    assert any((r.get("fields") or {}).get("turn")
               for r in SHIPPED["claude"]["stream"]["rules"]), \
        "the shipped rules do declare turn -- the stale set below is what's missing it"


def test_stale_pre_turn_claude_rules_reproduce_the_regression(tmp_path, monkeypatch):
    """The exact failure from the incident: a stale rule set with no `turn:`
    field counts the 222 step-kind lines individually instead of the 3 model
    turns they belong to -- a >70x inflation on this fixture alone."""
    stale = _custom(_STALE_PRE_TURN_CLAUDE_RULES)
    assert not _declares_turn(stale), "reconstruction sanity: this must NOT declare turn"
    sup, events = _replay_as_runner_would(stale, FIXTURE)
    step_kind_lines = sum(1 for e in events if e.kind == "step")
    assert sup.steps == step_kind_lines
    assert sup.steps > 200, f"only {sup.steps} steps -- reconstruction no longer reproduces the bug"
