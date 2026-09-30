"""H6 — `refresh-models` must not erase models because a listing failed.

context/specs/phase6-hardening.md (H6). The refresh rewrites models.yaml
wholesale from only the providers that listed, so a provider whose listing
fails — non-zero exit, timeout, a missing binary, output that parses to no
models — used to lose its own previous entries, and codex, which keeps no
model cache until its first run, failed every refresh before that first use.
Both must now be reported in `problems` with the reason while the file keeps
whatever that provider already had. A provider that lists still replaces its
own entries wholesale, and binary lookup stays on Provider.resolve_bin()
(H7 PS-R2).

Fake providers through a tmp config dir and fake binaries on a reduced PATH;
the codex cases drive the real shipped adapter with its profile under a tmp
HOME. Nothing reaches the network.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import yaml

import pytest

from multiagents.models import refresh_models
from multiagents.paths import shipped_defaults_dir
from multiagents.providers import Provider

DEFAULTS = shipped_defaults_dir()


def _exe(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n" + body + "\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


def _provider(name: str, **fields) -> Provider:
    return Provider.from_dict(name, {"bin": name, "spawn": {"args": ["go"]}, **fields})


def _fake_path(tmp_path: Path, monkeypatch) -> Path:
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}/usr/bin{os.pathsep}/bin")
    return bindir


def _written(target: Path) -> dict:
    data = yaml.safe_load(target.read_text()) or {}
    return data.get("models") or {}


def _ids(entries) -> list:
    return sorted(m.get("id") for m in entries or [] if isinstance(m, dict))


# ===========================================================================
# models_cmd providers: every failure mode keeps the previous entries
# ===========================================================================

def test_h6_failing_listing_keeps_its_own_entries_and_reports_the_reason(
        tmp_path, monkeypatch):
    bindir = _fake_path(tmp_path, monkeypatch)
    _exe(bindir / "flake", 'printf "old-one\\nold-two\\n"')
    p = _provider("flake", models_cmd=["flake", "models"])
    target = tmp_path / "models.yaml"

    first = refresh_models({"flake": p}, target)
    assert first["counts"]["flake"] == 2 and not first["problems"], first

    _exe(bindir / "flake", 'echo "flake: listing broke" >&2\nexit 3')
    second = refresh_models({"flake": p}, target)
    assert "flake: listing broke" in second["problems"]["flake"], second
    assert "kept 2 models" in second["problems"]["flake"], second
    assert _ids(_written(target)["flake"]) == ["old-one", "old-two"]


def test_h6_failing_provider_never_erases_another_providers_entries(
        tmp_path, monkeypatch):
    bindir = _fake_path(tmp_path, monkeypatch)
    _exe(bindir / "good", 'printf "good-a\\ngood-b\\n"')
    _exe(bindir / "bad", 'printf "bad-a\\n"')
    providers = {"good": _provider("good", models_cmd=["good", "models"]),
                 "bad": _provider("bad", models_cmd=["bad", "models"])}
    target = tmp_path / "models.yaml"
    refresh_models(providers, target)

    # good replaces wholesale (a withdrawn model disappears); bad breaks.
    _exe(bindir / "good", 'printf "good-b\\ngood-c\\n"')
    _exe(bindir / "bad", 'exit 7')
    out = refresh_models(providers, target)
    models = _written(target)
    assert _ids(models["good"]) == ["good-b", "good-c"], models
    assert _ids(models["bad"]) == ["bad-a"], models
    assert "bad" in out["problems"] and "good" not in out["problems"], out
    assert "exited 7" in out["problems"]["bad"], out


def test_h6_missing_binary_keeps_entries_naming_every_place_searched(
        tmp_path, monkeypatch):
    bindir = _fake_path(tmp_path, monkeypatch)
    _exe(bindir / "gone", 'printf "gone-a\\n"')
    p = _provider("gone", models_cmd=["gone", "models"],
                  bin_search=[str(tmp_path / "extra")])
    target = tmp_path / "models.yaml"
    refresh_models({"gone": p}, target)

    (bindir / "gone").unlink()
    out = refresh_models({"gone": p}, target)
    problem = out["problems"]["gone"]
    assert "not found" in problem and "searched" in problem, problem
    assert str(tmp_path / "extra") in problem    # bin_search is searched (H7 PS-R2)
    assert _ids(_written(target)["gone"]) == ["gone-a"]


def test_h6_timeout_keeps_previous_entries(tmp_path, monkeypatch):
    bindir = _fake_path(tmp_path, monkeypatch)
    _exe(bindir / "slow", 'printf "slow-a\\n"')
    p = _provider("slow", models_cmd=["slow", "models"])
    target = tmp_path / "models.yaml"
    refresh_models({"slow": p}, target)

    _exe(bindir / "slow", 'sleep 5')
    out = refresh_models({"slow": p}, target, timeout=1)
    assert "TimeoutExpired" in out["problems"]["slow"], out
    assert _ids(_written(target)["slow"]) == ["slow-a"]


@pytest.mark.parametrize("body", [
    'printf ""',
    'printf "Fetching models...\\nplan tiers only\\n"',
], ids=["silent", "unparsable"])
def test_h6_output_that_parses_to_no_models_keeps_previous_entries(
        tmp_path, monkeypatch, body):
    bindir = _fake_path(tmp_path, monkeypatch)
    _exe(bindir / "noisy", 'printf "noisy-a\\n"')
    p = _provider("noisy", models_cmd=["noisy", "models"])
    target = tmp_path / "models.yaml"
    refresh_models({"noisy": p}, target)

    _exe(bindir / "noisy", body)
    out = refresh_models({"noisy": p}, target)
    assert "parsed to no models" in out["problems"]["noisy"], out
    assert _ids(_written(target)["noisy"]) == ["noisy-a"]


# ===========================================================================
# the `models` action path (a provider with no models_cmd)
# ===========================================================================

def test_h6_failing_models_action_keeps_previous_entries(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    scripts_dir = tmp_path / "cfg" / "providers"
    _exe(scripts_dir / "acme.sh",
         'case "$1" in models) printf "acme-one\\tAcme one\\n";; *) exit 64;; esac')
    p = _provider("acme", models_parse="tsv")
    target = tmp_path / "models.yaml"
    refresh_models({"acme": p}, target, config_dir=tmp_path / "cfg")
    assert _ids(_written(target)["acme"]) == ["acme-one"]

    _exe(scripts_dir / "acme.sh",
         'case "$1" in models) echo "acme: listing broke" >&2; exit 3;; '
         '*) exit 64;; esac')
    out = refresh_models({"acme": p}, target, config_dir=tmp_path / "cfg")
    assert "acme: listing broke" in out["problems"]["acme"], out
    assert _ids(_written(target)["acme"]) == ["acme-one"]


def test_h6_models_action_output_that_parses_to_no_models_keeps_previous_entries(
        tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    scripts_dir = tmp_path / "cfg" / "providers"
    _exe(scripts_dir / "acme.sh",
         'case "$1" in models) printf "acme-one\\tAcme one\\n";; *) exit 64;; esac')
    p = _provider("acme", models_parse="tsv")
    target = tmp_path / "models.yaml"
    refresh_models({"acme": p}, target, config_dir=tmp_path / "cfg")

    _exe(scripts_dir / "acme.sh", 'case "$1" in models) printf "soon\\n";; '
                                  '*) exit 64;; esac')
    out = refresh_models({"acme": p}, target, config_dir=tmp_path / "cfg")
    assert "parsed to no models" in out["problems"]["acme"], out
    assert _ids(_written(target)["acme"]) == ["acme-one"]


# ===========================================================================
# codex before its first use: the shipped adapter, a profile under a tmp HOME
# ===========================================================================

def _codex_provider() -> Provider:
    raw = yaml.safe_load((DEFAULTS / "providers.yaml").read_text())["providers"]
    assert "codex" in raw
    return Provider.from_dict("codex", raw["codex"])


def test_h6_codex_before_first_use_is_reported_and_others_still_refresh(
        tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    bindir = _fake_path(tmp_path, monkeypatch)
    _exe(bindir / "stub", 'printf "stub-a\\n"')
    providers = {"codex": _codex_provider(),
                 "stub": _provider("stub", models_cmd=["stub", "models"])}
    target = tmp_path / "models.yaml"

    out = refresh_models(providers, target, config_dir=tmp_path / "cfg")
    models = _written(target)
    assert out["counts"].get("stub") == 1, out
    assert _ids(models["stub"]) == ["stub-a"], models
    assert "codex" not in models                  # nothing to keep on day one
    reason = out["problems"]["codex"]
    assert "model cache" in reason, reason        # the adapter's own reason


def test_h6_codex_models_arrive_after_first_use_and_survive_a_later_failure(
        tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    bindir = _fake_path(tmp_path, monkeypatch)
    _exe(bindir / "stub", 'printf "stub-a\\n"')
    providers = {"codex": _codex_provider(),
                 "stub": _provider("stub", models_cmd=["stub", "models"])}
    target = tmp_path / "models.yaml"
    profile = tmp_path / "home" / ".multiagents" / "profiles" / "codex"

    out = refresh_models(providers, target, config_dir=tmp_path / "cfg")
    assert "codex" in out["problems"], out

    # The first use writes the cache; the next refresh lists codex for real.
    profile.mkdir(parents=True, exist_ok=True)
    (profile / "models_cache.json").write_text(json.dumps({"models": [
        {"slug": "gpt-6-sol", "display_name": "GPT-6 Sol", "visibility": "list"},
        {"slug": "gpt-6-hidden", "display_name": "Hidden", "visibility": "hide"},
    ]}))
    out = refresh_models(providers, target, config_dir=tmp_path / "cfg")
    assert "codex" not in out["problems"], out
    assert _ids(_written(target)["codex"]) == ["gpt-6-sol"]

    # The cache going away again must not erase what the good refresh wrote,
    # and must not cost the other provider its refresh either.
    (profile / "models_cache.json").unlink()
    out = refresh_models(providers, target, config_dir=tmp_path / "cfg")
    models = _written(target)
    assert _ids(models["codex"]) == ["gpt-6-sol"], models
    assert _ids(models["stub"]) == ["stub-a"], models
    assert "model cache" in out["problems"]["codex"], out
