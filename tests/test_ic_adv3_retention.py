"""Adversary 3 — IC-C11 retention refs after 3127f4f7.

3127f4f7 omits `refs/heads/node-results/<attempt>` for a successful
non-writing isolated attempt that returned its input unchanged. Every other
case must keep the ref: RR-R3 recovery reads it back. These tests pin the
failed-run case for a non-writing isolated attempt whose result equals its
input — the one case where only `run.status == "done"` separates "omit" from
"keep" (dropping that clause survives the existing suite).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import ic_prov as ip  # noqa: E402

FINAL = ("done", "held", "failed", "cancelled")
# Provider error text that TF-R1 classes `other`: final, never auto-retried.
FINAL_ERROR = "sys.stderr.write('Error: invalid task syntax\\n'); sys.stderr.flush()\n"


@pytest.fixture
def w(tmp_path, monkeypatch):
    world = ip.make_world(tmp_path, monkeypatch)
    world.pair()
    world.agent("ro", "fxw", writes=False)
    yield world
    world.close()


def test_ic_adv3_a_failed_nonwriting_isolated_attempt_keeps_its_retention_ref_for_recovery(w):
    w.start_scheduler()
    node = w.simple("RO-FAIL", "ro", fx={"exit": 1, "exec": FINAL_ERROR})
    view = w.until(lambda: w.get(node) if w.get(node)["state"] in FINAL else None, ip.WAIT,
                   what="the failed non-writing attempt to settle")
    assert view["state"] == "done" and view["outcome"] == "failed", (view["state"], view.get("outcome"),
                                                                     view.get("hold"))
    attempt = w.runs(node)[-1]["attempt_id"]
    record = w.attempt(attempt)
    assert record.get("writes") is False, record.get("writes")
    assert (record.get("launch_evidence") or {}).get("executor", {}).get("kind") == "isolated"
    result = record.get("result") or {}
    assert result.get("commit") == record["input_commit"], result
    # Failed-run recovery reads this ref: it must exist for every non-`done` capture.
    assert w.host_refs().get(f"refs/heads/node-results/{attempt}") == record["input_commit"], \
        sorted(r for r in w.host_refs() if "node-results" in r)

    spawns = len(w.fxw.calls())
    reply = w.root_op("relaunch_node", node)
    assert reply.get("ok"), reply
    w.until(lambda: len(w.fxw.calls()) > spawns, ip.WAIT, what="the recovered relaunch to start")
    assert (w.get(node).get("hold") or {}).get("reason") != "recovery_conflict", w.get(node).get("hold")


def test_ic_adv3_a_pre_upgrade_attempt_without_a_writes_record_keeps_its_retention_ref(w):
    """An attempt admitted before 3127f4f7 carries no `writes` field. Its
    capture must keep the ref even when it is isolated, done and unchanged
    (only an explicit `writes: False` may omit it)."""
    from multiagents.scheduler.engine import attempts, save_attempt
    from multiagents.scheduler.store import Store

    w.start_scheduler()
    node = w.simple("RO-OLD", "ro", fx={"gate": "g"})
    w.wait_spawn("RO-OLD", w.fxw)
    attempt = w.runs(node)[-1]["attempt_id"]
    with Store(w.root).transaction() as db:
        record = attempts(db)[attempt]
        assert record.pop("writes") is False
        save_attempt(db, record)
    w.gate("g", w.fxw)
    view = w.until(lambda: w.get(node) if w.get(node)["state"] in FINAL else None, ip.WAIT,
                   what="the pre-upgrade attempt to settle")
    assert view["state"] == "done", (view["state"], view.get("hold"))
    record = w.attempt(attempt)
    assert "writes" not in record and (record.get("result") or {}).get("commit") == record["input_commit"]
    assert w.host_refs().get(f"refs/heads/node-results/{attempt}") == record["input_commit"]
