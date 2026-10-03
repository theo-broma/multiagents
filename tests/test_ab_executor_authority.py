"""AB-R2: budget executor selection cannot inherit a host auth request."""

import argparse
import json
import subprocess
from pathlib import Path

import pytest

import test_ab_agy_second_account as ab

from multiagents import auth, budget, cli, config, scripts
from multiagents.executor import executor_for
from multiagents.paths import ProjectPaths, global_config_dir
from multiagents.providers import load_providers

world = ab.world


@pytest.mark.parametrize("case", [
    "test_ab_r3d_two_spellings_of_one_home_are_one_identity",
    "test_ab_r3d_a_changed_home_within_the_ttl_is_a_different_cache_identity",
])
def test_ab_r2_cached_profiles_ignore_an_ambient_host_request(world, monkeypatch, case):
    monkeypatch.setenv("MULTIAGENTS_PROFILE", "host")
    getattr(ab, case)(world)


def test_ab_r2_provider_environment_cannot_change_the_executor(world, monkeypatch):
    provider = world.providers["agy-b"]
    world.login(world.second_home, "valid", ab.SECOND)
    monkeypatch.setitem(provider.env, "MULTIAGENTS_EXECUTOR", "local")

    assert ab.headroom_is(ab.read(world, "agy-b"), ab.SECOND)


def test_ab_r2_switching_executors_does_not_reuse_the_other_accounts_cache(world):
    world.login(world.second_home, "valid", ab.SECOND)

    local = ab.read(world, "agy-b", kind="local", use_cache=True)
    docker = ab.read(world, "agy-b", kind="docker", use_cache=True)
    local_again = ab.read(world, "agy-b", kind="local", use_cache=True)

    assert ab.headroom_is(local, ab.HOST_KEYRING)
    assert ab.headroom_is(docker, ab.SECOND)
    assert ab.headroom_is(local_again, ab.HOST_KEYRING)


def test_ab_r2_budget_ignores_an_explicit_host_auth_profile(world):
    world.login(world.second_home, "valid", ab.SECOND)
    code, out, err = scripts.run_action(
        "agy-b", world.providers["agy-b"], ab.PerProviderExecutor(world),
        "budget", world.config_dir,
        extra_env={"MULTIAGENTS_PROFILE": "host", "MULTIAGENTS_EXECUTOR": "local"})

    assert code == 0, err
    assert json.loads(out)["headroom"] == pytest.approx(ab.SECOND)
    assert world.docker_execs


@pytest.mark.parametrize("kind", ["", "unsupported"])
def test_ab_r2_an_unknown_executor_does_not_read_the_host(world, kind):
    reading = ab.read(world, "agy-b", kind=kind)

    assert not reading.known
    assert reading.headroom is None
    assert world.records("agy") == []


def test_ab_r2_a_cached_host_reading_cannot_hide_an_unknown_executor(world):
    assert ab.headroom_is(
        ab.read(world, "agy-b", kind="local", use_cache=True), ab.HOST_KEYRING)
    reading = ab.read(world, "agy-b", kind="unsupported", use_cache=True)

    assert not reading.known
    assert reading.headroom is None


@pytest.mark.parametrize("executor", [None, object()])
def test_ab_r2_a_cached_host_reading_cannot_hide_a_missing_executor(world, executor):
    assert ab.headroom_is(
        ab.read(world, "agy-b", kind="local", use_cache=True), ab.HOST_KEYRING)
    reading = budget.read_provider(
        "agy-b", world.providers["agy-b"], executor, world.config_dir,
        providers=world.providers, use_cache=True)

    assert not reading.known
    assert reading.headroom is None


def test_ab_r2_a_missing_container_does_not_read_the_host(world, monkeypatch):
    executor = ab.PerProviderExecutor(world)
    monkeypatch.setattr(executor, "container", "")
    code, out, err = scripts.run_action(
        "agy-b", world.providers["agy-b"], executor, "budget", world.config_dir)

    assert code == 0, err
    assert not json.loads(out)["known"]
    assert "multiagents docker login agy-b" in json.loads(out)["note"]
    assert world.records("agy") == []


def test_ab_r5_a_direct_budget_without_an_executor_keeps_the_local_behavior(world, monkeypatch):
    monkeypatch.delenv("MULTIAGENTS_EXECUTOR", raising=False)
    env = scripts.build_env("agy-b", world.providers["agy-b"], ab.PerProviderExecutor(world))
    env.pop("MULTIAGENTS_EXECUTOR")
    result = subprocess.run(
        ["sh", str(ab.SRC / "defaults" / "providers" / "agy.sh"), "budget"],
        env=env, capture_output=True, text=True, timeout=10)

    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["known"]
    assert data["headroom"] == pytest.approx(ab.HOST_KEYRING)
    assert not world.docker_execs
    assert len(world.records("agy")) == 1


def test_ab_r2_the_core_passes_an_unresolvable_executor_explicitly(world):
    env = scripts.build_env("agy-b", world.providers["agy-b"], object())
    assert "MULTIAGENTS_EXECUTOR" in env
    assert env["MULTIAGENTS_EXECUTOR"] == ""

    code, out, err = scripts.run_action(
        "agy-b", world.providers["agy-b"], object(), "budget", world.config_dir)

    assert code == 0, err
    assert not json.loads(out)["known"]
    assert world.records("agy") == []


@pytest.mark.parametrize("surface", ["budget", "doctor", "auth"])
def test_ab_r2_docker_projects_pass_the_executor_to_provider_actions(
        world, monkeypatch, capsys, surface):
    root = ab._project(world)
    paths = ProjectPaths(root)
    loaded = config.load(paths)
    providers = load_providers(loaded.providers)
    executor_of = executor_for(paths, loaded, providers)
    budget.invalidate_cache()
    calls = []
    original = subprocess.Popen

    def capture(argv, *args, **kwargs):
        if len(argv) >= 3 and Path(str(argv[1])).name == "agy.sh":
            calls.append((argv[2], kwargs["env"].get("MULTIAGENTS_EXECUTOR")))
        return original(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", capture)
    if surface == "budget":
        budget.read_all(providers, executor_of, global_config_dir(), paths.config,
                        use_cache=False)
    elif surface == "doctor":
        cli.cmd_doctor(argparse.Namespace(path=str(root), clear=None, force=False))
    else:
        cli.cmd_auth(argparse.Namespace(path=str(root), action="status"))

    expected_action = "check" if surface == "auth" else "budget"
    assert any(action == expected_action for action, _kind in calls), calls
    assert all(kind == "docker" for _action, kind in calls), calls


def test_ab_r2_docker_auth_login_has_an_explicit_executor(world):
    root = ab._project(world)
    paths = ProjectPaths(root)
    loaded = config.load(paths)
    providers = load_providers(loaded.providers)
    executor_of = executor_for(paths, loaded, providers)

    argv, env = auth.login_command(
        "agy-b", providers["agy-b"], executor_of("agy-b"),
        global_config_dir(), paths.config)

    assert argv[-1] == "login"
    assert env["MULTIAGENTS_EXECUTOR"] == "docker"
