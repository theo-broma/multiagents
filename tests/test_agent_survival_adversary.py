"""Adversarial tests attacking agent-survival (SV-R1..SV-R10).

Contract: `context/specs/agent-survival.md`.
Adversary subagent: ag-90d8ab.

Four defects demonstrated:
1. `multiagents stop <id>` ignores and leaks child agents (subagents).
2. Active server overwrites `cancelled` with `failed` when `stop <id>` is called.
3. Unterminated output line fuses with next turn's output during `steer_agent()`, breaking JSON parsing.
4. Adoption loop spams `adopt_failed` indefinitely on non-adoptable corrupted/missing specs.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import time
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import sv_harness as h  # noqa: E402


@pytest.fixture
def project(tmp_path):
    made: list[h.Project] = []

    def build(**kw) -> h.Project:
        base = tmp_path / f"p{len(made)}"
        base.mkdir()
        p = h.Project(base, **kw)
        made.append(p)
        return p

    yield build
    for p in made:
        p.cleanup()


# ---------------------------------------------------------------------------
# Defect 1: multiagents stop <id> ignores and leaks child agents
# ---------------------------------------------------------------------------

def test_adversary_stop_id_must_stop_child_nodes(project):
    """SV-R10 / BRIEF: `multiagents stop <id>` must cascade to child nodes.

    When an agent spawns child subagents, stopping the parent agent must stop
    its child subagents too, rather than leaving them running indefinitely
    in the background and marked as `running` in tree.json.
    """
    p = project()
    server = p.server()
    parent_hb = p.marker("parent.hb")
    child_hb = p.marker("child.hb")

    parent_id = server.start(h.plan_token([["heartbeat", str(parent_hb)]]), agent="spawner")
    assert h.growing(parent_hb)

    child_server = p.server(agent_id=parent_id, depth=1)
    child_id = child_server.start(h.plan_token([["heartbeat", str(child_hb)]]), agent="worker")
    assert h.growing(child_hb)

    child_node = p.node(child_id)
    assert child_node is not None and child_node.parent == parent_id

    # Stop the parent via CLI
    res = p.cli("stop", parent_id)
    assert res.returncode == 0

    # Both parent and child must be cancelled and their processes stopped
    assert p.status(parent_id) == "cancelled"
    assert p.status(child_id) == "cancelled", (
        f"child agent {child_id} was leaked in status {p.status(child_id)!r} "
        f"after parent {parent_id} was stopped"
    )
    assert h.stopped_growing(child_hb, 3.0), "child process was leaked and continues running"


# ---------------------------------------------------------------------------
# Defect 2: Active server overwrites cancelled with failed
# ---------------------------------------------------------------------------

def test_adversary_stop_id_supervised_node_stays_cancelled(project):
    """SV-R10: `multiagents stop <id>` on an actively supervised agent must not be overwritten to `failed`.

    When an external caller or operator runs `multiagents stop <id>`, SV-R10
    mandates the node is marked `cancelled`. However, the supervising server's
    `_consume` loop catches the process termination (-15) and its `_finalize`
    clobbers the `cancelled` status to `failed: exited -15`.
    """
    p = project()
    server = p.server()
    hb = p.marker("agent.hb")

    agent_id = server.start(h.plan_token([["heartbeat", str(hb)]]))
    assert h.growing(hb)
    assert p.status(agent_id) == "running"

    res = p.cli("stop", agent_id)
    assert res.returncode == 0

    # Wait for the supervising server to finalize its handling of the killed process
    time.sleep(1.5)

    status = p.status(agent_id)
    assert status == "cancelled", (
        f"expected status 'cancelled' per SV-R10, but server overwrote it with {p.describe(agent_id)!r}"
    )


# ---------------------------------------------------------------------------
# Defect 3: Unterminated output line fuses with next turn's output during steer_agent
# ---------------------------------------------------------------------------

def test_adversary_unterminated_last_line_survives_steer(project):
    """SV-R9 / SV-R7: An unterminated line at EOF must not corrupt steer_agent.

    If an agent exits or is interrupted without a trailing newline on its
    last line in output.ndjson, steering the agent opens output.ndjson in 'ab'
    mode and appends the new turn directly to the unclosed line. The fused
    line causes JSONDecodeError, dropping the turn's boundary events and
    failing the steer.
    """
    p = project()
    server = p.server()
    sid = "sv-steer-test-01"

    # Run turn 1 cleanly
    agent_id = server.start(h.plan_token([
        h.step_start(sid),
        h.text(sid, "first turn"),
        h.step_finish(sid, 10, 10, 0.01),
    ]))
    p.wait_status(agent_id, {"done"}, timeout=15)
    assert p.status(agent_id) == "done"

    # Simulate an unterminated write at the end of output.ndjson (e.g. from an
    # interrupted flush, raw output, or partial write before exit)
    output_file = p.run_dir(agent_id) / "output.ndjson"
    assert output_file.is_file()
    with output_file.open("ab") as fh:
        fh.write(b'{"type": "partial_fragment"')  # No trailing newline!

    # Steering the agent should resume it cleanly without failing due to corrupt JSON
    res = server.call("steer_agent", agent_id=agent_id, message="resume turn")
    assert isinstance(res, dict)
    assert res.get("steered") is True, f"steer_agent failed on unterminated output line: {res}"


# ---------------------------------------------------------------------------
# Defect 4: Adoption loop spams adopt_failed indefinitely on corrupt node
# ---------------------------------------------------------------------------

def test_adversary_adoption_does_not_loop_forever_on_corrupt_node(project):
    """SV-R6: A non-adoptable corrupted or missing spec node must not loop forever.

    When `_adopt_one` fails with an unhandled exception (e.g., deleted agent spec,
    corrupted command.json), `Runner.adopt()` releases the lock and emits an
    `adopt_failed` event without updating the node's status. Because the node
    remains in ADOPTABLE ('detached'), the periodic adoption loop retries it
    every 5 seconds indefinitely, spamming events.jsonl with adopt_failed.
    """
    p = project()
    server = p.server()
    hb = p.marker("agent.hb")

    # Start an agent and detach it
    agent_id = server.start(h.plan_token([["heartbeat", str(hb)]]))
    assert h.growing(hb)
    server.eof()
    assert server.exited(5.0)
    assert p.status(agent_id) == "detached"

    # Simulate a removed agent spec (e.g. branch switch or configuration cleanup)
    cfg_agents = p.root / ".multiagents" / "config" / "agents.yaml"
    cfg_agents.write_text(yaml.safe_dump({"agents": {}}))

    # A new server starts up and its adoption loop runs. TS-R2: every 0.5 s
    # instead of production's ADOPT_SECONDS=5.
    server2 = p.server(intervals={"ADOPT_SECONDS": 0.5})

    # Give the adoption loop time to run at least two cycles
    time.sleep(2.0)

    failed_events = p.events(agent_id, "adopt_failed")
    node = p.node(agent_id)

    # The node must be marked terminal (e.g. failed or orphaned) and must NOT
    # keep retrying endlessly (more than 1 adopt_failed event)
    assert node is not None and node.status in ("failed", "orphaned"), (
        f"corrupt node was left in adoptable status {node.status!r}, causing infinite retry loop"
    )
    assert len(failed_events) <= 1, (
        f"adoption loop spammed {len(failed_events)} adopt_failed events; "
        f"failed to terminate the unadoptable node"
    )
