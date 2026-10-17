"""UD-C6: slice 1 adds no MCP tool, CLI command, monitor route or instruction
text. These are guards that go red the moment a later slice's surface leaks
into this one; slices 2-5 replace them with their own contract."""
import asyncio
import re
from pathlib import Path

import pytest

from support.ud_support import REPO

# Slice 2 (UD-C7) deliberately adds exactly five tools: ask_user, withdraw_decision,
# mark_decision_handled, escalate_decision, answer_decision (tests/test_ud_c7_asking.py).
# The readers (list_decisions, get_decision) are slice 4 and stay forbidden here.
NEW_TOOLS = {"list_decisions", "get_decision", "decisions", "list_decision", "get_decisions"}


def test_c6_no_new_mcp_tool():
    import multiagents.server as srv
    names = {t.name for t in asyncio.run(srv.mcp.list_tools())}
    assert not (names & NEW_TOOLS), names & NEW_TOOLS
    assert {"list_questions", "answer_question"} <= names


@pytest.mark.parametrize("argv", [["decisions"], ["decisions", "--json"], ["decisions", "--status", "open"]])
def test_c6_no_decisions_cli_command(argv, capsys):
    from multiagents import cli
    with pytest.raises(SystemExit) as exit_:
        cli.main(argv)
    assert exit_.value.code == 2                      # argparse: invalid choice


def test_c6_no_decision_action_on_the_monitor(tmp_path):
    from multiagents.monitor import actions
    from multiagents.paths import ProjectPaths
    paths = ProjectPaths(tmp_path)
    paths.ensure()
    for name in ("answer_decision", "withdraw_decision", "list_decisions"):
        assert actions.perform(paths, name, {})["ok"] is False


def test_c6_no_decisions_route_on_the_monitor():
    source = (REPO / "src" / "multiagents" / "monitor" / "server.py").read_text()
    assert "/api/decisions" not in source


def test_c6_no_instruction_text_mentions_the_new_tool():
    # Slice 2 puts the tool's definition in server.py; instruction text (slice 5) stays clean.
    offenders = []
    for path in (REPO / "src" / "multiagents").rglob("*"):
        if path.is_file() and path.suffix in {".md", ".txt", ".yaml", ".py"} \
                and path.name not in {"decisions.py", "server.py"}:
            if re.search(r"\bask_user\b", path.read_text(errors="ignore")):
                offenders.append(str(path.relative_to(REPO)))
    assert offenders == []
