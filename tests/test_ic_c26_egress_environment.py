"""IC-C26 (slice 4B.1): the guest's egress credential and environment.

Contract: context/specs/phase7-part2-contract.md, "IC-C26" and D-S4B.4.
The scheduler on the real Docker backend over the fake `docker` CLI; the
guest's environment is read from the invocation the host handed to the
container. The gateway's own 407 behaviour is in test_ic_c27_egress_handler.py.
"""
import hashlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import ic_s4b as nb  # noqa: E402
import ic_support as ic  # noqa: E402

EVIL = "http://evil.test:3128"


@pytest.fixture
def ns(tmp_path, monkeypatch):
    s = nb.NetSched(tmp_path, monkeypatch)
    yield s
    s.close()


def two_attempts(s):
    s.start()
    a, b = s.launch(), s.launch()
    return (a, s.guest_env(a)), (b, s.guest_env(b))


def test_ic_c26_username_is_ma1_and_the_attempt_id_password_is_64_lowercase_hex(ns):
    ns.start()
    node = ns.launch()
    env = ns.guest_env(node)
    user, password = nb.proxy_url(env)
    assert user == "ma1-" + ns.runs(node)[-1]["attempt_id"]
    assert nb.PASSWORD.match(password), password


def test_ic_c26_all_four_proxy_variables_carry_the_same_gateway_url(ns):
    ns.start()
    env = ns.guest_env(ns.launch())
    user, password = nb.proxy_url(env)
    expected = f"http://{user}:{password}@ma-egress:8888"
    assert {name: env.get(name) for name in nb.PROXY_VARS} == {name: expected for name in nb.PROXY_VARS}


def test_ic_c26_no_proxy_lists_loopback_and_the_auth_alias_only(ns):
    ns.start()
    env = ns.guest_env(ns.launch())
    assert env.get("NO_PROXY") == nb.NO_PROXY_VALUE and env.get("no_proxy") == nb.NO_PROXY_VALUE


@pytest.mark.parametrize("name", ["ALL_PROXY", "all_proxy"])
def test_ic_c26_conflicting_all_proxy_is_removed_wherever_it_came_from(tmp_path, monkeypatch, name):
    monkeypatch.setenv(name, "socks5://host-proxy.invalid:1080")
    s = nb.NetSched(tmp_path, monkeypatch)
    try:
        s.fake.entry["env"] = {"ALL_PROXY": "socks5://overlay.invalid:1", "all_proxy": "socks5://overlay.invalid:1"}
        s.p.write()
        s._reset_server()
        s.start()
        env = s.guest_env(s.launch())
        assert "ALL_PROXY" not in env and "all_proxy" not in env, {k: v for k, v in env.items() if "proxy" in k.lower()}
        assert nb.proxy_url(env)
    finally:
        s.close()


def test_ic_c26_the_gateway_variables_win_over_overlays_and_inherited_values(tmp_path, monkeypatch):
    for name in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "NO_PROXY", "no_proxy"):
        monkeypatch.setenv(name, EVIL)
    s = nb.NetSched(tmp_path, monkeypatch)
    try:
        s.fake.entry["env"] = {name: EVIL for name in (*nb.PROXY_VARS, "NO_PROXY", "no_proxy")}
        s.p.write()
        s._reset_server()
        s.start()
        env = s.guest_env(s.launch())
        user, password = nb.proxy_url(env)
        url = f"http://{user}:{password}@ma-egress:8888"
        assert all(env[name] == url for name in nb.PROXY_VARS), {k: env.get(k) for k in nb.PROXY_VARS}
        assert env["NO_PROXY"] == env["no_proxy"] == nb.NO_PROXY_VALUE
        assert EVIL not in str(env)
    finally:
        s.close()


def test_ic_c26_a_network_disabled_attempt_gets_no_credentials_and_no_inherited_proxy(tmp_path, monkeypatch):
    for name in (*nb.PROXY_VARS, "ALL_PROXY", "all_proxy", "NO_PROXY", "no_proxy"):
        monkeypatch.setenv(name, EVIL)
    s = nb.NetSched(tmp_path, monkeypatch)
    try:
        s.provider_declares(network=False)
        s.start()
        container = s.running(s.launch())
        env = s.invocation(container)["env"]
        leftovers = {k: v for k, v in env.items() if "proxy" in k.lower() or "ma1-" in str(v)}
        assert leftovers == {}, leftovers
    finally:
        s.close()


