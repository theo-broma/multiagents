"""M2 — admission reuse and the gate-on front door: NC-R18, NC-R21 as limited by
NC-R51, NC-R25, NC-R56 (and the admission part of NC-R22/R52).

MCP tool functions of `multiagents.server` are called in-process as the
orchestrator (root) does; the scheduler is a real process; runs are fixture
agents. Observations: tool results, the node view over the NC-R8 socket,
`tree.json`, `events.jsonl`, the fixture's calls log.

Assumptions where the contract is silent (kept loose):
- `start_agent` gains an `urgent` keyword (NC-R21) and returns `node_id`,
  `agent_id` (only when a run started), `status`, `blocked` (a list of reasons,
  each a string or `{code, ...}`).
- A blocked tool result carries `blocked: [reasons]`; a reason is a string or a
  dict with `code`; the provider-limit one contains `provider_concurrency`.
- `steer_agent` on a finished run whose provider is full returns `blocked`.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from nc_fixture.agent import task  # noqa: E402
from nc_fixture.world import (World, blocked_codes, call_tool, run_id_of)  # noqa: E402


@pytest.fixture
def w(tmp_path, monkeypatch):
    world = World(tmp_path, monkeypatch)
    yield world
    world.close()


@pytest.fixture
def pw(w):
    """A world with a one-slot provider `pcfx` and its agent `pcworker`."""
    w.pc = w.provider("pcfx", max_concurrent=1)
    w.agent("pcworker", "pcfx")
    return w


def reasons(result: dict) -> list[str]:
    out = []
    for b in result.get("blocked") or []:
        out.append(b.get("code", "") if isinstance(b, dict) else str(b))
    return out


def saturate(w: World, tag: str = "H") -> str:
    node = w.simple(tag, "pcworker", fx={"gate": f"g{tag}"})
    w.wait_running(node)
    return node


# ----------------------------------------------------------------- NC-R18

def test_nc_r18_a_full_provider_leaves_the_node_open_with_an_admission_reason(pw):
    pw.start_scheduler()
    saturate(pw)
    b = pw.simple("B", "pcworker")
    view = pw.until(lambda: "admission:provider_concurrency" in blocked_codes(pw.get(b))
                    and pw.get(b), what="B blocked by provider concurrency")
    assert view["state"] == "open"
    assert view["eligible"] is False
    assert view["active_run"] in (None, {}, "")
    assert pw.pc.by_tag("B") == []


def test_nc_r18_no_deferred_or_queue_entry_is_written_while_blocked(pw):
    pw.start_scheduler()
    saturate(pw)
    before = pw.deferred()
    for tag in ("B", "C"):
        pw.simple(tag, "pcworker")
    pw.quiet(3)
    assert pw.deferred() == before == []
    listed = call_tool(pw, "list_deferred")
    assert not (listed["deferred"] if isinstance(listed, dict) else listed)


def test_nc_r18_when_the_slot_frees_the_node_launches_exactly_once(pw):
    pw.start_scheduler()
    saturate(pw)
    b = pw.simple("B", "pcworker", fx={"gate": "gB"})
    pw.until(lambda: "admission:provider_concurrency" in blocked_codes(pw.get(b)),
             what="B blocked")
    pw.gate("gH", pw.pc)
    pw.wait_running(b)
    pw.quiet(2)
    assert len(pw.pc.by_tag("B")) == 1
    assert len(pw.get(b)["runs"]) == 1
    assert pw.deferred() == []
    pw.gate("gB", pw.pc)
    assert pw.wait_state(b, "done")["outcome"] == "completed"
    assert len(pw.pc.by_tag("B")) == 1


def test_nc_r18_the_admission_reason_is_derived_not_stored(pw):
    pw.start_scheduler()
    saturate(pw)
    b = pw.simple("B", "pcworker")
    pw.until(lambda: "admission:provider_concurrency" in blocked_codes(pw.get(b)),
             what="B blocked")
    import re
    for path in pw.scheduler_files():
        text = path.read_bytes().decode("utf-8", "replace")
        assert not re.search(r'"(eligible|blocked|ready)"\s*:', text), path


def test_nc_r18_a_blocked_node_is_ready_but_not_eligible(pw):
    pw.start_scheduler()
    saturate(pw)
    b = pw.simple("B", "pcworker")
    view = pw.until(lambda: "admission:provider_concurrency" in blocked_codes(pw.get(b))
                    and pw.get(b), what="B blocked")
    assert view["ready"] is True and view["eligible"] is False
    assert view["ready_since"]


def test_nc_r18_independent_providers_admit_independently(pw):
    pw.start_scheduler()
    saturate(pw)
    free = pw.simple("F", "worker", fx={"gate": "gF"})
    pw.wait_running(free)


# ----------------------------------------------------------------- NC-R25

def test_nc_r25_start_agent_nodes_are_not_urgent_by_default(pw):
    pw.start_scheduler()
    r = call_tool(pw, "start_agent", "worker", task("S", gate="gS"))
    assert not r.get("error"), r
    assert pw.get(r["node_id"]).get("urgent") is False


def test_nc_r25_start_agent_urgent_marks_the_node(pw):
    pw.start_scheduler()
    r = call_tool(pw, "start_agent", "worker", task("S", gate="gS"), urgent=True)
    assert not r.get("error"), r
    assert pw.get(r["node_id"]).get("urgent") is True


def test_nc_r25_r24_an_urgent_start_agent_launches_before_an_earlier_ordinary_one(pw):
    pw.start_scheduler()
    saturate(pw)
    a = call_tool(pw, "start_agent", "pcworker", task("ORD", gate="gO"))
    b = call_tool(pw, "start_agent", "pcworker", task("URG", gate="gU"), urgent=True)
    assert a["node_id"] and b["node_id"] and not a.get("agent_id") and not b.get("agent_id")
    pw.gate("gH", pw.pc)
    first = pw.until(lambda: pw.pc.calls()[1:2], what="the next launch")
    assert first[0]["tag"] == "URG"


# ------------------------------------------------------- NC-R21 as limited by NC-R51

def test_nc_r21_start_agent_returns_the_node_the_run_and_a_status(pw):
    pw.start_scheduler()
    r = call_tool(pw, "start_agent", "worker", task("S", gate="gS"))
    assert not r.get("error"), r
    assert r["node_id"].startswith("nd-")
    assert r.get("agent_id") and r["agent_id"].startswith("ag-")
    assert r.get("status")
    node = pw.get(r["node_id"])
    assert node["kind"] == "simple" and node["agent"] == "worker"
    assert [x["run_id"] for x in node["runs"]] == [r["agent_id"]]
    assert pw.fx.spawns() == 1
    assert pw.deferred() == []


def test_nc_r21_start_agent_under_saturation_returns_the_node_id_and_the_reasons(pw):
    pw.start_scheduler()
    saturate(pw)
    r = call_tool(pw, "start_agent", "pcworker", task("S"))
    assert r["node_id"].startswith("nd-")
    assert not r.get("agent_id")
    assert any("provider_concurrency" in x for x in reasons(r)), r
    node = pw.get(r["node_id"])
    assert node["state"] == "open"
    assert pw.deferred() == []
    assert pw.pc.by_tag("S") == []


def test_nc_r21_a_blocked_start_returns_within_the_admission_timeout(pw):
    import time
    pw.project["scheduler"]["admission_timeout_seconds"] = 2
    pw.start_scheduler()
    saturate(pw)
    t0 = time.monotonic()
    r = call_tool(pw, "start_agent", "pcworker", task("S"))
    assert r["node_id"]
    assert time.monotonic() - t0 < 2 + 4


def test_nc_r21_the_node_carries_the_tool_arguments(pw):
    pw.start_scheduler()
    r = call_tool(pw, "start_agent", "worker", task("S", gate="gS"),
                  budget_tag="tag-xyz", timeout=777)
    assert not r.get("error"), r
    text = json.dumps(pw.get(r["node_id"]))
    assert "tag-xyz" in text and "777" in text


def test_nc_r21_start_agent_with_the_scheduler_stopped_is_unavailable_and_creates_nothing(pw):
    pw.start_scheduler()
    pw.stop_scheduler()
    before = dict(pw.tree_nodes())
    r = call_tool(pw, "start_agent", "worker", task("S"))
    assert r == {"error": "scheduler_unavailable"} or r.get("error") == "scheduler_unavailable"
    assert pw.tree_nodes() == before
    assert pw.fx.spawns() == 0


def test_nc_r21_the_node_view_of_a_started_agent_is_listed_for_the_orchestrator(pw):
    pw.start_scheduler()
    r = call_tool(pw, "start_agent", "worker", task("S", gate="gS"))
    ids = [n["id"] for n in pw.list()]
    assert r["node_id"] in ids


# ----------------------------------------------------------------- NC-R56

def test_nc_r56_start_agent_then_wait_and_release_launch_each_node_once(pw):
    pw.start_scheduler()
    holder = call_tool(pw, "start_agent", "pcworker", task("H", gate="gH"))
    assert holder["agent_id"]
    blocked = [call_tool(pw, "start_agent", "pcworker", task(t)) for t in ("X", "Y")]
    assert all(b["node_id"] and not b.get("agent_id") for b in blocked)
    call_tool(pw, "wait_for_agents", [holder["agent_id"]], timeout=1)
    assert pw.deferred() == []
    pw.gate("gH", pw.pc)
    for b in blocked:
        pw.wait_state(b["node_id"], "done", timeout=60)
    call_tool(pw, "wait_for_agents", [], timeout=1)
    pw.restart_scheduler()
    pw.quiet(2)
    assert [len(pw.pc.by_tag(t)) for t in ("H", "X", "Y")] == [1, 1, 1]
    for b in blocked:
        assert len(pw.get(b["node_id"])["runs"]) == 1
    assert pw.deferred() == []


def test_nc_r56_wait_for_agents_does_not_launch_anything_by_itself(pw):
    pw.start_scheduler()
    saturate(pw)
    b = pw.simple("B", "pcworker")
    pw.until(lambda: "admission:provider_concurrency" in blocked_codes(pw.get(b)), what="B blocked")
    call_tool(pw, "wait_for_agents", [], timeout=1)
    assert pw.pc.by_tag("B") == []
    assert pw.deferred() == []


def test_nc_r56_a_blocked_steer_returns_reasons_and_writes_no_queue_entry(pw):
    pw.start_scheduler()
    first = call_tool(pw, "start_agent", "pcworker", task("S"))
    pw.wait_state(first["node_id"], "done")
    saturate(pw)
    spawns = len(pw.pc.calls())
    r = call_tool(pw, "steer_agent", first["agent_id"], "one more thing")
    assert any("provider_concurrency" in x for x in reasons(r)), r
    assert not r.get("deferred") and not r.get("queued")
    assert pw.deferred() == []
    assert len(pw.pc.calls()) == spawns


def test_nc_r56_steering_a_managed_run_resumes_the_same_run_and_session(pw):
    pw.start_scheduler()
    r = call_tool(pw, "start_agent", "worker", task("S", session="ses_s1", gate="gS"))
    pw.wait_running(r["node_id"])
    out = call_tool(pw, "steer_agent", r["agent_id"], "change course")
    assert not out.get("error"), out
    resumed = pw.until(lambda: [c for c in pw.fx.calls() if c["resume"]],
                       what="the steered resume")
    assert resumed[0]["resume"] == "ses_s1"
    assert "change course" in resumed[0]["prompt"]
    done = pw.wait_state(r["node_id"], "done")
    assert [x["run_id"] for x in done["runs"]] == [r["agent_id"]]
