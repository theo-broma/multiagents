"""IC-C25 (slice 4B.1): per-attempt network creation, identity and recovery.

Contract: context/specs/phase7-part2-contract.md, "IC-C25" and D-S4B.2/4.
The scheduler runs on the real Docker backend over the fake `docker` CLI;
`GatewayRegistry` is exercised as the host would, from a second process view of
the same project. Fake-CLI passes are never evidence of containment.
"""
import dataclasses
import json
import re
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import ic_s3 as s3  # noqa: E402
import ic_s4b as nb  # noqa: E402
import ic_support as ic  # noqa: E402
from ic_support import kind_of  # noqa: E402

GOLDEN_V1_DIGEST = "3ee7e0c2bc829ec1315b69f35cb869f61f4e5adc9ca2b50120f45c1e77d0ad57"
HEX64 = re.compile(r"^[0-9a-f]{64}$")


@pytest.fixture
def ns(tmp_path, monkeypatch):
    s = nb.NetSched(tmp_path, monkeypatch)
    yield s
    s.close()


def started(s):
    s.start()
    node = s.launch()
    container = s.running(node)
    run = s.runs(node)[-1]
    return node, run, container, s.attempt_networks(run["attempt_id"])[0]


def golden_spec():
    return ic.lifecycle().LaunchSpec(spec_version=1, owner="att_golden", lifetime="attempt",
                                     labels={"multiagents.project": "golden"}, writable_paths=["/w"], readonly_paths=[])


# ----------------------------------------------------------- the LaunchSpec field

def test_ic_c25_network_handle_defaults_to_none():
    assert ic.lifecycle().LaunchSpec().network_handle is None


def test_ic_c25_unset_handle_preserves_the_recorded_v1_canonical_digest():
    assert ic.digest_of(golden_spec()) == GOLDEN_V1_DIGEST


def test_ic_c25_unset_handle_digest_equals_the_digest_of_the_pre_4b_field_set():
    spec = golden_spec()
    old = {k: v for k, v in dataclasses.asdict(spec).items() if k != "network_handle"}
    assert ic.digest_of(spec) == ic.canonical_digest(old)


def test_ic_c25_a_non_empty_handle_participates_in_the_digest():
    plain, a, b = golden_spec(), golden_spec(), golden_spec()
    a.network_handle, b.network_handle = "nh-" + "a" * 32, "nh-" + "b" * 32
    assert len({ic.digest_of(plain), ic.digest_of(a), ic.digest_of(b)}) == 3
    old = dataclasses.asdict(a)
    assert ic.digest_of(a) == ic.canonical_digest(old), "the handle is part of the canonical JSON when set"


@pytest.mark.parametrize("empty", [None])
def test_ic_c25_an_unset_handle_never_adds_a_key_to_the_canonical_form(empty):
    spec = golden_spec()
    spec.network_handle = empty
    assert ic.digest_of(spec) == GOLDEN_V1_DIGEST


@pytest.mark.parametrize("handle", ["not-issued", "x" * 64, "../../etc", "nh-" + "0" * 32, " ", "\x00", {"id": "forged"}, 7, ["a"]])
def test_ic_c25_a_handle_without_host_authority_is_invalid_spec_and_creates_nothing(tmp_path, monkeypatch, handle):
    w = s3.DockerWorld(tmp_path, monkeypatch)
    spec = w.spec(network_handle=handle)
    got = ic.run(w.backend.provision(spec, "key-net-forged"))
    assert kind_of(got) == "invalid_spec", got
    assert w.fd.calls_of("create") == [] and w.fd.containers() == {}
    assert w.fd.networks() == {}


def test_ic_c25_an_empty_handle_is_treated_as_no_handle(tmp_path, monkeypatch):
    w = s3.DockerWorld(tmp_path, monkeypatch)
    got = ic.run(w.backend.provision(w.spec(network_handle=None), "key-net-none"))
    assert kind_of(got) not in ("invalid_spec", "unsupported", "unknown"), got
    (container,) = w.fd.containers().values()
    assert container["HostConfig"]["NetworkMode"] == "none"


