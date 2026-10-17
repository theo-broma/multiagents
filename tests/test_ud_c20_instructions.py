"""UD-C20 / UD-C21 (slice 5): the instruction files `_orchestrator.md` and
`_initializer.md` teach the user-decision workflow. Presence tests check for a
distinctive anchor (a tool name, a status word), never exact wording.

Red until slice 5 is written. Two tests also wait for slice 4, which registers
`list_decisions`: test_c20_names_a_registered_tool_list_decisions (both files)
and test_c20_registry_has_every_tool_the_instructions_need. The other
list_decisions anchors (test_c20_reads_decisions_at_every_wake) only read the
markdown and go green with slice 5 alone."""
import asyncio
import re
from pathlib import Path

import pytest

from support.ud_support import REPO

TEAM = REPO / "src" / "multiagents" / "defaults" / "agents" / "team"
FILES = {"orchestrator": TEAM / "_orchestrator.md", "initializer": TEAM / "_initializer.md"}
BOTH = pytest.mark.parametrize("who", sorted(FILES))

# Backticked snake_case names that start with one of these verbs are read as MCP
# tool references; other backticked words (agent_id, loop_max, ...) are fields.
TOOL_VERBS = ("ask", "answer", "withdraw", "mark", "escalate", "list", "get", "wait", "check",
              "create", "close", "collect", "discard", "merge", "push", "refresh", "relaunch",
              "start", "steer", "stop", "submit", "update", "instantiate", "ack", "give", "supersede")


def text(who):
    return FILES[who].read_text()


def tool_names():
    import multiagents.server as srv
    return {t.name for t in asyncio.run(srv.mcp.list_tools())}


def mentioned_tools(who):
    names = set(re.findall(r"`([a-z][a-z0-9]*(?:_[a-z0-9]+)+)`", text(who)))
    names |= set(re.findall(r"`([a-z][a-z0-9_]*)\(", text(who)))
    return {n for n in names if n.split("_")[0] in TOOL_VERBS or n in tool_names()}


def paragraphs(who):
    return [p for p in re.split(r"\n\s*\n", text(who))]


@BOTH
def test_c20_every_tool_named_exists_in_the_registry(who):
    names = mentioned_tools(who)
    assert names, "parser found no tool names at all"
    assert names <= tool_names(), sorted(names - tool_names())


def test_c20_parser_sees_the_known_tools():
    assert "answer_question" in mentioned_tools("orchestrator")


@BOTH
def test_c20_names_a_registered_tool_list_decisions(who):
    # red until slice 4 registers list_decisions (and slice 5 names it)
    assert "list_decisions" in mentioned_tools(who)
    assert "list_decisions" in tool_names()


def test_c20_registry_has_every_tool_the_instructions_need():
    # red until slice 4 (list_decisions)
    need = {"ask_user", "list_decisions", "mark_decision_handled", "escalate_decision", "answer_question"}
    assert need <= tool_names(), sorted(need - tool_names())


def test_c20_initializer_does_not_use_need_decision_for_its_own_waits():
    offenders = [ln for ln in text("initializer").splitlines()
                 if "NEED_DECISION" in ln
                 and not re.search(r"\b(not|never|instead|rather than|no longer)\b", ln, re.I)]
    assert offenders == []


@BOTH
def test_c20_waits_on_the_user_go_through_ask_user(who):
    assert "ask_user" in text(who)


@BOTH
def test_c20_options_carry_implies_and_a_recommendation(who):
    t = text(who).lower()
    assert "implies" in t
    assert "recommend" in t
    assert "option" in t


@BOTH
def test_c20_brief_may_cite_the_decision_id(who):
    assert any("BRIEF" in p and re.search(r"\bd-", p) for p in paragraphs(who))


@BOTH
def test_c20_chat_alone_is_not_a_wait(who):
    assert any(re.search(r"\bchat\b", p, re.I) and "ask_user" in p for p in paragraphs(who))


@BOTH
def test_c20_reads_decisions_at_every_wake(who):
    t = text(who)
    assert "list_decisions" in t
    assert re.search(r"\bwake", t, re.I)


@BOTH
def test_c20_checks_refs_with_the_digest_before_acting(who):
    t = text(who).lower()
    assert "digest" in t
    assert "supersede" in t


@BOTH
def test_c20_marks_the_decision_handled(who):
    t = text(who)
    assert "mark_decision_handled" in t
    for outcome in ("applied", "not_applied", "error"):
        assert outcome in t, outcome


@BOTH
def test_c20_never_answers_a_user_decision(who):
    ps = [p for p in paragraphs(who) if "multiagents ask" in p and "monitor" in p]
    assert any(re.search(r"\b(never|not)\b", p, re.I) for p in ps)


@BOTH
def test_c20_tells_the_user_to_answer_with_multiagents_ask_or_the_monitor(who):
    t = text(who)
    assert "multiagents ask" in t
    assert "monitor" in t.lower()


def test_c20_orchestrator_answers_agent_questions_and_escalates_user_ones():
    t = text("orchestrator")
    assert "answer_question" in t
    assert "escalate_decision" in t
    assert any("escalate_decision" in p and re.search(r"\b(never|not)\b.*cop", p, re.I | re.S)
               for p in paragraphs("orchestrator"))


def test_c20_initializer_creates_its_own_waits_with_ask_user():
    t = text("initializer")
    assert "ask_user" in t
    assert any("ask_user" in p and re.search(r"open question|undecided|user", p, re.I)
               for p in paragraphs("initializer"))
