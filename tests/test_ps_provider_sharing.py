"""Black-box contract for `context/specs/ps-provider-sharing.md` (PS-R1..R9).

Read with the amendments of 2026-09-30: PS-R1a (inherited vs explicit env),
PS-R4a (scoped recovery), PS-R5a (validity of the payload), PS-R7a (deferred
entries record their destination), PS-R8a (real window keys, allowlists) and
PS-R8b (where the migration stops). They supersede the base text.

Seams, all already used by the existing suite:
  providers.load_providers       config load and its rejections
  scripts.run_action             a provider's script actions and their env
  budget.read_all                the budget reading of each provider
  auth.check_all                 authentication checks
  server.auth_status             the MCP status tool (Runner over fake CLIs)
  cli.main(["auth","login",..])  login, `refresh-quota`, `refresh-models`
  Runner.start / consult         admission, routing, the auth-failure breaker
  server.wait_for_agents         the deferred-queue drain (as in the T1 suite)
  config.load                    the merged config, where PS-R6 fails a roster
  DockerExecutor                 private_state / vault_state / refresh

Provider and agent names are invented (`own`, `dep`, `gem`, `partner`) except
where the shipped defaults are the subject (PS-R8).

ASSUMPTIONS ABOUT SEAMS (the contract does not fix them; each is deliberately
the loosest reading):
- A load rejection (PS-R1, PS-R5 load rules, PS-R6) raises `ValueError`
  (`CONFIG_ERROR` below), the type the existing load rejections use.
  `providers.load_providers(raw)` raises the provider ones and
  `config.load(paths, seed=False)` the roster ones.
- A refusal at start admission is either a raised exception or a returned
  dict with `error`; both are accepted, the message is what is asserted.
- The recorded destination of a NEW deferred entry (PS-R7a) is looked for
  among the values of the entry's `spec`, as the T1 suite looks for ids.
- Authentication blocks are read from `tree.cooldown(name)`: `cause == "auth"`
  and `needs_login` is true, the fields the existing breaker already writes.
"""
from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
import threading
import time
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import c1_harness as c1  # noqa: E402
import c2_harness as c2  # noqa: E402
import c3_harness as h  # noqa: E402
from multiagents import auth as auth_mod  # noqa: E402
from multiagents import budget as budget_mod  # noqa: E402
from multiagents import cli, config as config_mod, driver, scripts  # noqa: E402
from multiagents import server  # noqa: E402
from multiagents.config import AgentSpec  # noqa: E402
from multiagents.executor.local import LocalExecutor  # noqa: E402
from multiagents.paths import ProjectPaths, shipped_defaults_dir  # noqa: E402
from multiagents.providers import families, load_providers  # noqa: E402
from multiagents.tree import Node  # noqa: E402
from test_conversation_provider_change import _calls, _fake_cli, _flag  # noqa: E402

CONFIG_ERROR = ValueError


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def _rejected(raw, *needles):
    """`load_providers(raw)` must raise CONFIG_ERROR naming every needle."""
    with pytest.raises(CONFIG_ERROR) as caught:
        load_providers(raw)
    message = str(caught.value)
    for needle in needles:
        assert needle in message, f"{needle!r} missing from: {message}"
    return message


def _local():
    return LocalExecutor()


def _lines(path: Path) -> int:
    return len(path.read_text().splitlines()) if path.exists() else 0


def _wait_for(predicate, seconds=10.0):
    end = time.time() + seconds
    while time.time() < end:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    return predicate()


def _values(entry) -> list:
    out = []
    for value in entry.values():
        out.extend(_values(value) if isinstance(value, dict) else [value])
    return out


def _shipped_raw() -> dict:
    return yaml.safe_load((shipped_defaults_dir() / "providers.yaml").read_text())["providers"]


def _run_start(runner, agent, task="work", **kwargs):
    """`start`'s result, or {"raised": exc, "error": text} when it raises."""
    async def go():
        try:
            result = await runner.start(agent, task, **kwargs)
        except Exception as exc:
            return {"raised": exc, "error": str(exc)}
        run = runner.runs.get(result.get("agent_id"))
        if run:
            await asyncio.wait_for(run.done.wait(), 20)
        return result
    return asyncio.run(go())


def _run_consult(runner, name="advisor", message="next question"):
    async def go():
        try:
            return await runner.consult(name, message, timeout=60)
        except Exception as exc:
            return {"raised": exc, "error": str(exc)}
    return asyncio.run(go())


def _refused(result) -> bool:
    return bool(result.get("error")) and not result.get("agent_id") \
        and not result.get("reply")


# ===========================================================================
# PS-R1: the `auth_from` declaration
# ===========================================================================

def _pair(**dep):
    return {"own": {"bin": "ownbin", "env": {"OWN_HOME": "/o"}},
            "dep": {"bin": "depbin", "auth_from": "own", **dep}}


def test_ps_r1_control_a_valid_dependent_loads():
    providers = load_providers(_pair())
    assert set(providers) == {"own", "dep"}


def test_ps_r1_rejects_an_owner_that_is_not_declared():
    raw = {"dep": {"bin": "depbin", "auth_from": "ghost"}}
    _rejected(raw, "dep", "auth_from")


def test_ps_r1_rejects_an_owner_that_is_the_provider_itself():
    raw = {"dep": {"bin": "depbin", "auth_from": "dep"}}
    _rejected(raw, "dep", "auth_from")


def test_ps_r1_rejects_a_chain_because_the_owner_declares_its_own_auth_from():
    raw = {"a": {"bin": "a", "auth_from": "b"},
           "b": {"bin": "b", "auth_from": "c"},
           "c": {"bin": "c"}}
    message = _rejected(raw, "auth_from")
    assert "a" in message or "b" in message


def test_ps_r1_rejects_a_cycle():
    raw = {"a": {"bin": "a", "auth_from": "b"}, "b": {"bin": "b", "auth_from": "a"}}
    _rejected(raw, "auth_from")


def test_ps_r1_rejects_an_explicit_env_key_that_the_owner_also_sets():
    raw = _pair(env={"OWN_HOME": "/somewhere-else"})
    _rejected(raw, "dep", "auth_from")


def test_ps_r1_accepts_env_keys_the_owner_does_not_set():
    providers = load_providers(_pair(env={"DEP_ONLY": "1"}))
    assert "dep" in providers


def test_ps_r1a_an_explicit_value_equal_to_the_owners_is_not_a_conflict():
    providers = load_providers(_pair(env={"OWN_HOME": "/o"}))
    assert "dep" in providers


def test_ps_r1a_a_value_inherited_through_extends_from_the_owner_is_not_a_conflict():
    raw = {"own": {"bin": "ownbin", "env": {"OWN_HOME": "/o"}},
           "dep": {"extends": "own", "auth_from": "own"}}
    providers = load_providers(raw)
    assert "dep" in providers


def test_ps_r1a_an_explicit_differing_value_is_rejected_even_when_extends_is_used():
    raw = {"own": {"bin": "ownbin", "env": {"OWN_HOME": "/o"}},
           "dep": {"extends": "own", "auth_from": "own", "env": {"OWN_HOME": "/x"}}}
    _rejected(raw, "dep", "auth_from")


def test_ps_r1_a_disabled_owner_still_provides_credentials_to_an_enabled_dependent(tmp_path):
    raw = {"own": {"bin": "ownbin", "enabled": False, "script": "ps-own.sh"},
           "dep": {"bin": "depbin", "auth_from": "own"}}
    providers = load_providers(raw)
    assert providers["own"].enabled is False and providers["dep"].enabled is True
    cfg = tmp_path / "cfg"
    c2.case_script(cfg, "ps-own.sh", 'check) echo "owner login present"; exit 0 ;;')
    states = auth_mod.check_all(providers, lambda name: _local(), tmp_path / "g", cfg)
    assert states["dep"].status == "authenticated", states["dep"]


def test_ps_r1_the_budget_from_key_follows_the_same_load_rules():
    _rejected({"dep": {"bin": "d", "budget_from": "ghost"}}, "dep", "budget_from")
    _rejected({"dep": {"bin": "d", "budget_from": "dep"}}, "dep", "budget_from")
    _rejected({"a": {"bin": "a", "budget_from": "b"},
               "b": {"bin": "b", "budget_from": "c"}, "c": {"bin": "c"}},
              "budget_from")


def test_ps_r1_auth_from_and_budget_from_may_name_different_owners():
    raw = {"x": {"bin": "x"}, "y": {"bin": "y"},
           "dep": {"bin": "d", "auth_from": "x", "budget_from": "y"}}
    assert "dep" in load_providers(raw)


def test_ps_r9_extends_alone_keeps_allowing_a_different_explicit_env():
    """opencode-zai's shape: an instance may override its base's env key."""
    raw = {"base": {"bin": "b", "env": {"K": "1"}},
           "inst": {"extends": "base", "env": {"K": "2"}}}
    assert "inst" in load_providers(raw)


# ===========================================================================
# PS-R2: a dependent uses the owner's credentials, everywhere
# ===========================================================================

def _env_script(cfg: Path, name: str, out: Path):
    c2.write_script(cfg, name, f'env > "{out}"\nexit 0\n')


