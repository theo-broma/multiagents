"""IC-C13 — migration and coexistence: an upgrade never replaces an active
legacy checkout; an alias moves to a seeded repository at its next eligible
activation under `isolated`; dirty holds survive; a run returning to
`local`/`docker` gets today's legacy checkout (P9); pending TF retries keep
their recorded selection (IC-C2).

The worker/reviewer loop with alias `B` of `ic_prov.PAIR`; the executor kind is
switched live (`IcWorld.set_executor`, IC-C1: it applies to attempts not yet
recorded). `local` stands for the legacy mode (Docker needs a daemon).

"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import ic_prov as ip  # noqa: E402
from nc_fixture.m4_world import commit_entry, finding  # noqa: E402
from ic_prov import text_verdict_entry as verdict_entry  # noqa: E402
JUNK = ip.JUNK


def world(tmp_path, monkeypatch, kind, **kw):
    w = ip.make_world(tmp_path, monkeypatch, kind=kind, **kw)
    w.pair()
    return w


def kinds(w, node):
    return [r["executor"]["kind"] for r in w.runs(node)]


def test_ic_c13_a_local_alias_is_reseeded_at_its_next_activation_under_isolated(tmp_path, monkeypatch):
    w = world(tmp_path, monkeypatch, "local")
    try:
        w.fxw.queue(commit_entry("a.txt", "one\n", "r1"), commit_entry("b.txt", "two\n", "r2"))
        w.fxr.queue(verdict_entry("rejected", [finding("redo")], tag="R1", report=True, report_end=True, gate="g1", exec=JUNK),
                    verdict_entry("approved", tag="R2", report=True))
        top, wk, rv = ip.start_pair(w)
        w.wait_spawn("R1", w.fxr)
        w.set_executor("isolated")                       # the upgrade, while the legacy checkout is live
        w.checkpoint()
        w.gate("g1", w.fxr)
        w.wait_state(top, "done", timeout=ip.ROUNDS)
        assert kinds(w, rv) == ["local", "isolated"]
        r1, r2 = w.report(w.fxr, "R1"), w.report(w.fxr, "R2")
        assert r1["home"] != r2["home"]
        assert "keep.txt" in w.report(w.fxr, "R1", "end")["home_files"]
        assert "keep.txt" not in r2["home_files"]
        assert r1["alternates"], "the legacy activation should have been a shared clone"
        second = w.runs(rv)[1]
        assert ip.isolated_clone_problems(r2, w.root, second["input_commit"]) == []
        assert "ic-marker" not in r2["git_files"]                  # the old alias repository is not reused
        ghost, blob = (w.fxr.dir / "junk").read_text().split()
        assert ghost not in r2["objects"] and not [r for r in r2["refs"] if "junk" in r]
        first, again = w.fxr.calls()
        # 4A (D-S4A.3/4/5): the provider declares no session, so mode `none`: no resume.
        assert again["resume"] is None
        assert w.runs(rv)[1]["session_resume"] == "unsupported"
    finally:
        w.close()


def test_ic_c13_an_active_legacy_checkout_is_not_replaced_by_the_upgrade(tmp_path, monkeypatch):
    w = world(tmp_path, monkeypatch, "local")
    try:
        w.fxw.queue(commit_entry("a.txt", "one\n", "r1"))
        w.fxr.queue(verdict_entry("approved", tag="R1", report=True, gate="g1",
                                  early="open('.git/ic-marker', 'w').write('legacy')"))
        top, wk, rv = ip.start_pair(w)
        w.wait_spawn("R1", w.fxr)
        cwd = Path(w.fxr.calls()[0]["cwd"])
        ip.wait_until(lambda: (cwd / ".git" / "ic-marker").exists(), "the marker")
        w.set_executor("isolated")
        w.restart_scheduler()                              # the upgrade itself
        w.checkpoint()
        assert (cwd / ".git" / "ic-marker").read_text() == "legacy"
        assert (cwd / ".git" / "objects" / "info" / "alternates").exists()
        w.gate("g1", w.fxr)
        assert w.wait_state(top, "done", timeout=ip.ROUNDS)["outcome"] == "approved"
        assert kinds(w, rv) == ["local"]
    finally:
        w.close()


def test_ic_c13_an_earlier_dirty_hold_survives_the_upgrade_and_the_checkout_is_untouched(tmp_path, monkeypatch):
    w = world(tmp_path, monkeypatch, "local")
    try:
        w.fxw.queue(commit_entry("a.txt", "one\n", "r1"), commit_entry("b.txt", "two\n", "r2"))
        w.fxr.queue(verdict_entry("rejected", [finding("redo")], tag="R1", report=True,
                                  leave={"scratch.txt": "left behind\n"}),
                    {"tag": "CLEAN"},                       # the one resumed cleanup run (TB2-R6) pops this
                    verdict_entry("approved", tag="R2", report=True))
        top, wk, rv = ip.start_pair(w)
        held = w.wait_held(rv, "dirty_worktree", timeout=ip.ROUNDS)
        old = Path(w.fxr.calls()[0]["cwd"])
        spawns = w.fxr.spawns()
        w.set_executor("isolated")
        w.restart_scheduler()
        w.checkpoint()
        again = w.get(rv)
        assert again["state"] == "held" and (again["hold"] or {}).get("reason") == "dirty_worktree"
        assert (again["hold"] or {}).get("detail") == (held["hold"] or {}).get("detail")
        assert Path((again["hold"] or {})["detail"]).exists()
        assert (old / "scratch.txt").read_text() == "left behind\n"
        assert ip.git(old, "rev-parse", "HEAD") == w.fxr.calls()[0]["head"]
        assert w.fxr.spawns() == spawns and w.fxr.reports("R2") == []
    finally:
        w.close()


def test_ic_c13_p9_returning_to_local_gives_the_legacy_checkout_never_the_seeded_repository(tmp_path, monkeypatch):
    w = world(tmp_path, monkeypatch, "isolated")
    try:
        w.fxw.queue(commit_entry("a.txt", "one\n", "r1"), commit_entry("b.txt", "two\n", "r2"))
        w.fxr.queue(verdict_entry("rejected", [finding("redo")], tag="R1", report=True, report_end=True, gate="g1", exec=JUNK),
                    verdict_entry("approved", tag="R2", report=True))
        top, wk, rv = ip.start_pair(w)
        w.wait_spawn("R1", w.fxr)
        w.set_executor("local")                           # back to the legacy mode
        w.gate("g1", w.fxr)
        w.wait_state(top, "done", timeout=ip.ROUNDS)
        assert kinds(w, rv) == ["isolated", "local"]
        r1, r2 = w.report(w.fxr, "R1"), w.report(w.fxr, "R2")
        assert r1["home"] != r2["home"]
        assert "keep.txt" in w.report(w.fxr, "R1", "end")["home_files"]
        assert "keep.txt" not in r2["home_files"]
        assert not r1["alternates"]                          # the control: R1 was a seeded clone
        assert r2["alternates"] and str(w.root / ".git" / "objects") in r2["alternates"]
        assert "ic-marker" not in r2["git_files"] and not [r for r in r2["refs"] if "junk" in r]
        assert str(w.backend_root) not in r2["cwd"]
    finally:
        w.close()


def test_ic_c13_pending_retries_keep_their_isolated_selection_across_the_upgrade_back(tmp_path, monkeypatch):
    w = world(tmp_path, monkeypatch, "isolated")
    try:
        w.plan({"fail_from_nth_provision": 4, "mode": "raise"})
        w.fxw.queue(commit_entry("a.txt", "one\n", "r1"), commit_entry("b.txt", "two\n", "r2"))
        w.fxr.queue(verdict_entry("rejected", [finding("redo")], tag="R1", report=True),
                    verdict_entry("approved", tag="R2", report=True))
        top, wk, rv = ip.start_pair(w)
        ip.wait_until(lambda: len(w.runs(rv)) >= 2, "the first failing re-seed attempt", timeout=ip.ROUNDS)
        w.set_executor("local")
        ip.wait_until(lambda: len(w.runs(rv)) >= 3 or w.get(rv)["state"] == "held", "a retry", timeout=ip.ROUNDS)
        assert set(kinds(w, rv)) == {"isolated"}
        assert w.fxr.spawns() == 1                           # no reviewer was ever started in the shared mode
    finally:
        w.close()
