"""C17 checks beyond the docker argv seam: native login and action visibility."""

import argparse
import asyncio
import json
import os
import socket
import subprocess
import time

import pytest

from multiagents import auth, budget, cli, scripts
from multiagents.executor import docker as docker_mod
from multiagents.paths import global_config_dir
from test_c17_family_accounts import (
    NEW_SHAPE, OLD_SHAPE, _action, _rollout, login, refusal, shipped, two_codex, world,
)


@pytest.mark.parametrize("source", ["provider", "ambient", "overlay", "credential_owner"])
def test_host_login_action_cannot_inherit_container_action_marker(world, monkeypatch, source):
    marker = "MULTIAGENTS_CONTAINER_ACTION"
    raw = shipped()
    name = "agy"
    extra = None
    if source == "ambient":
        monkeypatch.setenv(marker, "1")
    elif source == "overlay":
        extra = {marker: "1"}
    else:
        raw["agy"]["env"] = {marker: "1"}
        if source == "credential_owner":
            name = "agy-b"
            raw[name] = {"extends": "agy", "auth_from": "agy"}
    ex = world.executor(raw)

    _, env = auth.login_command(name, ex.providers[name], ex, global_config_dir(),
                                extra_env=extra)

    assert marker not in env


def test_docker_login_sets_container_action_marker_only_in_exec_argv(login, monkeypatch):
    marker = "MULTIAGENTS_CONTAINER_ACTION"
    (login.paths.config / "providers.yaml").write_text(json.dumps({"providers": {
        "codex-b": {**NEW_SHAPE, "env": {marker: "0"}}}}))
    monkeypatch.setenv(marker, "0")
    original = auth.login_command

    def checked(*args, **kwargs):
        assert marker not in kwargs.get("extra_env", {})
        built = original(*args, **kwargs)
        assert marker not in built[1]
        return built

    monkeypatch.setattr(auth, "login_command", checked)

    flags, _, _ = login("codex-b")

    assert flags[marker] == "1"


@pytest.mark.parametrize("name, profile", [("codex", ".codex"), ("codex-b", ".codex-b")])
def test_login_action_executes_native_device_login_with_its_container_home(world, login, name, profile):
    flags, _, command = login(name)
    done = subprocess.run(command, env={**os.environ, **flags}, capture_output=True,
                          text=True, timeout=10)

    assert done.returncode == 0, done.stderr
    calls = world.calls()
    assert len(calls) == 1
    assert calls[0]["codex_home"] == str(world.home / profile)
    assert calls[0]["argv"][-2:] == ["login", "--device-auth"]


def test_instance_budget_does_not_scan_extra_accounts(world):
    ex = two_codex(world)
    ex.mounts()
    extra = world.tmp / "another-account"
    _rollout(extra, "reading", 80, time.time())

    done = _action(ex, "codex-b", "budget", MULTIAGENTS_CODEX_QUOTA_HOMES=str(extra))

    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout)["known"] is False


def test_collision_normalizes_parent_components(world):
    message = refusal(world, shipped(**{
        "codex-b": {"extends": "codex", "container_private_home": [".unused/../.codex"]}}))

    assert message is not None
    assert "codex-b" in message and str(world.home / ".codex") in message


def test_global_login_override_outside_existing_mounts_is_reachable(world):
    script = global_config_dir() / "providers" / "custom-login.sh"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("#!/bin/sh\nexit 0\n")
    ex = world.executor({"custom": {"bin": "true", "script": script.name,
                                    "container_private_home": [".custom"]}})

    assert (script, True) in ex.mounts()


@pytest.mark.parametrize("relative", ["../../etc", "../other-account", "/etc", ".", "",
                                     ".unused/../../etc"])
def test_private_home_cannot_escape_either_root(world, monkeypatch, relative):
    ex = two_codex(world, {"extends": "codex", "container_private_home": [relative]})
    monkeypatch.setattr(docker_mod, "_run", lambda *a, **kw: pytest.fail("docker was invoked"))

    state = ex.ensure_running()

    assert state["ok"] is False
    assert "container_private_home" in state["error"] and repr(relative) in state["error"]
    assert ex.private_state() == ex.private_state("codex-b") == {}
    with pytest.raises(ValueError, match="container_private_home"):
        ex.mounts()


def test_private_backing_is_normalized_inside_its_owner_root(world):
    ex = two_codex(world, {"extends": "codex",
                           "container_private_home": [".unused/../.codex-b"]})
    (home, backing), = ex.private_state("codex-b").items()

    assert home == world.home / ".codex-b"
    assert ".." not in backing.parts
    assert backing.parent.name == "codex-b"


def test_borrowed_credentials_do_not_exempt_an_unsafe_declaration(world):
    ex = world.executor({"owner": {"bin": "true", "container_private_home": [".owner"]},
                         "borrower": {"bin": "true", "auth_from": "owner",
                                      "container_private_home": ["/etc"]}})

    state = ex.ensure_running()

    assert state["ok"] is False
    assert "borrower" in state["error"] and "/etc" in state["error"]


def test_private_backing_symlink_cannot_escape_its_owner_root(world):
    ex = two_codex(world)
    root = docker_mod.state_root() / "container-state" / "shared" / "codex-b"
    root.mkdir(parents=True)
    outside = world.tmp / "outside"
    outside.mkdir()
    (root / ".codex-b").symlink_to(outside, target_is_directory=True)

    state = ex.ensure_running()

    assert state["ok"] is False
    assert "host backing root" in state["error"]
    assert ex.private_state() == {}


