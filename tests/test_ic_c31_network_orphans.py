"""IC-C31 (slice 4B.1): network orphans and explicit removal.

Contract: context/specs/phase7-part2-contract.md, "IC-C31" (IR-R8, K17.5).
The scheduler on the real Docker backend over the fake `docker` CLI, through
`scheduler_status`, `multiagents scheduler orphans` and
`multiagents scheduler remove-orphan-network`. Real orphan creation and stale
attachments are in test_ic_s4b_docker.py.
"""
import datetime
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import ic_s3 as s3  # noqa: E402
import ic_s4b as nb  # noqa: E402
import ic_support as ic  # noqa: E402

ROWS = {"resource_kind", "resource_id", "network_id", "container_id", "name", "labels", "created_at", "project",
        "attempt_id", "reason"}
CONTAINER_KEYS = {"container_id", "name", "labels", "created_at", "project", "attempt_id", "reason"}
CREATED = "2026-01-02T03:04:05.000000Z"


@pytest.fixture
def ns(tmp_path, monkeypatch):
    s = nb.NetSched(tmp_path, monkeypatch)
    yield s
    s.close()


def listing(s):
    reply = s.cli("scheduler", "orphans", timeout=nb.WAIT)
    assert reply.returncode == 0, reply.stderr
    value = json.loads(reply.stdout)
    assert set(value) == {"orphans", "orphans_error"}
    return value


def unrecorded_network(s, *, project=None, **kw):
    return s.fd.inject_network("orphan-" + os.urandom(3).hex(), {
        "multiagents.project": project or s.slug, "multiagents.owner": "att_unrecorded",
        "multiagents.creation_key": "key-unrecorded"}, created=kw.pop("created", CREATED), **kw)


def remove(s, network_id, env=None):
    proc = subprocess.run([sys.executable, "-m", "multiagents.cli", "--path", str(s.root), "scheduler",
                           "remove-orphan-network", network_id], capture_output=True, text=True, timeout=nb.WAIT * 2,
                          cwd=s.root, env={**os.environ, **(env or {})})
    try:
        value = json.loads(proc.stdout)
    except ValueError:
        value = {"unparsed": proc.stdout, "stderr": proc.stderr}
    return proc.returncode, value


def refused(s, network_id, errors, env=None):
    code, value = remove(s, network_id, env)
    assert code != 0, value
    assert value.get("ok") is False and value.get("network_id") == network_id, value
    assert value.get("error") in errors, value
    assert isinstance(value.get("reason"), str) and value["reason"]
    assert nb.leaks({"reason": value["reason"]}) == {}
    return value


def ended_attempt_with_stuck_network(s):
    """A finished attempt whose network could not be removed (Docker refused)."""
    s.start()
    node = s.launch()
    container = s.running(node)
    run = s.runs(node)[-1]
    (network,) = s.attempt_networks(run["attempt_id"])
    s.gateway_id = s.egress_gateways()[0]["Id"]  # captured before cleanup detaches it
    s.fd.rule("network", "rm", effect="fail", stderr="Error response from daemon: stuck")
    s.fd.rule("network", "remove", effect="fail", stderr="Error response from daemon: stuck")
    s.fd.finish(container["Id"], 0, stdout=s3.events_stdout())
    nb.wait_done(s, node)
    s.wait(lambda: any(o.get("resource_id") == network["Id"] for o in (listing(s)["orphans"] or [])) or None,
           nb.WAIT, "the stuck network to be reported")
    s.fd.clear_rules()
    return node, run, container, network


# ------------------------------------------------------------- row shapes

def test_ic_c31_container_rows_gain_resource_kind_and_resource_id_and_keep_their_fields(ns):
    ns.start()
    cid = ns.fd.inject("unrecorded", {ic.PROJECT_LABEL: ns.slug, "multiagents.owner": "att_unrecorded"})
    (row,) = listing(ns)["orphans"]
    assert set(row) == CONTAINER_KEYS | {"resource_kind", "resource_id"}, set(row)
    assert row["resource_kind"] == "container" and row["resource_id"] == cid == row["container_id"]