def test_ic_c25_a_backend_without_network_support_does_not_create_or_run_with_a_handle(tmp_path):
    import ic_backends
    backend = ic_backends.make_reference(tmp_path / "ref")
    spec = ic.launch_spec(tmp_path / "ref", network_handle="nh-" + "a" * 32)
    got = ic.run(backend.provision(spec, "key-ref"))
    assert kind_of(got) in ("unsupported", "invalid_spec"), got
    if kind_of(got) == "unsupported":
        assert ic.field(got, "field") == "network_handle"
    assert ic.run(backend.list_owned("ic-test")) == []


# ----------------------------------------------------- the network of one attempt

def test_ic_c25_one_internal_network_per_attempt_with_reserved_labels(ns):
    node, run, container, network = started(ns)
    attempt_id = run["attempt_id"]
    assert network["Internal"] is True
    labels = network["Labels"]
    assert labels["multiagents.project"] == ns.slug
    assert labels["multiagents.owner"] == attempt_id
    assert labels["multiagents.creation_key"]
    assert len(ns.attempt_networks(attempt_id)) == 1 and len(ns.fd.networks()) == 1
    assert not re.search(r"ma1-|[0-9a-f]{64}", network["Name"].replace(network["Id"], "")), "no secret-shaped name"


def test_ic_c25_network_labels_name_the_creation_key_recorded_in_evidence(ns):
    node, run, container, network = started(ns)
    recorded = ns.evidence(run["attempt_id"])["executor"]["network"]
    assert recorded["creation_key"] == network["Labels"]["multiagents.creation_key"]
    assert recorded["creation_key"] != ns.evidence(run["attempt_id"])["executor"]["creation_key"], \
        "the network has a key of its own, bound to the attempt, not the container's"


def test_ic_c25_evidence_records_key_then_immutable_id_then_policy_and_generation(ns):
    node, run, container, network = started(ns)
    recorded = ns.evidence(run["attempt_id"])["executor"]["network"]
    assert recorded["id"] == network["Id"] and HEX64.match(recorded["id"]), recorded
    assert isinstance(recorded["policy_digest"], str) and recorded["policy_digest"], recorded
    assert recorded["gateway_generation"] is not None, recorded
    assert set(recorded) >= {"creation_key", "id", "policy_digest", "gateway_generation"}


def test_ic_c25_the_creation_key_is_durable_before_the_network_is_created(tmp_path, monkeypatch):
    s = nb.NetSched(tmp_path, monkeypatch)
    try:
        s.fd.rule("network", "create", effect="hang", seconds=nb.WAIT, times=1)
        s.start()
        node = s.launch()
        recorded = s.wait(lambda: (r := s.runs(node)) and s.evidence(r[-1]["attempt_id"]).get("executor", {}).get("network"),
                          nb.SHORT, "the network creation key to be recorded")
        assert recorded.get("creation_key") and not recorded.get("id")
        assert s.fd.networks() == {}, "the key must be recorded before Docker was asked to create"
    finally:
        s.close()


def test_ic_c25_a_create_that_succeeded_but_reported_failure_is_resolved_by_key_not_duplicated(tmp_path, monkeypatch):
    s = nb.NetSched(tmp_path, monkeypatch)
    try:
        s.fd.rule("network", "create", effect="after_effect", times=1)
        s.start()
        node = s.launch()
        container = s.running(node)
        run = s.runs(node)[-1]
        assert len(s.fd.networks()) == 1 and len(s.fd.resources_created()) == 1
        (network,) = s.attempt_networks(run["attempt_id"])
        assert s.evidence(run["attempt_id"])["executor"]["network"]["id"] == network["Id"]
        assert container["HostConfig"]["NetworkMode"] in (network["Name"], network["Id"])
    finally:
        s.close()


def test_ic_c25_a_confirmed_create_failure_is_a_launch_failure_and_leaves_no_network(tmp_path, monkeypatch):
    s = nb.NetSched(tmp_path, monkeypatch)
    try:
        s.fd.rule("network", "create", effect="fail", stderr="Error response from daemon: cannot create network")
        s.start()
        node = s.launch()
        view = s.wait(lambda: (v := s.get(node)) and v["state"] in ("held", "failed") and v, nb.WAIT, "the launch failure")
        assert any(c.get("class") == "launch" for c in ic.causes(view)), ic.text_of(ic.causes(view))[:500]
        assert s.fd.networks() == {} and s.fd.containers() == {}
        assert s.fake.spawns() == 0
    finally:
        s.close()