def test_admission_rechecks_backing_after_a_read_only_lookup(world):
    ex = two_codex(world)
    (backing,) = ex.private_state("codex-b").values()
    backing.parent.mkdir(parents=True)
    outside = world.tmp / "outside"
    outside.mkdir()
    backing.symlink_to(outside, target_is_directory=True)

    state = ex.ensure_running()

    assert state["ok"] is False
    assert "host backing root" in state["error"]
    assert ex.private_state("codex-b") == {}


def test_collision_is_reported_cleanly_by_runtime_readers(world, monkeypatch):
    ex = two_codex(world, OLD_SHAPE)
    provider = ex.providers["codex-b"]
    monkeypatch.setattr(docker_mod, "_run", lambda *a, **kw: pytest.fail("docker was invoked"))

    env = scripts.build_env("codex-b", provider, ex)
    state = auth.check("codex-b", provider, ex, global_config_dir())
    reading = budget.read_provider("codex-b", provider, ex, global_config_dir())
    code, _, error = scripts.run_action("codex-b", provider, ex, "check", global_config_dir())
    argv, login_env = auth.login_command("codex-b", provider, ex, global_config_dir())
    refused = subprocess.run(argv, env=login_env, capture_output=True, text=True, timeout=10)

    assert "codex-b" in env["MULTIAGENTS_CONFIG_ERROR"]
    assert "MULTIAGENTS_PRIVATE_BACKING" not in env
    assert not state.ok and "codex-b" in state.detail
    assert not reading.known and "codex-b" in reading.note
    assert code != 0 and "codex-b" in error
    assert refused.returncode != 0 and "codex-b" in refused.stderr
    assert ex.preflight() and ex.mount_drift() and ex.stale_mounts()
    assert ex.exec_in_running(["true"], 1)[0] != 0
    assert world.calls() == []


def test_inside_admission_refuses_collision_before_launching(world, monkeypatch):
    ex = two_codex(world, OLD_SHAPE)
    monkeypatch.setattr(ex, "inside", lambda: True)

    with pytest.raises(RuntimeError, match="codex-b"):
        asyncio.run(ex.start(["true"], world.tmp, {}))
    assert world.calls() == []


@pytest.mark.parametrize("action", ["up", "status"])
def test_docker_cli_collision_is_a_clean_refusal(world, monkeypatch, capsys, action):
    ex = two_codex(world, OLD_SHAPE)
    monkeypatch.setattr(cli, "_resolve", lambda _: ex.paths)
    monkeypatch.setattr(cli, "_docker_executor", lambda _: ex)
    monkeypatch.setattr(ex, "image_exists", lambda _: True)
    monkeypatch.setattr(ex, "container_state", lambda _: "absent")

    code = cli.cmd_docker(argparse.Namespace(action=action, path=str(ex.paths.root)))

    captured = capsys.readouterr()
    assert code == 1
    assert "codex-b" in captured.out + captured.err
    assert "Traceback" not in captured.out + captured.err


@pytest.mark.parametrize("secret", [".ssh/id_rsa", ".aws/credentials", ".gnupg/private.key",
                                   ".codex/auth.json", ".multiagents/container-state/shared/custom/vault/token"])
def test_global_action_symlink_cannot_bind_a_credential(world, secret):
    target = world.home / secret
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("credential")
    script = global_config_dir() / "providers" / "custom-login.sh"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.symlink_to(target)
    ex = world.executor({"custom": {"bin": "true", "script": script.name,
                                    "container_private_home": [".custom"]}})

    assert ex.ensure_running()["ok"] is False
    with pytest.raises(ValueError, match="action script bind"):
        ex.mounts()


def test_auth_json_inside_script_directory_cannot_be_bound(world):
    script = global_config_dir() / "providers" / "auth.json"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("credential")
    ex = world.executor({"custom": {"bin": "true", "script": script.name,
                                    "container_private_home": [".custom"]}})

    assert ex.ensure_running()["ok"] is False
    with pytest.raises(ValueError, match="action script bind"):
        ex.mounts()


def test_hard_link_to_a_credential_cannot_be_bound(world):
    target = world.home / "auth.json"
    target.write_text("credential")
    script = global_config_dir() / "providers" / "custom-login.sh"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.hardlink_to(target)
    ex = world.executor({"custom": {"bin": "true", "script": script.name,
                                    "container_private_home": [".custom"]}})

    assert ex.ensure_running()["ok"] is False
    with pytest.raises(ValueError, match="action script bind"):
        ex.mounts()


@pytest.mark.parametrize("source", ["home", "directory", "socket"])
def test_global_action_bind_never_adds_a_directory_or_socket(world, source):
    directory = global_config_dir() / "providers"
    directory.mkdir(parents=True)
    path = directory / "custom-login.sh"
    sock = None
    if source == "home":
        path.symlink_to(world.home, target_is_directory=True)
    elif source == "directory":
        path.mkdir()
    else:
        sock = socket.socket(socket.AF_UNIX)
        sock.bind(str(path))
    try:
        ex = world.executor({"custom": {"bin": "true", "script": path.name,
                                        "container_private_home": [".custom"]}})
        mounts = dict(ex.mounts())

        assert path not in mounts
        assert directory not in mounts
        assert world.home not in mounts
    finally:
        if sock is not None:
            sock.close()