def test_ic_c31_endpoint_rows_gain_resource_kind_and_resource_id_and_keep_their_fields(ns):
    from multiagents.paths import ProjectPaths
    ns.start()
    name = "a1" * 16
    (ProjectPaths(ns.root).scheduler / "endpoints" / name).mkdir(parents=True)
    (row,) = listing(ns)["orphans"]
    assert row["resource_kind"] == "endpoint" and row["resource_id"] == name == row["endpoint_id"]
    assert row["reason"] == "orphan_endpoint"
    assert {k: row[k] for k in ("container_id", "name", "labels", "created_at", "attempt_id")} == \
        {"container_id": None, "name": None, "labels": None, "created_at": None, "attempt_id": None}
    assert row["project"] == ns.slug


def test_ic_c31_network_rows_have_exactly_the_contracted_shape(ns):
    ns.start()
    nid = unrecorded_network(ns)
    (row,) = listing(ns)["orphans"]
    assert set(row) == ROWS
    assert row["resource_kind"] == "network" and row["resource_id"] == nid == row["network_id"]
    assert row["container_id"] is None and row["attempt_id"] is None
    assert row["project"] == ns.slug and row["reason"] == "unrecorded_attempt"
    docker_side = ns.fd.network(nid)
    assert row["name"] == docker_side["Name"] and row["labels"] == docker_side["Labels"]


def test_ic_c31_network_creation_time_is_rfc3339_from_docker(ns):
    ns.start()
    unrecorded_network(ns, created=CREATED)
    (row,) = listing(ns)["orphans"]
    parsed = datetime.datetime.fromisoformat(row["created_at"].replace("Z", "+00:00"))
    assert parsed == datetime.datetime(2026, 1, 2, 3, 4, 5, tzinfo=datetime.timezone.utc)


def test_ic_c31_mixed_rows_sort_by_kind_then_id(ns):
    from multiagents.paths import ProjectPaths
    ns.start()
    for _ in range(2):
        ns.fd.inject("c" + os.urandom(2).hex(), {ic.PROJECT_LABEL: ns.slug, "multiagents.owner": "att_unrecorded"})
        unrecorded_network(ns)
    (ProjectPaths(ns.root).scheduler / "endpoints" / ("ff" * 16)).mkdir(parents=True)
    rows = listing(ns)["orphans"]
    assert [r["resource_kind"] for r in rows].count("network") == 2
    assert [(r["resource_kind"], r["resource_id"]) for r in rows] == sorted((r["resource_kind"], r["resource_id"]) for r in rows)
    assert [r["resource_kind"] for r in rows] == ["container", "container", "endpoint", "network", "network"]


def test_ic_c31_root_status_and_the_cli_return_the_same_discovery(ns):
    ns.start()
    unrecorded_network(ns)
    value = listing(ns)
    status = ns.status()
    assert {k: status[k] for k in value} == value


def test_ic_c31_scoped_status_omits_the_orphan_fields(ns):
    ns.start()
    node = ns.launch()
    ns.running(node)
    run = ns.runs(node)[0]
    token = ns.issue(run["run_id"], node)
    value = ns.ok("scheduler_status", {}, token=token)
    assert "orphans" not in value and "orphans_error" not in value


# ----------------------------------------------------------- classification

def test_ic_c31_other_projects_networks_are_never_listed_or_touched(ns):
    ns.start()
    foreign = unrecorded_network(ns, project="somebody-else")
    assert listing(ns) == {"orphans": [], "orphans_error": None}
    assert ns.fd.network(foreign) is not None


def test_ic_c31_listing_deletes_nothing(ns):
    ns.start()
    ids = [unrecorded_network(ns) for _ in range(2)]
    listing(ns), listing(ns)
    assert all(ns.fd.network(i) is not None for i in ids)


def test_ic_c31_an_active_attempts_network_is_never_an_orphan_and_gateways_are_not_orphans(ns):
    ns.start()
    node = ns.launch()
    container = ns.running(node)
    assert ns.egress_gateways(), "the gateway must exist for this test to mean anything"
    assert listing(ns) == {"orphans": [], "orphans_error": None}
    assert ns.fd.container(container["Id"])["State"]["Running"]


