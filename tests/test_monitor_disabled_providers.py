"""Disabled providers have no monitor card and schedule no background work."""

from types import SimpleNamespace

import pytest

from multiagents.manifest import ProbeResult
from multiagents.monitor import snapshot
from multiagents.providers import Provider
from test_c19_quota_details_page import Actions, entries, make, reading, uniq, win

pytestmark = pytest.mark.real_providers


@pytest.mark.parametrize("kind", ["local", "docker"])
@pytest.mark.parametrize("with_scripts", [False, True])
def test_disabled_providers_are_absent_and_run_no_monitor_actions(
        make, monkeypatch, kind, with_scripts):
    import multiagents.executor as executor_mod
    import multiagents.manifest as manifest_mod
    import multiagents.scripts as scripts_mod

    enabled, disabled = uniq("enabled"), uniq("disabled")
    acts = Actions()
    acts.identities = {(enabled, None): "enabled@example.invalid",
                       (disabled, None): "disabled@example.invalid"}
    fx = make({n: reading(n, {"week": win(20)}) for n in (enabled, disabled)}, acts)
    providers = {n: Provider.from_dict(n, {"enabled": n == enabled, "bin": "/fake"})
                 for n in (enabled, disabled)}
    monkeypatch.setattr(snapshot, "load_providers", lambda _: providers)
    monkeypatch.setattr(executor_mod, "executor_for",
                        lambda *a: lambda name: SimpleNamespace(kind=kind))
    actions, installations, probes = [], [], []

    def run_action(name, *args, **kwargs):
        actions.append((name, args[2]))
        return acts(name, *args, **kwargs)

    def available(provider):
        installations.append(provider.name)
        return "/fake"

    def probe(name, *args, **kwargs):
        probes.append(name)
        return ProbeResult(state="verified")

    monkeypatch.setattr(scripts_mod, "run_action", run_action)
    monkeypatch.setattr(Provider, "available", available)
    monkeypatch.setattr(manifest_mod, "probe", probe)

    rows = snapshot.providers_view(fx.paths, fx.config, fx.tree, with_scripts=with_scripts)
    assert [r["name"] for r in rows] == [enabled]
    rows = fx.wait(lambda rows: all(e["identity_available"] for e in entries(rows[enabled]))
                   and rows[enabled]["lines_from"] == "script"
                   and rows[enabled]["install_status"] == "installed")
    assert set(rows) == {enabled}
    assert fx.revealed(enabled, None) == "enabled@example.invalid"
    assert fx.reveal(disabled, None).status == 400
    assert "disabled@example.invalid" not in fx.get("/api/quota").text
    assert (enabled, "identity") in actions
    assert (enabled, "usage") in actions
    assert disabled not in installations
    assert disabled not in probes
    assert not any(name == disabled for name, _action in actions)
