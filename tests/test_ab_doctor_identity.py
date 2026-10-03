"""AB-R3c: doctor distinguishes an account label from mere authentication."""

import argparse
from types import SimpleNamespace

import pytest

from multiagents import cli, manifest
from multiagents.auth import AuthState


@pytest.mark.parametrize(
    "status, detail, expected",
    [
        ("authenticated", "logged in as person@example.org", "logged in as person@example.org"),
        ("authenticated", "logged in (host keyring)", "identity unverified"),
        ("authenticated", "container token present", "identity unverified"),
        ("authenticated", "", "identity unverified"),
        ("authenticated", "logged in as   ", "identity unverified"),
        ("not_authenticated", "logged in as person@example.org", "identity unverified"),
    ],
)
def test_ab_r3c_doctor_profile_uses_only_an_authenticated_account_label(
        monkeypatch, capsys, status, detail, expected):
    provider = SimpleNamespace(
        enabled=True, env={"HOME": "/profiles/second"},
        resolve_bin=lambda: SimpleNamespace(path="/bin/provider", via="PATH"),
    )
    config = SimpleNamespace(providers={}, warnings=[])
    monkeypatch.setattr(cli, "_resolve_if_project", lambda path: None)
    monkeypatch.setattr(cli, "load_config", lambda paths: config)
    monkeypatch.setattr(cli, "load_providers", lambda blocks: {"sample": provider})
    monkeypatch.setattr(cli, "executor_for", lambda *args: lambda name: object())
    monkeypatch.setattr(manifest, "cli_dependencies_section", lambda *args: 0)
    monkeypatch.setattr(cli, "_report_agents", lambda *args: 0)
    monkeypatch.setattr(cli.auth_mod, "check_all", lambda *args: {
        "sample": AuthState("sample", status, detail),
    })
    monkeypatch.setattr(cli.scripts, "build_env", lambda *args: {"HOME": "/profiles/second"})
    monkeypatch.setattr(cli, "_driver_host_states", lambda *args: {})
    monkeypatch.setattr(cli, "read_all", lambda *args: {})
    monkeypatch.setattr(cli, "find_shadowing", lambda *args: [])

    cli.cmd_doctor(argparse.Namespace(path=None, clear=None, force=False))

    profile_lines = [line.strip() for line in capsys.readouterr().out.splitlines()
                     if line.strip().startswith("profile ")]
    assert profile_lines == [f"profile /profiles/second — {expected}"]
