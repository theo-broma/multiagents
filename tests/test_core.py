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
from multiagents.runner import _merge_usage
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


def test_opencode_step_reports_cost():
    p = Provider.from_dict("opencode", {
        "bin": "opencode", "spawn": {},
        "stream": {"format": "ndjson", "rules": [
            {"match": {"type": "step_finish"}, "as": "step",
             "fields": {"tokens": "part.tokens", "cost": "part.cost"}},
        ]},
    })
    e = p.parse_line('{"type":"step_finish","part":{"tokens":{"total":100},"cost":0.00049685}}')
    assert e.cost == 0.00049685 and e.tokens == {"total": 100}


def test_delta_usage_is_summed_cumulative_is_maxed():
    """Getting this backwards silently corrupts every number above it.

    opencode emits per-step deltas; agy emits running totals. Summing a
    cumulative reporter inflates it, and maxing a delta reporter undercounts
    a multi-step run to the size of its largest single step.
    """
    steps = [{"total": 100, "cache": {"read": 10}}, {"total": 250, "cache": {"read": 20}}]

    delta = {}
    for s_ in steps:
        delta = _merge_usage(delta, s_, "delta")
    assert delta == {"total": 350, "cache": {"read": 30}}

    cumulative = {}
    for s_ in steps:
        cumulative = _merge_usage(cumulative, s_, "cumulative")
    assert cumulative == {"total": 250, "cache": {"read": 20}}


def test_cost_is_never_merged_as_a_token_field():
    # cost_usd is accumulated by the runner, not folded in by _merge_usage;
    # letting it through would double-count it.
    out = _merge_usage({"cost_usd": 0.5}, {"cost_usd": 0.9, "total": 10}, "delta")
    assert out["cost_usd"] == 0.5 and out["total"] == 10


def test_rollup_keeps_cost_fractional(tmp_path):
    from multiagents.tree import Node
    tree = Tree(tmp_path / "tree.json", tmp_path / "events.jsonl")
    for i, cost in enumerate([0.000627, 0.0023]):
        n = Node(id=f"ag-{i}", agent="a", provider="opencode", model="m", parent=None, depth=1)
        n.usage = {"total": 1000, "cost_usd": cost}
        tree.add(n)
    rolled = tree.rollup_usage()
    assert rolled["total"] == 2000              # tokens stay whole
    assert rolled["cost_usd"] == 0.002927       # cost keeps its fraction


def test_models_include_filters_by_namespace():
    """The generated list must cover the subscription, not a separate account.

    `opencode models` lists deepinfra/* alongside the subscription's own
    models; those bill against a different key, so an agent routed onto one
    spends from an account the user did not intend.
    """
    p = Provider.from_dict("opencode", {
        "bin": "opencode", "spawn": {}, "stream": {},
        "models_include": ["opencode/*", "opencode-go/*"],
    })
    out = [m["id"] for m in p.parse_models(
        "opencode/big-pickle\nopencode-go/glm-5.3-flash\n"
        "deepinfra/zai-org/GLM-5.3\ndeepinfra/Qwen/Qwen3.8-Max\n"
    )]
    assert out == ["opencode/big-pickle", "opencode-go/glm-5.3-flash"]


def test_models_exclude_beats_include():
    p = Provider.from_dict("x", {
        "bin": "x", "spawn": {}, "stream": {},
        "models_include": ["a/*"], "models_exclude": ["a/bad-*"],
    })
    assert p.allows_model("a/good-1") and not p.allows_model("a/bad-1")
    assert not p.allows_model("b/other")


def test_no_include_list_allows_everything():
    p = Provider.from_dict("x", {"bin": "x", "spawn": {}, "stream": {}})
    assert p.allows_model("anything/at-all")


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
