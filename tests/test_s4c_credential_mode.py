"""Slice 4C, D-S4C.1 / IC-C32: the `isolated_credentials` provider field, its
validation and layering, the mode recorded per attempt, and the `unsupported`
refusal `Unsupported("credential_handle")` with no fallback to shared mode.

Contract: context/specs/phase7-part2-contract.md, "Slice 4C".
Seams assumed (S1, S2): see tests/support/ic_s4c.py (NEED_INFO(s4c-seams)).
Tests that already pass on the 4B tree are marked as guards in their names.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import ic_s4b as nb  # noqa: E402
import ic_s4b2 as ab  # noqa: E402
import ic_s4b2_backend as be  # noqa: E402
import ic_s4c as c4  # noqa: E402
import ic_support as ic  # noqa: E402
import sc_harness as sc  # noqa: E402
from multiagents import config as config_mod  # noqa: E402
from multiagents.paths import ProjectPaths  # noqa: E402
from multiagents.providers import load_providers  # noqa: E402

ENDED = ("held", "failed", "done", "cancelled")
MODES = ("gateway", "projection", "unsupported")


def load(tmp_path, monkeypatch, **fields):
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
    return getattr(providers[name], "isolated_credentials", "<missing>")


def refusal(s, node, pattern):
    view = s.wait(lambda: (v := s.get(node)) and v["state"] in ENDED and v, nb.WAIT, "the node to end")
    assert view["state"] != "done", "the attempt must have been refused, it ran"
    found = [c for c in ic.causes(view) if pattern in ic.text_of(c).lower()]
    assert found, ic.text_of(ic.causes(view))[:600]
    assert found[0].get("retryable", False) is False
    return found[0]


def nothing_happened(s):
    assert s.fd.containers() == {}, "a refused launch must create no container"
    assert s.fd.networks() == {}, "a refused launch must create no network"
    assert s.fd.resources_created() == []
    assert s.fake.spawns() == 0, "a refused launch must not spawn the CLI on the host (no shared-mode fallback)"


@pytest.fixture
def make(tmp_path, monkeypatch):
    made = []

    def build(**kw):
        s = c4.CredSched(tmp_path, monkeypatch, **kw)
        made.append(s)
        return s
    yield build
    for s in made:
        s.close()


# ======================================================== the field and its validation (config)

def test_c32_the_default_is_unsupported(tmp_path, monkeypatch):
    assert declared(load(tmp_path, monkeypatch)) == "unsupported"


@pytest.mark.parametrize("value", MODES)
def test_c32_the_three_modes_load(tmp_path, monkeypatch, value):
    assert declared(load(tmp_path, monkeypatch, isolated_credentials=value)) == value


@pytest.mark.parametrize("value", ["Gateway", "GATEWAY", "Projection", "Unsupported", "", " gateway", "projection ",
                                   "none", "shared", "copy", "link", "gateway,projection", "anthropic", True, False,
                                   0, 1, 1.5, ["gateway"], {"mode": "gateway"}])
def test_c32_any_other_value_is_a_config_error_naming_the_key(tmp_path, monkeypatch, value):
    with pytest.raises(ValueError) as caught:
        load(tmp_path, monkeypatch, isolated_credentials=value)
    assert "isolated_credentials" in str(caught.value)


def test_c32_null_is_not_a_mode(tmp_path, monkeypatch):
    # an explicit null must not silently select a mode; it is the default or an error, never gateway/projection
    try:
        got = declared(load(tmp_path, monkeypatch, isolated_credentials=None))
    except ValueError as exc:
        assert "isolated_credentials" in str(exc)
    else:
        assert got == "unsupported"


def test_c32_an_extending_provider_inherits_the_mode(tmp_path, monkeypatch):
    loaded = load(tmp_path, monkeypatch, isolated_credentials="projection", child={})
    assert declared(loaded, "child") == "projection"


def test_c32_an_extending_provider_may_override_the_mode(tmp_path, monkeypatch):
    loaded = load(tmp_path, monkeypatch, isolated_credentials="gateway", child={"isolated_credentials": "unsupported"})
    assert declared(loaded, "child") == "unsupported" and declared(loaded, "acme") == "gateway"
    loaded = load(tmp_path / "again", monkeypatch, isolated_credentials="unsupported",
                  child={"isolated_credentials": "projection"})
    assert declared(loaded, "child") == "projection" and declared(loaded, "acme") == "unsupported"


def test_c32_an_invalid_value_in_an_extending_provider_is_a_config_error_naming_the_key(tmp_path, monkeypatch):
    with pytest.raises(ValueError) as caught:
        load(tmp_path, monkeypatch, isolated_credentials="gateway", child={"isolated_credentials": "shared"})
    assert "isolated_credentials" in str(caught.value)


def test_c32_the_field_adds_no_url_account_or_proxy_setting_to_the_provider(tmp_path, monkeypatch):
    plain = load_providers(*(lambda c: (c.providers, c.provider_sources))(load(tmp_path / "p", monkeypatch)))["acme"]
    marked_config = load(tmp_path / "m", monkeypatch, isolated_credentials="gateway")
    marked = load_providers(marked_config.providers, marked_config.provider_sources)["acme"]
    for field in ("env", "needs", "auth", "docker", "gateway_auth"):
        assert getattr(marked, field, None) == getattr(plain, field, None), field


def test_c32_the_shipped_providers_declare_their_modes(tmp_path, monkeypatch):
    loaded = load(tmp_path, monkeypatch)
    providers = load_providers(loaded.providers, loaded.provider_sources)
    assert getattr(providers["claude"], "isolated_credentials", None) == "gateway"
    for name in ("opencode", "opencode-go", "opencode-zen", "opencode-zai", "opencode-deepinfra"):
        assert getattr(providers[name], "isolated_credentials", None) == "projection", name
    for name in ("agy", "agy-partner"):
        assert getattr(providers[name], "isolated_credentials", None) == "unsupported", name


# ======================================================== the refusal: unsupported, never a fallback

@pytest.mark.parametrize("mode", [None, "unsupported"])
def test_c32_guard_unsupported_keeps_the_visible_credential_handle_refusal(make, mode):
    s = make(mode=mode)
    s.start()
    node = s.launch()
    cause = refusal(s, node, "credential_handle")
    assert "unsupported" in ic.text_of(cause).lower()
    nothing_happened(s)


@pytest.mark.parametrize("mode", [None, "unsupported"])
def test_c32_unsupported_never_falls_back_to_shared_mode(make, mode):
    """No container, no host spawn of the CLI, and the host's credential files are not placed anywhere."""
    s = make(mode=mode, vault=True)
    s.vault.mkdir(parents=True, exist_ok=True)
    c4.write_claude_vault(s.vault)
    c4.write_projection_vault(s.vault)
    s.start()
    refusal(s, s.launch(), "credential_handle")
    nothing_happened(s)
    secrets = c4.secrets_of_claude() + [c4.OAUTH_ACCESS, c4.OAUTH_REFRESH, c4.API_KEY]
    outside = {k: v for k, v in c4.tree_texts(s.root).items()}
    assert c4.leaks_of(outside, secrets) == {}, "a refused launch must not place vault material in the project"


