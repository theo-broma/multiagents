"""SC-R2a / SC-R3b / SC-R4a — behaviour that needs real server processes.

Contract: `context/specs/spend-caps.md` (amendments included). These are the
cross-process promises: a crossing seen by one process stops runs owned by
another within 15 s; a replayed (adopted) stream is not charged twice; a
charge is committed before the replay checkpoint passes it; concurrent
writers in separate processes lose nothing; a retry in a process that has not
yet noticed a crossing is still refused at spawn.

Real processes throughout, through `sv_harness`: `python -m multiagents.server`
over MCP stdio (a root server and a nested one, whose runs the root does not
own), a stub agent that prints opencode-shaped NDJSON plans. The stub provider
here carries the *shipped* opencode stream rules (whatever the implementation
adds there to capture step ids is inherited) and a `spend_cap`.

What is deliberately not asserted: how a runner acknowledges a stop request
"durably" (the contract fixes no place or shape), and any figure on
`budget_status` other than the period spend, found as in
`test_sc_visibility.py`.
"""
from __future__ import annotations

import json
import signal
import sys
import time
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import sv_harness as h  # noqa: E402
import sc_harness as sc  # noqa: E402

SID = "sc-session-{}"
TERMINAL = {"done", "failed", "cancelled", "limited", "truncated", "refused", "orphaned"}
STOP_WITHIN = 15.0


@pytest.fixture
def project(tmp_path):
    made: list[h.Project] = []

    def build(cap=None) -> h.Project:
        base = tmp_path / f"p{len(made)}"
        base.mkdir()
        p = h.Project(base)
        made.append(p)
        cfg = p.root / ".multiagents" / "config" / "providers.yaml"
        data = yaml.safe_load(cfg.read_text())
        entry = data["providers"]["svstub"]
        entry["stream"] = sc.shipped_opencode()["stream"]
        if cap is not None:
            entry["spend_cap"] = cap
        cfg.write_text(yaml.safe_dump(data))
        return p

    yield build
    for p in made:
        p.cleanup()


def step(sid: str, step_id: str | None, cost: float) -> list:
    part = {"reason": "tool-calls", "cost": cost,
            "tokens": {"input": 100, "output": 10, "reasoning": 0, "cache": {"read": 0, "write": 0}}}
    if step_id:
        part["id"] = step_id
    return ["emit", {"type": "step_finish", "sessionID": sid, "part": part}]


def said(sid: str, words: str = "done") -> list:
    return ["emit", {"type": "text", "sessionID": sid, "part": {"text": words}}]


def nested_server(p: h.Project) -> h.Server:
    parent = "ag-par001"
    p.tree.add(h.Node(id=parent, agent="spawner", provider="svstub", model="m", parent=None,
                      depth=1, status="running", task="parent", session=h.ORCH_SESSION,
                      started_at=h.tree_now()))
    return p.server(agent_id=parent, depth=1)


def day_spend(server: h.Server, values_of=("svstub",)) -> list[float]:
    status = server.call("budget_status")
    return [v for v in sc.period_numbers(status, "svstub", "day") if abs(v) < 1e6]


def spend_events(p: h.Project) -> list[dict]:
    if not p.paths.events_file.is_file():
        return []
    out = []
    for line in p.paths.events_file.read_text().splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("kind") == "spend_cap":
            out.append(e)
    return out


def settled(p: h.Project, agent_id: str, timeout: float) -> str:
    h.wait_until(lambda: p.status(agent_id) in TERMINAL, timeout)
    return p.status(agent_id)


# ------------------------------------------------------- SC-R4a: across processes --

@pytest.mark.parametrize("crosser_is_root", [True, False], ids=["root-crosses", "nested-crosses"])
def test_r4a_a_crossing_in_one_process_stops_a_run_owned_by_the_other_within_15s(
        project, crosser_is_root):
    p = project(cap={"usd": 1.0})
    root = p.server()
    nested = nested_server(p)
    crosser_srv, victim_srv = (root, nested) if crosser_is_root else (nested, root)

    victim_steps = [said(SID.format("v")), ["pidfile", str(p.marker("victim-pid"))],
                    ["heartbeat", str(p.marker("victim-beat"))]]
    victim = victim_srv.start("victim " + h.plan_token(victim_steps))
    victim_pid = h.read_pid(p.marker("victim-pid"))

    cross_steps = [step(SID.format("x"), "prt_cross", 1.5), said(SID.format("x")), ["heartbeat", str(p.marker("cross-beat"))]]
    crosser = crosser_srv.start("crosser " + h.plan_token(cross_steps))

    assert settled(p, crosser, 20) == "limited", p.describe(crosser)
    crossed_at = time.monotonic()
    assert h.wait_until(lambda: not h.alive(victim_pid), STOP_WITHIN), (
        f"the other process's run was still alive {STOP_WITHIN}s after the crossing")
    assert time.monotonic() - crossed_at <= STOP_WITHIN + 0.5
    assert settled(p, victim, 5) == "limited", p.describe(victim)
    assert "spend_cap" in p.describe(victim) or any(
        "spend_cap" in json.dumps(e) for e in p.events(victim)), p.describe(victim)
    assert len(spend_events(p)) == 1, spend_events(p)
    assert root.proc.poll() is None and nested.proc.poll() is None, "a server died of the stop"


