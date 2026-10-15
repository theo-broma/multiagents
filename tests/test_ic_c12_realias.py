"""IC-C12 — alias re-seed (IC-R3): each activation of a session alias gets a
fresh clone from its attempt's seed at the alias's stable path; the dirty-checkout
hold, the predecessor check and the swap intent (P7) keep their guarantees.

Scenarios run through a real scheduler (`ic_prov.IcWorld`: injected reference
backend, fixture providers). The worker/reviewer loop of `test_nc_m4_sessions`
(`ic_prov.PAIR`) is the alias; the reviewer is alias `B`.

Each test runs its own scenario (a module-scoped world would run outside the
per-test state redirects of `conftest.py`).

"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import ic_prov as ip  # noqa: E402
from nc_fixture.agent import task  # noqa: E402
from nc_fixture.m4_world import commit_entry, finding, verdict_entry  # noqa: E402

JUNK = ip.JUNK


def pair_world(tmp_path, monkeypatch, **kw):
    w = ip.make_world(tmp_path, monkeypatch, **kw)
    w.pair()
    return w


def test_ic_c12_each_activation_is_a_fresh_clone_with_nothing_of_the_last_one(tmp_path, monkeypatch):
    w = pair_world(tmp_path, monkeypatch)
    try:
        w, runs, r1, r2, (ghost, blob) = run_reseed_in(w)
        assert len(runs) == 2 and runs[0]["input_commit"] != runs[1]["input_commit"]
        for run, rep in zip(runs, (r1, r2)):
            assert ip.isolated_clone_problems(rep, w.root, run["input_commit"]) == []
        # P8: no object, ref or reflog outside the second seed's reachability
        assert ip.reachability_problems(r2, w.root, runs[1]["input_commit"]) == []
        assert ghost not in r2["objects"] and blob not in r2["objects"]
        assert not [r for r in r2["refs"] if "junk" in r]
        assert ghost not in r2["reflog_oids"]
        assert not w.host_has(ghost) and not w.host_has(blob)
        # the old .git is not carried over
        assert "ic-marker" not in r2["git_files"]
    finally:
        w.close()


def test_ic_c12_the_conversation_home_is_kept(tmp_path, monkeypatch):
    w = pair_world(tmp_path, monkeypatch)
    try:
        w, runs, r1, r2, _ = run_reseed_in(w)
        assert "keep.txt" in r2["home_files"]
        assert (Path(r2["home"]) / "keep.txt").read_text() == "home of activation one"
    finally:
        w.close()


def test_ic_c12_the_second_activation_resumes_the_session_on_the_new_input(tmp_path, monkeypatch):
    w = pair_world(tmp_path, monkeypatch)
    try:
        w, runs, r1, r2, _ = run_reseed_in(w)
        first, second = w.fxr.calls()
        assert second["resume"] == first["session"]
        assert second["head"] == runs[1]["input_commit"] != first["head"]
        assert "b.txt" in second["files"]
    finally:
        w.close()


def run_reseed_in(w):
    w.fxw.queue(commit_entry("a.txt", "one\n", "r1"), commit_entry("b.txt", "two\n", "r2"))
    w.fxr.queue(verdict_entry("rejected", [finding("redo")], tag="R1", report=True, exec=JUNK),
                verdict_entry("approved", tag="R2", report=True))
    top, wk, rv = ip.start_pair(w)
    w.wait_state(top, "done", timeout=ip.ROUNDS)
    return (w, w.runs(rv), w.report(w.fxr, "R1"), w.report(w.fxr, "R2"),
            (w.fxr.dir / "junk").read_text().split())


# ---------------------------------------------------------- dirty hold (TB2-R6)

def test_ic_c12_a_dirty_checkout_holds_the_activation_and_nothing_is_swapped(tmp_path, monkeypatch):
    w = pair_world(tmp_path, monkeypatch)
    try:
        w.fxw.queue(commit_entry("a.txt", "one\n", "r1"), commit_entry("b.txt", "two\n", "r2"))
        w.fxr.queue(verdict_entry("rejected", [finding("redo")], tag="R1", report=True,
                                  leave={"scratch.txt": "left behind\n"}),
                    {"tag": "CLEAN"},                       # the one resumed cleanup run (TB2-R6) pops this
                    verdict_entry("approved", tag="R2", report=True))
        top, wk, rv = ip.start_pair(w)
        held = w.wait_held(rv, "dirty_worktree", timeout=ip.ROUNDS)
        # TB2-R6 as today: one resumed cleanup run, never a reviewer activation on a dirty checkout
        assert [c["tag"] for c in w.fxr.calls()] == ["R1", "CLEAN"]
        first = w.fxr.calls()[0]
        old = Path(first["cwd"])
        assert (old / "scratch.txt").read_text() == "left behind\n"          # nothing deleted
        assert ip.git(old, "rev-parse", "HEAD") == first["head"]             # the old repository, not swapped
        archive = (held["hold"] or {}).get("detail", "")
        assert archive and Path(archive).exists()                            # the recovery artefact is host-side
        assert w.fxr.reports("R2") == []                                     # no second activation ran
        w.checkpoint()
        assert w.fxr.spawns() == 2 and w.get(rv)["state"] == "held"
        assert (old / "scratch.txt").exists() and ip.git(old, "rev-parse", "HEAD") == first["head"]
    finally:
        w.close()


# ---------------------------------------------------- a live predecessor (P7)

GROUP = """\
template: icaliased
version: 1
params:
  t1: {type: text, default: ""}
  t2: {type: text, default: ""}
