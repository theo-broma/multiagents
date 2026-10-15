"""Adversary (nd-750860bb): relaunching an isolated run inside one attempt.

`IsolatedExecutor.start` derives both keys from the owner only
(`resource-<owner>`, `invocation-<owner>`), and the owner of a relaunch inside
the same attempt repeats. Observed sequence through the scheduler:

  launch    owner = attempt id   -> resource-<attempt>, recorded
  steer #1  owner = run id       -> resource-<run>; the evidence of the first
                                    resource is overwritten (K5) and it is left
                                    with no record naming it
  steer #2  owner = run id again -> same creation key returns the old resource,
                                    same invocation key with a different spec
                                    -> IdentityConflict; the run has already
                                    been stopped and is not relaunched
"""
from __future__ import annotations

import json
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


def launched(s):
    s.fake.script(steps=[{"cost": 0, "sleep": 30}])
    s.start()
    node = s.create()["id"]
    run = s.wait_runs(node, 1)[0]
    s.wait(lambda: s.evidence(run["attempt_id"]).get("executor", {}).get("invocation_id"),
           timeout=ic.WAIT * 2, what="the first invocation to be recorded")
    return node, run


def steer(s, run_id, message):
    reply = s.rpc("steer_run", {"run_id": run_id, "message": message})
    command = reply["result"]["command_id"]
    answer = s.wait(lambda: (s.rpc("steer_result", {"run_id": run_id, "command_id": command})
                             .get("result") or {}).get("result"),
                    timeout=40, what="the steer result")
    return answer


def test_ic_adv_an_isolated_run_can_be_steered_twice(ics):
    _, run = launched(ics)
    first = steer(ics, run["run_id"], "continue once")
    assert first.get("steered") is True, first
    second = steer(ics, run["run_id"], "continue twice")
    assert second.get("steered") is True and not second.get("error"), second


def test_ic_adv_a_steer_leaves_no_resource_unrecorded_in_the_attempts_evidence(ics):
    _, run = launched(ics)
    original = ics.evidence(run["attempt_id"])["executor"]["identity"]
    assert steer(ics, run["run_id"], "continue").get("steered") is True
    evidence = json.dumps(ics.evidence(run["attempt_id"]))
    held = [identity.id for identity in ics.owned()]
    assert original not in held or original in evidence, (
        f"resource {original} is still held by the backend but no evidence names it: {evidence}")