def test_ic_c31_after_a_clean_end_nothing_remains_to_report_gateway_infrastructure_included(ns):
    ns.start()
    node = ns.launch()
    container = ns.running(node)
    ns.fd.finish(container["Id"], 0, stdout=s3.events_stdout())
    nb.wait_done(ns, node)
    ns.wait(lambda: not ns.fd.networks() or None, nb.WAIT, "network cleanup")
    assert listing(ns) == {"orphans": [], "orphans_error": None}


def test_ic_c31_a_confirmed_ended_attempt_whose_network_remained_is_reported_with_its_attempt(ns):
    node, run, container, network = ended_attempt_with_stuck_network(ns)
    (row,) = [o for o in listing(ns)["orphans"] if o["resource_id"] == network["Id"]]
    assert row["reason"] == "ended_attempt_network" and row["attempt_id"] == run["attempt_id"]
    assert row["resource_kind"] == "network"


def test_ic_c31_a_discarded_nodes_network_is_reported_as_discarded(ns):
    node, run, container, network = ended_attempt_with_stuck_network(ns)
    reply = ns.rpc("dispose_node", {"id": node, "revision": ns.get(node)["revision"]}, timeout=nb.WAIT)
    assert reply.get("ok") is True, reply
    rows = ns.wait(lambda: [o for o in listing(ns)["orphans"] if o["resource_id"] == network["Id"]] or None,
                   nb.WAIT, "the discarded node's network")
    assert rows[0]["reason"] == "discarded_node"


def test_ic_c31_the_prepare_create_crash_window_is_not_an_orphan(tmp_path, monkeypatch):
    s = nb.NetSched(tmp_path, monkeypatch)
    try:
        s.fd.rule("network", "create", effect="hang_after", seconds=nb.WAIT, times=1)
        s.start()
        node = s.launch()
        s.wait(lambda: s.fd.networks() or None, nb.WAIT, "the network to be created")
        s.kill9()
        s.fd.clear_rules()
        s.start()
        assert [o for o in listing(s)["orphans"] if o["resource_kind"] == "network"] == [], \
            "a network created in the prepare/create crash window belongs to a recorded attempt"
        assert len(s.fd.networks()) == 1
    finally:
        s.close()


# ------------------------------------------------------------ unknown discovery

@pytest.mark.parametrize("effect", ["fail", "garbage", "daemon_down"])
def test_ic_c31_incomplete_discovery_is_unknown_not_empty(ns, effect):
    ns.start()
    unrecorded_network(ns)
    ns.fd.rule("network", "ls", effect=effect, stderr="Error response from daemon: scripted")
    value = listing(ns)
    assert value["orphans"] is None and value["orphans_error"].get("reason"), value
    assert nb.leaks({"reason": json.dumps(value)}) == {}
    assert ns.status()["orphans"] is None


# ---------------------------------------------------------- explicit removal

def test_ic_c31_removal_of_an_ended_attempts_network_keeps_the_gateway_and_the_retained_container(ns):
    node, run, container, network = ended_attempt_with_stuck_network(ns)
    gateway = {"Id": ns.gateway_id}
    code, value = remove(ns, network["Id"])
    assert code == 0 and value == {"ok": True, "network_id": network["Id"], "state": "absent"}, value
    assert ns.fd.network(network["Id"]) is None
    assert ns.fd.container(container["Id"]) is not None, "retention keeps the exited container"
    assert ns.fd.container(gateway["Id"])["State"]["Running"], "removal must not stop gateway infrastructure"
    assert listing(ns) == {"orphans": [], "orphans_error": None}


def test_ic_c31_removal_revokes_a_stale_grant(ns):
    node, run, container, network = ended_attempt_with_stuck_network(ns)
    assert remove(ns, network["Id"])[0] == 0
    assert all(r["state"] != "ready" for r in ns.rows() if r["attempt_id"] == run["attempt_id"])