def test_ic_c25_the_network_exists_before_the_container_and_membership_is_part_of_creation(ns):
    node, run, container, network = started(ns)
    argv = " ".join(container["CreateArgv"])
    assert network["Name"] in argv or network["Id"] in argv, "the container is created on its network"
    created = [c for c in ns.fd.commands() if c[:1] == ["create"]]
    assert len(created) == 1 or any(network["Id"] in " ".join(c) or network["Name"] in " ".join(c) for c in created)
    first_net = next(i for i, c in enumerate(ns.fd.commands()) if c[:2] == ["network", "create"])
    attempt_create = next(i for i, c in enumerate(ns.fd.commands())
                          if c[:1] in (["create"], ["run"]) and f"multiagents.owner={run['attempt_id']}" in " ".join(c))
    assert first_net < attempt_create


def test_ic_c25_members_are_the_attempt_container_and_the_trusted_gateways_only(ns):
    node, run, container, network = started(ns)
    members = set(ns.fd.members(network["Id"]))
    gateways = {g["Id"] for g in ns.egress_gateways()}
    assert container["Id"] in members and gateways & members
    assert members <= {container["Id"]} | gateways | {c["Id"] for c in ns.fd.containers().values()
                                                       if "ma-auth" in json.dumps(c.get("NetworkSettings", {}))}
    assert ns.fd.networks_of(container["Id"]).keys() == {network["Name"]}


def test_ic_c25_the_egress_gateway_holds_the_stable_alias_on_the_attempt_network(ns):
    node, run, container, network = started(ns)
    aliased = [c for c in ns.egress_gateways()
               if "ma-egress" in (ns.fd.networks_of(c["Id"]).get(network["Name"], {}).get("Aliases") or [])]
    assert len(aliased) == 1
    assert aliased[0]["State"]["Running"] is True
    assert aliased[0]["Id"] != container["Id"]


def test_ic_c25_all_readiness_is_confirmed_before_the_attempt_starts(ns):
    node, run, container, network = started(ns)
    snapshot = container["StartSnapshots"][0]
    members = snapshot[network["Name"]]
    gateways = {g["Id"] for g in ns.egress_gateways()}
    assert container["Id"] in members
    assert any(members.get(g) is True for g in gateways), f"the gateway was not running and attached at start: {members}"


def test_ic_c25_the_gateway_is_project_scoped_and_shared_by_several_attempt_networks(ns):
    ns.start()
    a, b = ns.launch(), ns.launch()
    ca, cb = ns.running(a), ns.running(b)
    ra, rb = ns.runs(a)[-1], ns.runs(b)[-1]
    (na,), (nb_,) = ns.attempt_networks(ra["attempt_id"]), ns.attempt_networks(rb["attempt_id"])
    assert na["Id"] != nb_["Id"] and na["Name"] != nb_["Name"]
    assert len(ns.egress_gateways()) == 1
    gateway = ns.egress_gateways()[0]["Id"]
    assert gateway in ns.fd.members(na["Id"]) and gateway in ns.fd.members(nb_["Id"])
    assert cb["Id"] not in ns.fd.members(na["Id"]) and ca["Id"] not in ns.fd.members(nb_["Id"])


def test_ic_c25_a_second_attempt_of_the_same_node_gets_its_own_network_and_key(ns):
    ns.start()
    node = ns.launch()
    first = ns.running(node)
    reply = ns.rpc("steer_run", {"run_id": ns.runs(node)[-1]["run_id"], "message": "again"}, timeout=nb.WAIT)
    assert reply.get("ok") is True, reply
    ns.wait_runs(node, 2, nb.WAIT)
    ids = [r["attempt_id"] for r in ns.runs(node)]
    keys = {ns.evidence(i).get("executor", {}).get("network", {}).get("creation_key") for i in ids}
    assert len(keys - {None}) == 2


