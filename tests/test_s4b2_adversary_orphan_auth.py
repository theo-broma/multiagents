"""Adversary, slice 4B.2: orphan network removal refuses to remove networks attached to ma-auth.

HOLE (severity HIGH, unremovable orphan networks and stale auth grants):
`multiagents scheduler remove-orphan-network NETWORK_ID` (IC-C31, K4B2.1) fails to remove
any orphan network that had the auth gateway attached.

In `src/multiagents/scheduler/orphans.py`:
`members()` discovers gateway infrastructure by reading only `gateways/authority.json`
(the egress gateway receipt):
    gateway_id = (authority().get("gateway") or {}).get("id")
It compares each container on the network against `gateway_id`:
    if member == gateway_id and labels.get("multiagents.gateway"):
        allowed.append(member)
        continue
When `ma-auth` is attached to the network (K4B2.1: each service has its own gateway
container, label, alias, and IC-C30 receipt recorded in `gateways/auth/authority.json`),
its container id does not match `gateway_id` (which is `ma-egress`). The fallback check:
    if not current_attempt or labels.get("multiagents.owner") != current_attempt["attempt_id"]:
        return None
fails because `ma-auth` has no `multiagents.owner` label (it has `multiagents.gateway: auth`).
As a result, `members()` returns `None`, and `remove-orphan-network` refuses removal with:
    {"error": "not_orphan", "reason": "network contains an unexpected or live member"}
making every orphan network that used Anthropic auth permanently unremovable.

Furthermore, `remove-orphan-network` revokes grants with:
    registry = GatewayRegistry(paths, config=config, providers=config.providers)
which defaults to `service="egress"` and never revokes the stale grant on `ma-auth`.

IC-C31: `multiagents scheduler remove-orphan-network NETWORK_ID` "rechecks ownership,
journal and membership before mutation. Revoke any stale grant, then disconnect only
confirmed owned ended-attempt/gateway memberships and remove the network."
K4B2.1: "cleanup checks the ownership of both gateways' memberships."
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import ic_s3 as s3  # noqa: E402
import ic_s4b as nb  # noqa: E402
import ic_s4b2 as ab  # noqa: E402


@pytest.fixture
def asd(tmp_path, monkeypatch):
    s = ab.AuthSched(tmp_path, monkeypatch)
    yield s
    s.close()


def test_s4b2_adversary_remove_orphan_network_accepts_auth_gateway_attachment(asd):
    """HIGH: remove-orphan-network refuses networks attached to ma-auth as unexpected members."""
    asd.start()
    node = asd.launch()
    container = asd.running(node)
    run = asd.runs(node)[-1]
    auth_gateway_id = asd.gateways("auth")[0]["Id"]
    (network,) = asd.attempt_networks(run["attempt_id"])

    # Simulate an ended attempt whose network removal failed at attempt exit
    try:
        asd.fd.rule("network", "rm", effect="fail", stderr="Error response from daemon: stuck")
        asd.fd.rule("network", "remove", effect="fail", stderr="Error response from daemon: stuck")
        asd.fd.finish(container["Id"], 0, stdout=s3.events_stdout())
        nb.wait_done(asd, node)

        # The stuck network is discovered and listed as an attempt orphan
        reply = asd.cli("scheduler", "orphans", timeout=nb.WAIT)
        assert reply.returncode == 0, reply.stderr
        listing = json.loads(reply.stdout)
        assert any(o.get("resource_id") == network["Id"] for o in (listing.get("orphans") or [])), listing
    finally:
        asd.fd.clear_rules()

    asd.fd.attach(network["Name"], auth_gateway_id)

    # Operator explicitly removes the confirmed orphan network
    reply = asd.cli("scheduler", "remove-orphan-network", network["Id"], timeout=nb.WAIT * 2)
    try:
        val = json.loads(reply.stdout)
    except ValueError:
        val = {"stdout": reply.stdout, "stderr": reply.stderr}

    assert reply.returncode == 0, (
        f"remove-orphan-network failed with code {reply.returncode}: {val}; "
        f"orphans.py fails to identify ma-auth as a known gateway member"
    )
    assert val.get("ok") is True and val.get("state") == "absent", val
    assert asd.fd.network(network["Id"]) is None, "the orphan network was not removed"