def test_ps_r2_a_script_action_of_a_dependent_carries_the_owners_env_overlaid_with_its_own(tmp_path):
    raw = {"own": {"bin": "ownbin", "script": "ps-env.sh",
                   "env": {"OWN_PROFILE": "/profiles/own", "SHARED": "from-owner"}},
           "dep": {"bin": "depbin", "script": "ps-env.sh", "auth_from": "own",
                   "env": {"DEP_ONLY": "mine"}}}
    providers = load_providers(raw)
    cfg, out = tmp_path / "cfg", tmp_path / "env.txt"
    _env_script(cfg, "ps-env.sh", out)
    code, _, err = scripts.run_action("dep", providers["dep"], _local(), "check",
                                      tmp_path / "g", cfg)
    assert code == 0, err
    env = dict(line.split("=", 1) for line in out.read_text().splitlines() if "=" in line)
    assert env["OWN_PROFILE"] == "/profiles/own", "the owner's profile path"
    assert env["SHARED"] == "from-owner"
    assert env["DEP_ONLY"] == "mine", "the dependent's own non-conflicting env survives"


def test_ps_r2_a_provider_without_auth_from_does_not_receive_anothers_env(tmp_path):
    raw = {"own": {"bin": "ownbin", "script": "ps-env.sh", "env": {"OWN_PROFILE": "/o"}},
           "other": {"bin": "otherbin", "script": "ps-env.sh"}}
    providers = load_providers(raw)
    cfg, out = tmp_path / "cfg", tmp_path / "env.txt"
    _env_script(cfg, "ps-env.sh", out)
    scripts.run_action("other", providers["other"], _local(), "check", tmp_path / "g", cfg)
    assert "OWN_PROFILE=" not in out.read_text()


def test_ps_r2_a_launch_of_a_dependent_carries_the_owners_env(tmp_path, monkeypatch):
    probe = tmp_path / "envprobe"
    probe.mkdir()
    script = tmp_path / "envcli"
    script.write_text(
        f"#!{sys.executable}\nimport json, os, sys\n"
        f"open({str(probe / 'env.json')!r}, 'w').write(json.dumps(dict(os.environ)))\n"
        "print(json.dumps({'type': 'text', 'text': 'ok'}))\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    dep = {"bin": str(script), "auth_from": "own", "env": {"DEP_ONLY": "mine"},
           "spawn": {"args": ["--model", "{model}"]},
           "stream": {"format": "ndjson",
                      "rules": [{"match": {"type": "text"}, "as": "text",
                                 "fields": {"text": "text"}}]}}
    own, _ = _fake_cli(tmp_path, "own")
    own["env"] = {"OWN_PROFILE": "/profiles/own"}
    runner = h.make_runner(tmp_path / "proj", monkeypatch,
                           agents={"worker": AgentSpec("worker", "dep", "m")},
                           providers={"own": own, "dep": dep})
    result = _run_start(runner, "worker")
    assert result.get("agent_id"), result
    env = json.loads((probe / "env.json").read_text())
    assert env.get("OWN_PROFILE") == "/profiles/own", "the owner's profile, on a launch"
    assert env.get("DEP_ONLY") == "mine"


# -- the docker backing ------------------------------------------------------

def _docker_group(tmp_path, dependents=("dep1", "dep2"), extra_owner=False):
    raw = {"own": {"bin": "ownbin", "script": "ps-refresh.sh",
                   "container_private_home": [".acct"], "env": {"ACCT": "/x"}}}
    for name in dependents:
        raw[name] = {"bin": f"{name}bin", "script": "ps-refresh.sh", "auth_from": "own",
                     "container_private_home": [".acct"]}
    if extra_owner:
        raw["solo"] = {"bin": "solobin", "script": "ps-refresh.sh",
                       "container_private_home": [".solo"]}
    providers = load_providers(raw)
    return c1.make_docker_executor(tmp_path, providers), providers


def test_ps_r2_docker_dependents_share_the_owners_one_backing(tmp_path):
    ex, _ = _docker_group(tmp_path)
    backings = {name: set(ex.private_state(name).values()) for name in ("own", "dep1", "dep2")}
    assert all(backings.values()), backings
    assert backings["dep1"] == backings["own"] == backings["dep2"]
    assert len(set(ex.private_state().values())) == 1, "one backing for the whole group"


def test_ps_r2_docker_dependents_share_the_owners_one_vault(tmp_path):
    ex, _ = _docker_group(tmp_path)
    vaults = {name: set(ex.vault_state(name).values()) for name in ("own", "dep1", "dep2")}
    assert all(vaults.values()), vaults
    assert vaults["dep1"] == vaults["own"] == vaults["dep2"]


def test_ps_r2_distinct_logins_stay_distinct(tmp_path):
    ex, _ = _docker_group(tmp_path, dependents=("dep1",), extra_owner=True)
    assert set(ex.private_state("solo").values()) != set(ex.private_state("own").values())
    assert set(ex.vault_state("solo").values()) != set(ex.vault_state("own").values())
    assert len(set(ex.private_state().values())) == 2


def test_ps_r2_without_auth_from_every_provider_keeps_its_own_backing(tmp_path):
    """PS-R9 control: extends-only instances of one CLI are separate today."""
    raw = {"a": {"bin": "a", "container_private_home": [".a"]},
           "b": {"bin": "b", "container_private_home": [".b"]}}
    ex = c1.make_docker_executor(tmp_path, load_providers(raw))
    assert set(ex.private_state("a").values()) != set(ex.private_state("b").values())


_REFRESH = """
case "$1" in
  refresh) echo x >> "%(count)s"; sleep %(nap)s; echo renewed; exit 0 ;;
  *) exit 64 ;;
esac
"""


def _expiring_credentials(ex, *names):
    """An expiring token wherever each named provider keeps its credential.

    Every member of a group is given one: under the contract they all resolve
    to the owner's single store, so the writes land on the same file; under a
    per-provider store each would be renewed on its own, which is the defect.
    """
    soon = int((time.time() + 60) * 1000)
    for name in names or tuple(ex.providers):
        for root in ex.private_state(name).values():
            root.mkdir(parents=True, exist_ok=True)
            (root / ".credentials.json").write_text(
                json.dumps({"oauth": {"expiresAt": soon}}))


def test_ps_r2_two_dependents_of_one_owner_renew_the_credential_once(tmp_path):
    ex, _ = _docker_group(tmp_path)
    count = tmp_path / "renewals"
    c2.write_script(ex.paths.config, "ps-refresh.sh",
                    _REFRESH % {"count": count, "nap": "0"})
    _expiring_credentials(ex)
    ex.refresh_private_credentials()
    assert _lines(count) == 1, "owner + two dependents share one credential: one renewal"