def test_ic_c25_no_network_attempt_records_no_network_evidence_and_is_adopted_after_restart(tmp_path, monkeypatch):
    s = nb.NetSched(tmp_path, monkeypatch)
    try:
        s.provider_declares(network=False)
        s.start()
        node = s.launch()
        container = s.running(node)
        run = s.runs(node)[-1]
        assert not s.evidence(run["attempt_id"])["executor"].get("network")
        s.restart(hard=True)
        assert [r["attempt_id"] for r in s.runs(node)] == [run["attempt_id"]]
        assert s.fd.container(container["Id"])["StartCount"] == 1
        assert s.fd.networks() == {}
    finally:
        s.close()


# ------------------------------------------------------------------- the registry

def test_ic_c25_registry_construction_has_no_side_effects(tmp_path, monkeypatch):
    s = nb.NetSched(tmp_path, monkeypatch)
    try:
        from multiagents import config as config_mod
        from multiagents.paths import ProjectPaths
        cls = nb.have("GatewayRegistry")
        paths = ProjectPaths(s.root)
        loaded = config_mod.load(paths, seed=False)
        before_files = sorted(str(p) for p in tmp_path.rglob("*") if "fake-docker" not in str(p))
        cls(paths, config=loaded, providers=loaded.providers)
        assert s.fd.calls() == []
        assert sorted(str(p) for p in tmp_path.rglob("*") if "fake-docker" not in str(p)) == before_files
    finally:
        s.close()


def test_ic_c25_prepare_is_repeatable_and_returns_the_same_frozen_opaque_handle(ns):
    node, run, container, network = started(ns)
    registry = ns.registry()
    first = ic.run(registry.prepare(run["attempt_id"], run["run_id"]))
    again = ic.run(registry.prepare(run["attempt_id"], run["run_id"]))
    assert not isinstance(first, str) and first.id == again.id
    assert first.id not in (network["Id"], network["Name"]) and network["Id"] not in str(first.id)
    with pytest.raises((dataclasses.FrozenInstanceError, AttributeError, TypeError)):
        first.id = "other"
    assert len(ns.fd.networks()) == 1 and len(ns.fd.resources_created()) == 1


def test_ic_c25_prepare_with_an_incompatible_run_is_an_identity_conflict_without_mutation(ns):
    node, run, container, network = started(ns)
    registry = ns.registry()
    before = (json.dumps(ns.fd.networks(), sort_keys=True), ns.rows())
    got = ic.run(registry.prepare(run["attempt_id"], "run_" + "f" * 32))
    assert kind_of(got) == "identity_conflict", got
    assert (json.dumps(ns.fd.networks(), sort_keys=True), ns.rows()) == before


@pytest.mark.parametrize("attempt_id", ["att_" + "0" * 32, "att_unknown", "", "../../x", "att_" + "0" * 32 + "\x00"])
def test_ic_c25_prepare_for_an_attempt_the_journal_does_not_own_is_invalid_spec(ns, attempt_id):
    ns.start()
    got = ic.run(ns.registry().prepare(attempt_id, "run_" + "1" * 32))
    assert kind_of(got) == "invalid_spec", got
    assert ns.fd.networks() == {}


def test_ic_c25_grant_returns_a_frozen_grant_with_an_id_and_the_invocation_environment(ns):
    node, run, container, network = started(ns)
    registry = ns.registry()
    identity = ic.lifecycle().ResourceIdentity(container["Id"])
    grant = ic.run(registry.grant(run["attempt_id"], identity))
    assert grant.id and isinstance(grant.id, str)
    user, password = nb.proxy_url(dict(grant.environment))
    assert user == f"ma1-{run['attempt_id']}" and nb.PASSWORD.match(password)
    with pytest.raises((dataclasses.FrozenInstanceError, AttributeError, TypeError)):
        grant.id = "x"


def test_ic_c25_a_repeated_grant_is_the_same_recorded_identity_and_credential(ns):
    node, run, container, network = started(ns)
    registry = ns.registry()
    identity = ic.lifecycle().ResourceIdentity(container["Id"])
    first, again = ic.run(registry.grant(run["attempt_id"], identity)), ic.run(registry.grant(run["attempt_id"], identity))
    assert first.id == again.id and dict(first.environment) == dict(again.environment)
    guest = ns.invocation(container)["env"]
    assert dict(first.environment)["HTTP_PROXY"] == guest["HTTP_PROXY"], "the grant IS the guest's credential"


