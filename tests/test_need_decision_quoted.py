"""Decision examples in prose must not park an otherwise completed run."""

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import c3_harness as h


@pytest.mark.parametrize("text", [
    "- The `NEED_DECISION(topic): question` marker is parsed from assistant text.",
    "The marker NEED_DECISION(topic): question is an example.",
    "  `NEED_DECISION(topic): question`\nDEFAULT: example",
])
def test_quoted_or_midline_marker_does_not_park(tmp_path, monkeypatch, text):
    runner, agent = run_text(tmp_path, monkeypatch, text)
    assert runner.tree.get(agent).status == "done"
    assert runner.tree.open_questions(agent) == []


@pytest.mark.parametrize("indent", ["", "  ", "\t"])
def test_line_start_marker_parks_with_default(tmp_path, monkeypatch, indent):
    text = ("I need a choice before continuing.\n"
            f"{indent}NEED_DECISION(store): Postgres or SQLite?\n"
            "DEFAULT: SQLite")
    runner, agent = run_text(tmp_path, monkeypatch, text)
    assert runner.tree.get(agent).status == "awaiting_user"
    questions = runner.tree.open_questions(agent)
    assert len(questions) == 1
    assert questions[0]["topic"] == "store"
    assert questions[0]["question"] == "Postgres or SQLite?"
    assert questions[0]["proposed_default"] == "SQLite"


def run_text(tmp_path, monkeypatch, text):
    provider = h.fake_cli(tmp_path, events=[{"type": "text", "text": text}])
    runner = h.make_runner(
        tmp_path / "project", monkeypatch,
        agents={"worker": h.AgentSpec("worker", "fake", "m")},
        providers={"fake": provider},
    )

    async def go():
        started = await runner.start("worker", "go")
        agent = started["agent_id"]
        await asyncio.wait_for(runner.runs[agent].task, timeout=30)
        return agent

    return runner, asyncio.run(go())
