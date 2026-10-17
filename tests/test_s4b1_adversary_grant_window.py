"""Adversary, slice 4B.1: a granted attempt that has not started yet loses its grant.

HOLE (severity MEDIUM, silent loss of an issued grant / concurrent launches):
`GatewayRegistry._desired` (src/multiagents/scheduler/gateways.py) keeps an
attempt other than the one being prepared only if its container is already
`Running`. `grant()` publishes the gateway policy from `_desired`, and the
executor calls `grant()` before `backend.run` starts the container (IC-C25:
"grant follows confirmed provision and precedes run"). So when attempt B has
been granted but its `docker start` has not completed, any later publication
(attempt C's grant) rewrites `policy.json` WITHOUT B's grant. B then starts
with credentials the gateway no longer accepts: every request gets 407 until
some unrelated later publication happens to include it again. Nothing records
the loss; B's reconcile row and evidence still claim the grant.

IC-C29 / IC-C30: the gateway's authority is reconstructed "from host-recorded
active attempts"; publishing one attempt's grant must not withdraw another's
issued, unrevoked grant.

Observed through the fake CLI and the host's own policy file, which is exactly
what the trusted gateway enforces (`/authority/policy.json`).
"""
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import ic_s4b as nb  # noqa: E402

HANG = 5


@pytest.fixture
def ns(tmp_path, monkeypatch):
    s = nb.NetSched(tmp_path, monkeypatch)
    yield s
    s.close()


def authority_root(s):
    from multiagents.paths import ProjectPaths
    return ProjectPaths(s.root).scheduler / "gateways"


def authority(s):
    return json.loads((authority_root(s) / "authority.json").read_text())


def published_grants(s):
    return set(json.loads((authority_root(s) / "runtime" / "policy.json").read_text())["grants"])


def wait(predicate, timeout, what):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(.05)
    raise AssertionError("timed out waiting for " + what)


def test_s4b1_adversary_a_sibling_grant_does_not_withdraw_a_granted_attempt_that_is_still_starting(ns):
    ns.start()
    first = ns.launch()
    ns.running(first)

    # The next `docker start` (attempt B's) stalls; B is granted and Created meanwhile.
    # (The fake's `hang` then fails that start, so assert inside the window.)
    ns.fd.rule("start", effect="hang", seconds=HANG, times=1)
    second = ns.launch()
    b = wait(lambda: (ns.runs(second) or [None])[-1], nb.WAIT, "B's attempt")["attempt_id"]
    b_grant = wait(lambda: authority(ns)["attempts"].get(b, {}).get("grant_id"), nb.WAIT, "B's grant")
    wait(lambda: b_grant in published_grants(ns), nb.WAIT, "B's grant to be published")
    assert not ns.container_of(b)["State"]["Running"], "B started before the window could be observed"

    # Any other host publication while B is still Created: here attempt A's
    # repeated, idempotent grant (the same recorded identity, IC-C25).
    a = ns.runs(first)[-1]["attempt_id"]
    a_container = ns.container_of(a)
    from multiagents.executor.lifecycle import ResourceIdentity
    import ic_support as ic
    reply = ic.run(ns.registry().grant(a, ResourceIdentity(a_container["Id"])))
    assert type(reply).__name__ == "GatewayGrant", reply

    # Observed while B's `docker start` is still in flight: B is Created, granted
    # and unrevoked, and its start is about to succeed with these credentials.
    assert not ns.container_of(b)["State"]["Running"], "the window closed before it could be observed"
    assert not authority(ns)["attempts"][b].get("revoked"), "B was revoked: not the window under test"
    assert b_grant in published_grants(ns), (
        "attempt B's issued, unrevoked grant was dropped from the gateway policy by a sibling's "
        "publication while B's container was Created; B then starts with credentials the gateway refuses (407)")