def test_ic_c31_removal_is_repeatable_and_an_absent_id_is_success(ns):
    ns.start()
    nid = unrecorded_network(ns)
    for _ in range(2):
        code, value = remove(ns, nid)
        assert code == 0 and value == {"ok": True, "network_id": nid, "state": "absent"}, value
    code, value = remove(ns, "0" * 64)
    assert code == 0 and value == {"ok": True, "network_id": "0" * 64, "state": "absent"}


@pytest.mark.parametrize("form", ["name", "prefix", "empty", "malformed", "upper", "short", "long", "padded", "path"])
def test_ic_c31_only_an_exact_full_immutable_id_is_accepted(ns, form):
    ns.start()
    nid = unrecorded_network(ns)
    name = ns.fd.network(nid)["Name"]
    candidate = {"name": name, "prefix": nid[:12], "empty": "", "malformed": "g" * 64, "upper": nid.upper(),
                 "short": nid[:63], "long": nid + "0", "padded": " " + nid, "path": "../" + nid}[form]
    refused(ns, candidate, {"invalid_id"})
    assert ns.fd.network(nid) is not None


def test_ic_c31_another_projects_network_is_wrong_project(ns):
    ns.start()
    foreign = unrecorded_network(ns, project="somebody-else")
    refused(ns, foreign, {"wrong_project"})
    assert ns.fd.network(foreign) is not None


def test_ic_c31_an_active_attempts_network_is_not_an_orphan_and_nothing_is_stopped(ns):
    ns.start()
    node = ns.launch()
    container = ns.running(node)
    (network,) = ns.attempt_networks(ns.runs(node)[-1]["attempt_id"])
    refused(ns, network["Id"], {"not_orphan"})
    assert ns.fd.network(network["Id"]) is not None
    assert ns.fd.container(container["Id"])["State"]["Running"], "removal must never stop a live attempt"
    assert container["Id"] in ns.fd.members(network["Id"])


def test_ic_c31_an_unexpected_member_refuses_removal_and_is_not_disconnected_or_stopped(ns):
    ns.start()
    nid = unrecorded_network(ns)
    stranger = ns.fd.inject("stranger", {"somebody": "else"}, status="running")
    ns.fd.attach(ns.fd.network(nid)["Name"], stranger)
    refused(ns, nid, {"not_orphan", "unknown"})
    assert ns.fd.network(nid) is not None and stranger in ns.fd.members(nid)
    assert ns.fd.container(stranger)["State"]["Running"]


def test_ic_c31_uncertain_daemon_state_refuses_removal(ns):
    ns.start()
    nid = unrecorded_network(ns)
    ns.fd.daemon_down()
    refused(ns, nid, {"unknown"})
    ns.fd.daemon_down(False)
    assert ns.fd.network(nid) is not None


def test_ic_c31_removal_is_for_the_host_user_only(ns):
    ns.start()
    nid = unrecorded_network(ns)
    code, value = remove(ns, nid, env={"MULTIAGENTS_CONTAINER": "ct-1"})
    assert code != 0 and value.get("ok") is False
    assert ns.fd.network(nid) is not None


@pytest.mark.parametrize("op", ["remove_orphan_network", "remove_network", "remove_orphan"])
def test_ic_c31_no_agent_rpc_exposes_network_removal(ns, op):
    ns.start()
    nid = unrecorded_network(ns)
    reply = ns.rpc(op, {"network_id": nid, "container_id": nid}, timeout=nb.WAIT)
    assert reply.get("ok") is False
    assert ns.fd.network(nid) is not None


def test_ic_c31_existing_container_removal_outcomes_are_unchanged(ns):
    ns.start()
    cid = ns.fd.inject("unrecorded", {ic.PROJECT_LABEL: ns.slug, "multiagents.owner": "att_unrecorded"})
    reply = ns.cli("scheduler", "remove-orphan", cid, timeout=nb.WAIT)
    assert reply.returncode == 0 and json.loads(reply.stdout) == {"ok": True, "container_id": cid, "state": "absent"}
    assert ns.fd.container(cid) is None
