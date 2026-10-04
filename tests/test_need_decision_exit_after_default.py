"""Decision markers across text parts: separate blocks are separate lines.

A provider's rules may declare `fields.block`, the id of the content block
or message part a text event belongs to. Parts of different blocks are
separate lines of the message; parts of one block (or of a provider that
declares no ids) are concatenated as-is, so a marker mentioned mid-line
stays mid-line wherever the provider happens to split it.
"""

import asyncio
import importlib.util
import json
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import c3_harness as h
from multiagents.paths import shipped_defaults_dir
from multiagents.providers import Provider


def run_parts(tmp_path, monkeypatch, parts):
    """`parts`: (block id, text) pairs, played as text events, then exit 0."""
    provider = h.fake_cli(tmp_path, events=[
        {"type": "text", "text": text, "block": block} for block, text in parts
    ], exit_code=0)
    for rule in provider["stream"]["rules"]:
        if rule["as"] == "text":
            rule["fields"]["block"] = "block"
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


QUESTION = "NEED_DECISION(schema): keep the v1 column or drop it?\nDEFAULT: keep it"


@pytest.mark.parametrize("trailing", ["", "\n"])
@pytest.mark.parametrize("parts", [
    [("b1", QUESTION)],
    [("b1", "working"), ("b2", QUESTION)],
    [("b1", "working\n"), ("b2", QUESTION)],
    [("b1", "working"), ("b2", "NEED_DECISION(schema): keep the v1 column or drop it?"),
     ("b3", "DEFAULT: keep it")],
    [("b1", "working"), ("b2", "- NEED_DECI"),
     ("b2", "SION(schema): keep the v1 column or drop it?\nDEFAULT: keep it")],
], ids=["single", "after-block", "after-line", "separate-default", "split-in-block"])
def test_marker_and_default_then_exit_parks_with_the_default(tmp_path, monkeypatch, parts, trailing):
    parts = parts[:-1] + [(parts[-1][0], parts[-1][1] + trailing)]
    runner, agent = run_parts(tmp_path, monkeypatch, parts)
    assert runner.tree.get(agent).status == "awaiting_user"
    questions = runner.tree.open_questions(agent)
    assert len(questions) == 1
    assert questions[0]["topic"] == "schema"
    assert questions[0]["question"] == "keep the v1 column or drop it?"
    assert questions[0]["proposed_default"] == "keep it"
    result = json.loads((runner.paths.run_dir(agent) / "result.json").read_text())
    assert result["status"] == "awaiting_user"


MENTION = ["Here is a mention: ", "NEED_DECISION(topic): question and more prose."]


def test_a_mid_line_mention_split_inside_one_block_does_not_park(tmp_path, monkeypatch):
    runner, agent = run_parts(tmp_path, monkeypatch, [("b1", part) for part in MENTION])
    assert runner.tree.get(agent).status == "done"
    assert runner.tree.open_questions(agent) == []


def test_the_same_text_as_two_blocks_parks(tmp_path, monkeypatch):
    runner, agent = run_parts(tmp_path, monkeypatch, list(zip(["b1", "b2"], MENTION)))
    assert runner.tree.get(agent).status == "awaiting_user"
    questions = runner.tree.open_questions(agent)
    assert len(questions) == 1
    assert questions[0]["topic"] == "topic"


def test_without_block_ids_parts_are_concatenated(tmp_path, monkeypatch):
    runner, agent = run_parts(tmp_path, monkeypatch, [("", part) for part in MENTION])
    assert runner.tree.get(agent).status == "done"


# The shipped rules carry each provider's structural block identity.

def shipped(name):
    raw = yaml.safe_load((shipped_defaults_dir() / "providers.yaml").read_text())
    return Provider.from_dict(name, raw["providers"][name])


def test_opencode_text_carries_its_part_id():
    event = shipped("opencode").parse_line(json.dumps(
        {"type": "text", "sessionID": "s", "part": {"id": "prt_1", "type": "text", "text": "hi"}}))
    assert (event.kind, event.text, event.block) == ("text", "hi", "prt_1")


def test_claude_text_carries_its_line_uuid():
    event = shipped("claude").parse_line(json.dumps(
        {"type": "assistant", "uuid": "u-1", "session_id": "s",
         "message": {"id": "msg_1", "content": [{"type": "text", "text": "hi"}]}}))
    assert (event.kind, event.text, event.block, event.turn) == ("text", "hi", "u-1", "msg_1")


def test_codex_text_carries_its_item_id():
    spec = importlib.util.spec_from_file_location(
        "codex_adapter_under_test", shipped_defaults_dir() / "providers" / "codex.py")
    codex = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(codex)
    out = codex.Normalizer().event({"type": "item.completed",
                                    "item": {"id": "item_3", "type": "agent_message", "text": "hi"}})
    event = shipped("codex").parse_line(json.dumps(out))
    assert (event.kind, event.text, event.block) == ("text", "hi", "item_3")
