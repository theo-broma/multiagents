"""RM-R1: control markers through real provider streams and public results."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))
from c3_harness import AgentSpec, fake_cli, make_runner

WAIT_BOUND = 3.0
MARKERS = {
    "info": "NEED_INFO(db): which database?",
    "decision": "NEED_DECISION(db): which database?\nDEFAULT: sqlite",
    "verdict": "VERDICT(approved): checked",
}


def masked(text, context):
    return {
        "fence": f"```text\n{text}\n```\n",
        "tilde-fence": f"~~~text\n{text}\n~~~\n",
        "indent": "\n".join("    " + line for line in text.splitlines()) + "\n",
        "tab": "\n".join("\t" + line for line in text.splitlines()) + "\n",
        "inline": f"`{text}`\n",
        "quote": "\n".join("> " + line for line in text.splitlines()) + "\n",
    }[context]


async def finished(r, rid):
    deadline = asyncio.get_running_loop().time() + WAIT_BOUND
    while r.check(rid)["status"] in {"pending", "running"}:
        assert asyncio.get_running_loop().time() < deadline, r.check(rid)
        await asyncio.sleep(0.01)
    return r.check(rid)


def run_events(tmp_path, monkeypatch, events):
    p = fake_cli(tmp_path, events=events)
    p["stream"]["rules"].append({"match": {"type": "tool"}, "as": "tool",
                                 "fields": {"name": "name", "args": "args"}})
    r = make_runner(tmp_path, monkeypatch,
                    agents={"worker": AgentSpec("worker", "p", "m")}, providers={"p": p})

    async def scenario():
        rid = (await r.start("worker", "exercise markers"))["agent_id"]
        try:
            check = await finished(r, rid)
            return check, r.collect(rid, "full"), r.tree.read()["nodes"][rid]
        finally:
            if r.check(rid)["status"] in {"pending", "running"}:
                await r.stop(rid)
    return asyncio.run(scenario())


@pytest.mark.parametrize("marker", MARKERS)
@pytest.mark.parametrize("context", ["fence", "tilde-fence", "indent", "tab", "inline", "quote"])
@pytest.mark.parametrize("phase", ["live", "final"])
def test_rm_r1_masked_markers_do_not_trigger(tmp_path, monkeypatch, marker, context, phase):
    text = masked(MARKERS[marker], context)
    events = ([{"type": "text", "text": text}, {"type": "result", "subtype": "success", "result": "finished"}]
              if phase == "live" else [{"type": "result", "subtype": "success", "result": text}])
    check, collected, record = run_events(tmp_path, monkeypatch, events)
    assert check["status"] == "done", check
    assert "question" not in check, check
    assert collected["need_info"] == [], collected
    assert not record.get("verdict"), record


@pytest.mark.parametrize("marker", MARKERS)
def test_rm_r1_fence_state_survives_stream_chunks(tmp_path, monkeypatch, marker):
    events = [{"type": "text", "text": "```text\n"},
              {"type": "text", "text": MARKERS[marker] + "\n"},
              {"type": "text", "text": "```\nfinished"}]
    check, collected, record = run_events(tmp_path, monkeypatch, events)
    assert check["status"] == "done", check
    assert collected["need_info"] == [], collected
    assert not record.get("verdict"), record


@pytest.mark.parametrize("marker", MARKERS)
@pytest.mark.parametrize("prefix", ["", "- ", "* ", "1. "])
def test_rm_r1_unmasked_line_shapes_keep_their_meaning(tmp_path, monkeypatch, marker, prefix):
    text = prefix + MARKERS[marker] + "\n"
    check, collected, record = run_events(tmp_path, monkeypatch, [{"type": "text", "text": text}])
    if marker == "decision":
        assert check["status"] == "awaiting_user", check
        assert check["question"]["topic"] == "db"
        assert check["question"]["proposed_default"] == "sqlite"
    elif marker == "info":
        assert check["status"] == "done"
        assert collected["need_info"] == [text.strip()]
    else:
        assert record.get("verdict") == ("approved" if not prefix else ""), record


@pytest.mark.parametrize("context", ["fence", "indent", "inline", "quote"])
def test_rm_r1_a_masked_default_is_not_attached_to_a_real_question(tmp_path, monkeypatch, context):
    text = "NEED_DECISION(db): which database?\n" + masked("DEFAULT: unsafe", context) + "\n"
    check, _, _ = run_events(tmp_path, monkeypatch, [{"type": "text", "text": text}])
    assert check["status"] == "awaiting_user", check
    assert check["question"]["proposed_default"] == "", check


@pytest.mark.parametrize("marker", MARKERS)
def test_rm_r1_a_closed_fence_does_not_mask_a_later_real_marker(tmp_path, monkeypatch, marker):
    events = [{"type": "text", "text": "```text\n"},
              {"type": "text", "text": MARKERS[marker] + "\n```\n"},
              {"type": "text", "text": MARKERS[marker] + "\n"}]
    check, collected, record = run_events(tmp_path, monkeypatch, events)
    if marker == "info":
        assert len(collected["need_info"]) == 1, collected
    elif marker == "decision":
        assert check["status"] == "awaiting_user", check
    else:
        assert record.get("verdict") == "approved", record


@pytest.mark.parametrize("context", ["fence", "indent", "inline", "quote"])
def test_rm_r1_masked_contradictory_verdict_does_not_cancel_real_verdict(tmp_path, monkeypatch, context):
    text = masked("VERDICT(rejected, 2): example", context) + "\nVERDICT(approved): real\n"
    _, _, record = run_events(tmp_path, monkeypatch, [{"type": "text", "text": text}])
    assert record.get("verdict") == "approved", record


def test_rm_r1_decision_still_stops_live_before_final_result(tmp_path, monkeypatch):
    p = fake_cli(tmp_path, events=[{"type": "text", "text": MARKERS["decision"] + "\n"}])
    script = Path(p["bin"])
    # Gate the fake provider before it exits: a final-only parser cannot pass.
    script.write_text(script.read_text().replace("sys.exit(0)",
                      "from pathlib import Path\nwhile not Path('release').exists():\n    time.sleep(0.01)"))
    r = make_runner(tmp_path, monkeypatch,
                    agents={"worker": AgentSpec("worker", "p", "m")}, providers={"p": p})
    async def scenario():
        rid = (await r.start("worker", "live decision"))["agent_id"]
        try:
            check = await finished(r, rid)
            assert check["status"] == "awaiting_user", check
            assert check["question"]["proposed_default"] == "sqlite", check
        finally:
            if r.check(rid)["status"] in {"pending", "running"}:
                await r.stop(rid)
    asyncio.run(scenario())
