"""CW-R2 (the safe stop) and CW-R5 (agents preserved through the stop), end to
end: the real driver (`driver._run_supervised`, the R8f harness), a fake CLI
that owns a REAL `python -m multiagents.server` as its child (as the
orchestrator's CLI owns its MCP server), and real stub agents
(`tests/support/sv_harness.py`).

The fake CLI starts the server, drives it over MCP, and on SIGTERM dies as a
CLI does: its end of the server's stdin closes. The test observes only
outcomes: when the CLI was terminated relative to when a launch settled, which
nodes got a pid, which were adopted by the next server, what the driver
emitted. The handshake between driver and server is the developer's; nothing
here names it. The harness assumes only that the driver can find the
orchestrator's server the way it would in life: the server is a child of the
CLI the driver holds, with the project and session in its environment.

SEAMS ASSUMED (flagged in the Result):

1. A launch held "between claim and pid" is made by `tests/support/cw_gate/
   sitecustomize.py`, loaded in the server subprocess: while a gate file
   exists, starting an agent's launch wrapper (`subprocess.Popen` of
   `agentwrap`) blocks. `Runner._launch` has by then claimed the node and
   written its reservation, and has not recorded a pid. If the launch path
   stops using a Popen of the wrapper the gate never engages and the tests
   fail at "no launch reached the hold" — loudly, not vacuously. The server's
   event loop is blocked for the duration, as a slow Popen would block it.
2. The bound on waiting for the safe point is a limit named
   `compact_safe_point_seconds` (the contract says only "a config value
   following R8f.9"; NEED_INFO filed in the Result).
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import p0_context_harness as ch  # noqa: E402
import sv_harness as h  # noqa: E402

import test_phase0_interactive_compact as ic  # noqa: E402
from test_cw_compact_while_waiting import CwSession, SRC  # noqa: E402
from test_phase0_interactive_compact import KEPT, OVER, SID, stopped_then  # noqa: E402

SAFE_POINT_KEY = "compact_safe_point_seconds"      # ASSUMED, see the docstring
SAFE_POINT_SECONDS = 2
SUPPORT = str(Path(__file__).resolve().parent / "support")
GATE_DIR = str(Path(SUPPORT) / "cw_gate")

BOOT = '''    srv = None
    if me.get("server"):
        import threading
        sys.path[:0] = [ctl["src"], ctl["support"]]
        import sv_harness as h
        class P:
            base = pathlib.Path(ctl["base"]); root = pathlib.Path(ctl["root"]); servers = []
            def env(self, session=""):
                e = dict(os.environ); e.update(ctl["env"])
                e["PYTHONPATH"] = ctl["gate_dir"] + os.pathsep + ctl["src"]; e["MULTIAGENTS_SESSION_ID"] = session
                return e
        srv = h.Server(P(), session=os.environ["MULTIAGENTS_SESSION_ID"])
        record("server_up", pid=srv.pid)
        def starter(plan):
            while plan.get("after_file") and not os.path.exists(plan["after_file"]):
                time.sleep(0.05)
            try:
                got = srv.start("w " + h.plan_token(plan["steps"]), agent=plan.get("agent", "worker"), timeout=0)
            except BaseException as exc:
                record("start_failed", tag=plan.get("tag"), error=str(exc)[:300])
            else:
                record("started", tag=plan.get("tag"), id=got)
        for plan in me.get("agents", []):
            if plan.get("async"):
                threading.Thread(target=starter, args=(plan,), daemon=True).start()
            else:
                starter(plan)
'''

TERM = '''    def on_term(*_):
        record("sigterm", n=n + 1)
        if me.get("late"):
            try:
                got = srv.start("late " + h.plan_token(me["late"]["steps"]), timeout=0)
                record("late_result", ok=True, id=got)
            except BaseException as exc:
                record("late_result", ok=False, error=str(exc)[:300])
            time.sleep(float(me.get("late_hold", 0)))
        sys.exit(143)
'''


class Scenario:
    def __init__(self, tmp_path, monkeypatch, capsys, *, limits=None):
        self.state = os.environ.get("MULTIAGENTS_STATE_DIR", "")
        self.config_dir = os.environ.get("MULTIAGENTS_CONFIG_DIR", "")
        assert self.state and self.config_dir, "conftest redirects are missing"
        self.s = CwSession(tmp_path, monkeypatch, capsys, limits=limits)
        self.project = h.Project(tmp_path)           # same root, the stub provider
        text = self.s.script.read_text()
        old_term = ('    def on_term(*_):\n        record("sigterm", n=n + 1)\n'
                    '        sys.exit(143)\n')
        anchor = '    me = launches[min(n, len(launches) - 1)]\n'
        assert old_term in text and anchor in text, "the R8f fake changed shape"
        text = text.replace(anchor, anchor + BOOT, 1).replace(old_term, TERM, 1)
        self.s.script.write_text(text)
        self.gate = self.project.base / "launch.gate"

    # ---- the launch hold
    def hold_launches(self) -> None:
        """From now on a launch stalls after its claim and before its pid."""
        assert wait(lambda: bool(self.log("server_up")), 40), "the CLI's server never came up"
        self.gate.touch()

    def waiting(self) -> bool:
        return Path(str(self.gate) + ".waiting").exists()

    def release_launches(self) -> float:
        at = time.time()
        self.gate.unlink(missing_ok=True)
        return at

    def close(self) -> None:
        self.release_launches()
        self.project.cleanup()

    # ---- running
    def steps(self, tag: str, *, wait: bool = True) -> list:
        p = self.project
        return [["pidfile", str(p.marker(tag + "-pid"))],
                h.step_start("sv"), h.text("sv", tag), h.step_finish("sv", 10, 1, 0.0),
                ["touch", str(p.marker(tag + "-started"))],
                *([["wait_for", str(p.marker("go")), 90]] if wait else []),
                h.step_start("sv"), h.text("sv", tag + "-end"),
                h.step_finish("sv", 10, 1, 0.0),
                ["touch", str(p.marker(tag + "-finished"))], ["exit", 0]]

    def run(self, first: dict, second: dict | None = None, *, compact=None):
        ctl_extra = {"src": SRC, "support": SUPPORT,
                     "base": str(self.project.base), "root": str(self.project.root),
                     "env": {"MULTIAGENTS_STATE_DIR": self.state,
                             "MULTIAGENTS_CONFIG_DIR": self.config_dir,
                             "HOME": str(self.project.user_home),
                             "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
                             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
                             "PYTHONUNBUFFERED": "1",
                             "CW_LAUNCH_GATE": str(self.gate)}, "gate_dir": GATE_DIR}
        launches = [{"life": 90, "server": True, **first},
                    {"life": 10, "server": True, **(second or {})}]
        s = self.s
        s.ctl.write_text(json.dumps({
            "transcript": str(s.transcript), "events": str(s.paths.events_file),
            "launches": launches, "probe": 0, "compact": compact or
            {"exit": 0, "stdout": ic.FIGURES + "\n"}, **ctl_extra}))
        from multiagents import driver, scripts
        from multiagents.paths import global_config_dir
        argv, env = scripts.exec_action("fakeprov", s.provider, object(), "launch",
                                        global_config_dir(), s.paths.config,
                                        extra_env=dict(s.context))
        code = driver._run_supervised(s.paths, s.config, "orchestrator", s.spec,
                                      s.provider, object(), dict(s.context), argv, env)
        return code

    def log(self, action: str) -> list[dict]:
        return self.s.calls(action)

    def node_events(self, agent_id: str, kind: str) -> list[dict]:
        return [e for e in ch.events(self.s.paths.events_file, kind)
                if e.get("agent") == agent_id]


@pytest.fixture
def scenario(tmp_path, monkeypatch, capsys):
    made: list[Scenario] = []

    def build(**kw) -> Scenario:
        sc = Scenario(tmp_path, monkeypatch, capsys, **kw)
        made.append(sc)
        return sc

    yield build
    for sc in made:
        sc.close()


def wait(predicate, timeout: float = 30.0) -> bool:
    return h.wait_until(predicate, timeout)


def run_with_observer(sc: Scenario, observer, *args, **kwargs):
    """Run the driver on this thread while `observer(results)` watches."""
    results: dict = {}

    def watch():
        try:
            observer(results)
        except BaseException as exc:            # reported by the test, below
            results["observer_error"] = repr(exc)

    thread = threading.Thread(target=watch, daemon=True)
    thread.start()
    code = sc.run(*args, **kwargs)
    thread.join(60)
    assert "observer_error" not in results, results["observer_error"]
    return code, results


def held_launch_agents(sc: Scenario) -> list[dict]:
    return [{"tag": "held", "async": True, "after_file": str(sc.project.marker("lock-held")),
             "steps": sc.steps("held")}]


# =============================================================== CW-R2 ====

def test_cw_r2_the_cli_is_not_stopped_while_a_launch_is_between_claim_and_pid(scenario):
    sc = scenario()
    sc.s.reading(OVER)

    def observe(r):
        sc.hold_launches()
        sc.project.marker("lock-held").touch()
        assert wait(sc.waiting, 30), "no launch reached the hold"
        assert wait(lambda: bool(sc.s.events("compact_scheduled")), 30), "never due"
        # Well past announcement + grace: the compaction is due and waiting.
        time.sleep(ic.GRACE + 3.0)
        r["early_sigterm"] = sc.log("sigterm")
        r["early_compact"] = sc.log("compact")
        r["pid_while_held"] = [n.get("pid") for n in sc.project.tree.read()["nodes"].values()
                               if n.get("agent") == "worker"]
        r["released"] = sc.release_launches()

    code, r = run_with_observer(sc, observe,
                                {"agents": held_launch_agents(sc)}, {"life": 12})
    assert not sc.log("start_failed"), sc.log("start_failed")
    assert r["pid_while_held"] and not any(r["pid_while_held"]), (
        f"the launch was not held between claim and pid: {r['pid_while_held']}")
    assert r["early_sigterm"] == [], "the CLI was stopped while a launch was held"
    assert r["early_compact"] == [], "the session was compacted with a launch held"
    sigterm = sc.log("sigterm")
    assert sigterm and sigterm[0]["t"] >= r["released"], (
        "the CLI was terminated before the held launch settled")
    started = sc.log("started")
    assert len(started) == 1, "the held launch did not complete"
    agent_id = started[0]["id"]
    node = sc.project.node(agent_id)
    assert node is not None and node.pid, "the launch settled with no pid recorded"
    assert sc.project.marker("held-started").is_file(), "the agent never ran"
    # What the safe stop leaves, the next server resumes.
    assert wait(lambda: bool(sc.project.events(agent_id, "adopted")), 20), (
        f"the next server did not adopt it: {sc.project.describe(agent_id)}")
    assert code == 0


def test_cw_r2_a_launch_that_settles_before_the_stop_is_adopted_and_finishes(scenario):
    """Same, to the end: after the compaction the agent is alive under the
    next server, which follows it to `done` with its result."""
    sc = scenario()
    sc.s.reading(OVER)

    def observe(r):
        sc.hold_launches()
        sc.project.marker("lock-held").touch()
        assert wait(sc.waiting, 30), "no launch reached the hold"
        assert wait(lambda: bool(sc.s.events("compact_scheduled")), 30)
        time.sleep(ic.GRACE + 2.0)
        assert not sc.log("sigterm"), "the CLI was stopped with the launch still held"
        sc.release_launches()
        assert wait(lambda: bool(sc.log("started")), 30)
        agent_id = sc.log("started")[0]["id"]
        assert wait(lambda: bool(sc.project.events(agent_id, "adopted")), 30)
        sc.project.marker("go").touch()
        r["status"] = sc.project.wait_status(agent_id, {"done", "failed"}, 30)

    code, r = run_with_observer(sc, observe, {"agents": held_launch_agents(sc)},
                                {"life": 20})
    assert r["status"] == "done", r
    assert sc.project.marker("held-finished").is_file()
    assert len(sc.log("compact")) == 1


def test_cw_r2_a_safe_point_that_never_comes_cancels_and_leaves_the_cli_alone(scenario):
    sc = scenario(limits={SAFE_POINT_KEY: SAFE_POINT_SECONDS})
    sc.s.reading(OVER)

    def observe(r):
        sc.hold_launches()
        sc.project.marker("lock-held").touch()
        assert wait(sc.waiting, 30), "no launch reached the hold"
        assert wait(lambda: bool(sc.s.events("compact_cancelled") or sc.log("sigterm")), 40)
        assert not sc.log("sigterm"), "the CLI was stopped with the launch still held"
        assert wait(lambda: bool(sc.s.events("compact_cancelled")), 10), (
            "the compaction was neither done nor cancelled")
        r["cancelled_at"] = time.time()
        r["sigterm_at_cancel"] = sc.log("sigterm")
        r["compact_at_cancel"] = sc.log("compact")
        r["events"] = sc.s.events("compact_cancelled")
        r["blocked"] = sc.s.events("compact_blocked")
        time.sleep(1.0)
        r["sigterm_after"] = sc.log("sigterm")
        r["released"] = sc.release_launches()

    code, r = run_with_observer(sc, observe, {"agents": held_launch_agents(sc)},
                                {"life": 6})
    assert r["sigterm_at_cancel"] == [] and r["sigterm_after"] == [], (
        "the CLI was stopped although the safe point was never reached")
    assert r["compact_at_cancel"] == []
    cancelled = r["events"]
    assert len(cancelled) == 1 and str(cancelled[0].get("reason", "")).strip(), (
        f"compact_cancelled must carry a reason: {cancelled}")
    reasons = [e.get("reason") for e in r["blocked"]]
    assert "safe_point_timeout" in reasons, reasons
    assert all(e.get("path") == "interactive" for e in r["blocked"])
    scheduled = sc.s.events("compact_scheduled")
    waited = r["cancelled_at"] - scheduled[0]["t"]
    assert waited >= ic.GRACE + SAFE_POINT_SECONDS - 0.5, (
        f"cancelled {waited:.1f}s after the announcement; the bound is not honoured")


def test_cw_r2_a_cancelled_compaction_is_proposed_again_once_the_launch_settles(scenario):
    """R8f.14: not suppressed for the rest of the session."""
    sc = scenario(limits={SAFE_POINT_KEY: SAFE_POINT_SECONDS})
    sc.s.reading(OVER)

    def observe(r):
        sc.hold_launches()
        sc.project.marker("lock-held").touch()
        assert wait(sc.waiting, 30), "no launch reached the hold"
        assert wait(lambda: bool(sc.s.events("compact_cancelled") or sc.log("sigterm")), 40)
        assert not sc.log("sigterm"), "the CLI was stopped with the launch still held"
        assert wait(lambda: bool(sc.s.events("compact_cancelled")), 10)
        sc.release_launches()
        assert wait(lambda: bool(sc.log("sigterm")), 40), "never proposed again"

    code, _ = run_with_observer(sc, observe, {"agents": held_launch_agents(sc)},
                                {"life": 12})
    assert len(sc.log("compact")) == 1
    assert len(sc.s.events("compact_scheduled")) >= 2
    assert sc.log("sigterm")[0]["t"] > sc.s.events("compact_cancelled")[0]["t"]


def test_cw_r2_nothing_in_progress_stops_without_waiting_for_the_bound(scenario):
    """No launch in flight: the safe point is immediate, not the timeout."""
    sc = scenario(limits={SAFE_POINT_KEY: 30})
    sc.s.reading(OVER)
    began = time.time()
    code = sc.run({}, {"life": 1})
    sigterm = sc.log("sigterm")
    assert sigterm, "never stopped"
    scheduled = sc.s.events("compact_scheduled")[0]["t"]
    assert sigterm[0]["t"] - scheduled < ic.GRACE + 8, (
        "an idle server held the stop up for the safe-point bound")
    assert sc.s.events("compact_cancelled") == []


def test_cw_r2_no_launch_is_admitted_by_the_server_that_is_stopping(scenario):
    """A launch attempted by the dying CLI after the safe point is not started
    by the stopping server: it is refused, or never completes — but no agent
    process exists for it afterwards."""
    sc = scenario()
    sc.s.reading(OVER)
    late = {"steps": sc.steps("late", wait=False)}
    sc.run({"late": late, "late_hold": 1.0}, {"life": 6})
    assert sc.log("sigterm"), "never stopped"
    result = sc.log("late_result")
    assert result, "the scenario did not attempt the late launch"
    time.sleep(2.0)
    assert not sc.project.marker("late-started").exists(), (
        "the stopping server launched an agent admitted after the safe point")
    late_nodes = [n for n in sc.project.tree.read()["nodes"].values()
                  if "late" in str(n.get("task", "")) and n.get("pid")]
    assert late_nodes == []


# =============================================================== CW-R5 ====

def test_cw_r5_the_stop_cancels_and_orphans_nothing_and_every_agent_is_adopted(scenario):
    sc = scenario()
    sc.s.reading(OVER)
    agents = [{"tag": t, "steps": sc.steps(t)} for t in ("a", "b", "c")]
    ids: dict = {}

    def observe(r):
        assert wait(lambda: len(sc.log("started")) == 3, 40)
        for e in sc.log("started"):
            ids[e["tag"]] = e["id"]
        assert wait(lambda: bool(sc.log("sigterm")), 40)
        assert wait(lambda: all(sc.project.events(i, "adopted") for i in ids.values()), 40), (
            "not every detached agent was adopted: "
            + "; ".join(sc.project.describe(i) for i in ids.values()))
        sc.project.marker("go").touch()
        for tag, i in ids.items():
            r[tag] = sc.project.wait_status(i, {"done", "failed", "cancelled",
                                                "orphaned"}, 40)

    code, r = run_with_observer(sc, observe, {"agents": agents}, {"life": 25})
    assert [r["a"], r["b"], r["c"]] == ["done"] * 3, r
    cancelled = [e for e in ch.events(sc.s.paths.events_file, "status")
                 if e.get("status") in ("cancelled", "orphaned")]
    assert cancelled == []
    for tag in ids:
        assert sc.project.marker(f"{tag}-finished").is_file()
    assert len(sc.log("compact")) == 1 and len(sc.log("launch")) == 2


def test_cw_r5_an_adoption_that_fails_is_reported_not_silent(scenario):
    """Two agents; one cannot be adopted (its agent spec is gone). The
    other is adopted and finishes; the failure is an `adopt_failed` event, the
    node ends `failed` with the reason, and the relaunched session is told about
    both (in flight at the stop)."""
    sc = scenario()
    sc.s.reading(OVER)
    agents = [{"tag": "good", "steps": sc.steps("good")},
              {"tag": "bad", "agent": "spawner", "steps": sc.steps("bad")}]
    ids: dict = {}

    def observe(r):
        assert wait(lambda: len(sc.log("started")) == 2, 40)
        for e in sc.log("started"):
            ids[e["tag"]] = e["id"]
        assert wait(lambda: bool(sc.log("compact")), 40)
        # Between the stop and the relaunch: the bad one's agent spec is gone,
        # the way the SV adversary suite makes a node unadoptable.
        import yaml
        cfg = sc.project.root / ".multiagents" / "config" / "agents.yaml"
        keep = yaml.safe_load(cfg.read_text())["agents"]
        cfg.write_text(yaml.safe_dump({"agents": {"worker": keep["worker"]}}))
        assert wait(lambda: bool(sc.project.events(ids["good"], "adopted")), 40)
        assert wait(lambda: sc.project.status(ids["bad"]) == "failed", 40), (
            sc.project.describe(ids["bad"]))
        sc.project.marker("go").touch()
        r["good"] = sc.project.wait_status(ids["good"], {"done", "failed"}, 40)

    code, r = run_with_observer(
        sc, observe, {"agents": agents}, {"life": 25},
        compact={"exit": 0, "stdout": ic.FIGURES + "\n", "sleep": 1.0})
    assert r["good"] == "done"
    bad = ids["bad"]
    assert sc.project.events(bad, "adopt_failed"), "the failure left no event"
    assert sc.project.events(bad, "adopted") == []
    node = sc.project.node(bad)
    assert node.status == "failed" and node.reason
    relaunch = sc.log("launch")[1]["env"].get("MULTIAGENTS_RESUME_PROMPT", "")
    assert ids["good"] in relaunch and bad in relaunch, (
        "the relaunch message does not list the agents that were in flight")
