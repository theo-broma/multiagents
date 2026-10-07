"""TB-R1/TB-R2 guards for parser quoting and completed alias relaunches."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from multiagents.runner import Runner
from nc_fixture.m4_world import M4World


@pytest.mark.parametrize("text, expected", [
    ("1. NEED_INFO(topic): choose `sqlite`?", ["1. NEED_INFO(topic): choose `sqlite`?"]),
    ("* NEED_INFO(topic): which?", ["* NEED_INFO(topic): which?"]),
    ("NEED_INFO(" + "topic" * 30 + "): which?", ["NEED_INFO(" + "topic" * 30 + "): which?"]),
    ("~~~text\nNEED_INFO(topic): which?\n~~~", []),
    ("```text\nNEED_INFO(topic): which?", []),
    ("``example\nNEED_INFO(topic): which?\nend``", []),
    ("> NEED_INFO(topic): which?", []),
])
def test_tb_r1_markdown_examples_and_real_questions(text, expected):
    assert Runner.need_info(text) == expected


@pytest.mark.parametrize("aliased", [False, True], ids=["simple", "aliased"])
def test_tb_r2_a_completed_simple_relaunch_keeps_commits_and_starts_fresh(tmp_path, monkeypatch, aliased):
    w = M4World(tmp_path, monkeypatch)
    try:
        fx = w.provider("fxw")
        w.agent("wk", "fxw", writes=True)
        fx.queue({"text": "finished", "write": {"first.txt": "one\n"}, "commit": "first"},
                 {"text": "finished again", "write": {"second.txt": "two\n"}, "commit": "second"})
        w.start_scheduler()
        if aliased:
            w.register_ok("""template: fresh
version: 1
params: {}
root:
  key: task
  kind: simple
  agent: wk
  task: keep going
  session: A
""")
            node = w.instantiate_ok("fresh", {})
        else:
            node = w.simple("N", "wk")
        assert w.wait_state(node, "done", timeout=8)["outcome"] == "completed"
        assert w.root_op("relaunch_node", node).get("ok") is True
        done = w.wait_state(node, "done", timeout=8)
        assert done["outcome"] == "completed"
        first, second = fx.calls()
        assert second["resume"] is None and second["session"] != first["session"]
        assert w.show(f"refs/heads/nodes/{node}:first.txt") == "one\n"
        assert w.show(f"refs/heads/nodes/{node}:second.txt") == "two\n"
        assert len(done["generations"]) == 2
    finally:
        w.close()
