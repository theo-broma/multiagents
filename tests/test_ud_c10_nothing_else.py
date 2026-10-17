"""UD-C10: slice 2 adds the five MCP tools and nothing else. No CLI command,
no monitor action or route, no instruction text; the existing question paths
behave as before."""
import asyncio
import re

import pytest

from support.ud2_support import world  # noqa: F401
from support.ud_support import REPO

FIVE = {"ask_user", "withdraw_decision", "mark_decision_handled", "escalate_decision",
        "answer_decision"}


def test_c10_exactly_the_five_decision_tools_are_new():
    import multiagents.server as srv
    names = {t.name for t in asyncio.run(srv.mcp.list_tools())}
    assert FIVE <= names
    mine = {n for n in names if "decision" in n or n == "ask_user"}
    assert mine == FIVE, mine - FIVE
    assert {"list_questions", "answer_question"} <= names


@pytest.mark.parametrize("argv", [["decisions"], ["decisions", "--json"], ["ask-user"], ["ask_user"],
                                  ["decision"], ["answer-decision"], ["check-refs"]])
def test_c10_no_decisions_cli_command(argv):
    from multiagents import cli
    with pytest.raises(SystemExit) as exit_:
        cli.main(argv)
    assert exit_.value.code == 2


def test_c10_no_decision_action_or_route_on_the_monitor(tmp_path):
    from multiagents.monitor import actions
    from multiagents.paths import ProjectPaths
    paths = ProjectPaths(tmp_path)
    paths.ensure()
    for name in ("answer_decision", "withdraw_decision", "ask_user", "check_refs",
                 "mark_decision_handled", "escalate_decision"):
        assert actions.perform(paths, name, {})["ok"] is False
    source = (REPO / "src" / "multiagents" / "monitor" / "server.py").read_text()
    assert "/api/decisions" not in source


def test_c10_the_list_questions_still_exclude_new_decisions(world):
    from support.ud2_support import ask, call, succeeded
    succeeded(ask())
    q = world.tree.add_question("ag-3", "store", "Postgres?")
    out = call("list_questions")
    assert [o["id"] for o in out["open"]] == [q["id"]] and out["count"] == 1


def test_c10_no_instruction_text_mentions_the_new_tools():
    names = FIVE | {"check_refs"}
    offenders = []
    for path in (REPO / "src" / "multiagents").rglob("*"):
        if (path.is_file() and path.suffix in {".md", ".txt", ".yaml", ".yml", ".j2"}
                and re.search("|".join(rf"\b{n}\b" for n in names), path.read_text(errors="ignore"))):
            offenders.append(str(path.relative_to(REPO)))
    assert offenders == []