def test_ic_c26_network_none_config_with_a_networkless_provider_gets_no_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", EVIL)
    s = nb.NetSched(tmp_path, monkeypatch, network="none")
    try:
        s.provider_declares(network=False)
        s.start()
        env = s.invocation(s.running(s.launch()))["env"]
        assert not any("proxy" in k.lower() for k in env), env
    finally:
        s.close()


def test_ic_c26_each_attempt_has_its_own_user_and_a_fresh_password(ns):
    (na, ea), (nb_, eb) = two_attempts(ns)
    (ua, pa), (ub, pb) = nb.proxy_url(ea), nb.proxy_url(eb)
    assert ua != ub and pa != pb
    assert ua.endswith(ns.runs(na)[-1]["attempt_id"]) and ub.endswith(ns.runs(nb_)[-1]["attempt_id"])


def invocation_written(ns, container):
    try:
        ns.invocation(container)
    except (FileNotFoundError, ValueError):
        return False
    return True


def test_ic_c26_a_retry_of_the_same_node_gets_a_fresh_credential(ns):
    ns.start()
    node = ns.launch()
    first = nb.proxy_url(ns.guest_env(node))
    reply = ns.rpc("steer_run", {"run_id": ns.runs(node)[-1]["run_id"], "message": "again"}, timeout=nb.WAIT)
    assert reply.get("ok") is True, reply
    runs = ns.wait_runs(node, 2, nb.WAIT)
    second_container = ns.wait(lambda: ns.container_of(runs[-1]["attempt_id"]), nb.WAIT, "the second container")
    second = nb.proxy_url(ns.wait(lambda: ns.invocation(second_container).get("env") or None
                                  if invocation_written(ns, second_container) else None,
                                  nb.WAIT, "the second invocation.json"))
    assert first[0] != second[0] and first[1] != second[1]


def test_ic_c26_the_password_is_not_derived_from_any_id(ns):
    ns.start()
    node = ns.launch()
    env = ns.guest_env(node)
    run = ns.runs(node)[-1]
    _, password = nb.proxy_url(env)
    seeds = [run["attempt_id"], run["run_id"], node, ns.slug, "ma1-" + run["attempt_id"]]
    derived = set()
    for seed in seeds:
        for algo in (hashlib.sha256, hashlib.md5, hashlib.sha1, hashlib.sha512, hashlib.blake2b, hashlib.blake2s):
            derived.add(algo(seed.encode()).hexdigest())
        derived.add(seed.encode().hex())
    assert password not in derived and not any(password.startswith(d[:16]) for d in derived)


def test_ic_c26_passwords_look_random_not_patterned(ns):
    ns.start()
    nodes = [ns.launch() for _ in range(3)]
    passwords = [nb.proxy_url(ns.guest_env(n))[1] for n in nodes]
    assert len(set(passwords)) == 3
    for password in passwords:
        assert len(set(password)) >= 8, password
        assert password != password[:32] * 2 and password != password[::-1]


def test_ic_c26_egress_and_rpc_authority_are_independent(tmp_path, monkeypatch):
    s = nb.NetSched(tmp_path, monkeypatch, rpc=True)
    try:
        s.start()
        node = s.launch()
        env = s.guest_env(node)
        secret = nb.proxy_url(env)[1]
        # The egress password is not an RPC token or capability, and carries none.
        rpc_values = {k: v for k, v in env.items() if "RPC" in k or "TOKEN" in k}
        assert secret not in str(rpc_values)
        reply = s.rpc("get_node", {"id": node}, token=secret, timeout=nb.WAIT)
        assert reply.get("ok") is not True
    finally:
        s.close()


def test_ic_c26_the_credential_is_in_the_invocation_not_the_container_environment_or_command_line(ns):
    ns.start()
    node = ns.launch()
    container = ns.running(node)
    password = nb.proxy_url(ns.invocation(container)["env"])[1]
    assert password not in str(container["Config"]) and password not in str(container["CreateArgv"])
    assert password not in " ".join(" ".join(c) for c in ns.fd.calls())
