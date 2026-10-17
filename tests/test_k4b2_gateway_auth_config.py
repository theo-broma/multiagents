"""K4B2.4 (slice 4B.2): the `gateway_auth` provider field, through the
configuration loader and the scheduler.

Contract: context/specs/phase7-part2-contract.md, "K4B2.4" and IC-C28: values
`none` (default) or `anthropic`, normal provider layering and `extends`,
validated at load, naming the key. It selects a trusted upstream protocol, never
an account or a URL, and does not lift `needs.credentials`.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import ic_s4b as nb  # noqa: E402
import ic_s4b2 as ab  # noqa: E402
import ic_support as ic  # noqa: E402
import sc_harness as sc  # noqa: E402
from multiagents import config as config_mod  # noqa: E402
from multiagents.paths import ProjectPaths  # noqa: E402
from multiagents.providers import load_providers  # noqa: E402

ENDED = ("held", "failed", "done", "cancelled")


def load(tmp_path, monkeypatch, **fields):
    """The loaded configuration of a project whose `acme` provider carries `fields`
    (and a `child` provider extending it when `child` is given)."""
    import c3_harness as h
    h.as_root(monkeypatch)
    child = fields.pop("child", None)
    p = sc.Project(tmp_path)
    p.add_provider("acme")
    p.providers["acme"].update(fields)
    if child is not None:
        p.providers["child"] = dict(child, extends="acme")
    p.add_agent("worker", "acme", "acme/m1")
    p.project["executor"] = {"kind": "isolated", "isolated": {"backend": "docker"}}
    p.write()
    return config_mod.load(ProjectPaths(p.root), seed=False)


def declared(loaded, name="acme"):
    providers = load_providers(loaded.providers, loaded.provider_sources)
    return getattr(providers[name], "gateway_auth", "<missing>")


def test_k4b2_4_the_default_is_none(tmp_path, monkeypatch):
    assert declared(load(tmp_path, monkeypatch)) == "none"


@pytest.mark.parametrize("value", ["none", "anthropic"])
def test_k4b2_4_the_two_known_values_load(tmp_path, monkeypatch, value):
    assert declared(load(tmp_path, monkeypatch, gateway_auth=value)) == value


@pytest.mark.parametrize("value", ["Anthropic", "ANTHROPIC", "None", "NONE", "", " anthropic", "anthropic ", "openai",
                                   "claude", "api.anthropic.com", "https://api.anthropic.com", "http://evil.test",
                                   "anthropic,none", True, False, 0, 1, 1.5, ["anthropic"], {"protocol": "anthropic"}])
def test_k4b2_4_any_other_value_is_a_config_error_naming_the_key(tmp_path, monkeypatch, value):
    with pytest.raises(ValueError) as caught:
        load(tmp_path, monkeypatch, gateway_auth=value)
    assert "gateway_auth" in str(caught.value)


def test_k4b2_4_an_extending_provider_inherits_the_declaration(tmp_path, monkeypatch):
    loaded = load(tmp_path, monkeypatch, gateway_auth="anthropic", child={})
    assert declared(loaded, "child") == "anthropic"


def test_k4b2_4_an_extending_provider_may_override_the_declaration(tmp_path, monkeypatch):
    loaded = load(tmp_path, monkeypatch, gateway_auth="anthropic", child={"gateway_auth": "none"})
    assert declared(loaded, "child") == "none" and declared(loaded, "acme") == "anthropic"
    loaded = load(tmp_path / "again", monkeypatch, gateway_auth="none", child={"gateway_auth": "anthropic"})
    assert declared(loaded, "child") == "anthropic" and declared(loaded, "acme") == "none"


def test_k4b2_4_an_invalid_value_in_an_extending_provider_is_a_config_error_naming_the_key(tmp_path, monkeypatch):
    with pytest.raises(ValueError) as caught:
        load(tmp_path, monkeypatch, gateway_auth="none", child={"gateway_auth": "bridge"})
    assert "gateway_auth" in str(caught.value)


def test_k4b2_4_the_field_adds_no_url_account_or_proxy_setting_to_the_provider(tmp_path, monkeypatch):
    plain_config = load(tmp_path / "plain", monkeypatch)
    marked_config = load(tmp_path / "marked", monkeypatch, gateway_auth="anthropic")
    plain = load_providers(plain_config.providers, plain_config.provider_sources)["acme"]
    marked = load_providers(marked_config.providers, marked_config.provider_sources)["acme"]
    for field in ("env", "needs", "auth", "docker"):
        assert getattr(marked, field, None) == getattr(plain, field, None), field


def test_k4b2_4_an_invalid_value_launches_nothing(tmp_path, monkeypatch):
    s = ab.AuthSched(tmp_path, monkeypatch, gateway_auth="bridge")
    try:
        try:
            s.start()
        except AssertionError as refused:
            assert "gateway_auth" in str(refused), "the startup refusal must name the key"
        else:
            node = s.launch()
            view = s.wait(lambda: (v := s.get(node)) and v["state"] in ENDED and v, nb.WAIT, "a decision")
            assert "gateway_auth" in ic.text_of(view), "the config error must name the key"
        assert s.fd.containers() == {} and s.fd.networks() == {}
    finally:
        s.close()
