"""Unit tests for the logic that live runs do not reliably exercise.

Doom-loop detection is the main one: a well-behaved agent never triggers it, so
without tests it would ship unverified. Redaction matters for the same reason —
it only proves itself on the day something secret reaches a log.
"""

import sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from multiagents.config import deep_merge
from multiagents.providers import Provider
from multiagents.redact import MASK, register_literal, scrub
from multiagents.supervisor import Supervisor, looks_like_quota_failure
from multiagents.tree import Tree

OPENCODE = Provider.from_dict("opencode", {
    "bin": "opencode",
    "spawn": {"args": ["run", "{prompt}", "-m", "{model}"], "permission": {"full": ["--auto"]}},
    "stream": {
        "format": "ndjson",
        "session_id_paths": ["sessionID"],
        "rules": [
            {"match": {"type": "text"}, "as": "text", "fields": {"text": "part.text"}},
            {"match": {"type": "tool_use"}, "as": "tool",
             "fields": {"name": "part.tool", "args": "part.state.input"}},
        ],
    },
})

AGY = Provider.from_dict("agy", {
    "bin": "agy",
    "spawn": {"args": ["-p", "{prompt}"]},
    "stream": {
        "format": "ndjson",
        "session_id_paths": ["result.conversation_id"],
        "rules": [
            {"match": {"event": "step_update", "step_update.step_type": "tool"}, "as": "tool",
             "fields": {"name": "step_update.tool_name", "args": "step_update.tool_info.parameters"}},
            {"match": {"event": "result"}, "as": "result",
             "fields": {"status": "result.status", "text": "result.response"}},
        ],
    },
})


def test_parses_real_opencode_events():
    e = OPENCODE.parse_line('{"type":"tool_use","sessionID":"ses_1","part":'
                            '{"tool":"glob","state":{"input":{"pattern":"*.py"}}}}')
    assert e.kind == "tool" and e.name == "glob"
    assert e.args == {"pattern": "*.py"} and e.session_id == "ses_1"


def test_parses_real_agy_events():
    e = AGY.parse_line('{"event":"step_update","step_update":{"step_type":"tool",'
                       '"tool_name":"run_command","tool_info":{"parameters":{"CommandLine":"ls"}}}}')
    assert e.kind == "tool" and e.args == {"CommandLine": "ls"}
    r = AGY.parse_line('{"event":"result","result":{"status":"SUCCESS","response":"hi",'
                       '"conversation_id":"c1"}}')
    assert r.kind == "result" and r.status == "SUCCESS" and r.session_id == "c1"


def test_unmatched_lines_are_kept_not_dropped():
    # Human-readable notices interleaved with JSON must survive as `raw` so a
    # new integration can be debugged with `multiagents probe`.
    assert OPENCODE.parse_line("jetski: some notice").kind == "raw"
    assert OPENCODE.parse_line('{"type":"mystery"}').kind == "raw"


def test_prompt_braces_cannot_corrupt_argv():
    argv = OPENCODE.build_command(prompt="use {model} literally", model="m1", workdir="/w")
    assert argv[2] == "use {model} literally" and argv[4] == "m1"


def test_doom_loop_detects_identical_repeats():
    sup = Supervisor(loop_repeats=3)
    same = '{"type":"tool_use","part":{"tool":"edit","state":{"input":{"file":"a.py"}}}}'
    assert sup.observe(OPENCODE.parse_line(same)) is None
    assert sup.observe(OPENCODE.parse_line(same)) is None
    trip = sup.observe(OPENCODE.parse_line(same))
    assert trip and trip.reason == "doom_loop"


def test_doom_loop_ignores_varied_work():
    sup = Supervisor(loop_repeats=3)
    for name in ("read", "edit", "bash", "glob", "read2", "write"):
        line = '{"type":"tool_use","part":{"tool":"%s","state":{"input":{"f":"%s"}}}}' % (name, name)
        assert sup.observe(OPENCODE.parse_line(line)) is None


def test_doom_loop_detects_two_step_cycle():
    sup = Supervisor(loop_repeats=3)
    a = '{"type":"tool_use","part":{"tool":"edit","state":{"input":{"f":"a"}}}}'
    b = '{"type":"tool_use","part":{"tool":"bash","state":{"input":{"c":"test"}}}}'
    trip = None
    for line in [a, b] * 4:
        trip = trip or sup.observe(OPENCODE.parse_line(line))
    assert trip and trip.reason == "doom_loop"


def test_only_first_trip_is_reported():
    sup = Supervisor(loop_repeats=2)
    same = '{"type":"tool_use","part":{"tool":"edit","state":{"input":{"f":"a"}}}}'
    sup.observe(OPENCODE.parse_line(same))
    assert sup.observe(OPENCODE.parse_line(same)) is not None
    assert sup.observe(OPENCODE.parse_line(same)) is None


def test_silence_and_wall_clock_trip():
    sup = Supervisor(silence_timeout=0.01, wall_timeout=999)
    time.sleep(0.05)
    trip = sup.check_timers()
    assert trip and trip.reason == "silence"

    sup2 = Supervisor(silence_timeout=999, wall_timeout=0.01)
    time.sleep(0.05)
    trip2 = sup2.check_timers()
    assert trip2 and trip2.reason == "timeout"


def test_quota_failure_classification():
    assert looks_like_quota_failure("RESOURCE_EXHAUSTED", "", "")
    assert looks_like_quota_failure("", "429 Too Many Requests", "")
    assert not looks_like_quota_failure("SUCCESS", "", "all done")


def test_redaction_masks_shapes_keys_and_literals():
    register_literal("hunter2-super-secret-value")
    out = scrub({
        "access_token": "abc123",                       # secret key name
        "note": "bearer sk-abcdefghijklmnopqrstuvwx",   # recognisable shape
        "leak": "hunter2-super-secret-value",           # registered literal
        "nested": [{"refresh_token": "zzz"}],
        "safe": "ordinary text",
    })
    assert out["access_token"] == MASK
    assert "sk-abcdefghijklmnopqrstuvwx" not in out["note"]
    assert out["leak"] == MASK
    assert out["nested"][0]["refresh_token"] == MASK
    assert out["safe"] == "ordinary text"


def test_config_deep_merge_semantics():
    base = {"limits": {"max_depth": 3, "max_children": 2}, "chain": ["a", "b"]}
    over = {"limits": {"max_depth": 5}, "chain": ["c"]}
    merged = deep_merge(base, over)
    assert merged["limits"] == {"max_depth": 5, "max_children": 2}   # maps recurse
    assert merged["chain"] == ["c"]                                   # lists replace


def test_tree_survives_a_corrupt_file(tmp_path):
    tree = Tree(tmp_path / "tree.json", tmp_path / "events.jsonl")
    (tmp_path / "tree.json").write_text("{ this is not json")
    assert tree.read()["nodes"] == {}          # degrades, does not wedge


def test_tree_render_keeps_roots_separate(tmp_path):
    from multiagents.tree import Node
    tree = Tree(tmp_path / "tree.json", tmp_path / "events.jsonl")
    tree.add(Node(id="ag-1", agent="a", provider="p", model="m", parent=None, depth=1))
    tree.add(Node(id="ag-2", agent="b", provider="p", model="m", parent=None, depth=1))
    tree.add(Node(id="ag-3", agent="c", provider="p", model="m", parent="ag-1", depth=2))
    lines = tree.render().splitlines()
    assert lines[0].startswith("ag-1") and lines[1].startswith("└─ ag-3")
    assert lines[2].startswith("ag-2")       # second root is NOT a child of the first