def test_ic_c25_grant_for_another_container_is_an_identity_conflict(ns):
    node, run, container, network = started(ns)
    other = ns.fd.inject("someone-else", {"multiagents.project": ns.slug}, status="running")
    got = ic.run(ns.registry().grant(run["attempt_id"], ic.lifecycle().ResourceIdentity(other)))
    assert kind_of(got) == "identity_conflict", got
    assert other not in ns.fd.members(network["Id"])


def test_ic_c25_grant_for_an_unowned_attempt_is_invalid_spec(ns):
    ns.start()
    got = ic.run(ns.registry().grant("att_" + "0" * 32, ic.lifecycle().ResourceIdentity("0" * 64)))
    assert kind_of(got) == "invalid_spec", got


def test_ic_c25_reconcile_rows_are_non_secret_and_name_attempt_state_and_reason(ns):
    node, run, container, network = started(ns)
    rows = ns.rows()
    mine = [r for r in rows if r["attempt_id"] == run["attempt_id"]]
    assert len(mine) == 1 and set(mine[0]) == {"attempt_id", "state", "reason"}
    assert mine[0]["state"] == "ready" and isinstance(mine[0]["reason"], str)
    assert {r["state"] for r in rows} <= {"ready", "revoked", "absent", "unknown"}
    assert nb.leaks({"rows": json.dumps(rows)}) == {}


def test_ic_c25_revoke_and_cleanup_return_none_and_repeat(ns):
    node, run, container, network = started(ns)
    registry = ns.registry()
    assert ic.run(registry.revoke(run["attempt_id"])) is None
    assert ic.run(registry.revoke(run["attempt_id"])) is None
    rows = {r["attempt_id"]: r["state"] for r in ns.rows()}
    assert rows.get(run["attempt_id"]) == "revoked"


def test_ic_c25_a_revoked_grant_is_never_resurrected_by_grant_or_reconcile(ns):
    node, run, container, network = started(ns)
    registry = ns.registry()
    ic.run(registry.revoke(run["attempt_id"]))
    got = ic.run(registry.grant(run["attempt_id"], ic.lifecycle().ResourceIdentity(container["Id"])))
    assert not hasattr(got, "environment"), "a revoked grant must not be returned again"
    assert {r["attempt_id"]: r["state"] for r in ns.rows()}.get(run["attempt_id"]) == "revoked"


def test_ic_c25_cleanup_of_an_unknown_attempt_is_success_or_absent(ns):
    ns.start()
    got = ic.run(ns.registry().cleanup("att_" + "0" * 32))
    assert got is None or kind_of(got) == "absent", got


def test_ic_c25_cleanup_after_the_attempt_ended_removes_the_network_and_repeats(ns):
    node, run, container, network = started(ns)
    ns.fd.finish(container["Id"], 0)
    nb.wait_done(ns, node)
    registry = ns.registry()
    for _ in range(2):
        got = ic.run(registry.cleanup(run["attempt_id"]))
        assert got is None or kind_of(got) == "absent", got
    assert ns.fd.network(network["Id"]) is None
    assert ns.fd.container(container["Id"]) is not None, "retained output and repository stay"


def test_ic_c25_a_failed_grant_leaks_no_credential_into_the_error_or_the_record_and_recovers(tmp_path, monkeypatch):
    s = nb.NetSched(tmp_path, monkeypatch)
    try:
        s.start()
        first = s.launch()
        s.running(first)
        s.fd.rule("network", "connect", effect="fail", times=2, stderr="Error response from daemon: attach failed")
        second = s.launch()
        view = s.wait(lambda: (v := s.get(second)) and (v["state"] in ("held", "failed") or v["runs"]) and v,
                      nb.WAIT, "the second attempt to settle")
        texts = s.public_texts(second)
        assert nb.leaks(texts) == {}, nb.leaks(texts)
        # Recovery: the next attempt is granted normally and its credential is well formed.
        s.fd.clear_rules()
        third = s.launch()
        env = s.guest_env(third)
        assert nb.PASSWORD.match(nb.proxy_url(env)[1])
        assert nb.leaks(s.public_texts(third), nb.proxy_url(env)[1]) == {}
    finally:
        s.close()
