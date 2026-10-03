"""Additional QD-R4a/R7 coverage for shared credentials and unsafe claims."""

import json
from types import SimpleNamespace

import pytest

from multiagents.monitor import quota, snapshot
from test_c19_quota_details_page import Actions, make, reading, uniq, win

pytestmark = pytest.mark.real_providers


def test_qd_r7_two_aliases_of_one_profile_share_the_identity_fetch(make, monkeypatch, tmp_path):
    one, two = uniq("claude"), uniq("claude-alias")
    acts = Actions()
    acts.identities = {(one, None): "shared@example.invalid", (two, None): "shared@example.invalid"}
    fx = make({n: reading(n, {"week": win(20)}) for n in (one, two)}, acts)
    providers = {n: SimpleNamespace(
        name=n, billing="plan", available=lambda: "/fake", container_account="",
        budget_profile_env="CLAUDE_CONFIG_DIR",
        env={"CLAUDE_CONFIG_DIR": str(tmp_path / "profile")}) for n in (one, two)}
    monkeypatch.setattr(snapshot, "load_providers", lambda _: providers)
    assert fx.revealed(one, None) == fx.revealed(two, None) == "shared@example.invalid"
    assert len(acts.calls) == 1


def test_qd_r4a_auth_from_uses_the_credential_owners_action(make, monkeypatch):
    owner, partner = uniq("owner"), uniq("partner")
    acts = Actions()
    acts.identities[(owner, None)] = "owner@example.invalid"
    fx = make({n: reading(n, {"week": win(20)}) for n in (owner, partner)}, acts)
    providers = {n: SimpleNamespace(
        name=n, billing="plan", available=lambda: "/fake", container_account="",
        auth_from=owner if n == partner else "") for n in (owner, partner)}
    monkeypatch.setattr(snapshot, "load_providers", lambda _: providers)
    assert fx.revealed(partner, None) == fx.revealed(owner, None) == "owner@example.invalid"
    assert acts.calls == [(owner, None)]


def test_qd_r7_an_expired_identity_is_refetched(make, monkeypatch):
    name = uniq("owner")
    acts = Actions()
    acts.identities[(name, None)] = "before@example.invalid"
    fx = make({name: reading(name, {"week": win(20)})}, acts)
    assert fx.revealed(name, None) == "before@example.invalid"
    with quota._LOCK:
        key = next(k for k in quota._CACHE if k[0] == str(fx.paths.config))
        stamp, identity = quota._CACHE[key]
        monkeypatch.setitem(quota._CACHE, key, (stamp - quota.IDENTITY_TTL - 1, identity))
    acts.identities[(name, None)] = "after@example.invalid"
    assert fx.revealed(name, None) == "after@example.invalid"
    assert len(acts.calls) == 2


def test_c19_a_truncated_script_note_is_not_repeated_without_windows(make):
    name = uniq("metered")
    note = "Metered billing without a quota surface. " * 5
    acts = Actions()
    acts.usage[name] = note[:120]
    fx = make({name: reading(name, known=False, note=note)}, acts)
    rows = fx.wait(lambda rows: rows[name]["lines_from"] == "script")
    assert rows[name]["lines"] == [note]


@pytest.mark.parametrize("secret", [
    "sk-secret-account", "rt-secret-refresh", "Bearer hidden-token",
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJzZWNyZXQifQ.signature",
])
def test_qd_r7_a_secret_disguised_as_a_scalar_claim_is_unknown(make, secret):
    name = uniq("owner")
    acts = Actions()
    acts.behave[name] = lambda _: (0, json.dumps({"identity": secret, "kind": "account"}), "")
    fx = make({name: reading(name, {"week": win(20)})}, acts)
    assert fx.revealed(name, None) is None
    assert secret not in fx.get("/api/quota").text