root:
  key: top
  kind: group
  children:
    - {key: first, kind: simple, agent: rv, task: {param: t1}, session: B}
    - {key: second, kind: simple, agent: rv, task: {param: t2}, session: B}
"""

MARK = ("open('.git/ic-marker', 'w').write('predecessor')\n"
        "open(os.path.join(os.environ['HOME'], 'keep.txt'), 'w').write('kept')\n")


def test_ic_c12_a_live_predecessor_blocks_the_swap_then_the_next_activation_is_fresh(tmp_path, monkeypatch):
    w = pair_world(tmp_path, monkeypatch)
    try:
        w.start_scheduler()
        w.register_ok(GROUP)
        reply = w.instantiate("icaliased", {
            "t1": task("F", gate="gf", report=True, early=MARK),
            "t2": task("G", gate="gg", report=True, early=MARK)})
        assert reply.get("ok"), reply
        from nc_fixture.world import unwrap
        top = unwrap(reply["result"]).get("id") or unwrap(reply["result"])["root"]
        first = w.until(lambda: next((t for t in ("F", "G") if w.fxr.by_tag(t)), None), what="one child to start")
        second = "G" if first == "F" else "F"
        gate_of = {"F": "gf", "G": "gg"}
        old = Path(w.fxr.by_tag(first)[0]["cwd"])
        ip.wait_until(lambda: (old / ".git" / "ic-marker").exists(), "the predecessor's marker")
        w.checkpoint()
        assert w.fxr.by_tag(second) == [], "two activations of one alias at the same time"
        assert w.fxr.spawns() == 1
        assert (old / ".git" / "ic-marker").read_text() == "predecessor"              # untouched while live
        w.gate(gate_of[first], w.fxr)
        w.until(lambda: w.fxr.by_tag(second), ip.ROUNDS, what="the successor to start")
        w.gate(gate_of[second], w.fxr)
        rep = w.report(w.fxr, second)
        assert ip.reachability_problems(rep, w.root, rep["head"]) == []
        assert "ic-marker" not in rep["git_files"]
        assert "keep.txt" in rep["home_files"]
    finally:
        w.close()


# --------------------------------------------------- failed staging (P7)

def test_ic_c12_p7_a_failed_staging_keeps_the_old_checkout_and_home(tmp_path, monkeypatch):
    w = pair_world(tmp_path, monkeypatch)
    try:
        w.plan({"fail_from_nth_provision": 4, "mode": "raise"})   # wk r1, rv R1, wk r2 ok; rv R2 never provisions
        w.fxw.queue(commit_entry("a.txt", "one\n", "r1"), commit_entry("b.txt", "two\n", "r2"))
        w.fxr.queue(verdict_entry("rejected", [finding("redo")], tag="R1", report=True, exec=JUNK),
                    verdict_entry("approved", tag="R2", report=True))
        top, wk, rv = ip.start_pair(w)
        w.until(lambda: len(w.fxr.calls()) >= 1 and w.fxw.spawns() >= 2, ip.ROUNDS, what="the second worker round")
        ip.wait_until(lambda: (w.get(rv)["state"] in ("held", "failed"))
                      or len(w.runs(rv)) >= 4, "the re-seed to be given up", timeout=ip.ROUNDS)
        assert w.fxr.spawns() == 1, "the second activation ran although it could not be provisioned"
        rep1 = w.fxr.reports("R1", "start")[0]["report"]
        old = Path(rep1["cwd"])
        assert (old / ".git" / "ic-marker").read_text() == "activation one"
        assert ip.git(old, "rev-parse", "HEAD") == rep1["head"]
        assert not ip.git(old, "status", "--porcelain")
        assert (Path(rep1["home"]) / "keep.txt").read_text() == "home of activation one"
        assert w.get(rv)["state"] != "done"
    finally:
        w.close()


# -------------------------------------------------------- the swap intent (P7)

def test_ic_c12_p7_the_swap_recovers_across_a_scheduler_restart(tmp_path, monkeypatch):
    """The 4th resource (reviewer's second activation) is staged and then its
    provisioning pauses before returning; scheduler and attempt owner die; after restart the
    activation runs on a complete fresh clone, the HOME is kept, and no old
    object is there."""
    w = pair_world(tmp_path, monkeypatch, scheduler={"retries": {"backoff_seconds": [4, 4, 4]}})
    try:
        w.plan({"fail_nth_provision": 4, "mode": "pause_after"})
        w.fxw.queue(commit_entry("a.txt", "one\n", "r1"), commit_entry("b.txt", "two\n", "r2"))
        w.fxr.queue(verdict_entry("rejected", [finding("redo")], tag="R1", report=True, exec=JUNK),
                    verdict_entry("approved", tag="R2", report=True))
        top, wk, rv = ip.start_pair(w)
        staged = Path(str(w.plan_file) + ".staged")
        ip.wait_until(staged.exists, "the completed 4th clone before return", timeout=ip.ROUNDS)
        assert w.fxr.spawns() == 1
        w.crash_paused_provision()
        w.plan({})
        w.start_scheduler()
        w.wait_state(top, "done", timeout=ip.ROUNDS + 10)
        rep = w.report(w.fxr, "R2")
        runs = w.runs(rv)
        assert ip.isolated_clone_problems(rep, w.root, runs[-1]["input_commit"]) == []
        assert "ic-marker" not in rep["git_files"]
        assert "keep.txt" in rep["home_files"]
        ghost, blob = (w.fxr.dir / "junk").read_text().split()
        assert ghost not in rep["objects"] and blob not in rep["objects"]
    finally:
        w.close()
