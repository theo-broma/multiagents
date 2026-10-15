"""IC-C8 guard for slice 2 (D-S2.1): with `executor.kind: local` the checkout is
still today's shared clone and the alias is still reset in place; no seed is
built or recorded. Characterisation only: these must stay green and must not be
edited to fit the implementation. (The Docker path needs a daemon and the
container image; it is not exercised here.)
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import ic_prov as ip  # noqa: E402
from nc_fixture.m4_world import commit_entry, finding, verdict_entry  # noqa: E402
JUNK = ip.JUNK


def test_ic_c8_a_local_attempt_still_runs_in_a_shared_clone_without_a_seed(tmp_path, monkeypatch):
    w = ip.make_world(tmp_path, monkeypatch, kind="local")
    w.pair()
    try:
        w.start_scheduler()
        node = w.simple("L1", "wk", fx={"report": True, "write": {"x.txt": "x\n"}, "commit": "work"})
        view = w.wait_state(node, "done", timeout=ip.WAIT)
        rep = w.report(w.fxw, "L1")
        assert rep["alternates"] and str(w.root / ".git" / "objects") in rep["alternates"]
        assert rep["git_is_dir"] and not rep["commondir"]
        run = w.runs(node)[0]
        if "executor" in run:                                 # the IC-C2 projection, once slice 1 is merged
            assert run["executor"]["kind"] == "local" and run["executor"]["backend"] == ""
        evidence = w.evidence(run["attempt_id"]).get("executor") or {}
        assert "seed" not in evidence
        assert not [r for r in w.host_refs() if r.startswith("refs/multiagents/seeds/")]
        assert w.host_has(view["generations"][0]["commit"])
    finally:
        w.close()


def test_ic_c8_a_local_alias_is_still_reset_in_place(tmp_path, monkeypatch):
    w = ip.make_world(tmp_path, monkeypatch, kind="local")
    w.pair()
    try:
        w.fxw.queue(commit_entry("a.txt", "one\n", "r1"), commit_entry("b.txt", "two\n", "r2"))
        w.fxr.queue(verdict_entry("rejected", [finding("redo")], tag="R1", report=True, exec=JUNK),
                    verdict_entry("approved", tag="R2", report=True))
        top, wk, rv = ip.start_pair(w)
        w.wait_state(top, "done", timeout=ip.ROUNDS)
        r1, r2 = w.report(w.fxr, "R1"), w.report(w.fxr, "R2")
        assert r1["cwd"] == r2["cwd"] and r2["alternates"]
        assert "ic-marker" in r2["git_files"]                 # the same .git, reset in place (today's behaviour)
        assert [r for r in r2["refs"] if "junk" in r]         # and its old refs are still there
    finally:
        w.close()
