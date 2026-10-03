"""FQ-R1: auth sharing preserves the pre-C21 serial budget call count."""

from collections import Counter
import json
import threading
from types import SimpleNamespace

import pytest

from multiagents import budget, scripts
from multiagents.config import parse_once
from multiagents.providers import load_providers, resolved_profile


pytestmark = pytest.mark.real_providers


@pytest.mark.parametrize("flags", [{}, {"use_cache": False}, {"force": True},
                                   {"use_cache": False, "force": True}])
@pytest.mark.parametrize("dependent_first", [False, True])
def test_fq_r1_auth_from_only_fetches_each_provider_as_serial_did(
        tmp_path, monkeypatch, flags, dependent_first):
    owner, dependent = "c21-auth-owner", "c21-auth-dependent"
    profile = str(tmp_path / "shared-profile")
    raw = {
        owner: {"bin": "sh", "env": {"C21_PROFILE": profile},
                "budget_profile_env": "C21_PROFILE"},
        dependent: {"bin": "sh", "auth_from": owner,
                    "budget_profile_env": "C21_PROFILE"},
    }
    if dependent_first:
        raw = dict(reversed(list(raw.items())))
    providers = load_providers(raw)
    assert providers[dependent].auth_owner is providers[owner]
    assert not providers[dependent].budget_from
    assert resolved_profile(providers[owner]) == resolved_profile(providers[dependent]) == profile

    calls = Counter()
    lock = threading.Lock()
    payloads = {
        owner: {"known": True, "headroom": 0.8, "windows": {}},
        dependent: {"known": True, "headroom": 0.3, "windows": {}},
    }

    def action(name, provider, executor, action, config_dir,
               project_config=None, **kwargs):
        assert action == "budget"
        with lock:
            calls[name] += 1
        return 0, json.dumps(payloads[name]), ""

    monkeypatch.setattr(scripts, "run_action", action)
    executor = SimpleNamespace(kind="local")
    config_dir = tmp_path / "global"
    project_config = tmp_path / "project-config"
    budget.invalidate_cache()
    try:
        # Pre-C21 read_all traversed providers serially with one parse view
        # and one shared _pass. Use that same fetch path as the reference.
        seen = set()
        with parse_once():
            serial = {
                name: budget.read_provider(
                    name, provider, executor, config_dir, project_config,
                    providers=providers, _pass=seen, **flags)
                for name, provider in providers.items()
            }
        serial_calls = calls.copy()
        assert serial_calls == Counter({owner: 1, dependent: 1})

        budget.invalidate_cache()
        calls.clear()
        parallel = budget.read_all(
            providers, lambda _name: executor, config_dir, project_config, **flags)
        assert calls == serial_calls
        assert list(parallel) == list(serial)
        assert {name: reading.to_dict() for name, reading in parallel.items()} == {
            name: reading.to_dict() for name, reading in serial.items()}
    finally:
        budget.invalidate_cache()
