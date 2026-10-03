"""DK-R3/R4/R5: host vault actions and account-aware container telemetry."""

import argparse
import json
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from multiagents import auth, authproxy, budget, cli, scripts
from multiagents.executor import docker
from multiagents.paths import ProjectPaths
from multiagents.providers import load_providers


def login(path, token, offset=7200, refresh=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    block = {"accessToken": token, "expiresAt": int((time.time() + offset) * 1000)}
    if refresh:
        block["refreshToken"] = "fake-refresh"
    path.write_text(json.dumps({"claudeAiOauth": block}))


@pytest.fixture
def rig(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    binary = tmp_path / "fake-cli"
    binary.write_text("""#!/usr/bin/env python3
import json, os, pathlib, sys, time
root = pathlib.Path(os.environ['CLAUDE_CONFIG_DIR'])
if sys.argv[1:3] == ['auth', 'login']:
    root.mkdir(parents=True, exist_ok=True)
    data = {'claudeAiOauth': {'accessToken': 'new-login', 'refreshToken': 'fake-refresh'}}
else:
    data = json.loads((root / '.credentials.json').read_text())
    with open(os.environ['RENEW_LOG'], 'a') as out:
        out.write(str(root) + '\\n')
data['claudeAiOauth']['expiresAt'] = int((time.time() + 7200) * 1000)
(root / '.credentials.json').write_text(json.dumps(data))
""")
    binary.chmod(0o755)
    raw = {
        "claude": {"bin": str(binary), "script": "claude.sh",
                   "container_private_home": [".claude"],
                   "budget_profile_env": "CLAUDE_CONFIG_DIR"},
        "second": {"extends": "claude", "container_account": "b",
                   "env": {"CLAUDE_CONFIG_DIR": str(home / "second")}},
        "borrowed": {"auth_from": "claude", "container_account": "b"},
    }
    providers = load_providers(raw)
    paths = ProjectPaths(tmp_path / "project")
    paths.root.mkdir()
    paths.config.mkdir(parents=True)
    ex = docker.DockerExecutor({"auth_proxy": True, "mount_cli_from_host": False},
                               paths=paths, providers=providers)
    monkeypatch.setattr(ex, "inside", lambda: False)
    vault = ex.vault_state("claude")["claude"]
    mounted = next(iter(ex.private_state("claude").values()))
    mounted.mkdir(parents=True)
    monkeypatch.setenv("RENEW_LOG", str(tmp_path / "renewed"))
    return SimpleNamespace(ex=ex, vault=vault, mounted=mounted, paths=paths,
                           providers=providers, raw=raw, log=tmp_path / "renewed")


@pytest.mark.parametrize("label", ["Upper", "bad.space", "../escape", "", None])
def test_dk_r4b_config_validates_pin_labels(label):
    data = {"sample": {"container_account": label}}
    if label == "":
        assert load_providers(data)["sample"].container_account == ""
    else:
        with pytest.raises(ValueError):
            load_providers(data)


def test_dk_r4a_claim_version_cannot_reinterpret_a_legacy_identity():
    agent = "v2.Zg.second"
    legacy = authproxy.mint_token(agent, "fake-secret")
    assert authproxy.read_claim(legacy, "fake-secret") == (agent, "")
    assert authproxy.read_claim(legacy.replace("mxa_", "mxa2_", 1), "fake-secret") == ("", "")
    claim = authproxy.mint_token(agent, "fake-secret", provider="second")
    assert authproxy.read_claim(claim.replace("mxa2_", "mxa_", 1), "fake-secret") == ("", "")


def test_dk_r4_vault_owner_and_native_launch_claim(rig):
    ex = rig.ex
    assert ex.vault_state("second") == ex.vault_state("borrowed") == ex.vault_state("claude")
    assert ex.private_state("second") == ex.private_state("claude")
    login(rig.vault / ".credentials.json", "real-token")
    ex.project_placeholder()
    assert authproxy.read_token(json.loads((rig.mounted / ".credentials.json").read_text())[
        "claudeAiOauth"]["accessToken"], authproxy.load_secret(rig.vault)) == ex.slug
    assert next(iter(ex.private_state("second"))) in dict(ex.mounts())
    env = ex.adapter_env(["fake-cli"], {"CLAUDE_CONFIG_DIR": "/host-only",
                                         "MULTIAGENTS_AGENT_ID": "first"}, "second")
    assert "CLAUDE_CONFIG_DIR" not in env
    assert authproxy.read_claim(env["ANTHROPIC_AUTH_TOKEN"], authproxy.load_secret(rig.vault)) == ("first", "second")
    again = ex.adapter_env(["fake-cli"], {"MULTIAGENTS_AGENT_ID": "next"}, "second")
    assert again["ANTHROPIC_AUTH_TOKEN"] != env["ANTHROPIC_AUTH_TOKEN"]
    assert "real-token" not in (rig.mounted / ".credentials.json").read_text()


def test_dk_r4_nested_launch_uses_host_signed_claim(rig, monkeypatch):
    rig.ex.project_placeholder()
    inside_profile = next(iter(rig.ex.private_state("second")))
    inside_profile.mkdir(parents=True)
    (inside_profile / ".proxy-claims.json").write_text((rig.mounted / ".proxy-claims.json").read_text())
    monkeypatch.setattr(rig.ex, "inside", lambda: True)
    env = rig.ex.adapter_env(["fake-cli"], {}, "second")
    assert authproxy.read_claim(env["ANTHROPIC_AUTH_TOKEN"], authproxy.load_secret(rig.vault)) == (rig.ex.slug, "second")


def test_dk_r3_refresh_enumerates_accounts_even_with_healthy_default(rig):
    login(rig.vault / ".credentials.json", "default")
    for label in ("a", "b"):
        login(rig.vault / "accounts" / label / ".credentials.json", label, -60, refresh=True)
    notes = rig.ex.refresh_private_credentials()
    assert notes and all("second:" not in note and "borrowed:" not in note for note in notes)
    assert set(rig.log.read_text().splitlines()) == {
        str(rig.vault / "accounts" / label) for label in ("a", "b")}
    assert not rig.ex.refresh_private_credentials()
    for label in ("a", "b"):
        assert json.loads((rig.vault / "accounts" / label / ".credentials.json").read_text())[
            "claudeAiOauth"]["expiresAt"] > time.time() * 1000


def test_dk_r3_pinned_refresh_action_still_renews_every_account(rig):
    login(rig.vault / ".credentials.json", "default", -60, refresh=True)
    for label in ("a", "b"):
        login(rig.vault / "accounts" / label / ".credentials.json", label, -60, refresh=True)
    code, out, err = scripts.run_action("second", rig.providers["second"], rig.ex,
                                        "refresh", rig.paths.config)
    assert code == 0, (out, err)
    assert set(rig.log.read_text().splitlines()) == {
        str(rig.vault), str(rig.vault / "accounts/a"), str(rig.vault / "accounts/b")}


@pytest.mark.parametrize("pin_status", ["expired", "missing", "ok"])
def test_dk_r3a_check_and_auth_status_use_the_pinned_account(rig, pin_status):
    login(rig.vault / ".credentials.json", "default")
    login(rig.vault / "accounts/a/.credentials.json", "a", -60)
    if pin_status != "missing":
        login(rig.vault / "accounts/b/.credentials.json", "b", -60 if pin_status == "expired" else 7200)
    states = auth.check_all(rig.providers, lambda _: rig.ex, rig.paths.config)
    assert states["claude"].ok
    for name in ("second", "borrowed"):
        assert states[name].ok == (pin_status == "ok")
        assert f'"b": "{pin_status}"' in states[name].detail
        if pin_status != "ok":
            assert states[name].fix == f"multiagents auth login {name}"
    assert '"default": "ok"' in states["claude"].detail
    assert '"a": "expired"' in states["claude"].detail


def test_dk_r3a_unpinned_check_excludes_reserved_account(rig):
    login(rig.vault / "accounts/b/.credentials.json", "b")
    (rig.vault / "accounts/empty").mkdir()
    states = auth.check_all(rig.providers, lambda _: rig.ex, rig.paths.config)
    assert not states["claude"].ok
    assert '"empty": "missing"' in states["claude"].detail
    assert states["second"].ok


def test_dk_r3a_a_sidecar_placeholder_is_never_a_vault_login(rig):
    rig.ex.project_placeholder()
    state = auth.check("claude", rig.providers["claude"], rig.ex, rig.paths.config)
    assert not state.ok
    assert not (rig.vault / ".credentials.json").exists()


def test_dk_r6_refresh_repairs_nonproxy_projection_with_healthy_default(rig):
    rig.ex.config["auth_proxy"] = False
    login(rig.vault / ".credentials.json", "default", refresh=True)
    code, out, err = scripts.run_action("claude", rig.providers["claude"], rig.ex,
                                        "refresh", rig.paths.config)
    assert code == 0, (out, err)
    projection = json.loads((rig.mounted / ".credentials.json").read_text())["claudeAiOauth"]
    assert projection["accessToken"] == "default"
    assert "refreshToken" not in projection
    assert not rig.log.exists()


def docker_login(rig, monkeypatch, name, account=""):
    monkeypatch.setattr(cli, "_resolve", lambda _: rig.paths)
    monkeypatch.setattr(cli, "_docker_executor", lambda _: rig.ex)
    monkeypatch.setattr(cli, "load_config", lambda _: SimpleNamespace(providers=rig.raw))
    captured = []

    def hand_over(argv, env, script):
        captured.append((argv, env))
        result = subprocess.run(argv, env={**env, "HOME": str(rig.paths.root)}, input="y\n",
                                text=True, capture_output=True)
        assert result.returncode == 0, (result.stdout, result.stderr)
        return result.returncode

    monkeypatch.setattr(cli.driver, "_hand_over", hand_over)
    code = cli.cmd_docker(argparse.Namespace(action="login", path=None, provider=name,
                                             account=account))
    return code, captured


def test_dk_r4_docker_login_refuses_different_pin(rig, monkeypatch):
    code, captured = docker_login(rig, monkeypatch, "second", "a")
    assert code == 2 and not captured
    assert not rig.vault.exists()


@pytest.mark.parametrize("provider", ["second", "borrowed"])
def test_dk_r4_docker_login_targets_owner_vault_pin(rig, monkeypatch, provider):
    code, captured = docker_login(rig, monkeypatch, provider)
    assert code == 0 and captured
    assert (rig.vault / "accounts/b/.credentials.json").is_file()
    assert not (rig.vault / ".credentials.json").exists()


@pytest.mark.parametrize("account", ["", "default"])
def test_dk_r1_docker_login_targets_top_level_default(rig, monkeypatch, account):
    code, _ = docker_login(rig, monkeypatch, "claude", account)
    assert code == 0
    assert (rig.vault / ".credentials.json").is_file()
    assert not (rig.vault / "accounts/default").exists()


def test_dk_r4b_sidecar_pins_derive_from_config_and_reload(rig, monkeypatch):
    calls = []
    monkeypatch.setattr(rig.ex, "container_state", lambda _: "missing")
    def run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
    monkeypatch.setattr(docker, "_run", run)
    assert rig.ex.ensure_auth_proxy()["created"]
    argv = next(call for call in calls if "run" in call)
    config_mount = next(value for value in argv if value.endswith(":/proxy-config:ro"))
    config = Path(config_mount.rsplit(":", 2)[0]) / "pins.json"
    accounts = authproxy.Accounts(rig.vault, pins_path=config)
    assert accounts.provider_pins == {"second": "b", "borrowed": "b"}
    rig.providers["second"].container_account = "a"
    assert rig.ex.ensure_auth_proxy()["created"]
    accounts.reload_pins()
    assert accounts.provider_pins["second"] == "a"
    assert authproxy.Accounts(rig.vault, pins_path=config).provider_pins == accounts.provider_pins


def install_quota_reader(monkeypatch):
    calls = []
    def usage(config_dir, **kwargs):
        calls.append(config_dir)
        token = json.loads((config_dir / ".credentials.json").read_text())["claudeAiOauth"]["accessToken"]
        percent = {"default": 70, "a": 20, "b": 95}[token]
        return {"five_hour": {"utilization": percent}, "seven_day": {"utilization": percent / 2}}, ""
    monkeypatch.setattr(budget, "_shared_usage", usage)
    monkeypatch.setattr(budget, "_from_script", lambda *args, **kwargs: None)
    return calls


def read(rig, name):
    return budget.read_provider(name, rig.providers[name], rig.ex, rig.paths.config,
                                providers=rig.providers)


def test_dk_r5_docker_budget_reports_pinned_and_best_eligible_accounts(rig, monkeypatch):
    for label in ("default", "a", "b"):
        login(rig.vault / (".credentials.json" if label == "default" else
                          f"accounts/{label}/.credentials.json"), label)
    calls = install_quota_reader(monkeypatch)
    pool, pin = read(rig, "claude"), read(rig, "second")
    assert pool.headroom == pytest.approx(.8)
    assert pin.headroom == pytest.approx(.05)
    assert {detail["account"] for detail in pool.windows.values()} == {"default", "a"}
    assert {detail["account"] for detail in pin.windows.values()} == {"b"}
    assert calls == [rig.vault / "accounts/a", rig.vault, rig.vault / "accounts/b"]
    assert read(rig, "borrowed").headroom == pytest.approx(.05)


def test_dk_r5_account_selection_is_part_of_cache_identity(rig, monkeypatch):
    for label in ("a", "b"):
        login(rig.vault / f"accounts/{label}/.credentials.json", label)
    calls = install_quota_reader(monkeypatch)
    assert read(rig, "second").headroom == pytest.approx(.05)
    rig.providers["second"].container_account = "a"
    assert read(rig, "second").headroom == pytest.approx(.8)
    assert calls == [rig.vault / "accounts/b", rig.vault / "accounts/a"]
    assert not read(rig, "claude").known  # both accounts are now reserved


@pytest.mark.parametrize("name", ["claude", "second"])
def test_dk_r5_missing_or_expired_vault_never_falls_back_to_host(rig, monkeypatch, name):
    host = Path.home() / "host"
    login(host / ".credentials.json", "a")
    monkeypatch.setattr(budget, "CLAUDE_CREDENTIALS", host / ".credentials.json")
    login(rig.vault / "accounts/b/.credentials.json", "b", -60)
    calls = install_quota_reader(monkeypatch)
    result = read(rig, name)
    assert not result.known and result.headroom is None
    assert not calls


def test_dk_r6_local_ignores_container_pin(rig, monkeypatch):
    local = SimpleNamespace(kind="local")
    env = scripts.build_env("second", rig.providers["second"], local)
    assert env["CLAUDE_CONFIG_DIR"] == str(Path.home() / "second")
    assert "MULTIAGENTS_CONTAINER_ACCOUNT" not in env