def test_r4a_a_run_started_in_any_process_is_refused_once_the_cap_is_spent(project):
    p = project(cap={"usd": 1.0})
    root = p.server()
    nested = nested_server(p)
    crosser = root.start("crosser " + h.plan_token([step(SID.format("x"), "prt_c", 1.5), said(SID.format("x")), ["heartbeat", str(p.marker("hb"))]]))
    assert settled(p, crosser, 20) == "limited", p.describe(crosser)
    before = len(p.invocations())
    refused = nested.call("start_agent", 60, args={"agent": "worker", "task": "later"})
    assert not refused.get("agent_id") and "spend_cap" in json.dumps(refused), refused
    assert len(p.invocations()) == before


# ------------------------------------------------------------ SC-R3b: retry guard --

def test_r3b_a_retry_in_a_process_that_has_not_noticed_the_crossing_is_refused_at_spawn(project):
    p = project(cap={"usd": 1.0})
    root = p.server()
    nested = nested_server(p)
    go = p.marker("go")
    # exits 1 with nothing to say once released: the shape of the runner's free retry
    dies = nested.start("BEEB " + h.plan_token(
        [["touch", str(p.marker("dies-started"))], ["wait_for", str(go), 60], ["exit", 1]]))
    assert h.wait_until(p.marker("dies-started").is_file, 15)
    crosser = root.start("crosser " + h.plan_token(
        [step(SID.format("x"), "prt_r", 1.5), ["heartbeat", str(p.marker("cross-beat"))]]))
    assert settled(p, crosser, 20) == "limited", p.describe(crosser)
    go.write_text("go")                                       # the victim now dies by itself
    assert settled(p, dies, 30) in TERMINAL, p.describe(dies)
    time.sleep(2.0)                                           # room for a retry, were one coming
    launches = [i for i in p.invocations() if "BEEB" in " ".join(i["argv"])]
    assert len(launches) == 1, f"the victim was relaunched {len(launches) - 1} time(s) under a spent cap"
    assert p.status(dies) == "limited", p.describe(dies)


# --------------------------------------------------------- SC-R2a: replay & order --

def plan_with_gap(sid: str, p: h.Project, with_ids: bool) -> list[list]:
    ids = ("prt_a", "prt_b", "prt_c") if with_ids else (None, None, None)
    return [step(sid, ids[0], 0.25), step(sid, ids[1], 0.25),
            ["touch", str(p.marker("started"))],
            ["wait_for", str(p.marker("go")), 60],
            step(sid, ids[2], 0.25), said(sid), ["exit", 0]]


@pytest.mark.parametrize("with_ids", [True, False], ids=["step-ids", "no-step-ids"])
def test_r2_an_adopted_run_does_not_charge_its_replayed_stream_twice(project, with_ids):
    p = project()
    s1 = p.server()
    agent_id = s1.start("replay " + h.plan_token(plan_with_gap(SID.format("r"), p, with_ids)))
    assert h.wait_until(p.marker("started").is_file, 15)
    assert h.wait_until(lambda: 0.5 in day_spend(s1), 15), day_spend(s1)
    s1.kill()                                                 # the agent survives, detached
    s2 = p.server()
    assert h.wait_until(lambda: p.status(agent_id) in ("running", "detached"), 20)
    assert h.wait_until(lambda: 0.5 in day_spend(s2), 20), \
        f"the new server does not show the spend: {day_spend(s2)}"
    p.marker("go").write_text("go")
    assert settled(p, agent_id, 40) == "done", p.describe(agent_id)
    assert h.wait_until(lambda: 0.75 in day_spend(s2), 10), (
        f"the replayed steps were charged again or lost: {day_spend(s2)}")
    time.sleep(1.0)
    assert 0.75 in day_spend(s2) and not any(v > 0.75 + 1e-9 for v in day_spend(s2))


def test_r2a_a_charge_is_in_the_ledger_before_the_replay_checkpoint_passes_it(project):
    """Once the tree reflects a step's cost (the checkpoint a replay resumes
    from), killing the server at that instant must not lose the charge."""
    p = project()
    s1 = p.server()
    agent_id = s1.start("order " + h.plan_token(
        [step(SID.format("o"), "prt_o", 0.3), ["heartbeat", str(p.marker("beat"))]]))
    assert h.wait_until(lambda: ((p.node(agent_id) and p.node(agent_id).usage) or {})
                        .get("cost_usd", 0) >= 0.3, 20), "the tree never reflected the cost"
    s1.kill()
    s2 = p.server()
    assert h.wait_until(lambda: 0.3 in day_spend(s2), 20), (
        f"a charge the tree had already moved past is not in the ledger: {day_spend(s2)}")


# ------------------------------------------------------------ SC-R2: many writers --

def test_r2_concurrent_writers_in_separate_processes_lose_and_corrupt_nothing(project):
    p = project()
    root = p.server()
    nested = nested_server(p)
    ids = []
    for i, srv in enumerate([root] * 2 + [nested] * 2):
        sid = SID.format(f"w{i}")
        steps = []
        for k in range(8):
            steps += [step(sid, f"prt_w{i}_{k}", 0.03125), ["sleep", 0.05]]
        ids.append(srv.start(f"writer{i} " + h.plan_token(steps + [said(sid), ["exit", 0]])))
    for agent_id in ids:
        assert settled(p, agent_id, 60) == "done", p.describe(agent_id)
    expected = 4 * 8 * 0.03125
    for srv in (root, nested):
        assert h.wait_until(lambda: any(abs(v - expected) < 1e-9 for v in day_spend(srv)), 10), (
            f"expected {expected}, saw {day_spend(srv)}")
