"""Adversary: retried isolated attempt's launch evidence is recorded onto Attempt 1.

IC-C2: "each attempt records its backend... projected into each row of
get_node().runs[] as executor: {kind, backend, backend_version}".
K5: "Alongside the existing fields, record:
launch_evidence.executor.creation_key and invocation_key, each written before
the operation that uses it; identity and invocation_id, written after the operation
returns them."

In Runner._record_node_launch_evidence (runner.py:4048):
`attempt = next(iter(matching_records(attempts(db), "node_id", managed)), None)`
looks up the attempt by node_id instead of current attempt_id. When an attempt
fails and is retried (TF retry), matching_records returns Attempt 1 first.
The retry's launch evidence (creation_key, invocation_key, identity, invocation_id)
is written into Attempt 1's record, while Attempt 2's record in the Store
receives no runtime executor evidence.

Consequently:
1. Attempt 1's recorded creation_key and identity are overwritten by Attempt 2's.
2. Attempt 2 has no creation_key, invocation_key, identity, or invocation_id
   in its attempt record.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import ic_support as ic  # noqa: E402


@pytest.fixture
def ics(tmp_path, monkeypatch):
    s = ic.IcSched(tmp_path, monkeypatch)
    yield s
    s.close()


def test_ic_adv2_retry_records_evidence_on_its_own_attempt_not_predecessor(ics):
    # Plan a launch failure on attempt 1, then success on attempt 2
    ics.plan({"provision": "raise", "times": 1})
    ics.start()
    node = ics.create()["id"]
    runs = ics.wait_runs(node, 2)
    assert len(runs) == 2

    att1_id = runs[0]["attempt_id"]
    att2_id = runs[1]["attempt_id"]
    assert att1_id != att2_id

    # Attempt 2's run row exists before its provisioning finishes: wait for the
    # post-provision identity (bounded) before reading its evidence.
    ics.wait(lambda: ics.evidence(att2_id).get("executor", {}).get("identity"),
             what="attempt 2's identity in its evidence")
    ev1 = ics.evidence(att1_id).get("executor", {})
    ev2 = ics.evidence(att2_id).get("executor", {})

    # Attempt 2 must have its own creation_key and identity in its own attempt record
    assert ev2.get("creation_key"), f"Attempt 2 missing creation_key in its evidence: {ev2}"
    assert ev2.get("identity"), f"Attempt 2 missing identity in its evidence: {ev2}"

    # Attempt 1 must NOT have Attempt 2's creation_key
    assert ev1.get("creation_key") != ev2.get("creation_key"), (
        f"Attempt 1 creation_key was overwritten by Attempt 2: {ev1.get('creation_key')}"
    )