def test_c32_unsupported_is_not_retried_into_another_mode(make):
    s = make(mode="unsupported")
    s.start()
    node = s.launch()
    refusal(s, node, "credential_handle")
    assert len(s.runs(node)) == 1
    assert s.fake.spawns() == 0 and s.fd.containers() == {}


@pytest.mark.parametrize("mode", ["gateway", "projection"])
def test_c32_a_declared_mode_lifts_the_credential_handle_refusal(make, mode):
    s = make(mode=mode)
    s.start()
    node = s.launch()
    container = s.running(node)
    assert container["State"]["Running"]
    assert s.get(node)["state"] == "running"


@pytest.mark.parametrize("mode", ["gateway", "projection"])
def test_c32_every_attempt_records_the_mode_it_ran_under(make, mode):
    s = make(mode=mode)
    s.start()
    node = s.launch()
    s.running(node)
    attempt = s.runs(node)[-1]["attempt_id"]
    assert s.recorded_modes(attempt) == [mode], s.evidence(attempt)


def test_c32_the_recorded_mode_is_per_attempt_and_survives_a_later_declaration_change(make):
    s = make(mode="gateway")
    s.start()
    first = s.launch()
    s.running(first)
    attempt = s.runs(first)[-1]["attempt_id"]
    s.declare_mode("unsupported")
    second = s.launch()
    refusal(s, second, "credential_handle")
    assert s.recorded_modes(attempt) == ["gateway"], "an earlier attempt's recorded mode must not change"


def test_c32_the_second_attempt_of_a_node_records_its_own_mode(make):
    s = make(mode="projection")
    s.start()
    node = s.launch()
    s.running(node)
    reply = s.rpc("steer_run", {"run_id": s.runs(node)[-1]["run_id"], "message": "again"}, timeout=nb.WAIT)
    assert reply.get("ok") is True, reply
    runs = s.wait_runs(node, 2, nb.WAIT)
    s.wait(lambda: s.recorded_modes(runs[-1]["attempt_id"]) or None, nb.WAIT, "the second attempt's record")
    assert s.recorded_modes(runs[-1]["attempt_id"]) == ["projection"]
    assert s.recorded_modes(runs[0]["attempt_id"]) == ["projection"]


def test_c32_a_gateway_failure_does_not_fall_back_to_another_mode(tmp_path, monkeypatch):
    """A gateway provider whose gateway cannot be built is held or failed: no container, no CLI on the host,
    no vault material placed anywhere (IC-D7)."""
    s = c4.CredSched(tmp_path, monkeypatch, mode="gateway", backend_spec=be.BROKEN_SPEC)
    try:
        s.start()
        node = s.launch()
        view = s.ended(node, nb.DECIDED_WAIT)
        assert view["state"] != "done"
        assert s.fd.containers() == {}
        assert s.fake.spawns() == 0
        assert c4.leaks_of(c4.tree_texts(s.root), c4.secrets_of_claude()) == {}
    finally:
        s.close()