def test_ps_r2_two_concurrent_refreshes_through_two_dependents_perform_one_renewal(tmp_path):
    ex1, _ = _docker_group(tmp_path, dependents=("dep1",))
    ex2, _ = _docker_group(tmp_path, dependents=("dep2",))
    count = tmp_path / "renewals"
    # The renewal is slow and leaves the credential expiring, so the second
    # caller can only skip it by finding the lock (one lock per owner) held.
    c2.write_script(ex1.paths.config, "ps-refresh.sh",
                    _REFRESH % {"count": count, "nap": "1.5"})
    _expiring_credentials(ex1)
    _expiring_credentials(ex2)
    gate = threading.Barrier(2)

    def go(ex):
        gate.wait()
        ex.refresh_private_credentials()

    threads = [threading.Thread(target=go, args=(ex,)) for ex in (ex1, ex2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert _lines(count) == 1, "two providers must never renew the same credentials concurrently"


def test_ps_r2_distinct_owners_still_renew_separately(tmp_path):
    ex, _ = _docker_group(tmp_path, dependents=("dep1",), extra_owner=True)
    count = tmp_path / "renewals"
    c2.write_script(ex.paths.config, "ps-refresh.sh",
                    _REFRESH % {"count": count, "nap": "0"})
    _expiring_credentials(ex)
    ex.refresh_private_credentials()
    assert _lines(count) == 2, "the group's credential once, the unrelated login once"


# ===========================================================================
# PS-R3: login and auth status follow the owner
# ===========================================================================

def _cli_project(tmp_path, monkeypatch, providers):
    root = h.make_git_repo((tmp_path / "proj").resolve())
    config = root / ".multiagents" / "config"
    (config / "providers").mkdir(parents=True, exist_ok=True)
    disabled = {name: {"enabled": False} for name in _shipped_raw()}
    (config / "providers.yaml").write_text(
        yaml.safe_dump({"providers": {**disabled, **providers}}))
    monkeypatch.chdir(root)
    return root, config


def test_ps_r3_login_on_a_dependent_runs_the_owners_login_once_and_says_so(
        tmp_path, monkeypatch, capsys):
    root, config = _cli_project(tmp_path, monkeypatch, {
        "own": {"bin": "sh", "script": "ps-own.sh", "env": {"OWN_PROFILE": "/o"}},
        "dep": {"bin": "sh", "auth_from": "own"}})
    c2.case_script(config, "ps-own.sh", "login) echo logging-in; exit 0 ;;")
    handed = []
    monkeypatch.setattr(driver, "_hand_over",
                        lambda argv, env, script: handed.append((argv, env)) or 0)
    cli.main(["--path", str(root), "auth", "login", "dep"])
    out = capsys.readouterr().out
    assert len(handed) == 1, "the owner's login action is invoked exactly once"
    argv, env = handed[0]
    assert argv[-2].endswith("ps-own.sh") and argv[-1] == "login", argv
    assert env.get("OWN_PROFILE") == "/o", "the login runs with the owner's env"
    assert "dep uses own's login" in out, out


def test_ps_r3_login_on_the_owner_itself_does_not_claim_to_borrow(
        tmp_path, monkeypatch, capsys):
    root, config = _cli_project(tmp_path, monkeypatch, {
        "own": {"bin": "sh", "script": "ps-own.sh"},
        "dep": {"bin": "sh", "auth_from": "own"}})
    c2.case_script(config, "ps-own.sh", "login) exit 0 ;;")
    handed = []
    monkeypatch.setattr(driver, "_hand_over",
                        lambda argv, env, script: handed.append(argv) or 0)
    cli.main(["--path", str(root), "auth", "login", "own"])
    assert len(handed) == 1
    assert "uses" not in capsys.readouterr().out.replace("this machine uses", "")


def _auth_runner(tmp_path, monkeypatch, check_body, third=False):
    """A Runner with `own`, `dep` (auth_from own) and optionally `other`."""
    counter = tmp_path / "checks"
    body = (f'check) echo x >> "{counter}"; {check_body} ;;')
    providers = {}
    for name in ("own", "dep") + (("other",) if third else ()):
        providers[name], _ = _fake_cli(tmp_path, name)
    providers["own"]["script"] = "ps-own.sh"
    providers["dep"]["auth_from"] = "own"
    if third:
        providers["other"]["script"] = "ps-other.sh"
    runner = h.make_runner(tmp_path / "proj", monkeypatch,
                           agents={"worker": AgentSpec("worker", "dep", "m")},
                           providers=providers)
    c2.case_script(runner.paths.config, "ps-own.sh", body)
    if third:
        c2.case_script(runner.paths.config, "ps-other.sh", body)
    monkeypatch.setattr(server, "runner", lambda: runner)
    return runner, counter


def test_ps_r3_auth_status_checks_the_owner_once_and_reports_both(tmp_path, monkeypatch):
    runner, counter = _auth_runner(tmp_path, monkeypatch, 'echo "logged in"; exit 0')
    out = server.auth_status()
    assert _lines(counter) == 1, "one check for owner plus dependent"
    providers = out["providers"]
    assert set(providers) >= {"own", "dep"}
    assert providers["own"]["status"] == providers["dep"]["status"] == "authenticated"
    assert providers["dep"]["auth_from"] == "own", "a field naming the owner"


def test_ps_r3_the_dependent_reports_the_owners_failing_state(tmp_path, monkeypatch):
    runner, counter = _auth_runner(tmp_path, monkeypatch, 'echo "expired"; exit 10')
    out = server.auth_status()
    assert _lines(counter) == 1
    assert out["providers"]["dep"]["status"] == "not_authenticated"
    assert out["providers"]["own"]["status"] == "not_authenticated"
    assert {"own", "dep"} <= set(out["needs_attention"])


def test_ps_r3_an_unrelated_provider_is_still_checked_separately(tmp_path, monkeypatch):
    runner, counter = _auth_runner(tmp_path, monkeypatch, 'echo ok; exit 0', third=True)
    out = server.auth_status()
    assert out["providers"]["dep"]["status"] == "authenticated"
    assert _lines(counter) == 2, "owner group once, the unrelated provider once"


def test_ps_r3_a_provider_without_auth_from_has_no_auth_from_field(tmp_path, monkeypatch):
    runner, _ = _auth_runner(tmp_path, monkeypatch, 'echo ok; exit 0')
    assert not server.auth_status()["providers"]["own"].get("auth_from")


def test_ps_r3_check_all_runs_a_group_once_and_an_extends_only_pair_twice(tmp_path):
    cfg = tmp_path / "cfg"
    counter = tmp_path / "checks"
    c2.case_script(cfg, "ps-x.sh", f'check) echo x >> "{counter}"; exit 0 ;;')
    grouped = load_providers({"own": {"bin": "o", "script": "ps-x.sh"},
                              "dep": {"bin": "d", "auth_from": "own"}})
    states = auth_mod.check_all(grouped, lambda n: _local(), tmp_path / "g", cfg)
    assert states["dep"].status == states["own"].status == "authenticated"
    assert _lines(counter) == 1
    counter.unlink()
    plain = load_providers({"base": {"bin": "b", "script": "ps-x.sh"},
                            "inst": {"extends": "base"}})
    auth_mod.check_all(plain, lambda n: _local(), tmp_path / "g", cfg)
    assert _lines(counter) == 2, "PS-R9: no auth_from, so each is checked as today"


# ===========================================================================
# PS-R4: an authentication failure blocks the whole credential group
# ===========================================================================

AUTH_MARKER = "Error: invalid api key\n"


class Group:
    """`own` (owner) and `dep` (auth_from own), with a check script whose
    verdict is a file. `worker` runs on dep, `ownerworker` on own."""

    def __init__(self, tmp_path, monkeypatch, *, dep_stderr=AUTH_MARKER,
                 check_body=None, failing="dep", threshold=1):
        self.ok_file = tmp_path / "logged-in"
        if failing == "own":
            own = h.fake_cli(tmp_path, "own", exit_code=1, stderr=AUTH_MARKER)
            dep, self.dep_probe = _fake_cli(tmp_path, "dep")
            self.own_probe = None
        else:
            own, self.own_probe = _fake_cli(tmp_path, "own")
            dep = h.fake_cli(tmp_path, "dep", exit_code=1, stderr=dep_stderr)
            self.dep_probe = None
        own["script"] = "ps-own.sh"
        dep["auth_from"] = "own"
        self.runner = h.make_runner(
            tmp_path / "proj", monkeypatch,
            agents={"worker": AgentSpec("worker", "dep", "m"),
                    "ownerworker": AgentSpec("ownerworker", "own", "m")},
            providers={"own": own, "dep": dep},
            project={"limits": {"provider_failure_threshold": threshold,
                                "provider_probe_seconds": 0}})
        body = check_body or (
            f'check) if [ -f "{self.ok_file}" ]; then echo ok; exit 0; fi; '
            'echo "not logged in"; exit 10 ;;')
        c2.case_script(self.runner.paths.config, "ps-own.sh", body)
        self.tree = self.runner.tree

    def fail_on_dep(self):
        return _run_start(self.runner, "worker")

    def block(self, name):
        return self.tree.cooldown(name)

    def log_in(self):
        self.ok_file.write_text("yes")


def _auth_blocked(entry) -> bool:
    return bool(entry) and entry.get("cause") == "auth" and entry.get("needs_login") is True


def test_ps_r4_an_auth_failure_on_the_dependent_blocks_the_owner_too(tmp_path, monkeypatch):
    g = Group(tmp_path, monkeypatch)
    g.fail_on_dep()
    assert _wait_for(lambda: g.block("dep")), "control: the dependent itself is blocked"
    assert _auth_blocked(g.block("dep")), g.block("dep")
    assert _auth_blocked(g.block("own")), "the credential group is blocked together"


def test_ps_r4_the_group_trips_at_the_same_threshold_as_a_single_provider(tmp_path, monkeypatch):
    g = Group(tmp_path, monkeypatch, threshold=2)
    g.fail_on_dep()
    time.sleep(0.5)
    assert g.block("dep") is None and g.block("own") is None, "one failure is below the threshold"
    g.fail_on_dep()
    assert _wait_for(lambda: _auth_blocked(g.block("dep")) and _auth_blocked(g.block("own")))


def test_ps_r4_a_blocked_owner_refuses_a_run_that_needs_it(tmp_path, monkeypatch):
    g = Group(tmp_path, monkeypatch)
    g.fail_on_dep()
    _wait_for(lambda: g.block("own"))
    result = _run_start(g.runner, "ownerworker")
    assert not _calls(g.own_probe), "no run was launched on the blocked owner"
    assert result.get("deferred") or result.get("error"), result


def test_ps_r4_recovery_clears_every_member_together(tmp_path, monkeypatch):
    g = Group(tmp_path, monkeypatch)
    g.fail_on_dep()
    assert _wait_for(lambda: g.block("own") and g.block("dep"))
    g.log_in()
    result = _run_start(g.runner, "ownerworker")
    assert result.get("agent_id"), result
    assert g.block("own") is None
    assert g.block("dep") is None, "the dependent recovers with the owner"


def test_ps_r4_a_failing_check_keeps_every_member_blocked(tmp_path, monkeypatch):
    g = Group(tmp_path, monkeypatch)
    g.fail_on_dep()
    assert _wait_for(lambda: g.block("own") and g.block("dep"))
    _run_start(g.runner, "ownerworker")           # not logged in: the probe fails
    assert _auth_blocked(g.block("own")) and _auth_blocked(g.block("dep"))


def test_ps_r4_a_non_auth_failure_of_the_dependent_does_not_block_the_owner(tmp_path, monkeypatch):
    """provider_down stays per provider: the check says the login is fine."""
    g = Group(tmp_path, monkeypatch, dep_stderr="segfault in renderer\n")
    g.log_in()
    g.fail_on_dep()
    assert _wait_for(lambda: g.block("dep")), "control: the dependent tripped"
    assert not _auth_blocked(g.block("dep"))
    assert g.block("own") is None, "provider_down is not an authentication failure"


def test_ps_r4_a_quota_block_on_the_dependent_leaves_the_owner_usable(tmp_path, monkeypatch):
    g = Group(tmp_path, monkeypatch)
    g.tree.set_cooldown("dep", time.time() + 7200, "out of quota", cause="quota")
    result = _run_start(g.runner, "ownerworker")
    assert result.get("agent_id"), result
    assert _calls(g.own_probe), "the owner ran"


def test_ps_r4a_recovery_leaves_a_quota_block_on_a_member_alone(tmp_path, monkeypatch):
    g = Group(tmp_path, monkeypatch, failing="own")
    g.tree.set_cooldown("dep", time.time() + 7200, "out of quota", cause="quota")
    _run_start(g.runner, "ownerworker")            # the owner's login is rejected
    assert _wait_for(lambda: _auth_blocked(g.block("own"))), "control: the owner is blocked"
    g.log_in()
    _run_start(g.runner, "worker")                 # a start runs the recovery probe
    assert g.block("own") is None, "the authentication block cleared"
    kept = g.block("dep")
    assert kept and kept.get("cause") == "quota", "the quota block on the dependent stays"


def test_ps_r4a_a_host_only_login_does_not_clear_a_block_from_the_default_context(
        tmp_path, monkeypatch):
    """The group is per execution context. This check passes only when asked
    about the HOST profile; the failure being recovered from is the default
    (agents') context, which a host pass must not clear."""
    body = ('check) if [ "${MULTIAGENTS_PROFILE:-}" = host ]; then echo ok; exit 0; fi; '
            'echo "not logged in"; exit 10 ;;')
    g = Group(tmp_path, monkeypatch, check_body=body)
    g.fail_on_dep()
    assert _wait_for(lambda: _auth_blocked(g.block("own")) and _auth_blocked(g.block("dep")))
    _run_start(g.runner, "ownerworker")
    assert _auth_blocked(g.block("own")) and _auth_blocked(g.block("dep"))


# ===========================================================================
# PS-R5: shared quota source, separate quota
# ===========================================================================

def _window(left, reset, **extra):
    return {"headroom": left, "percent": round((1 - left) * 100, 1),
            "resets_at": reset, **extra}


FAR = {"gemini-weekly": "2099-01-01T00:00:00Z", "gemini-5h": "2099-01-02T00:00:00Z",
       "3p-weekly": "2099-01-03T00:00:00Z", "3p-5h": "2099-01-04T00:00:00Z"}


def _payload(gw=0.8, g5=0.93, tw=0.32, t5=0.0, resets=None, flags=False, **top):
    resets = {**FAR, **(resets or {})}
    counted = (lambda key: {"counted": key.startswith("gemini")}) if flags else (lambda key: {})
    windows = {"gemini-weekly": _window(gw, resets["gemini-weekly"], **counted("gemini-weekly")),
               "gemini-5h": _window(g5, resets["gemini-5h"], **counted("gemini-5h")),
               "3p-weekly": _window(tw, resets["3p-weekly"], **counted("3p-weekly")),
               "3p-5h": _window(t5, resets["3p-5h"], **counted("3p-5h"))}
    out = {"known": True, "headroom": min(gw, g5), "resets_at": resets["gemini-weekly"],
           "windows": windows}
    out.update(top)
    return out


class Quota:
    """A budget script that counts its runs and prints `payload`."""

    def __init__(self, tmp_path, raw, payload):
        tmp_path.mkdir(parents=True, exist_ok=True)
        self.cfg = tmp_path / "cfg"
        self.count = tmp_path / "budget-runs"
        self.payload_file = tmp_path / "payload.json"
        self.set(payload)
        c2.case_script(self.cfg, "ps-agy.sh",
                       f'budget) echo x >> "{self.count}"; cat "{self.payload_file}"; exit 0 ;;')
        self.providers = load_providers(raw)
        self.tmp = tmp_path

    def set(self, payload):
        self.payload_file.write_text(json.dumps(payload))

    def read(self, **kwargs):
        return budget_mod.read_all(self.providers, lambda n: _local(),
                                   self.tmp / "g", self.cfg, **kwargs)

    @property
    def runs(self):
        return _lines(self.count)


def _pair_raw(owner_windows=("gemini*",), dep_windows=("3p-*",), extends=False, **dep):
    own = {"bin": "agy", "script": "ps-agy.sh"}
    if owner_windows is not None:
        own["budget_windows"] = list(owner_windows)
    partner = ({"extends": "agy"} if extends else {"bin": "partnerbin"})
    partner.update({"budget_from": "agy", **dep})
    if dep_windows is not None:
        partner["budget_windows"] = list(dep_windows)
    return {"agy": own, "agy-partner": partner}


def test_ps_r5_each_provider_is_constrained_only_by_its_own_windows(tmp_path):
    q = Quota(tmp_path, _pair_raw(), _payload(gw=0.80, g5=0.93, tw=0.32, t5=0.0))
    got = q.read()
    assert got["agy"].known and got["agy"].headroom == pytest.approx(0.80)
    assert got["agy"].resets_at == FAR["gemini-weekly"]
    assert got["agy"].severity == "normal"
    assert got["agy-partner"].known and got["agy-partner"].headroom == pytest.approx(0.0)
    assert got["agy-partner"].resets_at == FAR["3p-5h"]
    assert got["agy-partner"].severity == "critical"


def test_ps_r5_the_worst_counted_window_is_the_constraint(tmp_path):
    q = Quota(tmp_path, _pair_raw(), _payload(gw=0.75, g5=0.04, tw=0.60, t5=0.30))
    got = q.read()
    assert got["agy"].headroom == pytest.approx(0.04)
    assert got["agy"].resets_at == FAR["gemini-5h"]
    assert got["agy-partner"].headroom == pytest.approx(0.30)
    assert got["agy-partner"].resets_at == FAR["3p-5h"]


def test_ps_r5_a_full_partner_bucket_leaves_agy_usable(tmp_path):
    q = Quota(tmp_path, _pair_raw(), _payload(gw=0.8, g5=0.9, tw=0.0, t5=0.0))
    got = q.read()
    assert got["agy-partner"].usable is False
    assert got["agy"].usable is True


def test_ps_r5_a_full_gemini_bucket_leaves_the_partner_usable(tmp_path):
    q = Quota(tmp_path, _pair_raw(), _payload(gw=0.0, g5=0.0, tw=0.6, t5=0.7))
    got = q.read()
    assert got["agy"].usable is False
    assert got["agy-partner"].usable is True
    assert got["agy-partner"].headroom == pytest.approx(0.6)


def test_ps_r5_windows_not_counted_stay_in_the_reading_marked_not_counted(tmp_path):
    got = Quota(tmp_path, _pair_raw(), _payload()).read()
    agy, partner = got["agy"].windows, got["agy-partner"].windows
    assert set(agy) == set(partner) == set(FAR), "nothing is hidden, both display all four"
    assert agy["gemini-weekly"]["counted"] is True and agy["3p-5h"]["counted"] is False
    assert partner["3p-5h"]["counted"] is True and partner["gemini-5h"]["counted"] is False
    assert partner["3p-5h"]["headroom"] == 0.0, "values unchanged, only the flag differs"


def test_ps_r5_the_selector_overrides_the_counted_flags_of_the_payload(tmp_path):
    """agy.sh marks 3p-* `counted: false`; the partner's selector still counts it."""
    got = Quota(tmp_path, _pair_raw(), _payload(flags=True, t5=0.0)).read()
    assert got["agy-partner"].headroom == pytest.approx(0.0)
    assert got["agy-partner"].windows["3p-5h"]["counted"] is True


def test_ps_r5_without_budget_windows_the_counted_flags_of_the_payload_are_used(tmp_path):
    q = Quota(tmp_path, _pair_raw(owner_windows=None, dep_windows=None),
              _payload(gw=0.8, g5=0.93, tw=0.32, t5=0.0, flags=True))
    got = q.read()
    assert got["agy"].headroom == pytest.approx(0.8), "as today: gemini counts, 3p does not"
    assert got["agy-partner"].headroom == pytest.approx(0.8)


def _control_known(tmp_path):
    """The same payload with a selector that does match: the dependent has a
    reading, so an unknown one elsewhere is the selector's doing."""
    control = Quota(tmp_path / "control", _pair_raw(), _payload()).read()["agy-partner"]
    assert control.known, "control: a matching selector yields a known reading"


def test_ps_r5_a_selector_matching_no_window_makes_the_reading_unknown(tmp_path):
    _control_known(tmp_path)
    q = Quota(tmp_path, _pair_raw(dep_windows=("nothing-*",)), _payload())
    got = q.read()
    partner = got["agy-partner"]
    assert partner.known is False, "never the owner's aggregate"
    assert partner.headroom != pytest.approx(0.8)
    assert partner.usable is True, "unknown headroom is not no headroom"
    assert got["agy"].known and got["agy"].headroom == pytest.approx(0.8)


def test_ps_r5a_an_empty_selector_list_matches_nothing(tmp_path):
    _control_known(tmp_path)
    got = Quota(tmp_path, _pair_raw(dep_windows=()), _payload()).read()
    assert got["agy-partner"].known is False
    assert got["agy"].known is True


def test_ps_r5a_an_empty_selector_on_the_owner_also_matches_nothing(tmp_path):
    got = Quota(tmp_path, _pair_raw(owner_windows=()), _payload()).read()
    assert got["agy"].known is False
    assert got["agy-partner"].known is True


def test_ps_r5_overlapping_selectors_make_both_providers_count_the_window(tmp_path):
    q = Quota(tmp_path, _pair_raw(dep_windows=("gemini-5h", "3p-weekly")),
              _payload(gw=0.8, g5=0.93, tw=0.32, t5=0.0))
    got = q.read()
    assert got["agy"].headroom == pytest.approx(0.8)
    assert got["agy-partner"].headroom == pytest.approx(0.32)
    assert got["agy"].windows["gemini-5h"]["counted"] is True
    assert got["agy-partner"].windows["gemini-5h"]["counted"] is True


def test_ps_r5_a_selector_is_a_glob_over_the_whole_key(tmp_path):
    _control_known(tmp_path)
    q = Quota(tmp_path, _pair_raw(dep_windows=("weekly",)), _payload())
    assert q.read()["agy-partner"].known is False, "no window is called exactly `weekly`"


def test_ps_r5a_a_known_dependent_reading_survives_an_unknown_owner_aggregate(tmp_path):
    """The gemini buckets are absent, so the owner's aggregate is `known: false`."""
    windows = {"3p-weekly": _window(0.32, FAR["3p-weekly"]),
               "3p-5h": _window(0.10, FAR["3p-5h"])}
    q = Quota(tmp_path, _pair_raw(), {"known": False, "note": "no gemini quota", "windows": windows})
    got = q.read()
    assert got["agy-partner"].known is True
    assert got["agy-partner"].headroom == pytest.approx(0.10)
    assert got["agy"].known is False, "gemini* matches nothing in this payload"


def test_ps_r5a_a_window_without_a_counted_field_counts(tmp_path):
    windows = {"3p-5h": _window(0.25, FAR["3p-5h"])}
    q = Quota(tmp_path, _pair_raw(), {"known": False, "windows": windows})
    got = q.read()["agy-partner"]
    assert got.known and got.headroom == pytest.approx(0.25)
    assert got.windows["3p-5h"]["counted"] is True


def test_ps_r5a_a_selected_window_that_is_not_a_valid_entry_yields_unknown(tmp_path):
    _control_known(tmp_path)
    q = Quota(tmp_path, _pair_raw(), {"known": True, "headroom": 0.5,
                                      "windows": {"gemini-5h": _window(0.5, FAR["gemini-5h"]),
                                                  "3p-5h": "garbage"}})
    got = q.read()
    assert got["agy-partner"].known is False
    assert got["agy"].known is True


def test_ps_r5_the_reset_margin_recomputation_never_lets_a_foreign_window_constrain(tmp_path):
    """agy's gemini-weekly has lapsed; the recomputation over the REMAINING
    windows must not pick the empty 3p-5h as agy's constraint."""
    payload = _payload(gw=0.9, g5=0.6, tw=0.5, t5=0.0,
                       resets={"gemini-weekly": "2000-01-01T00:00:00Z"})
    got = Quota(tmp_path, _pair_raw(), payload).read()
    assert got["agy"].headroom == pytest.approx(0.6), got["agy"]
    assert got["agy"].usable is True
    assert got["agy"].resets_at == FAR["gemini-5h"]


def test_ps_r5_the_reset_margin_recomputation_for_the_partner_ignores_gemini(tmp_path):
    payload = _payload(gw=0.5, g5=0.0, tw=0.9, t5=0.7,
                       resets={"3p-weekly": "2000-01-01T00:00:00Z"})
    got = Quota(tmp_path, _pair_raw(), payload).read()
    assert got["agy-partner"].headroom == pytest.approx(0.7), got["agy-partner"]
    assert got["agy-partner"].usable is True
    assert got["agy"].headroom == pytest.approx(0.0), "the gemini bucket is still full for agy"


def test_ps_r5_when_every_counted_window_has_lapsed_the_provider_has_room(tmp_path):
    payload = _payload(gw=0.0, g5=0.0, tw=0.5, t5=0.5,
                       resets={"gemini-weekly": "2000-01-01T00:00:00Z",
                               "gemini-5h": "2000-01-01T00:00:00Z"})
    got = Quota(tmp_path, _pair_raw(), payload).read()
    assert got["agy"].usable is True
    assert got["agy-partner"].headroom == pytest.approx(0.5)


def test_ps_r5_one_script_invocation_serves_owner_and_dependent(tmp_path):
    q = Quota(tmp_path, _pair_raw(), _payload())
    got = q.read()
    assert got["agy"].known and got["agy-partner"].known, "both are served by the one run"
    assert q.runs == 1
    q.read()
    assert q.runs == 1, "shared within the cache period"


def test_ps_r5_a_dependent_read_alone_still_finds_its_payload(tmp_path):
    q = Quota(tmp_path, _pair_raw(), _payload())
    got = budget_mod.read_all({"agy-partner": q.providers["agy-partner"]},
                              lambda n: _local(), q.tmp / "g", q.cfg)
    assert got["agy-partner"].known and got["agy-partner"].headroom == pytest.approx(0.0)
    assert q.runs == 1


def test_ps_r5_a_forced_refresh_refetches_the_shared_payload_once(tmp_path):
    q = Quota(tmp_path, _pair_raw(), _payload())
    q.read()
    q.set(_payload(gw=0.5, g5=0.5, tw=0.9, t5=0.9))
    got = q.read(use_cache=False, force=True)
    assert q.runs == 2, "one refetch for owner plus dependent, not one each"
    assert got["agy"].headroom == pytest.approx(0.5)
    assert got["agy-partner"].headroom == pytest.approx(0.9)


def _refresh_quota(root, *names):
    return cli.main(["--path", str(root), "refresh-quota", *names])


def test_ps_r5_refresh_quota_on_the_dependent_alone_refreshes_the_payload_once(
        tmp_path, monkeypatch, capsys):
    raw = _pair_raw()
    raw["agy"]["bin"] = "sh"
    root, config = _cli_project(tmp_path, monkeypatch, raw)
    count = tmp_path / "runs"
    c2.case_script(config, "ps-agy.sh",
                   f'budget) echo x >> "{count}"; echo \'{json.dumps(_payload())}\'; exit 0 ;;')
    _refresh_quota(root, "agy-partner")
    out = capsys.readouterr().out
    assert "unknown provider" not in out
    line = next(l for l in out.splitlines() if l.startswith("agy-partner"))
    assert "unknown" not in line and "0% headroom" in line, out
    assert _lines(count) == 1


def test_ps_r5_refresh_quota_on_owner_and_dependent_refreshes_the_payload_once(
        tmp_path, monkeypatch, capsys):
    raw = _pair_raw()
    raw["agy"]["bin"] = "sh"
    root, config = _cli_project(tmp_path, monkeypatch, raw)
    count = tmp_path / "runs"
    c2.case_script(config, "ps-agy.sh",
                   f'budget) echo x >> "{count}"; echo \'{json.dumps(_payload())}\'; exit 0 ;;')
    _refresh_quota(root, "agy", "agy-partner")
    out = capsys.readouterr().out
    line = next(l for l in out.splitlines() if l.startswith("agy-partner"))
    assert "unknown" not in line and "0% headroom" in line, out
    assert _lines(count) == 1


def test_ps_r5_cooldowns_stay_keyed_by_provider(tmp_path):
    q = Quota(tmp_path, _pair_raw(), _payload())
    until = time.time() + 3600
    got = q.read(cooldowns={"agy-partner": {"until": until, "reason": "cooling"}})
    assert got["agy-partner"].known, "control: the partner has a reading of its own"
    assert got["agy-partner"].cooldown_until == until and got["agy-partner"].usable is False
    assert got["agy"].cooldown_until is None and got["agy"].usable is True


def test_ps_r5_spend_stays_keyed_by_provider(tmp_path):
    q = Quota(tmp_path, _pair_raw(), _payload())
    got = q.read(spend_by_provider={"agy-partner": {"input": 5}})
    assert got["agy-partner"].known, "control: the partner has a reading of its own"
    assert got["agy-partner"].spent == {"input": 5}
    assert got["agy"].spent == {}


def test_ps_r5_the_age_rules_apply_to_the_shared_payloads_age(tmp_path):
    q = Quota(tmp_path, _pair_raw(), _payload(stale_seconds=5000))
    got = q.read(max_reading_age=600)
    assert got["agy"].stale is True and got["agy-partner"].stale is True
    fresh = Quota(tmp_path / "again", _pair_raw(), _payload(stale_seconds=5)).read(
        max_reading_age=600, use_cache=False)
    assert fresh["agy"].stale is False and fresh["agy-partner"].stale is False


def test_ps_r9_providers_without_budget_from_read_their_own_script_each(tmp_path):
    cfg = tmp_path / "cfg"
    count = tmp_path / "runs"
    c2.case_script(cfg, "ps-x.sh", f'budget) echo x >> "{count}"; '
                   'echo \'{"known": true, "headroom": 0.5}\'; exit 0 ;;')
    providers = load_providers({"base": {"bin": "b", "script": "ps-x.sh"},
                                "inst": {"extends": "base"}})
    got = budget_mod.read_all(providers, lambda n: _local(), tmp_path / "g", cfg)
    assert _lines(count) == 2, "no budget_from: each reads as today"
    assert got["base"].headroom == got["inst"].headroom == 0.5


# ===========================================================================
# PS-R6: a model runs only on a provider that allows it
# ===========================================================================

def _roster_project(tmp_path, agents, providers=None):
    root = tmp_path / "rp"
    paths = ProjectPaths(root)
    paths.ensure()
    paths.config.mkdir(parents=True, exist_ok=True)
    base = {"gem": {"bin": "gembin", "models_include": ["gemini-*"]},
            "partner": {"bin": "partnerbin", "models_include": ["claude-*", "gpt-*"]},
            "free": {"bin": "freebin"}}
    (paths.config / "providers.yaml").write_text(
        yaml.safe_dump({"providers": providers or base}))
    (paths.config / "agents.yaml").write_text(yaml.safe_dump({"agents": agents}))
    return paths


def _load_roster(tmp_path, agents, providers=None):
    return config_mod.load(_roster_project(tmp_path, agents, providers), seed=False)


def _agent(provider, model, **extra):
    return {"provider": provider, "model": model, "role": "x", **extra}


def test_ps_r6_control_routes_inside_their_allowlists_load(tmp_path):
    cfg = _load_roster(tmp_path, {
        "ok": _agent("gem", "gemini-3.1-pro-high", models={"partner": "claude-opus-4-6-thinking"}),
        "anything": _agent("free", "whatever-9")})
    assert "ok" in cfg.agents and "anything" in cfg.agents


def test_ps_r6_a_primary_route_outside_the_allowlist_fails_at_load_and_names_the_way_out(tmp_path):
    with pytest.raises(CONFIG_ERROR) as caught:
        _load_roster(tmp_path, {"scribe": _agent("gem", "claude-opus-4-6-thinking")})
    message = str(caught.value)
    for needle in ("scribe", "gem", "claude-opus-4-6-thinking", "partner"):
        assert needle in message, f"{needle!r} missing from: {message}"


def test_ps_r6_a_models_entry_outside_the_allowlist_fails_at_load(tmp_path):
    with pytest.raises(CONFIG_ERROR) as caught:
        _load_roster(tmp_path, {"scribe": _agent(
            "gem", "gemini-3.1-pro-high", models={"partner": "gemini-3.1-pro-high"})})
    message = str(caught.value)
    for needle in ("scribe", "partner", "gemini-3.1-pro-high", "gem"):
        assert needle in message, f"{needle!r} missing from: {message}"


def test_ps_r6_without_an_accepting_provider_the_message_names_no_provider_as_a_fix(tmp_path):
    with pytest.raises(CONFIG_ERROR) as caught:
        _load_roster(tmp_path, {"scribe": _agent("partner", "mystery-1")},
                     providers={"partner": {"bin": "p", "models_include": ["claude-*"]},
                                "gem": {"bin": "g", "models_include": ["gemini-*"]}})
    message = str(caught.value)
    assert "scribe" in message and "mystery-1" in message and "partner" in message
    assert "gem" not in message.replace("gemini", ""), "no provider accepts it, so none is suggested"


def test_ps_r6_a_provider_without_an_allowlist_allows_everything(tmp_path):
    cfg = _load_roster(tmp_path, {"a": _agent("free", "claude-opus-4-6-thinking"),
                                  "b": _agent("free", "gemini-3.1-pro-high")})
    assert set(cfg.agents) >= {"a", "b"}


@pytest.mark.parametrize("model,accepted", [
    ("gemini-", True), ("gemini-3", True), ("gemini", False),
    ("xgemini-3", False), ("claude-opus-4-6-thinking", False)])
def test_ps_r6_allowlist_boundaries_of_a_glob(tmp_path, model, accepted):
    agents = {"scribe": _agent("gem", model)}
    if accepted:
        assert "scribe" in _load_roster(tmp_path, agents).agents
    else:
        with pytest.raises(CONFIG_ERROR):
            _load_roster(tmp_path, agents)


class Admission:
    """`worker` runs on gem (gemini-a); `routed` also has a partner route;
    an empty model catalog, so only the allowlist can refuse a pin."""

    def __init__(self, tmp_path, monkeypatch, catalog=False):
        self.gem, self.gem_probe = _fake_cli(tmp_path, "gem")
        self.partner, self.partner_probe = _fake_cli(tmp_path, "partner")
        self.gem["models_include"] = ["gemini-*"]
        self.partner["models_include"] = ["claude-*", "gpt-*"]
        self.partner["family"] = "partner"
        self.headroom = {"gem": 1.0, "partner": 1.0}
        monkeypatch.setattr(budget_mod, "read_all", lambda *a, **kw: {
            n: budget_mod.Budget(n, known=True, headroom=v) for n, v in self.headroom.items()})
        self.runner = h.make_runner(
            tmp_path / "proj", monkeypatch,
            agents={"worker": AgentSpec("worker", "gem", "gemini-a"),
                    "routed": AgentSpec.from_dict("routed", {
                        "provider": "gem", "model": "gemini-a",
                        "models": {"partner": "claude-x"}}),
                    "advisor": AgentSpec("advisor", "gem", "gemini-a", conversational=True)},
            providers={"gem": self.gem, "partner": self.partner},
            project={"budget": {"blind_cooldown_seconds": 1}})
        self.runner.config.models = (
            {"gem": [{"id": "gemini-a"}], "partner": [{"id": "claude-x"}]} if catalog else {})
        self.tree = self.runner.tree

    def nodes(self):
        return self.tree.read()["nodes"]

    def assert_no_side_effect(self):
        assert self.nodes() == {}, "a refusal must create no node"
        assert not _calls(self.gem_probe) and not _calls(self.partner_probe)


def test_ps_r6_a_pinned_start_outside_the_allowlist_is_refused_with_no_node(tmp_path, monkeypatch):
    a = Admission(tmp_path, monkeypatch)
    result = _run_start(a.runner, "worker", model="claude-x")
    assert _refused(result), result
    for needle in ("worker", "gem", "claude-x", "partner"):
        assert needle in result["error"], (needle, result)
    a.assert_no_side_effect()


def test_ps_r6_the_refusal_names_the_accepting_provider_even_with_a_model_catalog(tmp_path, monkeypatch):
    a = Admission(tmp_path, monkeypatch, catalog=True)
    result = _run_start(a.runner, "worker", model="claude-x")
    assert _refused(result), result
    assert "partner" in result["error"] and "claude-x" in result["error"], result
    a.assert_no_side_effect()


def test_ps_r6_a_pin_no_provider_accepts_is_refused_without_naming_a_fix(tmp_path, monkeypatch):
    a = Admission(tmp_path, monkeypatch)
    result = _run_start(a.runner, "worker", model="mystery-1")
    assert _refused(result), result
    assert "mystery-1" in result["error"] and "partner" not in result["error"]
    a.assert_no_side_effect()


def test_ps_r6_a_refused_pin_is_refused_again_identically_and_still_creates_nothing(tmp_path, monkeypatch):
    a = Admission(tmp_path, monkeypatch)
    first = _run_start(a.runner, "worker", model="claude-x")
    second = _run_start(a.runner, "worker", model="claude-x")
    assert first["error"] == second["error"]
    a.assert_no_side_effect()


def test_ps_r6_a_pin_inside_the_allowlist_still_runs(tmp_path, monkeypatch):
    a = Admission(tmp_path, monkeypatch)
    result = _run_start(a.runner, "worker", model="gemini-b")
    assert result.get("agent_id"), result
    assert _flag(_calls(a.gem_probe)[0], "--model") == "gemini-b"


def test_ps_r6_a_pin_naming_the_partner_route_runs_on_the_partner(tmp_path, monkeypatch):
    a = Admission(tmp_path, monkeypatch)
    result = _run_start(a.runner, "routed", model="claude-x")
    assert result.get("agent_id"), result
    assert _calls(a.partner_probe) and not _calls(a.gem_probe)


def test_ps_r7_an_idle_conversation_on_a_provider_that_no_longer_allows_its_model_refuses(
        tmp_path, monkeypatch):
    a = Admission(tmp_path, monkeypatch)
    worktree = a.runner.paths.worktree("ag-c0nv01")
    worktree.mkdir(parents=True)
    a.tree.add(Node(id="ag-c0nv01", agent="advisor", provider="gem", model="claude-x",
                    parent=None, depth=1, status="idle", session_id="sess-old",
                    worktree=str(worktree), conversation=True, turns=1))
    result = _run_consult(a.runner)
    assert _refused(result), result
    for needle in ("claude-x", "partner"):
        assert needle in result["error"], (needle, result)
    assert not _calls(a.gem_probe) and not _calls(a.partner_probe), "no substitution"
    node = a.tree.get("ag-c0nv01")
    assert (node.status, node.session_id, node.turns) == ("idle", "sess-old", 1)
    assert set(a.nodes()) == {"ag-c0nv01"}, "and no new conversation was started for it"


def test_ps_r7_control_a_conversation_whose_model_is_still_allowed_resumes(tmp_path, monkeypatch):
    a = Admission(tmp_path, monkeypatch)
    worktree = a.runner.paths.worktree("ag-c0nv02")
    worktree.mkdir(parents=True)
    a.tree.add(Node(id="ag-c0nv02", agent="advisor", provider="gem", model="gemini-a",
                    parent=None, depth=1, status="idle", session_id="sess-old",
                    worktree=str(worktree), conversation=True, turns=1))
    result = _run_consult(a.runner)
    assert not result.get("error"), result
    assert _flag(_calls(a.gem_probe)[0], "--resume") == "sess-old"


# -- the catalog ------------------------------------------------------------

def test_ps_r6_a_stale_catalog_entry_is_dropped_on_refresh(tmp_path, monkeypatch, capsys):
    root, config = _cli_project(tmp_path, monkeypatch, {
        "gem": {"bin": "sh", "script": "ps-models.sh", "models_parse": "tsv",
                "models_include": ["gemini-*"], "spawn": {"args": ["x"]}},
        "free": {"bin": "sh", "script": "ps-models.sh", "models_parse": "tsv",
                 "spawn": {"args": ["x"]}}})
    c2.case_script(config, "ps-models.sh", "models) echo 'listing broke' >&2; exit 1 ;;")
    (config / "models.yaml").write_text(yaml.safe_dump({"models": {
        "gem": [{"id": "gemini-a"}, {"id": "claude-x"}],
        "free": [{"id": "gemini-a"}, {"id": "claude-x"}]}}))
    cli.main(["--path", str(root), "refresh-models"])
    capsys.readouterr()
    models = yaml.safe_load((config / "models.yaml").read_text())["models"]
    assert [m["id"] for m in models["gem"]] == ["gemini-a"], "retained but now excluded: dropped"
    assert [m["id"] for m in models["free"]] == ["gemini-a", "claude-x"], "no allowlist, no drop"


def test_ps_r6_a_freshly_listed_provider_only_records_what_it_allows(tmp_path, monkeypatch, capsys):
    root, config = _cli_project(tmp_path, monkeypatch, {
        "gem": {"bin": "sh", "script": "ps-models.sh", "models_parse": "tsv",
                "models_include": ["gemini-*"], "spawn": {"args": ["x"]}}})
    c2.case_script(config, "ps-models.sh",
                   "models) printf 'gemini-a\\tA\\nclaude-x\\tX\\n'; exit 0 ;;")
    cli.main(["--path", str(root), "refresh-models"])
    capsys.readouterr()
    models = yaml.safe_load((config / "models.yaml").read_text())["models"]
    assert [m["id"] for m in models["gem"]] == ["gemini-a"]


# ===========================================================================
# PS-R7 / PS-R7a: existing work is never silently re-routed
# ===========================================================================

class Queue:
    """gem (gemini-*) and partner (claude-*/gpt-*), each its own fake CLI."""

    def __init__(self, tmp_path, monkeypatch):
        self.monkeypatch = monkeypatch
        self.adm = Admission(tmp_path, monkeypatch)
        self.runner, self.tree = self.adm.runner, self.adm.tree
        self.headroom = self.adm.headroom
        monkeypatch.setattr(server, "runner", lambda: self.runner)

    def queue(self, agent="worker", model=None, task="work"):
        spec = {"agent": agent, "task": task, "timeout": None, "model": model, "workdir": None}
        return self.tree.defer(spec, time.time() - 1.0, "quota")["id"]

    def call(self, fn, *args, **kwargs):
        async def go():
            value = fn(*args, **kwargs)
            return await value if asyncio.iscoroutine(value) else value
        return asyncio.run(go())

    def drain(self, timeout=10):
        return self.call(server.wait_for_agents, timeout=timeout)

    def listed(self):
        result = self.call(server.list_deferred)
        if isinstance(result, dict):
            (result,) = [v for v in result.values() if isinstance(v, list)]
        return {e["id"]: e for e in result}


@pytest.fixture
def q(tmp_path, monkeypatch):
    return Queue(tmp_path, monkeypatch)


def test_ps_r7_a_legacy_pinned_entry_the_provider_no_longer_allows_is_refused_with_the_hint(q):
    df = q.queue(model="claude-x")
    q.drain(timeout=1)
    entry = q.listed().get(df)
    assert entry and entry["status"] == "refused", (entry, "the entry must stay listed")
    for needle in ("worker", "claude-x", "partner"):
        assert needle in entry["reason"], (needle, entry["reason"])
    q.adm.assert_no_side_effect()


def test_ps_r7_a_refused_entry_is_never_retried_or_substituted(q):
    df = q.queue(model="claude-x")
    q.drain(timeout=1)
    q.drain(timeout=1)
    q.drain(timeout=1)
    assert (q.listed().get(df) or {}).get("status") == "refused"
    q.adm.assert_no_side_effect()
    assert [e["outcome"] for e in q_exits(q)] == ["refused"], "one exit, not one per drain"


def q_exits(q):
    path = q.runner.paths.events_file
    events = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return [e for e in events if e.get("kind") == "deferred_exit"]


def test_ps_r7a_a_legacy_entry_with_no_pin_routes_normally(q):
    df = q.queue(model=None)
    result = q.drain()
    assert len(result["deferred"]["restarted"]) == 1, result
    assert df not in q.listed()
    assert _calls(q.adm.gem_probe) and not _calls(q.adm.partner_probe)


def test_ps_r7a_a_legacy_entry_pinned_to_an_allowed_model_of_its_configured_provider_restarts(q):
    q.queue(model="gemini-b")
    result = q.drain()
    assert len(result["deferred"]["restarted"]) == 1, result
    assert _flag(_calls(q.adm.gem_probe)[0], "--model") == "gemini-b"


def test_ps_r7a_a_legacy_pin_to_the_agents_configured_partner_route_restarts_there(q):
    q.queue(agent="routed", model="claude-x")
    result = q.drain()
    assert len(result["deferred"]["restarted"]) == 1, result
    assert _calls(q.adm.partner_probe) and not _calls(q.adm.gem_probe)


def test_ps_r7a_a_roster_change_never_moves_a_pinned_entry_to_another_family(q):
    """The agent loses its partner route while the entry waits: the pin is
    refused, it does not run on gem and it does not silently find the partner."""
    df = q.queue(agent="routed", model="claude-x")
    q.runner.config.agents["routed"] = AgentSpec.from_dict("routed", {
        "provider": "gem", "model": "gemini-a"})
    q.drain(timeout=1)
    assert (q.listed().get(df) or {}).get("status") == "refused"
    q.adm.assert_no_side_effect()


def test_ps_r7a_a_new_deferred_entry_records_its_destination(q):
    q.headroom["gem"] = 0.0
    q.headroom["partner"] = 0.0
    result = q.call(server.start_agent, "worker", "real work")
    assert result.get("deferred") is True, result
    (entry,) = [d for d in q.tree.read()["deferred"] if d["id"] == result["deferred_id"]]
    recorded = [v for v in _values(entry["spec"]) if v in ("gem", "partner")]
    assert recorded, f"no provider recorded next to agent and model in {entry['spec']}"
    assert entry["spec"]["agent"] == "worker" and "model" in entry["spec"]


def test_ps_r7_history_recorded_under_the_owner_is_not_rewritten(q):
    q.tree.add(Node(id="ag-hist01", agent="worker", provider="gem", model="claude-x",
                    parent=None, depth=1, status="done"))
    q.queue(model="claude-x")
    q.drain(timeout=1)
    node = q.tree.get("ag-hist01")
    assert (node.provider, node.model, node.status) == ("gem", "claude-x", "done")


# ===========================================================================
# PS-R8 / R8a / R8b: the shipped configuration
# ===========================================================================

def _shipped():
    return load_providers(_shipped_raw())


def test_ps_r8_the_shipped_defaults_load_and_yield_agy_and_agy_partner():
    providers = _shipped()
    assert {"agy", "agy-partner"} <= set(providers)


def test_ps_r8_agy_partner_is_its_own_family_and_never_a_substitute_for_agy():
    providers = _shipped()
    fam = families(providers)
    assert providers["agy-partner"].family == "agy-partner"
    assert providers["agy"].family == "agy"
    assert fam["agy"] == ["agy"] and fam["agy-partner"] == ["agy-partner"]


def test_ps_r8a_the_allowlists_split_gemini_from_claude_and_gpt():
    p = _shipped()
    for model in ("gemini-3.1-pro-high", "gemini-3.8-flash-medium"):
        assert p["agy"].allows_model(model) and not p["agy-partner"].allows_model(model)
    for model in ("claude-opus-4-6-thinking", "claude-sonnet-4-6", "gpt-oss-120b-medium"):
        assert p["agy-partner"].allows_model(model) and not p["agy"].allows_model(model)
    assert not p["agy"].allows_model("") and not p["agy-partner"].allows_model("")


def test_ps_r8_agy_partner_extends_agys_integration():
    p = _shipped()
    assert p["agy-partner"].bin == p["agy"].bin
    assert p["agy-partner"].spawn == p["agy"].spawn


def test_ps_r8_the_shipped_agents_still_load_against_the_shipped_allowlists(tmp_path):
    paths = ProjectPaths(tmp_path / "plain")
    paths.ensure()
    cfg = config_mod.load(paths, seed=False)
    assert cfg.agents, "the shipped roster passes PS-R6 with the split providers"


def test_ps_r8_the_shipped_opencode_zai_uses_extends_but_neither_new_key():
    zai = _shipped_raw()["opencode-zai"]
    assert zai.get("extends") == "opencode"
    assert "auth_from" not in zai and "budget_from" not in zai


def test_ps_r8_the_providers_yaml_comment_block_explains_the_three_keys():
    text = (shipped_defaults_dir() / "providers.yaml").read_text()
    comments = "\n".join(line for line in text.splitlines() if line.lstrip().startswith("#"))
    for key in ("auth_from", "budget_from", "budget_windows"):
        assert key in comments, f"{key} is not explained in the comment block"


def test_ps_r8b_the_comment_block_says_how_to_adopt_the_split():
    text = (shipped_defaults_dir() / "providers.yaml").read_text()
    comments = "\n".join(line for line in text.splitlines() if line.lstrip().startswith("#"))
    assert "agy-partner" in comments


def _fake_agy_on_path(tmp_path, monkeypatch, stdout: str):
    bindir = tmp_path / "agybin"
    bindir.mkdir()
    count = tmp_path / "agy-runs"
    binary = bindir / "agy"
    binary.write_text(f"#!/bin/sh\necho x >> '{count}'\ncat <<'EOF'\n{stdout}\nEOF\n")
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}/usr/bin{os.pathsep}/bin")
    return count


def _usage(gw, g5, tw, t5):
    def bucket(ident, left, reset):
        return {"id": ident, "window": ident.split("-")[-1],
                "remaining_fraction": left, "reset_time": reset}
    return json.dumps({"command": {"name": "usage", "data": {"groups": [
        {"name": "Gemini Models", "buckets": [
            bucket("gemini-weekly", gw, FAR["gemini-weekly"]),
            bucket("gemini-5h", g5, FAR["gemini-5h"])]},
        {"name": "Claude and GPT models", "buckets": [
            bucket("3p-weekly", tw, FAR["3p-weekly"]),
            bucket("3p-5h", t5, FAR["3p-5h"])]}]}}})


def _shipped_pair(tmp_path):
    providers = _shipped()
    return {n: providers[n] for n in ("agy", "agy-partner")}


def test_ps_r8_shipped_agy_and_partner_split_one_real_agy_payload(tmp_path, monkeypatch):
    count = _fake_agy_on_path(tmp_path, monkeypatch, _usage(0.80, 0.93, 0.32, 0.0))
    got = budget_mod.read_all(_shipped_pair(tmp_path), lambda n: _local(),
                              tmp_path / "g", tmp_path / "none")
    assert got["agy"].known and got["agy"].headroom == pytest.approx(0.80)
    assert got["agy-partner"].known and got["agy-partner"].headroom == pytest.approx(0.0)
    assert got["agy"].usable and not got["agy-partner"].usable
    assert _lines(count) == 1, "one agy invocation serves both"
    assert got["agy"].windows["3p-5h"]["counted"] is False
    assert got["agy-partner"].windows["3p-5h"]["counted"] is True
    assert got["agy-partner"].windows["gemini-5h"]["counted"] is False


def test_ps_r8_shipped_gemini_window_full_does_not_block_the_partner(tmp_path, monkeypatch):
    _fake_agy_on_path(tmp_path, monkeypatch, _usage(0.0, 0.0, 0.6, 0.7))
    got = budget_mod.read_all(_shipped_pair(tmp_path), lambda n: _local(),
                              tmp_path / "g", tmp_path / "none")
    assert not got["agy"].usable and got["agy-partner"].usable
    assert got["agy-partner"].headroom == pytest.approx(0.6)


def test_ps_r8a_the_partner_reading_does_not_show_the_inherited_gemini_note(tmp_path, monkeypatch):
    _fake_agy_on_path(tmp_path, monkeypatch, _usage(0.80, 0.04, 0.32, 0.5))
    got = budget_mod.read_all(_shipped_pair(tmp_path), lambda n: _local(),
                              tmp_path / "g", tmp_path / "none")
    assert "gemini" in got["agy"].note.lower(), "control: agy keeps its own note"
    assert "gemini" not in got["agy-partner"].note.lower()


def test_ps_r8_shipped_agy_keeps_its_own_counted_flags_in_the_script_output(tmp_path, monkeypatch):
    _fake_agy_on_path(tmp_path, monkeypatch, _usage(0.80, 0.93, 0.32, 0.0))
    script = shipped_defaults_dir() / "providers" / "agy.sh"
    import subprocess
    out = subprocess.run(["sh", str(script), "budget"], capture_output=True, text=True,
                         env={"PATH": "/usr/bin:/bin",
                              "MULTIAGENTS_BIN": str(tmp_path / "agybin" / "agy")})
    data = json.loads(out.stdout)
    assert data["windows"]["3p-5h"]["counted"] is False
    assert data["windows"]["gemini-weekly"]["counted"] is True


def test_ps_r8_shipped_agy_and_partner_are_checked_once_and_both_authenticate(tmp_path, monkeypatch):
    count = _fake_agy_on_path(tmp_path, monkeypatch, '{"status":"SUCCESS"}')
    states = auth_mod.check_all(_shipped_pair(tmp_path), lambda n: _local(),
                                tmp_path / "g", tmp_path / "none")
    assert states["agy"].status == "authenticated"
    assert states["agy-partner"].status == "authenticated"
    assert _lines(count) == 1, "the partner shares agy's login and check"


def test_ps_r8b_a_global_agy_override_without_the_new_keys_still_gets_the_defaults(tmp_path):
    from multiagents.paths import global_config_dir
    gdir = global_config_dir()
    gdir.mkdir(parents=True, exist_ok=True)
    override = gdir / "providers.yaml"
    override.write_text(yaml.safe_dump({"providers": {"agy": {"notes": "mine"}}}))
    paths = ProjectPaths(tmp_path / "plain")
    paths.ensure()
    cfg = config_mod.load(paths, seed=False)
    agy = load_providers(cfg.providers)["agy"]
    assert agy.notes == "mine"
    assert agy.allows_model("gemini-3.1-pro-high") and not agy.allows_model("claude-x")


def test_ps_r8b_a_global_override_that_replaces_the_allowlist_wins(tmp_path):
    from multiagents.paths import global_config_dir
    gdir = global_config_dir()
    gdir.mkdir(parents=True, exist_ok=True)
    (gdir / "providers.yaml").write_text(yaml.safe_dump(
        {"providers": {"agy": {"models_include": ["*"]}}}))
    paths = ProjectPaths(tmp_path / "plain")
    paths.ensure()
    agy = load_providers(config_mod.load(paths, seed=False).providers)["agy"]
    assert agy.allows_model("claude-opus-4-6-thinking"), "the override replaced the list"


def test_ps_r8b_existing_provider_overrides_are_not_rewritten_by_loading(tmp_path):
    from multiagents.paths import global_config_dir
    gdir = global_config_dir()
    gdir.mkdir(parents=True, exist_ok=True)
    override = gdir / "providers.yaml"
    text = yaml.safe_dump({"providers": {"agy": {"notes": "mine"}}})
    override.write_text(text)
    paths = ProjectPaths(tmp_path / "plain")
    paths.ensure()
    config_mod.load(paths)
    assert override.read_text() == text


# ===========================================================================
# PS-R9: nothing else changes
# ===========================================================================

def test_ps_r9_a_provider_block_with_none_of_the_new_keys_loads_exactly_as_before():
    raw = {"claude": {"bin": "claude", "env": {"K": "1"}},
           "claude-work": {"extends": "claude", "env": {"K": "2"}}}
    providers = load_providers(raw)
    assert providers["claude-work"].env == {"K": "2"}
    assert families(providers) == {"claude": ["claude", "claude-work"]}


# ===========================================================================
# PS-R10: decisions on the silences of R1..R9
# ===========================================================================

@pytest.mark.parametrize("key", ["auth_from", "budget_from"])
@pytest.mark.parametrize("value", ["", 7, ["own"], True])
def test_ps_r10_an_empty_or_non_string_owner_is_a_config_error(key, value):
    raw = {"own": {"bin": "o"}, "dep": {"bin": "d", key: value}}
    _rejected(raw, "dep", key)


@pytest.mark.parametrize("value", ["gemini*", 5, [1], ["ok", 2], {"a": "b"}])
def test_ps_r10_budget_windows_must_be_a_list_of_strings(value):
    raw = {"own": {"bin": "o", "budget_windows": value}}
    _rejected(raw, "own", "budget_windows")


def test_ps_r10_a_list_of_strings_including_empty_is_accepted():
    load_providers({"a": {"bin": "a", "budget_windows": []},
                    "b": {"bin": "b", "budget_windows": ["x*", "y"]}})


def test_ps_r10_a_disabled_budget_owner_still_provides_its_payload(tmp_path):
    raw = _pair_raw()
    raw["agy"]["enabled"] = False
    q = Quota(tmp_path, raw, _payload())
    got = q.read()
    assert "agy" not in got, "a disabled provider is not read for routing"
    assert got["agy-partner"].known and got["agy-partner"].headroom == pytest.approx(0.0)


def test_ps_r10_an_env_key_inherited_through_any_extends_base_is_never_a_conflict():
    raw = {"own": {"bin": "o", "env": {"K": "1"}},
           "base": {"bin": "b", "env": {"K": "2"}},
           "dep": {"extends": "base", "auth_from": "own"}}
    assert "dep" in load_providers(raw)


def test_ps_r10_an_explicit_key_is_still_checked_when_extends_is_from_another_base():
    raw = {"own": {"bin": "o", "env": {"K": "1"}},
           "base": {"bin": "b"},
           "dep": {"extends": "base", "auth_from": "own", "env": {"K": "2"}}}
    _rejected(raw, "dep", "auth_from")


def test_ps_r10_invalid_selected_windows_are_ignored_and_the_valid_ones_count(tmp_path):
    _control_known(tmp_path)
    payload = {"known": True, "headroom": 0.5, "windows": {
        "gemini-5h": _window(0.5, FAR["gemini-5h"]),
        "3p-weekly": _window(0.4, FAR["3p-weekly"]),
        "3p-5h": "garbage"}}
    got = Quota(tmp_path, _pair_raw(), payload).read()["agy-partner"]
    assert got.known is True
    assert got.headroom == pytest.approx(0.4)


def test_ps_r10_a_bad_route_on_a_disabled_agent_is_still_a_config_error(tmp_path):
    with pytest.raises(CONFIG_ERROR) as caught:
        _load_roster(tmp_path, {"parked": _agent("gem", "claude-opus-4-6-thinking",
                                                 disabled=True)})
    assert "parked" in str(caught.value) and "partner" in str(caught.value)


def test_ps_r10_the_login_hint_for_a_dependent_names_the_owner(tmp_path, monkeypatch):
    runner, _ = _auth_runner(tmp_path, monkeypatch, 'echo "expired"; exit 10')
    out = server.auth_status()
    assert "multiagents auth login own" in out["providers"]["dep"]["fix"]
    assert "auth login dep" not in out["providers"]["dep"]["fix"]
    states = auth_mod.check_all(runner.providers, lambda n: _local(),
                                tmp_path / "g", runner.paths.config)
    assert states["dep"].fix == "multiagents auth login own"
