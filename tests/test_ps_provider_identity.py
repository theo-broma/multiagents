"""PS1 / F113: script identity is the configured provider instance."""
import subprocess
from types import SimpleNamespace

import pytest

from multiagents import auth, scripts
from multiagents.providers import Provider, load_providers

ACTION_TIMEOUT = 3


def executor_for(kind="local"):
    return SimpleNamespace(kind=kind, container="test", private_state=lambda name: {})


def install_script(tmp_path, name="owner.sh"):
    folder = tmp_path / "providers"
    folder.mkdir(exist_ok=True)
    path = folder / name
    path.write_text('printf "%s|%s|%s|%s\\n" "$MULTIAGENTS_PROVIDER" "$OWNER_CREDENTIAL" "$MULTIAGENTS_CONTAINER_ACCOUNT" "$1"\n')
    return path


@pytest.mark.parametrize("surface", ["build_env", "resolve", "run_action", "exec_action"])
@pytest.mark.parametrize("installed", [False, True])
def test_ps1_identity_mismatch_is_rejected_before_any_action(tmp_path, surface, installed):
    marker = tmp_path / "ran"
    provider = Provider.from_dict("actual", {"script": "owner.sh", "bin": "sh"})
    if installed:
        path = install_script(tmp_path)
        path.write_text(f'touch "{marker}"\n')
    executor = SimpleNamespace(kind="local")
    with pytest.raises(ValueError) as caught:
        if surface == "build_env":
            scripts.build_env("requested", provider, executor)
        elif surface == "resolve":
            scripts.resolve("requested", provider, tmp_path)
        else:
            getattr(scripts, surface)("requested", provider, executor, "check", tmp_path)
    assert "actual" in str(caught.value) and "requested" in str(caught.value)
    assert not marker.exists()


@pytest.mark.parametrize("depth", [1, 4])
def test_ps1_inherited_script_sees_child_identity_and_inherited_credentials(tmp_path, depth):
    install_script(tmp_path)
    raw = {"owner": {"bin": "sh", "script": "owner.sh", "env": {"OWNER_CREDENTIAL": "inherited"}, "container_account": "account-a"}}
    parent = "owner"
    for level in range(depth):
        child = f"child-{level}"
        raw[child] = {"extends": parent}
        parent = child
    provider = load_providers(raw)[child]
    assert provider.name == child
    code, out, err = scripts.run_action(child, provider, executor_for("docker"), "check", tmp_path, timeout=ACTION_TIMEOUT)
    assert (code, out, err) == (0, f"{child}|inherited|account-a|check\n", "")


@pytest.mark.parametrize("surface", ["build_env", "run_action", "exec_action"])
def test_ps1_identity_survives_provider_and_action_overlays(tmp_path, surface):
    install_script(tmp_path)
    provider = Provider.from_dict("instance", {"bin": "sh", "script": "owner.sh", "env": {"MULTIAGENTS_PROVIDER": "provider-overlay", "OWNER_CREDENTIAL": "retained"}})
    executor = SimpleNamespace(kind="local")
    extra = {"MULTIAGENTS_PROVIDER": "action-overlay"}
    if surface == "build_env":
        assert scripts.build_env("instance", provider, executor, extra)["MULTIAGENTS_PROVIDER"] == "instance"
    elif surface == "run_action":
        assert scripts.run_action("instance", provider, executor, "check", tmp_path, timeout=ACTION_TIMEOUT, extra_env=extra) == (0, "instance|retained||check\n", "")
    else:
        argv, env = scripts.exec_action("instance", provider, executor, "login", tmp_path, extra_env=extra)
        result = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=ACTION_TIMEOUT)
        assert (result.returncode, result.stdout, result.stderr) == (0, "instance|retained||login\n", "")


@pytest.mark.parametrize("surface", ["build_env", "resolve", "run_action", "exec_action"])
def test_ps1_provider_like_without_name_uses_argument(tmp_path, surface):
    path = install_script(tmp_path, "instance.sh")
    provider = SimpleNamespace(bin="sh", env={})
    executor = SimpleNamespace(kind="local")
    if surface == "build_env":
        assert scripts.build_env("instance", provider, executor)["MULTIAGENTS_PROVIDER"] == "instance"
    elif surface == "resolve":
        assert scripts.resolve("instance", provider, tmp_path) == path
    elif surface == "run_action":
        assert scripts.run_action("instance", provider, executor, "check", tmp_path, timeout=ACTION_TIMEOUT) == (0, "instance|||check\n", "")
    else:
        argv, env = scripts.exec_action("instance", provider, executor, "login", tmp_path)
        assert env["MULTIAGENTS_PROVIDER"] == "instance"
        result = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=ACTION_TIMEOUT)
        assert result.stdout == "instance|||login\n" and result.returncode == 0


@pytest.mark.parametrize("kind,profile,pin", [("local", "", ""), ("docker", "", ""), ("docker", "", "account-b"), ("docker", auth.HOST, "account-b")])
def test_ps1_borrowed_auth_check_runs_owner_script_as_instance(tmp_path, monkeypatch, kind, profile, pin):
    monkeypatch.setattr(auth, "CHECK_TIMEOUT", ACTION_TIMEOUT)
    path = install_script(tmp_path)
    providers = load_providers({"owner": {"bin": "sh", "script": "owner.sh", "env": {"OWNER_CREDENTIAL": "borrowed"}, "container_account": "account-a"}, "instance": {"bin": "sh", "script": "missing-instance.sh", "auth_from": "owner", "container_account": pin}})
    state = auth.check("instance", providers["instance"], executor_for(kind), tmp_path, profile=profile)
    assert state.ok and state.provider == "instance" and state.auth_from == "owner"
    assert state.script == str(path)
    expected_pin = (pin if pin and profile != auth.HOST else "account-a") if kind == "docker" else ""
    assert state.detail == f"instance|borrowed|{expected_pin}|check"


def test_ps1_borrowed_pinned_login_keeps_instance_identity_and_owner_script(tmp_path):
    path = install_script(tmp_path)
    providers = load_providers({"owner": {"bin": "sh", "script": "owner.sh", "env": {"OWNER_CREDENTIAL": "borrowed"}, "container_account": "account-a"}, "instance": {"bin": "sh", "auth_from": "owner", "container_account": "account-b"}})
    argv, env = auth.login_command("instance", providers["instance"], executor_for("docker"), tmp_path)
    result = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=ACTION_TIMEOUT)
    assert str(path) in argv
    assert (result.returncode, result.stdout, result.stderr) == (0, "instance|borrowed|account-b|login\n", "")
