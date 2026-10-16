"""Slice 4B.1, D-S4B.1 (scope and refusal) and D-S4B.5 (configuration and
prerequisites): through the scheduler on the real Docker backend over the fake
`docker` CLI, and through the configuration loader.

Contract: context/specs/phase7-part2-contract.md, "Slice 4B".
Nothing here claims containment; see test_ic_s4b_docker.py for that.
"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import ic_s3 as s3  # noqa: E402
import ic_s4b as nb  # noqa: E402
import ic_support as ic  # noqa: E402
import sc_harness as sc  # noqa: E402
from multiagents import config as config_mod  # noqa: E402
from multiagents.paths import ProjectPaths  # noqa: E402

ENDED = ("held", "failed", "done", "cancelled")
CUSTOM_IMAGE = "registry.test/gateway:7"


@pytest.fixture
def ns(tmp_path, monkeypatch):
    s = nb.NetSched(tmp_path, monkeypatch)
    yield s
    s.close()


def refusal(s, node, pattern):
    """The node ends on a visible, non-retryable refusal matching `pattern`."""
    view = s.wait(lambda: (v := s.get(node)) and v["state"] in ENDED and v, nb.WAIT, "the node to end")
    assert view["state"] != "done", "the attempt must have been refused, it ran"
    found = [c for c in ic.causes(view) if c.get("class") == "other" and pattern in ic.text_of(c).lower()]
    assert found, ic.text_of(ic.causes(view))[:600]
    assert found[0].get("retryable", False) is False
    assert len(s.runs(node)) == 1
    return found[0]


def nothing_happened(s):
    assert s.fd.containers() == {}, "a refused launch must create no container"
    assert s.fd.networks() == {}, "a refused launch must create no network"
    assert s.fd.resources_created() == []


def load_config(tmp_path, monkeypatch, isolated):
    import c3_harness as h
    h.as_root(monkeypatch)
    p = sc.Project(tmp_path)
    p.add_provider("acme")
    p.add_agent("worker", "acme", "acme/m1")
    p.project["executor"] = {"kind": "isolated", "isolated": {"backend": "docker", **isolated}}
    p.write()
    return config_mod.load(ProjectPaths(p.root), seed=False)


# ============================================================== D-S4B.1

def test_d_s4b_1_needs_network_is_no_longer_refused(ns):
    ns.start()
    node = ns.launch()
    container = ns.running(node)
    assert container["State"]["Running"]
    assert ns.get(node)["state"] == "running"


def test_d_s4b_1_needs_credentials_is_still_unsupported_even_with_network(tmp_path, monkeypatch):
    s = nb.NetSched(tmp_path, monkeypatch)
    try:
        s.provider_declares(credentials=True, network=True, rpc=False)
        s.start()
        cause = refusal(s, s.launch(), "credential_handle")
        assert "unsupported" in ic.text_of(cause).lower()
        nothing_happened(s)
    finally:
        s.close()


def test_d_s4b_1_credentials_are_never_silently_cleared_when_network_is_the_only_other_need(tmp_path, monkeypatch):
    s = nb.NetSched(tmp_path, monkeypatch, network="allowlist")
    try:
        s.provider_declares(credentials=True, network=True, rpc=True)
        s.start()
        refusal(s, s.launch(), "credential_handle")
        assert s.fake.spawns() == 0
    finally:
        s.close()


def test_d_s4b_1_a_provider_without_network_need_keeps_network_none(tmp_path, monkeypatch):
    s = nb.NetSched(tmp_path, monkeypatch)
    try:
        s.provider_declares(network=False)
        s.start()
        node = s.launch()
        container = s.running(node)
        assert container["HostConfig"]["NetworkMode"] == "none"
        assert s.fd.networks() == {}, "no network is created for an attempt that needs none"
        assert len(s.fd.containers()) == 1, "no gateway is started for an attempt that needs no network"
    finally:
        s.close()


def test_d_s4b_1_a_network_attempt_is_never_none_bridge_host_or_default(ns):
    ns.start()
    node = ns.launch()
    container = ns.running(node)
    mode = container["HostConfig"]["NetworkMode"]
    assert mode not in ("none", "bridge", "host", "default", ""), mode
    (network,) = ns.attempt_networks(ns.attempt(node)["attempt_id"])
    assert mode in (network["Name"], network["Id"])
    assert network["Internal"] is True, "the attempt network must be internal"
    assert set(ns.fd.networks_of(container["Id"])) == {network["Name"]}


def test_d_s4b_1_there_is_no_fallback_to_the_legacy_project_network(ns):
    ns.start()
    node = ns.launch()
    container = ns.running(node)
    attempt_id = ns.attempt(node)["attempt_id"]
    # The only user-defined network in the daemon is this attempt's own, and it is internal.
    assert [n["Id"] for n in ns.fd.networks().values()] == [n["Id"] for n in ns.attempt_networks(attempt_id)]
    assert all(n["Internal"] for n in ns.fd.networks().values())
    assert set(ns.fd.networks_of(container["Id"])) == {n["Name"] for n in ns.fd.networks().values()}


def test_d_s4b_1_network_authority_grants_no_rpc_permission(ns):
    ns.start()
    node = ns.launch()
    container = ns.running(node)
    destinations = {m["destination"] for m in nb.fdk.mounts_of(container)}
    assert not any(d.startswith("/run/multiagents/rpc") for d in destinations), destinations
    env = ns.invocation(container)["env"]
    assert "MULTIAGENTS_RPC_SOCKET" not in env, "a network-only attempt must not receive an RPC endpoint"
    assert nb.proxy_url(env), "but it must receive the egress credential"


def test_d_s4b_1_rpc_stays_on_the_unix_socket_and_never_tcp_with_network(tmp_path, monkeypatch):
    s = nb.NetSched(tmp_path, monkeypatch, rpc=True)
    try:
        s.start()
        node = s.launch()
        container = s.running(node)
        env = s.invocation(container)["env"]
        assert env.get("MULTIAGENTS_RPC_SOCKET") == "/run/multiagents/rpc/rpc.sock", env
        assert "ma-egress" not in env["MULTIAGENTS_RPC_SOCKET"]
        assert not any("MULTIAGENTS_RPC_PORT" in k or "MULTIAGENTS_RPC_URL" in k for k in env)
    finally:
        s.close()


def test_d_s4b_2_the_run_gets_no_published_ports_host_namespaces_or_docker_socket(ns):
    ns.start()
    node = ns.launch()
    container = ns.running(node)
    host = container["HostConfig"]
    argv = container["CreateArgv"]
    assert not any(a in ("-p", "-P", "--publish", "--publish-all") or a.startswith("--publish=") for a in argv)
    assert host["PidMode"] in ("", None) and host["IpcMode"] in ("private", "", None)
    assert host["NetworkMode"] != "host" and host["Privileged"] is False
    assert not any("docker.sock" in m["source"] or "docker.sock" in m["destination"] for m in nb.fdk.mounts_of(container))


def test_d_s4b_2_a_network_attempt_gets_no_mount_a_network_free_attempt_does_not(tmp_path, monkeypatch):
    s = nb.NetSched(tmp_path, monkeypatch)
    try:
        s.provider_declares(network=False)
        s.start()
        plain = {m["destination"] for m in nb.fdk.mounts_of(s.running(s.launch()))}
        s.provider_declares(network=True)
        networked = {m["destination"] for m in nb.fdk.mounts_of(s.running(s.launch()))}
        assert networked == plain, (sorted(networked - plain), sorted(plain - networked))
    finally:
        s.close()


# ============================================================== D-S4B.5

def test_d_s4b_5_network_defaults_to_allowlist(tmp_path, monkeypatch):
    s = nb.NetSched(tmp_path, monkeypatch, network=None)
    try:
        s.start()
        container = s.running(s.launch())
        assert s.egress_gateways(), "the default is the allowlist topology"
        assert container["HostConfig"]["NetworkMode"] not in ("none", "bridge")
    finally:
        s.close()


def test_d_s4b_5_explicit_allowlist_is_the_same_as_the_default(tmp_path, monkeypatch):
    s = nb.NetSched(tmp_path, monkeypatch, network="allowlist")
    try:
        s.start()
        s.running(s.launch())
        assert s.egress_gateways()
    finally:
        s.close()


def test_d_s4b_5_none_with_needs_network_is_a_visible_non_retryable_refusal(tmp_path, monkeypatch):
    s = nb.NetSched(tmp_path, monkeypatch, network="none")
    try:
        s.start()
        cause = refusal(s, s.launch(), "network")
        assert "isolated.network" in ic.text_of(cause) or "none" in ic.text_of(cause).lower()
        nothing_happened(s)
        assert s.fake.spawns() == 0
    finally:
        s.close()


def test_d_s4b_5_none_does_not_refuse_an_attempt_that_needs_no_network(tmp_path, monkeypatch):
    s = nb.NetSched(tmp_path, monkeypatch, network="none")
    try:
        s.provider_declares(network=False)
        s.start()
        container = s.running(s.launch())
        assert container["HostConfig"]["NetworkMode"] == "none"
    finally:
        s.close()


@pytest.mark.parametrize("value", ["bridge", "host", "ALLOWLIST", "None", "", " allowlist", "allowlist ",
                                   "all", "open", "proxy", True, False, 0, 1, ["allowlist"], {"mode": "none"}])
def test_d_s4b_5_unknown_network_values_are_config_errors_naming_the_key(tmp_path, monkeypatch, value):
    with pytest.raises(ValueError) as caught:
        load_config(tmp_path, monkeypatch, {"network": value})
    assert "executor.isolated.network" in str(caught.value)


@pytest.mark.parametrize("value", ["allowlist", "none"])
def test_d_s4b_5_known_network_values_load(tmp_path, monkeypatch, value):
    loaded = load_config(tmp_path, monkeypatch, {"network": value})
    assert loaded.project["executor"]["isolated"]["network"] == value


def test_d_s4b_5_an_invalid_network_value_launches_nothing(tmp_path, monkeypatch):
    s = nb.NetSched(tmp_path, monkeypatch, network="bridge")
    try:
        s.start()
        node = s.launch()
        view = s.wait(lambda: (v := s.get(node)) and v["state"] in ENDED and v, nb.WAIT, "a decision")
        assert "executor.isolated.network" in ic.text_of(view), "the config error must name the key"
        nothing_happened(s)
    finally:
        s.close()


def test_d_s4b_5_gateway_image_defaults_to_the_workspace_image(ns):
    ns.start()
    container = ns.running(ns.launch())
    gateways = ns.egress_gateways()
    assert gateways, "no egress gateway was started"
    assert {g["Config"]["Image"] for g in gateways} == {nb.GATEWAY_IMAGE}
    assert container["Config"]["Image"] == nb.GATEWAY_IMAGE


def test_d_s4b_5_a_configured_gateway_image_is_the_gateways_not_the_attempts(tmp_path, monkeypatch):
    s = nb.NetSched(tmp_path, monkeypatch, gateway_image=CUSTOM_IMAGE, images=[nb.GATEWAY_IMAGE, CUSTOM_IMAGE])
    try:
        s.start()
        container = s.running(s.launch())
        assert {g["Config"]["Image"] for g in s.egress_gateways()} == {CUSTOM_IMAGE}
        assert container["Config"]["Image"] == nb.GATEWAY_IMAGE
    finally:
        s.close()


@pytest.mark.parametrize("bad", ["", "   ", "has space", "tab\there", "nul\x00byte", 5, True, ["img"], {"a": 1}])
def test_d_s4b_5_a_malformed_gateway_image_is_a_config_error_naming_the_key(tmp_path, monkeypatch, bad):
    with pytest.raises(ValueError) as caught:
        load_config(tmp_path, monkeypatch, {"docker": {"gateway_image": bad}})
    assert "executor.isolated.docker.gateway_image" in str(caught.value)


def block_text(s, node):
    return ic.text_of(s.blocked(node)) + ic.text_of(ic.causes(s.get(node)))


def test_d_s4b_5_a_missing_gateway_image_is_isolation_unavailable_before_any_side_effect(tmp_path, monkeypatch):
    s = nb.NetSched(tmp_path, monkeypatch, gateway_image=CUSTOM_IMAGE, images=[nb.GATEWAY_IMAGE])
    try:
        s.start()
        node = s.launch()
        s.wait(lambda: CUSTOM_IMAGE in block_text(s, node) or None, nb.WAIT, "the refusal naming the gateway image")
        assert "isolation_unavailable" in block_text(s, node)
        assert s.get(node)["state"] != "done" and s.runs(node) == []
        nothing_happened(s)
        assert s.fake.spawns() == 0
    finally:
        s.close()


def test_d_s4b_5_no_image_is_built_or_pulled_at_launch(ns):
    ns.start()
    ns.running(ns.launch())
    heads = {tuple(c[:2]) for c in ns.fd.commands()}
    assert not ({"pull", "build"} & {h[0] for h in heads}), heads
    assert not ({("image", "pull"), ("image", "build"), ("image", "load")} & heads), heads
    assert ns.egress_gateways(), "the launch must have reached the gateway to make this meaningful"


def test_d_s4b_5_a_missing_workspace_image_blocks_a_network_attempt_before_any_network_exists(tmp_path, monkeypatch):
    s = nb.NetSched(tmp_path, monkeypatch, images=["some/other:1"])
    try:
        s.start()
        node = s.launch()
        s.wait(lambda: "image" in block_text(s, node).lower() or None, nb.WAIT, "the image block")
        assert "isolation_unavailable" in block_text(s, node)
        nothing_happened(s)
    finally:
        s.close()


def test_d_s4b_5_every_docker_command_uses_the_configured_cli(tmp_path, monkeypatch):
    s = nb.NetSched(tmp_path, monkeypatch)
    try:
        fake = str(s.fd.docker)
        s.configure(docker={"cli": fake})
        # The `docker` on PATH is no longer the fake: only the configured CLI works.
        here = os.environ["PATH"].split(os.pathsep)
        monkeypatch.setenv("PATH", os.pathsep.join(p for p in here if Path(p) != s.fd.bin_dir))
        s.start()
        container = s.running(s.launch())
        assert s.egress_gateways() and container["HostConfig"]["NetworkMode"] not in ("none", "bridge")
    finally:
        s.close()


def test_d_s4b_5_gateway_settings_are_recorded_with_the_executor_settings(tmp_path, monkeypatch):
    s = nb.NetSched(tmp_path, monkeypatch, gateway_image=CUSTOM_IMAGE, images=[nb.GATEWAY_IMAGE, CUSTOM_IMAGE])
    try:
        s.start()
        node = s.launch()
        s.running(node)
        executor = s.evidence(s.attempt(node)["attempt_id"])["executor"]
        assert CUSTOM_IMAGE in ic.text_of(executor), "K19: the gateway image is recorded with the executor settings"
    finally:
        s.close()
