"""Assistant event boundaries must not invent decision-marker lines."""

import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import c3_harness as h


def run_parts(tmp_path, monkeypatch, parts):
    provider = h.fake_cli(tmp_path, events=[
        {"type": "text", "text": part} for part in parts
    ])
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


def test_split_inline_quoted_marker_keeps_full_output(tmp_path, monkeypatch):
    parts = ["The `", "NEED_DECISION(topic): question` marker is an example."]
    runner, agent = run_parts(tmp_path, monkeypatch, parts)
    assert runner.tree.get(agent).status == "done"
    assert runner.tree.open_questions(agent) == []
    assert runner.runs[agent].text_parts == parts
    result = json.loads((runner.paths.run_dir(agent) / "result.json").read_text())
    assert result["text"] == "\n".join(parts)


@pytest.mark.parametrize("parts", [
    ["I need a choice", " before continuing.\nNEED_DECISION(store): SQLite?\nDEFAULT: SQLite"],
    ["NEED_DECI", "SION(store): SQLite?\nDEFAULT: SQLite"],
    ["NEED_DECISION(store): SQL", "ite?"],
    ["- NEED_DECI", "SION(store): SQLite?\nDEFAULT: SQLite"],
])
def test_split_genuine_marker_parks(tmp_path, monkeypatch, parts):
    runner, agent = run_parts(tmp_path, monkeypatch, parts)
    assert runner.tree.get(agent).status == "awaiting_user"
    questions = runner.tree.open_questions(agent)
    assert len(questions) == 1
    assert questions[0]["topic"] == "store"
    assert questions[0]["question"] == "SQLite?"
    assert questions[0]["proposed_default"] == ("SQLite" if "DEFAULT:" in "".join(parts) else "")


@pytest.mark.parametrize("parts", [
    ["NEED_DECISION(store): SQLite?\n", "DEFAULT: SQLite\n", "must not be consumed"],
    ["NEED_DECISION(store): SQLite?\nDEFAULT: SQLit", "e\n", "must not be consumed"],
])
def test_waits_for_complete_streamed_default(tmp_path, monkeypatch, parts):
    runner, agent = run_parts(tmp_path, monkeypatch, parts)
    assert runner.tree.get(agent).status == "awaiting_user"
    questions = runner.tree.open_questions(agent)
    assert len(questions) == 1
    assert questions[0]["question"] == "SQLite?"
    assert questions[0]["proposed_default"] == "SQLite"
    assert runner.runs[agent].text_parts == parts[:2]


def test_no_default_stops_after_next_complete_line(tmp_path, monkeypatch):
    parts = ["NEED_DECISION(store): SQLite?\n", "There is no default.\n",
             "DEFAULT: must not be consumed\n"]
    runner, agent = run_parts(tmp_path, monkeypatch, parts)
    assert runner.tree.get(agent).status == "awaiting_user"
    questions = runner.tree.open_questions(agent)
    assert len(questions) == 1
    assert questions[0]["proposed_default"] == ""
    assert runner.runs[agent].text_parts == parts[:2]


def test_last_marker_without_newline_or_default_parks(tmp_path, monkeypatch):
    runner, agent = run_parts(tmp_path, monkeypatch,
                              ["NEED_DECISION(store): SQLite?"])
    assert runner.tree.get(agent).status == "awaiting_user"
    questions = runner.tree.open_questions(agent)
    assert len(questions) == 1
    assert questions[0]["question"] == "SQLite?"
    assert questions[0]["proposed_default"] == ""


def test_nondefault_paragraph_stops_before_its_newline(tmp_path, monkeypatch):
    parts = ["NEED_DECISION(store): SQLite?\n", "  ",
             "This paragraph continues without a newline",
             " and keeps discussing the available databases",
             " without offering any proposed default."]
    runner, agent = run_parts(tmp_path, monkeypatch, parts)
    assert runner.tree.get(agent).status == "awaiting_user"
    questions = runner.tree.open_questions(agent)
    assert len(questions) == 1
    assert questions[0]["proposed_default"] == ""
    assert runner.runs[agent].text_parts == parts[:3]
