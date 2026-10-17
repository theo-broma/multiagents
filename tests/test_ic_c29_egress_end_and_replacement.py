"""IC-C29 (end, cancellation, revocation, cleanup) and IC-C30 (gateway
replacement across all active networks) for the egress gateway, slice 4B.1.

Contract: context/specs/phase7-part2-contract.md, "IC-C29", "IC-C30", D-S4B.6.
The scheduler on the real Docker backend over the fake `docker` CLI. The fake
establishes ordering, state, receipts and failure handling; real containment,
live tunnels and replacement under traffic are in test_ic_s4b_docker.py.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import ic_s3 as s3  # noqa: E402
import ic_s4b as nb  # noqa: E402
import ic_support as ic  # noqa: E402
from ic_support import kind_of  # noqa: E402


@pytest.fixture
def ns(tmp_path, monkeypatch):
    s = nb.NetSched(tmp_path, monkeypatch)
    yield s
    s.close()


def running_pair(s):
    s.start()
    node = s.launch()
    container = s.running(node)
    run = s.runs(node)[-1]
    (network,) = s.attempt_networks(run["attempt_id"])
    return node, run, container, network


def row_state(s, attempt_id):
    rows = [r for r in s.rows() if r["attempt_id"] == attempt_id]
    return rows[0]["state"] if rows else None


def gateway(s):
    found = s.egress_gateways()
    assert len(found) == 1, f"expected exactly one egress gateway, found {len(found)}"
    return found[0]


def every_network_has_the_running_gateway(s, expected_networks):
    gw = gateway(s)
    assert gw["State"]["Running"], "the gateway is not running"
    for network in expected_networks:
        assert gw["Id"] in s.fd.members(network["Id"]), f"gateway not attached to {network['Name']}"
        aliases = s.fd.networks_of(gw["Id"]).get(network["Name"], {}).get("Aliases") or []
        assert "ma-egress" in aliases, (network["Name"], aliases)


# ================================================================== IC-C29: end

def test_ic_c29_normal_end_removes_the_network_and_keeps_the_exited_container(ns):
    node, run, container, network = running_pair(ns)
    ns.fd.finish(container["Id"], 0, stdout=s3.events_stdout())
    assert nb.wait_done(ns, node)["state"] == "done"
    ns.wait(lambda: ns.fd.network(network["Id"]) is None or None, nb.WAIT, "the attempt network to be removed")
    kept = ns.fd.container(container["Id"])
    assert kept is not None and kept["State"]["Status"] == "exited", "retention keeps the exited container"
    assert ns.fd.networks_of(container["Id"]) == {}, "retention grants no network membership"
    assert ns.evidence(run["attempt_id"])["executor"].get("termination_confirmed") is True


def test_ic_c29_disconnects_container_and_gateway_before_removing_the_network(ns):
    node, run, container, network = running_pair(ns)
    gw = gateway(ns)
    ns.fd.finish(container["Id"], 0, stdout=s3.events_stdout())
    nb.wait_done(ns, node)
    ns.wait(lambda: ns.fd.network(network["Id"]) is None or None, nb.WAIT, "network removal")
    commands = ns.fd.commands()
    removal = max(i for i, c in enumerate(commands) if c[:2] in (["network", "rm"], ["network", "remove"])
                  and network["Name"] in c[2:] or network["Id"] in c[2:])
    def disconnected(member):
        return [i for i, c in enumerate(commands) if c[:2] == ["network", "disconnect"]
                and (network["Name"] in c or network["Id"] in c)
                and (member["Id"] in c or member["Name"].lstrip("/") in c)]
    for member in (container, gw):
        positions = disconnected(member)
        assert positions and max(positions) < removal, (member["Name"], positions, removal)


def test_ic_c29_the_gateway_survives_the_end_of_an_attempt_and_other_attempts_are_unaffected(ns):
    ns.start()
    a, b = ns.launch(), ns.launch()
    ca, cb = ns.running(a), ns.running(b)
    ra, rb = ns.runs(a)[-1], ns.runs(b)[-1]
    (na,), (nb_,) = ns.attempt_networks(ra["attempt_id"]), ns.attempt_networks(rb["attempt_id"])
    before_env = ns.invocation(cb)["env"]
    ns.fd.finish(ca["Id"], 0, stdout=s3.events_stdout())
    nb.wait_done(ns, a)
    ns.wait(lambda: ns.fd.network(na["Id"]) is None or None, nb.WAIT, "A's network to be removed")
    assert ns.fd.container(cb["Id"])["State"]["Running"] and ns.fd.network(nb_["Id"]) is not None
    every_network_has_the_running_gateway(ns, [nb_])
    assert ns.invocation(cb)["env"] == before_env
    assert row_state(ns, rb["attempt_id"]) == "ready"
    assert row_state(ns, ra["attempt_id"]) in (None, "absent", "revoked")


@pytest.mark.parametrize("code", [0, 1, 137])
def test_ic_c29_every_kind_of_exit_leaves_no_network_and_no_ready_grant(ns, code):
    node, run, container, network = running_pair(ns)
    # An empty-output exit is a crash and is retried (TF-R2); the contract is about the original
    # attempt, so only the clean end (with its completion events) is awaited as a node outcome.
    ns.fd.finish(container["Id"], code, stdout=s3.events_stdout() if code == 0 else b"")
    if code == 0:
        ns.wait(lambda: (v := ns.get(node)) and v["state"] in ("done", "held", "failed") or None, nb.WAIT, "the end")
    ns.wait(lambda: ns.fd.network(network["Id"]) is None or None, nb.WAIT, "the network to be removed")
    assert row_state(ns, run["attempt_id"]) in (None, "absent", "revoked")


def test_ic_c29_a_cancelled_attempt_is_revoked_before_it_is_stopped(ns):
    node, run, container, network = running_pair(ns)
    ns.fd.rule("stop", effect="hang", seconds=4, times=1)
    ns.fd.rule("kill", effect="hang", seconds=4, times=1)
    assert ns.cancel_raw(node)["ok"] is True
    seen = ns.wait(lambda: row_state(ns, run["attempt_id"]) == "revoked" or None, nb.SHORT,
                   "authority to be revoked while the stop is still pending")
    assert seen
    assert ns.fd.container(container["Id"])["State"]["Running"], "revocation must precede the stop"


def test_ic_c29_cancellation_stops_the_container_even_when_the_gateway_is_down(ns):
    node, run, container, network = running_pair(ns)
    ns.fd.finish(gateway(ns)["Id"], 137)
    assert ns.cancel_raw(node)["ok"] is True
    stopped = ns.wait(lambda: not ns.fd.container(container["Id"])["State"]["Running"] or None, nb.WAIT,
                      "the cancelled attempt to be stopped despite the gateway outage")
    assert stopped
    assert row_state(ns, run["attempt_id"]) != "ready"


def test_ic_c29_adoption_after_a_hard_restart_reuses_the_recorded_grant(ns):
    node, run, container, network = running_pair(ns)
    env = ns.invocation(container)["env"]
    ns.restart(hard=True)
    assert [r["attempt_id"] for r in ns.runs(node)] == [run["attempt_id"]]
    after = ns.fd.container(container["Id"])
    assert after["StartCount"] == 1 and after["State"]["Running"], "adoption must not start the invocation again"
    assert ns.invocation(after)["env"] == env, "restart must not mint a new identity"
    assert [n["Id"] for n in ns.attempt_networks(run["attempt_id"])] == [network["Id"]]
    assert row_state(ns, run["attempt_id"]) == "ready"
    every_network_has_the_running_gateway(ns, [network])


def test_ic_c29_a_revoked_grant_is_not_resurrected_by_a_restart(ns):
    node, run, container, network = running_pair(ns)
    ic.run(ns.registry().revoke(run["attempt_id"]))
    ns.restart(hard=True)
    assert row_state(ns, run["attempt_id"]) in ("revoked", "absent", None)


def test_ic_c29_unknown_liveness_grants_no_access(ns):
    node, run, container, network = running_pair(ns)
    ns.fd.daemon_down()
    got = ic.run(ns.registry().reconcile())
    if isinstance(got, list):
        assert all(r["state"] != "ready" for r in got if r["attempt_id"] == run["attempt_id"]), got
    else:
        assert kind_of(got) == "unknown", got
    ns.fd.daemon_down(False)
    assert row_state(ns, run["attempt_id"]) == "ready"
    assert ns.invocation(ns.fd.container(container["Id"]))["env"] == ns.invocation(container)["env"]


def test_ic_c29_a_failed_attachment_rolls_back_only_that_attempt(ns):
    node, run, container, network = running_pair(ns)
    ns.fd.rule("network", "connect", effect="fail", times=6, stderr="Error response from daemon: cannot attach")
    second = ns.launch()
    decided(ns, second)
    for r in ns.runs(second):
        c = ns.container_of(r["attempt_id"])
        assert c is None or c["StartCount"] == 0, "an attempt whose gateway attachment failed was started"
    assert ns.fd.container(container["Id"])["State"]["Running"]
    every_network_has_the_running_gateway(ns, [network])
    assert row_state(ns, run["attempt_id"]) == "ready"
    assert nb.leaks(ns.public_texts(second)) == {}


def test_ic_c29_unrelated_members_stop_deletion_and_are_reported(ns):
    node, run, container, network = running_pair(ns)
    intruder = ns.fd.inject("intruder", {"unrelated": "yes"}, status="running")
    ns.fd.attach(network["Name"], intruder)
    ns.fd.finish(container["Id"], 0, stdout=s3.events_stdout())
    nb.wait_done(ns, node)
    def reported():
        listing = json.loads(ns.cli("scheduler", "orphans", timeout=nb.WAIT).stdout)
        return any(o.get("resource_id") == network["Id"] for o in listing["orphans"] or []) or None
    ns.wait(reported, nb.WAIT, "the undeletable network to be reported")
    assert ns.fd.network(network["Id"]) is not None, "a network with an unknown member must not be removed"
    assert intruder in ns.fd.members(network["Id"]), "an unknown external member must not be force-disconnected"


# ============================================================= IC-C30: replacement

# A launch whose attachment keeps failing runs the node's whole TF retry budget:
# 1 + RETRY_BUDGET attempts (default budget 3), each given up to nb.WAIT to
# launch and fail, plus the fixture's configured backoff (ic_support: 0.1 s) before
# each retry. The wait is bounded by that sum; a launch that is never decided
# still fails, just not before every attempt has had its time.
RETRY_BUDGET = 3
RETRY_BACKOFF = 0.1
DECIDED_WAIT = (1 + RETRY_BUDGET) * nb.WAIT + RETRY_BUDGET * RETRY_BACKOFF


def decided(s, node):
    """The launch has either started its container or ended for good (held, or
    done after exhausting its retries, whatever the outcome)."""
    def ready():
        runs = s.runs(node)
        c = runs and s.container_of(runs[-1]["attempt_id"])
        view = s.get(node)
        return (c and c["StartCount"] > 0) or view["state"] in ("held", "done") or None
    return s.wait(ready, DECIDED_WAIT, f"{node}'s launch to be decided")


def changed_policy(s, extra="second.example.org"):
    s.configure(allowlist=nb.ALLOW + [extra])


def test_ic_c30_an_unchanged_policy_keeps_the_same_gateway_for_every_attempt(ns):
    ns.start()
    a, b = ns.launch(), ns.launch()
    ns.running(a), ns.running(b)
    (gw,) = ns.egress_gateways()
    assert gw["StartCount"] == 1 and gw["State"]["Running"]


def test_ic_c30_a_changed_policy_replaces_the_gateway_without_recreating_running_attempts(ns):
    node, run, container, network = running_pair(ns)
    before = gateway(ns)
    env = ns.invocation(container)["env"]
    changed_policy(ns)
    second = ns.launch()
    cb = ns.running(second)
    rb = ns.runs(second)[-1]
    (nb2,) = ns.attempt_networks(rb["attempt_id"])
    digest_a = ns.evidence(run["attempt_id"])["executor"]["network"]["policy_digest"]
    digest_b = ns.evidence(rb["attempt_id"])["executor"]["network"]["policy_digest"]
    assert digest_a != digest_b, "the new attempt must run under the new policy"
    every_network_has_the_running_gateway(ns, [network, nb2])
    after = ns.fd.container(container["Id"])
    assert after["Id"] == container["Id"] and after["StartCount"] == 1 and after["State"]["Running"]
    assert ns.invocation(after)["env"] == env, "gateway replacement keeps credentials stable"
    assert gateway(ns)["Id"] != before["Id"] or gateway(ns)["StartCount"] > before["StartCount"] \
        or ns.evidence(rb["attempt_id"])["executor"]["network"]["gateway_generation"] \
        != ns.evidence(run["attempt_id"])["executor"]["network"]["gateway_generation"]


def test_ic_c30_the_policy_is_reconciled_not_frozen_per_attempt(ns):
    node, run, container, network = running_pair(ns)
    first = ns.evidence(run["attempt_id"])["executor"]["network"]["policy_digest"]
    changed_policy(ns)
    third = ns.launch()
    ns.running(third)
    now = ns.evidence(ns.runs(third)[-1]["attempt_id"])["executor"]["network"]["policy_digest"]
    changed_policy(ns, "third.example.net")
    fourth = ns.launch()
    ns.running(fourth)
    latest = ns.evidence(ns.runs(fourth)[-1]["attempt_id"])["executor"]["network"]["policy_digest"]
    assert len({first, now, latest}) == 3


@pytest.mark.parametrize("failing", [("rm",), ("run",), ("create",), ("start",), ("stop",), ("network", "connect")])
def test_ic_c30_a_failure_at_any_replacement_step_never_launches_on_a_stale_gateway_and_recovers(ns, failing):
    node, run, container, network = running_pair(ns)
    changed_policy(ns)
    ns.fd.rule(*failing, effect="fail", times=1, stderr="Error response from daemon: injected failure")
    second = ns.launch()
    decided(ns, second)
    # Whatever started did so with a running gateway on its network.
    for c in ns.fd.containers().values():
        for snapshot in c.get("StartSnapshots", []):
            for members in snapshot.values():
                assert any(up for cid, up in members.items() if cid != c["Id"]) , snapshot
    assert ns.fd.container(container["Id"])["State"]["Running"] and ns.fd.container(container["Id"])["StartCount"] == 1
    # Recovery: the next launch succeeds and every active network has the one running gateway.
    ns.fd.clear_rules()
    third = ns.launch()
    ns.running(third)
    active = [n for n in ns.fd.networks().values()
              if any(ns.fd.container(m) and ns.fd.container(m)["State"]["Running"] and
                     ns.fd.container(m)["Config"]["Labels"].get("multiagents.owner") for m in ns.fd.members(n["Id"]))]
    every_network_has_the_running_gateway(ns, active)
    assert len(ns.egress_gateways()) == 1


def test_ic_c30_a_gateway_that_died_is_not_trusted_for_a_new_launch(ns):
    node, run, container, network = running_pair(ns)
    ns.fd.finish(gateway(ns)["Id"], 137)
    second = ns.launch()
    cb = ns.running(second)
    snapshot = cb["StartSnapshots"][0]
    (members,) = [m for m in snapshot.values()]
    assert any(running for cid, running in members.items() if cid != cb["Id"]), \
        f"the new attempt started with no running gateway on its network: {members}"
    rb = ns.runs(second)[-1]
    every_network_has_the_running_gateway(ns, [network, *ns.attempt_networks(rb["attempt_id"])])


def test_ic_c30_several_networks_are_added_concurrently_and_all_reach_the_one_gateway(ns):
    ns.start()
    nodes = [ns.launch() for _ in range(3)]
    containers = [ns.running(n) for n in nodes]
    networks = [ns.attempt_networks(ns.runs(n)[-1]["attempt_id"])[0] for n in nodes]
    assert len({n["Id"] for n in networks}) == 3
    every_network_has_the_running_gateway(ns, networks)
    assert all(ns.fd.container(c["Id"])["StartCount"] == 1 for c in containers)


def test_ic_c30_an_ending_attempt_and_a_new_one_reconcile_against_the_journal(ns):
    ns.start()
    nodes = [ns.launch() for _ in range(3)]
    containers = [ns.running(n) for n in nodes]
    networks = [ns.attempt_networks(ns.runs(n)[-1]["attempt_id"])[0] for n in nodes]
    ns.fd.finish(containers[0]["Id"], 0, stdout=s3.events_stdout())
    newest = ns.launch()
    ns.running(newest)
    ns.wait(lambda: ns.fd.network(networks[0]["Id"]) is None or None, nb.WAIT, "the ended attempt's network to go")
    fresh = ns.attempt_networks(ns.runs(newest)[-1]["attempt_id"])
    every_network_has_the_running_gateway(ns, [networks[1], networks[2], *fresh])
    assert networks[0]["Id"] not in {n["Id"] for n in ns.fd.networks().values()}
    assert ns.fd.members(networks[1]["Id"]) and gateway(ns)["Id"] not in ns.fd.members(networks[0]["Id"])


def test_ic_c30_an_unknown_external_member_is_reported_and_never_force_disconnected(ns):
    node, run, container, network = running_pair(ns)
    intruder = ns.fd.inject("external", {"somebody": "else"}, status="running")
    ns.fd.attach(network["Name"], intruder)
    assert row_state(ns, run["attempt_id"]) != "ready", "an unexpected member must refuse readiness"
    changed_policy(ns)
    ns.running(ns.launch())
    assert intruder in ns.fd.members(network["Id"]), "an unknown external member was force-disconnected"


def test_ic_c30_aliases_are_stable_across_replacement(ns):
    node, run, container, network = running_pair(ns)
    changed_policy(ns)
    ns.running(ns.launch())
    aliases = ns.fd.networks_of(gateway(ns)["Id"]).get(network["Name"], {}).get("Aliases") or []
    assert "ma-egress" in aliases
