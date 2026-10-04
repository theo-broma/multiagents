"""M6 — acceptance: NC-R45 (baseline), NC-R46 (the user's example) and the
parts of NC-R47 (adversarial list) that no earlier suite covers.

NC-R47 coverage map -- each item of the contract's list, and where it is tested:
  forged token ............................ test_nc_m1_rpc.py (`..._unauthenticated_for_every_op`)
                                            + here, against a REAL launched run
  supplied parent outside scope ........... test_nc_m1_rpc.py + here (real run token)
  run creating a node outside its subtree . test_nc_m1_rpc.py + here (real run token)
  verdict from the wrong child / wrong
    generation / twice .................... M4 suite (NC-R34), not repeated here; the
                                            end-to-end loop below uses honest verdicts
  rejected generation passed as input ..... test_nc_m3_deps.py
  crash between claimed and recorded ...... test_nc_m3_crash.py, test_nc_m2_lifecycle.py
  lock released before confirmed
    termination ........................... test_nc_m5_suspend.py (NC-R69), test_nc_m2_order_locks.py
  window closing mid-loop ................. test_nc_m5_suspend.py
New here: a real run's capability, end to end (what reaches the run, what it can
and cannot do with it, that it dies with the run, that the root capability
never reaches the run), and the verdict op refused for a run that was not
given the verdict permission.

Assumptions where the contract is silent (kept loose):
- the loop test drives the reviewer through `ReviewerProvider` below: a fixture
  provider whose verdicts are taken from a queue file, one per activation, and
  sent with the run's own token over the NC-R8 socket. It scrapes the
  generation under review from the prompt exactly as `gitworld.ShellFixtureProvider`'s
  `verdict_rpc` does (a GUESS about NC-R66's wording, shared with the M3/M4 tests).
- `instantiate_template` of `implement` returns the top node (or a result from
  which the node carrying `template` can be found with `list_nodes`).
- `relaunch_node` pins are `{child_id: {"model": "fx/m2"}}` and the provider
  adapter passes the model on the command line, so `fx/m2` appears in the
  fixture's argv for the relaunched activation only.
- a transition is `{seq, kind, node_id, at, detail}` (NC-R77).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from nc_fixture.agent import task  # noqa: E402
from nc_fixture.gitworld import GitWorld, ShellFixtureProvider  # noqa: E402
from nc_fixture.world import err_code, unwrap  # noqa: E402

_POP = '''vr = fx.get("verdict_rpc")
if not vr:
    qf = os.path.join(base, "verdicts.queue")
    if os.path.exists(qf):
        lines = open(qf).read().split()
        if lines:
            n = int(open(os.path.join(base, "verdicts.count")).read() or 0) + 1 \\
                if os.path.exists(os.path.join(base, "verdicts.count")) else 1
            open(os.path.join(base, "verdicts.count"), "w").write(str(n))
            open(qf, "w").write("\\n".join(lines[1:]))
            vr = {"verdict": lines[0],
                  "findings": [{"summary": "FINDING-%d" % n, "severity": "major"}]
                              if lines[0] == "rejected" else []}'''


class ReviewerProvider(ShellFixtureProvider):
    """A reviewer that answers from a queue: one verdict per activation."""

    def queue(self, verdicts: list[str]) -> None:
        (self.dir / "verdicts.queue").write_text("\n".join(verdicts))

    def __init__(self, tmp, name, **extra):
        super().__init__(tmp, name, **extra)
        script = self.dir / "agent.py"
        text = script.read_text()
        assert 'vr = fx.get("verdict_rpc")' in text
        script.write_text(text.replace('vr = fx.get("verdict_rpc")', _POP, 1))


def trio(world: GitWorld):
    world.agent("tester", "fx", writes=True)
    world.agent("implementer", "fx", writes=True)
    world.rv = ReviewerProvider(world.tmp, "rv")
    world.providers["rv"] = world.rv
    world.agent("reviewer", "rv", writes=False)
    world.write_config()


@pytest.fixture
def w(tmp_path, monkeypatch):
    world = GitWorld(tmp_path, monkeypatch)
    yield world
    world.close()


@pytest.fixture
def uw(w):
    trio(w)
    return w


def kinds_for(world: GitWorld, node_id: str) -> list[str]:
    return world.transitions(node_id)


# =================================================================== NC-R45

@pytest.fixture
def pcw(w):
    w.pc = w.provider("pcfx", max_concurrent=1)
    w.agent("pcw", "pcfx", writes=True)
    w.write_config()
    return w


def test_nc_r45_three_dependent_nodes_survive_disconnect_saturation_and_a_restart(pcw):
    w = pcw
    w.start_scheduler()
    hold = w.simple("H", "pcw", fx={"gate": "gH"})
    w.wait_running(hold)

    def dep(node):
        return {"depends_on": [{"node": node}], "inputs": [{"node": node}]}

    a = w.coder("A", {"a.txt": "a\n"}, agent="pcw")
    b = w.coder("B", {"b.txt": "b\n"}, agent="pcw", **dep(a),
                fx={"shell": ["git rev-parse HEAD > $FX_DIR/b.start"]})
    c = w.coder("C", {"c.txt": "c\n"}, agent="pcw", **dep(b),
                fx={"shell": ["git rev-parse HEAD > $FX_DIR/c.start"]})
    # the orchestrator is disconnected: nobody calls wait_for_nodes below
    w.quiet(3)
    assert [len(w.pc.by_tag(t)) for t in "ABC"] == [0, 0, 0], "launched past a full provider"
    assert w.deferred() == []
    w.kill9()
    w.start_scheduler()
    w.quiet(2)
    assert [len(w.pc.by_tag(t)) for t in "ABC"] == [0, 0, 0]
    w.gate("gH", w.pc)
    for n in (a, b, c):
        assert w.done(n, timeout=120)["outcome"] == "completed"
    w.restart_scheduler()
    w.quiet(2)
    assert [len(w.pc.by_tag(t)) for t in "ABC"] == [1, 1, 1], "each node launches exactly once"
    for n in (a, b, c):
        assert len(w.get(n)["runs"]) == 1
    ga, gb = w.generations(a)[-1], w.generations(b)[-1]
    assert w.get(b)["runs"][0]["input_commit"] == ga["commit"]
    assert w.get(c)["runs"][0]["input_commit"] == gb["commit"]
    assert (w.pc.dir / "b.start").read_text().strip() == ga["commit"]
    assert (w.pc.dir / "c.start").read_text().strip() == gb["commit"]
    assert {"a.txt", "b.txt", "c.txt"} <= w.files(w.nodes_ref(a)) | w.files(w.nodes_ref(c)) \
        | w.files(w.nodes_ref(b))
    w.quiet(1)
    assert w.deferred() == []


def test_nc_r45_every_transition_is_delivered_after_the_orchestrator_reconnects(pcw):
    w = pcw
    w.start_scheduler()
    hold = w.simple("H", "pcw", fx={"gate": "gH"})
    w.wait_running(hold)
    a = w.coder("A", {"a.txt": "a\n"}, agent="pcw")
    b = w.coder("B", {"b.txt": "b\n"}, agent="pcw", depends_on=[{"node": a}])
    w.restart_scheduler()
    w.gate("gH", w.pc)
    w.done(b, timeout=120)
    got = w.ok("wait_for_nodes", {"timeout": 2})
    by_node: dict[str, list[str]] = {}
    for t in got["transitions"]:
        by_node.setdefault(t["node_id"], []).append(t["kind"].removeprefix("node."))
    for n in (a, b):
        seq = by_node[n]
        assert seq.count("created") == 1 and seq.count("launched") == 1 and seq.count("done") == 1, seq
        assert seq.index("created") < seq.index("launched") < seq.index("done"), seq
    seqs = [t["seq"] for t in got["transitions"]]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    started = [t for t in got["transitions"] if t["kind"].removeprefix("node.") == "scheduler_started"]
    assert len(started) >= 2, "the restart is a transition too"
    assert by_node[a].index("done") < by_node[b].index("launched")
    again = w.ok("wait_for_nodes", {"timeout": 0.5})
    assert [t["seq"] for t in again["transitions"]] == seqs, "un-acked transitions are redelivered"
    w.ok("ack_nodes", {"cursor": got["next_cursor"]})
    after = w.ok("wait_for_nodes", {"timeout": 0.5})
    assert [t for t in after["transitions"] if t["seq"] <= got["next_cursor"]] == []


# =================================================================== NC-R46

def params(**over):
    base = {
        "spec_path": "context/specs/x.md",
        "tests_task": task("TESTS", "write the tests",
                           shell=["date +%s%N >> tests.txt"], commit="tests round"),
        "implement_task": task("IMPL", "implement it",
                               shell=["ls > $FX_DIR/impl.ls.$(date +%s%N)",
                                      "date +%s%N >> impl.txt"], commit="impl round"),
        "tester": "tester", "reviewer": "reviewer", "implementer": "implementer",
        "test_rounds": 2, "impl_rounds": 2,
    }
    base.update(over)
    return base


def find_plan(w: GitWorld):
    nodes = w.list()
    top = [n for n in nodes if n.get("template")][0]
    loops = [n for n in nodes if n["kind"] == "loop"]
    by_id = {n["id"]: n for n in nodes}

    def agents_of(loop):
        return {by_id[c]["agent"] for c in loop["children"] if by_id[c]["kind"] == "simple"}

    test_loop = [l for l in loops if "tester" in agents_of(l)][0]
    impl_loop = [l for l in loops if "implementer" in agents_of(l)][0]
    impl_child = [c for c in impl_loop["children"] if by_id[c]["agent"] == "implementer"][0]
    return top, test_loop, impl_loop, impl_child


def held_for(w, loop_id, reason):
    return w.until(lambda: (n := w.get(loop_id))["state"] == "held"
                   and (n.get("hold") or {}).get("reason") == reason and n,
                   timeout=180, what=f"{loop_id} held for {reason}")


def test_nc_r46_the_users_example_end_to_end(uw):
    w = uw
    w.rv.queue(["rejected", "approved", "rejected", "rejected", "approved"])
    w.start_scheduler()
    main_before = w.main_tip()
    w.ok("instantiate_template", {"name": "implement", "params": params()})
    top, test_loop, impl_loop, impl_child = find_plan(w)

    # tests rejected once, then approved -> the first loop exits
    done = w.until(lambda: (n := w.get(test_loop["id"]))["state"] == "done" and n,
                   timeout=180, what="the tests loop to finish")
    assert done["outcome"] == "approved"
    assert done["loop"]["rounds_rejected"] == 1
    assert len(w.fx.by_tag("TESTS")) == 2
    assert "FINDING-1" in w.fx.by_tag("TESTS")[1]["prompt"], "findings reach the next activation"
    assert "FINDING-1" not in w.fx.by_tag("TESTS")[0]["prompt"]

    # implementation rejected until impl_rounds -> loop_max, orchestrator notified
    held = held_for(w, impl_loop["id"], "loop_max")
    assert held["loop"]["rounds_rejected"] == 2
    assert len(w.fx.by_tag("IMPL")) == 2
    assert "FINDING-3" in w.fx.by_tag("IMPL")[1]["prompt"]
    w.quiet(3)
    assert len(w.fx.by_tag("IMPL")) == 2, "no third launch at the maximum"
    names = w.transitions(impl_loop["id"])
    assert names.count("loop_max") == 1 and "loop_exited" not in names
    assert w.get(top["id"])["state"] != "done"
    notified = [t for t in w.ok("wait_for_nodes", {"timeout": 1})["transitions"]
                if t["node_id"] == impl_loop["id"] and t["kind"].removeprefix("node.") == "loop_max"]
    assert len(notified) == 1, "the orchestrator is told, once"
    # the first IMPL sees the tests of the first loop in its checkout
    listing = sorted(w.fx.dir.glob("impl.ls.*"))
    assert listing and "tests.txt" in listing[0].read_text()

    # the root relaunches with another model and a raised maximum
    rev = w.get(impl_loop["id"])["revision"]
    out = w.rpc("relaunch_node", {"id": impl_loop["id"], "revision": rev, "max_rounds": 3,
                                  "pins": {impl_child: {"model": "fx/m2"}}})
    assert out.get("ok"), out
    final = w.until(lambda: (n := w.get(top["id"]))["state"] == "done" and n,
                    timeout=180, what="the whole plan to finish")
    assert final["outcome"] == "approved"
    assert w.get(impl_loop["id"])["outcome"] == "approved"
    assert w.get(impl_loop["id"])["loop"]["rounds_rejected"] == 2, "the counter is kept"
    impl_calls = w.fx.by_tag("IMPL")
    assert len(impl_calls) == 3
    assert "fx/m2" not in json.dumps(impl_calls[0]["argv"] + impl_calls[1]["argv"])
    assert "fx/m2" in json.dumps(impl_calls[2]["argv"]), "the model change applies to the implementer"
    assert "FINDING-4" in impl_calls[2]["prompt"]

    # reviewer-B: one provider session across both loops, the alias is not the model
    rcalls = w.rv.calls()
    assert len(rcalls) == 5
    first = rcalls[0]["session"]
    assert all(c["resume"] == first for c in rcalls[1:]), [c["resume"] for c in rcalls]
    assert rcalls[0]["resume"] is None
    assert all("fx/m2" not in json.dumps(c["argv"]) for c in rcalls)

    # done is notified, then merge_node lands the plan on main
    assert w.transitions(top["id"]).count("done") == 1
    assert w.main_tip() == main_before, "the scheduler never writes main"
    merged = w.rpc("merge_node", {"id": top["id"]})
    assert merged.get("ok"), merged
    assert w.main_tip() != main_before
    assert {"tests.txt", "impl.txt"} <= w.files(w.base)
    parents = w.git("rev-list", "--parents", "-n", "1", w.base).split()
    assert len(parents) == 2, "a squash: one single-parent commit"
    assert w.get(top["id"]).get("published") == w.main_tip()


def test_nc_r46_a_reviewer_that_never_approves_is_never_merged_without_force(uw):
    w = uw
    w.rv.queue(["rejected", "rejected"])
    w.start_scheduler()
    w.ok("instantiate_template", {"name": "implement", "params": params(test_rounds=2)})
    top, test_loop, *_ = find_plan(w)
    held_for(w, test_loop["id"], "loop_max")
    main_before = w.main_tip()
    out = w.rpc("merge_node", {"id": top["id"]})
    assert err_code(out) in ("not_done", "not_approved"), out
    assert w.main_tip() == main_before


# =================================================================== NC-R47

def run_token(call: dict) -> str | None:
    return call.get("env_token") or (call.get("mcp_env") or {}).get("MULTIAGENTS_RPC_TOKEN")


def test_nc_r47_a_real_run_gets_its_own_token_and_never_the_roots(w):
    w.start_scheduler()
    n = w.simple("DELEG", "spawner", fx={"gate": "g"})
    w.wait_running(n)
    call = w.wait_spawn("DELEG")
    token = run_token(call)
    assert token and len(token) >= 40
    root = w.root_token()
    blob = json.dumps(w.fx.calls())
    assert root not in blob, "the root capability reached a run"
    assert token != root
    sock = (call.get("mcp_env") or {}).get("MULTIAGENTS_RPC_SOCKET")
    assert sock and Path(sock) == w.sock


def test_nc_r47_a_real_runs_token_creates_under_its_own_node_and_nowhere_else(w):
    w.start_scheduler()
    n = w.simple("DELEG", "spawner", fx={"gate": "g"})
    sibling = w.simple("SIB", "worker", fx={"gate": "gs"})
    w.wait_running(n)
    token = run_token(w.wait_spawn("DELEG"))
    kid = w.rpc("create_node", {"kind": "simple", "agent": "worker", "task": task("KID"),
                                "plan_revision": w.plan_revision()}, token)
    assert kid.get("ok"), kid
    assert unwrap(kid["result"])["parent"] == n
    assert unwrap(kid["result"])["created_by"] != "root"
    before = [x["id"] for x in w.list()]
    for parent in (sibling, None):
        fields = {"kind": "simple", "agent": "worker", "task": task("EVIL"),
                  "plan_revision": w.plan_revision()}
        if parent:
            fields["parent"] = parent
        out = w.rpc("create_node", fields, token)
        if parent:
            assert err_code(out) == "forbidden", out
    assert w.fx.by_tag("EVIL") == []
    assert [x["id"] for x in w.list()][:len(before)] == before
    assert w.rpc("cancel_node", {"id": sibling, "revision": w.get(sibling)["revision"]},
                 token)["ok"] is False
    assert w.get(sibling)["state"] == "running"
    assert err_code(w.rpc("get_node", {"id": sibling}, token)) in ("forbidden", "not_found")


def test_nc_r47_a_forged_or_altered_run_token_is_unauthenticated(w):
    w.start_scheduler()
    n = w.simple("DELEG", "spawner", fx={"gate": "g"})
    w.wait_running(n)
    token = run_token(w.wait_spawn("DELEG"))
    for bad in (token + "0", token[:-1] + ("0" if token[-1] != "0" else "1"), token[1:], "", "root"):
        out = w.rpc("list_nodes", {}, bad)
        assert err_code(out) == "unauthenticated", (bad, out)
    assert w.rpc("list_nodes", {}, token)["ok"]


def test_nc_r47_a_run_token_is_refused_once_its_run_has_ended(w):
    w.start_scheduler()
    n = w.simple("DELEG", "spawner", fx={"gate": "g"})
    w.wait_running(n)
    token = run_token(w.wait_spawn("DELEG"))
    assert w.rpc("list_nodes", {}, token)["ok"]
    w.gate("g")
    w.wait_state(n, "done")
    assert err_code(w.rpc("list_nodes", {}, token)) == "unauthenticated"


def test_nc_r47_a_cancelled_nodes_run_token_is_refused_after_the_stop(w):
    w.start_scheduler()
    n = w.simple("DELEG", "spawner", fx={"gate": "g"})
    w.wait_running(n)
    token = run_token(w.wait_spawn("DELEG"))
    assert w.cancel(n)["ok"]
    w.until(lambda: err_code(w.rpc("list_nodes", {}, token)) == "unauthenticated",
            what="the token of a cancelled run to be revoked")


def test_nc_r47_a_plain_run_cannot_give_a_verdict_nor_use_root_operations(w):
    w.start_scheduler()
    n = w.simple("PLAIN", "worker", fx={"gate": "g"})
    w.wait_running(n)
    token = run_token(w.wait_spawn("PLAIN"))
    assert token, "a run that may not spawn still gets a read-only capability (NC-R58)"
    verdict = w.rpc("give_verdict", {"node_id": n, "generation_seq": 1, "commit": "0" * 40,
                                     "verdict": "approved", "findings": []}, token)
    assert err_code(verdict) == "forbidden", verdict
    for op, args in (("merge_node", {"id": n}), ("ack_nodes", {"cursor": 0}),
                     ("register_template", {"yaml": "template: x\n"}),
                     ("relaunch_node", {"id": n, "revision": 1}),
                     ("create_node", {"kind": "simple", "agent": "worker", "task": "t",
                                      "plan_revision": w.plan_revision()})):
        assert err_code(w.rpc(op, args, token)) == "forbidden", op
    assert w.get(n)["state"] == "running"
    assert len(w.list()) == 1


def test_nc_r47_the_root_capability_is_not_in_any_launch_environment_or_config(w):
    w.start_scheduler()
    n = w.simple("PLAIN", "worker", fx={"gate": "g"})
    w.wait_running(n)
    call = w.wait_spawn("PLAIN")
    root = w.root_token()
    assert root not in json.dumps(call)
