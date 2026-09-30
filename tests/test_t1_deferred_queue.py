"""Black-box contract for T1, the deferred queue: DQ-R1..DQ-R7.

Contract: `context/specs/t1-deferred-queue.md`. The incident it answers is
ticket 2 in `context/tickets/2026-09-30-unfiled.md`: a deferred entry pinned to
a model that was later removed from the agent's configuration vanished during
a `wait_for_agents` drain, with no event, and the result said only
"no active agents".

Seams: a real Runner over a throwaway project with fake provider CLIs
(`c3_harness`), only the provider quota readings stubbed. The MCP tools are
called as functions of `multiagents.server`, with `server.runner` pointed at
that Runner. Every observation is the tool result, `events.jsonl`, the tree
state file, or the fake CLIs' effects.

Assumptions where the contract is silent (each is deliberately loose):
- `list_deferred` may return a bare list or a dict holding one list; both are
  read as "the entries".
- The new deferred id inside a `re_deferred` event is looked for among the
  event's field values, because the contract does not name the field.
- An entry whose agent left the roster is `refused` (DQ-R3) or `dropped`
  (DQ-R1, and today's behaviour); either is accepted, but the queue, the event
  and the result must agree with each other.
- `stopped_on`/exception handling for transient errors is not tested through
  monkeypatched internals; a transient condition is a provider that has no
  headroom again, which is the real path.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import c3_harness as h  # noqa: E402
from multiagents import budget as budget_mod  # noqa: E402
from multiagents import server  # noqa: E402
from multiagents.config import AgentSpec  # noqa: E402
from multiagents.tree import Node  # noqa: E402

TEXT = [{"type": "text", "text": "done"}]


class Proj:
    """agents: `worker` (acme, m1, fallback zeta:z1) and `slow` (a long run)."""

    def __init__(self, tmp_path, monkeypatch):
        self.monkeypatch = monkeypatch
        self.headroom = {"acme": 1.0, "zeta": 1.0, "slowp": 1.0}
        monkeypatch.setattr(budget_mod, "read_all", lambda *a, **kw: {
            n: budget_mod.Budget(n, known=True, headroom=v)
            for n, v in self.headroom.items()})
        providers = {
            "acme": h.fake_cli(tmp_path, "acme", events=TEXT),
            "zeta": h.fake_cli(tmp_path, "zeta", events=TEXT),
            "slowp": h.fake_cli(tmp_path, "slowp", events=TEXT, delay=6),
        }
        self.worker = {"provider": "acme", "model": "m1", "models": {"zeta": "z1"}}
        agents = {"worker": AgentSpec.from_dict("worker", self.worker),
                  "plain": AgentSpec.from_dict("plain", {"provider": "acme", "model": "m1"}),
                  "slow": AgentSpec.from_dict("slow", {"provider": "slowp", "model": "s1"})}
        self.r = h.make_runner(tmp_path / "project", monkeypatch, agents=agents,
                               providers=providers,
                               project={"budget": {"blind_cooldown_seconds": 1}})
        self.r.config.models = {"acme": [{"id": "m1"}], "zeta": [{"id": "z1"}]}
        monkeypatch.setattr(server, "runner", lambda: self.r)
        self.tree = self.r.tree

    # -- setup ----------------------------------------------------------
    def remove_fallback(self):
        """The operator edits the roster: `worker` loses its zeta fallback."""
        plain = {"provider": "acme", "model": "m1"}
        self.r.config.agents["worker"] = AgentSpec.from_dict("worker", plain)

    def queue(self, task="work", model=None, agent="worker", ago=1.0):
        spec = {"agent": agent, "task": task, "timeout": None,
                "model": model, "workdir": None}
        return self.tree.defer(spec, time.time() - ago, "quota")["id"]

    def defer_for_real(self, agent="worker", task="real work"):
        """Ask the server to start while acme has no headroom: a genuine deferral."""
        self.headroom["acme"] = 0.0
        result = self.call(server.start_agent, agent, task)
        self.headroom["acme"] = 1.0
        return result

    def let_due(self):
        time.sleep(1.3)         # blind_cooldown_seconds is 1

    # -- act ------------------------------------------------------------
    def call(self, fn, *args, **kwargs):
        async def go():
            value = fn(*args, **kwargs)
            return await value if asyncio.iscoroutine(value) else value
        return asyncio.run(go())

    def wait(self, timeout=10):
        return self.call(server.wait_for_agents, timeout=timeout)

    def wait_beside_a_long_run(self, timeout=1):
        """One loop, so the long run is still alive when the wait times out."""
        async def go():
            started = await server.start_agent("slow", "long job")
            assert started.get("agent_id"), started
            return await server.wait_for_agents(timeout=timeout)
        return asyncio.run(go())

    def listed(self):
        result = self.call(server.list_deferred)
        return entries_of(result)

    # -- observe --------------------------------------------------------
    def events(self, kind=None):
        path = self.r.paths.events_file
        out = []
        if path.exists():
            for line in path.read_text().splitlines():
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if kind is None or e.get("kind") == kind:
                    out.append(e)
        return out

    def exits(self):
        return self.events("deferred_exit")

    def queue_ids(self):
        return [d["id"] for d in self.tree.read()["deferred"]]

    def as_child(self, agent_id="ag-child", parent=None, depth=1):
        h.as_subagent(self.monkeypatch, agent_id=agent_id, parent=parent or "",
                      depth=depth, can_spawn=True)


def entries_of(result):
    if isinstance(result, list):
        return result
    assert isinstance(result, dict), result
    lists = [v for v in result.values() if isinstance(v, list)]
    assert len(lists) == 1, f"expected one list of entries in {result}"
    return lists[0]


@pytest.fixture
def p(tmp_path, monkeypatch):
    return Proj(tmp_path, monkeypatch)


def blob(x) -> str:
    return json.dumps(x, default=str)


# ---------------------------------------------------------------------------
# DQ-R1 — every exit from the queue is an event
# ---------------------------------------------------------------------------

def test_dq_r1_a_restart_writes_one_restarted_event_with_the_new_agent_id(p):
    df = p.queue()
    result = p.wait()
    assert p.queue_ids() == []
    exits = p.exits()
    assert len(exits) == 1, exits
    e = exits[0]
    assert (e["deferred_id"], e["agent"], e["outcome"]) == (df, "worker", "restarted")
    assert e.get("agent_id"), e
    assert p.tree.get(e["agent_id"]) is not None, "the id names a real node"
    assert e["agent_id"] in blob(result), "and the caller was told the same id"


def test_dq_r1_a_re_deferral_writes_one_re_deferred_event_naming_the_new_entry(p):
    df = p.queue()
    p.headroom["acme"] = 0.0                    # the window closed again
    p.wait(timeout=1)
    exits = p.exits()
    assert len(exits) == 1, exits
    e = exits[0]
    assert (e["deferred_id"], e["agent"], e["outcome"]) == (df, "worker", "re_deferred")
    remaining = p.queue_ids()
    assert len(remaining) == 1 and remaining[0] != df, "the old entry is replaced, not kept"
    assert remaining[0] in [str(v) for v in e.values()], "the event names the new id"


def test_dq_r1_a_refusal_writes_one_refused_event_with_a_reason(p):
    df = p.queue(model="z1")
    p.remove_fallback()
    p.wait(timeout=1)
    exits = p.exits()
    assert len(exits) == 1, exits
    e = exits[0]
    assert (e["deferred_id"], e["agent"], e["outcome"]) == (df, "worker", "refused")
    assert e.get("reason"), e


def test_dq_r1_an_agent_removed_from_the_roster_exits_with_an_event_not_silently(p):
    df = p.queue(agent="deleted-agent")
    result = p.wait(timeout=1)
    exits = p.exits()
    assert len(exits) == 1, exits
    e = exits[0]
    assert (e["deferred_id"], e["agent"]) == (df, "deleted-agent")
    assert e["outcome"] in ("refused", "dropped"), e
    assert e.get("reason"), e
    # The queue, the event and the result agree about which it was.
    if e["outcome"] == "dropped":
        assert df not in p.queue_ids()
        assert "deleted-agent" in blob(result["deferred"]["dropped"])
    else:
        assert df in p.queue_ids()
        assert "deleted-agent" in blob(result["deferred"]["refused"])


def test_dq_r1_cancelling_writes_one_cancelled_event_and_removes_the_entry(p):
    df = p.queue(ago=-3600)                     # not due for an hour
    p.call(server.cancel_deferred, df)
    assert p.queue_ids() == []
    exits = p.exits()
    assert len(exits) == 1, exits
    assert (exits[0]["deferred_id"], exits[0]["agent"], exits[0]["outcome"]) == \
        (df, "worker", "cancelled")


def test_dq_r1_an_entry_that_stays_in_the_queue_writes_no_exit_event(p):
    p.queue(ago=-3600)                          # not due
    p.wait(timeout=1)
    assert p.exits() == []
    assert len(p.queue_ids()) == 1


def test_dq_r1_each_entry_in_a_batch_gets_its_own_event(p):
    ids = [p.queue(task=f"t{n}") for n in range(3)]
    p.wait(timeout=1)
    exits = p.exits()
    assert sorted(e["deferred_id"] for e in exits) == sorted(ids)
    assert {e["outcome"] for e in exits} == {"restarted"}


def test_dq_r1_a_second_drain_does_not_repeat_events_for_the_first(p):
    p.queue()
    p.wait(timeout=1)
    p.wait(timeout=1)
    assert len(p.exits()) == 1


# ---------------------------------------------------------------------------
# DQ-R2 — the wait_for_agents result reports the drain
# ---------------------------------------------------------------------------

def test_dq_r2_a_drain_with_no_agent_running_lists_the_restart_and_its_agent_id(p):
    p.queue()
    result = p.wait()
    d = result["deferred"]
    assert len(d["restarted"]) == 1, d
    new_id = p.exits()[0]["agent_id"]
    assert new_id in blob(d["restarted"])
    assert d["refused"] == [] and d["dropped"] == []
    assert d["still_deferred"] == 0


def test_dq_r2_a_restarted_run_is_an_active_agent_in_the_same_result(p):
    p.queue()
    result = p.wait()
    new_id = p.exits()[0]["agent_id"]
    assert result.get("reason") != "no active agents", result
    assert new_id in blob(result.get("still_running", [])) + blob(result.get("changed", [])), result


def test_dq_r2_a_refused_start_is_listed_with_the_reason(p):
    p.queue(model="z1")
    p.remove_fallback()
    result = p.wait(timeout=1)
    d = result["deferred"]
    assert d["restarted"] == [] and d["dropped"] == []
    assert len(d["refused"]) == 1, d
    text = blob(d["refused"])
    assert "worker" in text and "z1" in text, "the reason names the agent and the model"


def test_dq_r2_the_field_is_present_on_the_no_active_agents_return(p):
    """The incident: the result said only 'no active agents'."""
    p.queue(model="z1")
    p.remove_fallback()
    result = p.wait(timeout=1)
    assert "deferred" in result, result
    assert result["deferred"]["refused"], result


def test_dq_r2_the_field_is_present_on_a_timeout(p):
    p.queue(model="z1")
    p.remove_fallback()
    result = p.wait_beside_a_long_run()
    assert result.get("timed_out") is True, result
    assert len(result["deferred"]["refused"]) == 1, result


def test_dq_r2_still_deferred_counts_what_is_left_waiting(p):
    p.queue(task="a")
    p.queue(task="b")
    p.headroom["acme"] = 0.0
    result = p.wait(timeout=1)
    d = result["deferred"]
    assert d["restarted"] == []
    assert d["still_deferred"] == 2, d


# ---------------------------------------------------------------------------
# DQ-R3 — a refused restart stays visible
# ---------------------------------------------------------------------------

def test_dq_r3_the_incident_a_pin_to_a_removed_model_is_refused_not_lost(p):
    """Control first: with the fallback still configured the same entry runs."""
    df = p.queue(model="z1")
    p.remove_fallback()
    p.wait(timeout=1)

    assert df in p.queue_ids(), "the entry did not vanish"
    listed = {e["id"]: e for e in p.listed()}
    assert listed[df]["status"] == "refused", listed
    assert "worker" in listed[df]["reason"] and "z1" in listed[df]["reason"]
    assert [e["outcome"] for e in p.exits()] == ["refused"]


def test_dq_r3_control_the_same_pinned_entry_runs_while_the_model_is_configured(p):
    p.queue(model="z1")
    result = p.wait()
    assert len(result["deferred"]["restarted"]) == 1, result
    assert result["deferred"]["refused"] == []


def test_dq_r3_a_refused_entry_is_never_retried_automatically(p):
    df = p.queue(model="z1")
    p.remove_fallback()
    p.wait(timeout=1)
    # Even after the configuration is repaired, nobody asked for a retry.
    p.r.config.agents["worker"] = AgentSpec.from_dict("worker", p.worker)
    p.wait(timeout=1)
    p.wait(timeout=1)
    assert df in p.queue_ids()
    assert [e["outcome"] for e in p.exits()] == ["refused"], "one exit, not one per drain"
    assert not [n for n in p.tree.read()["nodes"].values() if n.get("agent") == "worker"]


def test_dq_r3_a_refused_entry_leaves_only_through_cancel_deferred(p):
    df = p.queue(model="z1")
    p.remove_fallback()
    p.wait(timeout=1)
    p.wait(timeout=1)
    assert df in p.queue_ids()
    p.call(server.cancel_deferred, df)
    assert df not in p.queue_ids()
    assert [e["outcome"] for e in p.exits()] == ["refused", "cancelled"]


def test_dq_r3_a_refused_entry_does_not_hold_the_pause(p):
    df = p.queue(model="z1")
    p.remove_fallback()
    p.wait(timeout=1)
    assert df in p.queue_ids(), "it is still queued (refused) and yet..."
    assert not p.tree.read().get("pause")
    result = p.call(server.start_agent, "worker", "still works")
    assert result.get("agent_id"), result


def test_dq_r3_a_transient_shortage_keeps_the_entry_waiting_and_stops_the_drain(p):
    first = p.queue(task="first")
    second = p.queue(task="second")
    p.headroom["acme"] = 0.0
    p.wait(timeout=1)
    listed = p.listed()
    assert len(listed) == 2 and {e["status"] for e in listed} == {"waiting"}, listed
    assert second in [e["id"] for e in listed], "the rest of the batch was left alone"
    assert first not in [e["id"] for e in listed], "the first was re-queued as a new entry"
    assert [e["outcome"] for e in p.exits()] == ["re_deferred"]


# ---------------------------------------------------------------------------
# DQ-R4 — list_deferred
# ---------------------------------------------------------------------------

def test_dq_r4_lists_two_waiting_and_one_refused_with_all_fields(p):
    # One refusal first (drained while nothing is paused) ...
    refused = p.queue(model="z1", task="pinned")
    p.remove_fallback()
    p.wait(timeout=1)
    # ... then two real deferrals, the second by a subagent.
    p.r.config.agents["worker"] = AgentSpec.from_dict("worker", p.worker)
    assert p.defer_for_real(task="first real").get("deferred") is True
    tree_nodes = p.tree
    tree_nodes.add(Node(id="ag-child", agent="worker", provider="acme", model="m1",
                        parent=None, depth=1, status="running"))
    p.as_child("ag-child", depth=1)
    p.headroom["acme"] = 0.0
    p.tree.defer({"agent": "worker", "task": "x" * 500, "timeout": None,
                  "model": None, "workdir": None}, time.time() + 600, "quota")

    listed = p.listed()
    by_status = sorted(e["status"] for e in listed)
    assert by_status == ["refused", "waiting", "waiting"], listed
    for e in listed:
        for key in ("id", "agent", "task", "model", "retry_after", "status",
                    "reason", "deferred_by"):
            assert key in e, (key, e)
    r = next(e for e in listed if e["id"] == refused)
    assert r["status"] == "refused" and r["model"] == "z1" and r["task"] == "pinned"
    assert "z1" in r["reason"]


def test_dq_r4_task_is_cut_to_200_characters(p):
    p.queue(task="y" * 500, ago=-3600)
    (e,) = p.listed()
    assert e["task"] == "y" * 200


def test_dq_r4_model_is_empty_when_the_entry_has_none(p):
    p.queue(model=None, ago=-3600)
    (e,) = p.listed()
    assert e["model"] in ("", None)
    assert e["status"] == "waiting"


def test_dq_r4_retry_after_is_iso_8601_utc(p):
    when = time.time() + 3600
    p.tree.defer({"agent": "worker", "task": "t"}, when, "quota")
    (e,) = p.listed()
    parsed = datetime.fromisoformat(str(e["retry_after"]).replace("Z", "+00:00"))
    assert parsed.utcoffset() == timedelta(0), e["retry_after"]
    assert abs(parsed.timestamp() - when) < 2


def test_dq_r4_deferred_by_is_orchestrator_for_the_root_and_the_caller_for_a_subagent(p):
    assert p.defer_for_real(task="from root").get("deferred") is True
    p.tree.add(Node(id="ag-child", agent="worker", provider="acme", model="m1",
                    parent=None, depth=1, status="running"))
    p.as_child("ag-child")
    p.headroom["acme"] = 0.0
    result = p.call(server.start_agent, "worker", "from child")
    assert result.get("deferred") is True, result
    by_task = {e["task"]: e for e in p.listed()}
    assert by_task["from root"]["deferred_by"] == "orchestrator"
    assert by_task["from child"]["deferred_by"] == "ag-child"


def test_dq_r4_an_empty_queue_lists_nothing(p):
    assert p.listed() == []


def test_dq_r4_is_read_only(p):
    df = p.queue(ago=-3600)
    before = (p.queue_ids(), p.tree.read().get("pause"), len(p.events()))
    p.listed()
    p.listed()
    assert (p.queue_ids(), p.tree.read().get("pause"), len(p.events())) == before
    assert p.queue_ids() == [df]


# ---------------------------------------------------------------------------
# DQ-R5 — cancel_deferred
# ---------------------------------------------------------------------------

def test_dq_r5_cancelling_removes_the_entry_and_returns_no_error(p):
    df = p.queue(ago=-3600)
    other = p.queue(task="keep me", ago=-3600)
    result = p.call(server.cancel_deferred, df)
    assert "error" not in result, result
    assert p.queue_ids() == [other]


def test_dq_r5_an_unknown_id_errors_and_changes_nothing(p):
    df = p.queue(ago=-3600)
    events = len(p.events())
    result = p.call(server.cancel_deferred, "df-nosuch")
    assert result.get("error"), result
    assert p.queue_ids() == [df]
    assert len(p.events()) == events


def test_dq_r5_cancelling_twice_errors_the_second_time_and_writes_one_event(p):
    df = p.queue(ago=-3600)
    p.call(server.cancel_deferred, df)
    second = p.call(server.cancel_deferred, df)
    assert second.get("error"), second
    assert len(p.exits()) == 1


def test_dq_r5_an_empty_id_errors(p):
    p.queue(ago=-3600)
    assert p.call(server.cancel_deferred, "").get("error")
    assert len(p.queue_ids()) == 1


def _child_defers(p, agent_id, parent=None, depth=1):
    p.tree.add(Node(id=agent_id, agent="worker", provider="acme", model="m1",
                    parent=parent, depth=depth, status="running"))
    p.as_child(agent_id, parent=parent, depth=depth)
    p.headroom["acme"] = 0.0
    known = set(p.queue_ids())
    result = p.call(server.start_agent, "worker", f"from {agent_id}")
    assert result.get("deferred") is True, result
    (new,) = set(p.queue_ids()) - known
    return new


def test_dq_r5_a_sibling_cannot_cancel_another_agents_entry(p):
    df = _child_defers(p, "ag-a")
    p.tree.add(Node(id="ag-b", agent="worker", provider="acme", model="m1",
                    parent=None, depth=1, status="running"))
    p.as_child("ag-b")
    result = p.call(server.cancel_deferred, df)
    assert result.get("error"), result
    assert p.queue_ids() == [df]
    assert p.exits() == []


def test_dq_r5_the_agent_that_deferred_it_may_cancel_it(p):
    df = _child_defers(p, "ag-a")
    result = p.call(server.cancel_deferred, df)
    assert "error" not in result, result
    assert p.queue_ids() == []


def test_dq_r5_an_ancestor_of_the_deferring_agent_may_cancel_it(p):
    p.tree.add(Node(id="ag-top", agent="worker", provider="acme", model="m1",
                    parent=None, depth=1, status="running"))
    df = _child_defers(p, "ag-mid", parent="ag-top", depth=2)
    p.as_child("ag-top", depth=1)
    result = p.call(server.cancel_deferred, df)
    assert "error" not in result, result
    assert p.queue_ids() == []
    assert p.exits()[-1]["outcome"] == "cancelled"


def test_dq_r5_a_descendant_may_not_cancel_its_ancestors_entry(p):
    p.tree.add(Node(id="ag-top", agent="worker", provider="acme", model="m1",
                    parent=None, depth=1, status="running"))
    df = _child_defers(p, "ag-top-def", parent=None, depth=1)
    p.tree.add(Node(id="ag-under", agent="worker", provider="acme", model="m1",
                    parent="ag-top-def", depth=2, status="running"))
    p.as_child("ag-under", parent="ag-top-def", depth=2)
    result = p.call(server.cancel_deferred, df)
    assert result.get("error"), result
    assert p.queue_ids() == [df]


def test_dq_r5_the_orchestrator_may_cancel_any_entry(p):
    df = _child_defers(p, "ag-a")
    h.as_root(p.monkeypatch)
    result = p.call(server.cancel_deferred, df)
    assert "error" not in result, result
    assert p.queue_ids() == []


# ---------------------------------------------------------------------------
# DQ-R6 — the pause follows the waiting entries
# ---------------------------------------------------------------------------

def test_dq_r6_a_deferral_then_a_cancel_leaves_no_pause_and_start_works_at_once(p):
    result = p.defer_for_real()
    assert result.get("deferred") is True
    assert p.tree.read().get("pause"), "a deferral pauses the tree"
    (df,) = p.queue_ids()

    p.call(server.cancel_deferred, df)

    assert not p.tree.read().get("pause")
    started = p.call(server.start_agent, "worker", "right now")
    assert started.get("agent_id") and not started.get("deferred"), started


def test_dq_r6_the_pause_holds_while_any_waiting_entry_remains(p):
    p.defer_for_real(task="one")
    p.headroom["acme"] = 0.0
    p.tree.defer({"agent": "worker", "task": "two"}, time.time() + 600, "quota")
    ids = p.queue_ids()
    assert len(ids) == 2
    p.call(server.cancel_deferred, ids[0])
    assert p.tree.read().get("pause"), "one waiting entry is left"
    p.call(server.cancel_deferred, ids[1])
    assert not p.tree.read().get("pause")


def test_dq_r6_cancelling_a_refused_entry_leaves_the_pause_of_a_waiting_one(p):
    refused = p.queue(model="z1")
    p.remove_fallback()
    p.wait(timeout=1)
    p.r.config.agents["worker"] = AgentSpec.from_dict("worker", p.worker)
    p.defer_for_real()
    assert p.tree.read().get("pause")
    p.call(server.cancel_deferred, refused)
    assert p.tree.read().get("pause"), "the waiting entry still holds it"


def test_dq_r6_cancelling_the_last_waiting_entry_lifts_the_pause_though_a_refusal_remains(p):
    refused = p.queue(model="z1")
    p.remove_fallback()
    p.wait(timeout=1)
    p.r.config.agents["worker"] = AgentSpec.from_dict("worker", p.worker)
    p.defer_for_real()
    (waiting,) = [e["id"] for e in p.listed() if e["status"] == "waiting"]
    p.call(server.cancel_deferred, waiting)
    assert p.queue_ids() == [refused]
    assert not p.tree.read().get("pause")


def test_dq_r6_lifting_the_pause_is_recorded_as_resumed(p):
    p.defer_for_real()
    (df,) = p.queue_ids()
    before = len(p.events("resumed"))
    p.call(server.cancel_deferred, df)
    assert len(p.events("resumed")) == before + 1


def test_dq_r6_a_drain_that_restarts_the_last_waiting_entry_leaves_no_pause(p):
    p.defer_for_real()
    p.let_due()
    result = p.wait()
    assert len(result["deferred"]["restarted"]) == 1, result
    assert not p.tree.read().get("pause")


# ---------------------------------------------------------------------------
# DQ-R7 — nothing else changes (regression guards)
# ---------------------------------------------------------------------------

def test_dq_r7_a_deferral_still_returns_deferred_true_with_retry_after(p):
    before = time.time()
    result = p.defer_for_real()
    assert result["deferred"] is True
    assert result["retry_after"] > before
    assert result.get("paused") is True
    assert len(p.queue_ids()) == 1
    assert [e["kind"] for e in p.events() if e["kind"] == "deferred"] == ["deferred"]


def test_dq_r7_a_due_entry_still_restarts_at_the_next_wait_for_agents(p):
    p.defer_for_real(task="later")
    assert p.tree.read()["nodes"] == {}, "nothing ran while deferred"
    p.let_due()
    result = p.wait()
    nodes = [n for n in p.tree.read()["nodes"].values() if n.get("agent") == "worker"]
    assert len(nodes) == 1, nodes
    assert p.queue_ids() == []
    assert result.get("changed") or result.get("still_running"), result


def test_dq_r7_start_is_still_refused_while_the_pause_holds(p):
    """`plain` has no provider but the exhausted one, so the pause covers it."""
    p.defer_for_real(agent="plain")
    try:
        result = p.call(server.start_agent, "plain", "another")
    except RuntimeError as exc:
        result = {"error": str(exc)}
    assert "paused" in str(result.get("error", "")).lower(), result
    assert not result.get("agent_id")


def test_dq_r7_a_not_yet_due_entry_is_left_alone(p):
    df = p.queue(ago=-3600)
    p.wait(timeout=1)
    assert p.queue_ids() == [df]
    assert p.tree.read()["nodes"] == {}
