"""RR-R3: captured failed work is a starting point, never an approval.

Captured ref faults are injected on the host after a genuine failed run.
The spec fixes the recovered_from reply shape, but leaves durable record
field names unspecified; these tests do not choose a storage schema.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_lr_loop_resume import make_world
from nc_fixture.m4_world import commit_entry, unwrap

WAIT = 5


@pytest.fixture
def w(tmp_path, monkeypatch):
    world = make_world(tmp_path, monkeypatch)
    yield world
    world.close()


def dead_work(w, **kw):
    # The provider error text makes the failure final (`other`, never retried
    # under TF-R2), so the dead run stays the node's only attempt until the
    # manual relaunch under test.
    w.fxw.queue(commit_entry("wip.txt", "saved work\n", "dead run work", exit=1,
                             stderr="Error: invalid task syntax\n"))
    w.start_scheduler()
    n = w.simple("N", "wk", **kw)
    assert w.wait_state(n, "done", timeout=WAIT)["outcome"] == "failed"
    attempt = w.get(n)["runs"][-1]["attempt_id"]
    ref = f"refs/heads/node-results/{attempt}"
    result = w.git("rev-parse", "--verify", ref)
    assert result.returncode == 0, "fixture failed to capture the dead run"
    return n, attempt, result.stdout.strip(), ref


def retry(w, n, **kw):
    w.fxw.queue({"gate": "retry"})
    reply = w.root_op("relaunch_node", n, **kw)
    assert reply.get("ok"), reply
    return reply


def test_rr_r3_failed_commit_is_recovered_and_reported_without_approval(w):
    n, attempt, commit, _ = dead_work(w)
    try:
        reply = retry(w, n)
        assert unwrap(reply["result"]).get("recovered_from") == {"attempt": attempt, "commit": commit}, reply
        w.until(lambda: len(w.fxw.calls()) == 2, WAIT, what="recovered launch")
        call = w.fxw.calls()[-1]
        assert call["head"] == commit
        assert "wip.txt" in call["files"]
        assert (Path(call["cwd"]) / "wip.txt").read_text() == "saved work\n"
        assert not any(g.get("approved") for g in w.get(n)["generations"])
        denied = w.root_op("publish_node", n)
        assert not denied.get("ok"), "recovering WIP must not authorise publication"
    finally:
        w.gate("retry", w.fxw)


def test_rr_r3_new_dependency_input_is_merged_with_captured_work(w):
    w.fxw.queue(commit_entry("input.txt", "old input\n", "input"))
    w.start_scheduler()
    dep = w.simple("DEP", "wk")
    w.wait_state(dep, "done", timeout=WAIT)
    w.fxw.queue(commit_entry("wip.txt", "saved work\n", "dead work", exit=1,
                             stderr="Error: invalid task syntax\n"))
    n = w.simple("N", "wk", inputs=[{"node": dep}])
    w.wait_state(n, "done", timeout=WAIT)
    w.fxw.queue(commit_entry("input.txt", "new input\n", "advanced input"))
    assert w.root_op("relaunch_node", dep).get("ok")
    w.until(lambda: len(w.fxw.calls()) == 3 and w.get(dep)["state"] == "done", WAIT,
            what="advanced dependency")
    try:
        reply = retry(w, n)
        assert unwrap(reply["result"]).get("recovered_from"), reply
        w.until(lambda: len(w.fxw.calls()) == 4, WAIT, what="merged recovery launch")
        cwd = Path(w.fxw.calls()[-1]["cwd"])
        assert (cwd / "wip.txt").read_text() == "saved work\n"
        assert (cwd / "input.txt").read_text() == "new input\n"
    finally:
        w.gate("retry", w.fxw)


@pytest.mark.parametrize("fault", ["missing", "moved", "ancestry"])
def test_rr_r3_invalid_captured_evidence_holds_and_opt_out_keeps_task_patch(w, fault):
    n, attempt, commit, ref = dead_work(w)
    if fault == "missing":
        assert w.git("update-ref", "-d", ref).returncode == 0
    elif fault == "moved":
        assert w.git("update-ref", ref, w.fxw.calls()[0]["head"]).returncode == 0
    else:
        # Successful capture is recorded, then its evidence is replaced by an
        # unrelated valid commit, keeping ref and journal consistent. Only the
        # ancestry check can reject it.
        tree = w.git("rev-parse", commit + "^{tree}").stdout.strip()
        unrelated = w.git("-c", "user.name=t", "-c", "user.email=t@example.invalid",
                          "commit-tree", tree, "-m", "unrelated").stdout.strip()
        assert unrelated
        assert w.git("update-ref", ref, unrelated).returncode == 0
        from multiagents.scheduler.rpc import Service
        from multiagents.scheduler.engine import attempts, save_attempt
        from multiagents.tree import now
        store = Service(w.root, now()).store
        with store.transaction() as db:
            record = attempts(db)[attempt]
            record["result"]["commit"] = unrelated
            save_attempt(db, record)
    reply = w.root_op("relaunch_node", n, task="KEEP-RECOVERY-PATCH")
    assert reply.get("ok"), reply
    held = w.wait_held(n, "recovery_conflict", timeout=WAIT)
    detail = json.dumps(held["hold"]["detail"])
    assert attempt in detail and detail != json.dumps(attempt), detail
    assert held["task"] == "KEEP-RECOVERY-PATCH"
    assert len(w.fxw.calls()) == 1, "invalid recovery must not launch a provider"
    try:
        reply = retry(w, n, recover_wip=False)
        assert not unwrap(reply["result"]).get("recovered_from"), reply
        w.until(lambda: len(w.fxw.calls()) == 2, WAIT, what="opted-out recovery")
        assert "wip.txt" not in w.fxw.calls()[-1]["files"]
        assert "KEEP-RECOVERY-PATCH" in w.fxw.calls()[-1]["prompt"]
    finally:
        w.gate("retry", w.fxw)


def test_rr_r3_conflicting_current_inputs_hold_instead_of_dropping_work(w):
    w.fxw.queue(commit_entry("shared.txt", "base\n"))
    w.start_scheduler()
    dep = w.simple("DEP", "wk")
    w.wait_state(dep, "done", timeout=WAIT)
    w.fxw.queue(commit_entry("shared.txt", "dead WIP\n", exit=1,
                             stderr="Error: invalid task syntax\n"))
    n = w.simple("N", "wk", inputs=[{"node": dep}])
    w.wait_state(n, "done", timeout=WAIT)
    attempt = w.get(n)["runs"][-1]["attempt_id"]
    w.fxw.queue(commit_entry("shared.txt", "new upstream\n"))
    assert w.root_op("relaunch_node", dep).get("ok")
    w.until(lambda: len(w.fxw.calls()) == 3 and w.get(dep)["state"] == "done", WAIT,
            what="conflicting dependency update")
    assert w.root_op("relaunch_node", n).get("ok")
    held = w.wait_held(n, "recovery_conflict", timeout=WAIT)
    assert attempt in json.dumps(held["hold"]["detail"])
    assert len(w.fxw.calls()) == 3
    try:
        retry(w, n, recover_wip=False)
        w.until(lambda: len(w.fxw.calls()) == 4, WAIT, what="current inputs after opt out")
        assert (Path(w.fxw.calls()[-1]["cwd"]) / "shared.txt").read_text() == "new upstream\n"
    finally:
        w.gate("retry", w.fxw)


def test_rr_r3_explicit_opt_out_uses_current_inputs(w):
    n, _, _, _ = dead_work(w)
    try:
        reply = retry(w, n, recover_wip=False)
        assert not unwrap(reply["result"]).get("recovered_from"), reply
        w.until(lambda: len(w.fxw.calls()) == 2, WAIT, what="opt out launch")
        assert "wip.txt" not in w.fxw.calls()[-1]["files"]
    finally:
        w.gate("retry", w.fxw)


def test_rr_r3_no_recorded_capture_preserves_existing_relaunch(w):
    n, attempt, _, _ = dead_work(w)
    from multiagents.scheduler.rpc import Service
    from multiagents.scheduler.engine import attempts, save_attempt
    from multiagents.tree import now
    store = Service(w.root, now()).store
    with store.transaction() as db:
        record = attempts(db)[attempt]
        record.pop("result", None)
        save_attempt(db, record)
    try:
        reply = retry(w, n)
        assert not unwrap(reply["result"]).get("recovered_from"), reply
        w.until(lambda: len(w.fxw.calls()) == 2, WAIT, what="legacy relaunch without capture")
        assert "wip.txt" not in w.fxw.calls()[-1]["files"]
    finally:
        w.gate("retry", w.fxw)


def test_rr_r3_prelaunch_exec_failure_keeps_recovery_for_the_next_try(w):
    n, attempt, commit, _ = dead_work(w)
    script = w.fxw.dir / "agent.py"
    original = script.read_text()
    # An executable that passed config checks but whose interpreter vanished
    # fails exec before a provider process can launch.
    script.write_text("#!/rr-missing-interpreter\n" + original.split("\n", 1)[1])
    try:
        reply = w.root_op("relaunch_node", n)
        assert reply.get("ok"), reply
        assert unwrap(reply["result"]).get("recovered_from") == {"attempt": attempt, "commit": commit}
        w.until(lambda: w.get(n)["state"] in {"held", "done"}, WAIT,
                what="prelaunch exec failure to settle")
        assert len(w.fxw.calls()) == 1, "fault unexpectedly launched a provider"
    finally:
        script.write_text(original)
    try:
        reply = retry(w, n)
        assert unwrap(reply["result"]).get("recovered_from") == {"attempt": attempt, "commit": commit}, reply
        w.until(lambda: len(w.fxw.calls()) == 2, WAIT, what="recovery after exec failure")
        assert w.fxw.calls()[-1]["head"] == commit
    finally:
        w.gate("retry", w.fxw)


def test_rr_r3_loop_relaunch_recovers_only_the_failed_child(w):
    from test_lr_loop_resume import mkloop, wait_run_failed
    # One final failure (its error text matches nothing, TF-R2 never retries
    # it) leaves the newest dead work as the captured commit to recover from.
    w.fxw.queue(commit_entry("wip.txt", "newest dead work\n", exit=1,
                             stderr="Error: invalid task syntax\n"))
    w.start_scheduler()
    loop, wk, rv = mkloop(w)
    wait_run_failed(w, loop)
    attempt = w.get(wk)["runs"][-1]["attempt_id"]
    commit = w.git("rev-parse", f"refs/heads/node-results/{attempt}").stdout.strip()
    assert commit
    w.fxw.queue({"gate": "retry"})
    try:
        reply = w.root_op("relaunch_node", loop)
        assert reply.get("ok"), reply
        w.until(lambda: len(w.fxw.calls()) == 3, WAIT, what="recovered loop child")
        call = w.fxw.calls()[-1]
        assert call["head"] == commit
        assert (Path(call["cwd"]) / "wip.txt").read_text() == "newest dead work\n"
        assert w.fxr.calls() == [], "the next sibling must still wait for the recovering worker"
    finally:
        w.gate("retry", w.fxw)
