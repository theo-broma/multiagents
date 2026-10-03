"""C17 — two accounts of one provider family coexist under docker.

Contract: `context/specs/c17-family-account-isolation.md` (FA-R1..R5, with the
revision section: FA-R1a, FA-R2a, FA-R3a, which override the earlier wording).

Seams, all existing:
  providers.load_providers                    config load
  DockerExecutor.private_state / mounts       the mount map
  DockerExecutor.container_credential_owner   who owns a provider's credentials
  DockerExecutor.adapter_env                  the environment of a run
  scripts.build_env + the codex adapter       what `check`/`budget` read
  cli.cmd_docker(login)                       the `docker exec` argv built
                                              (os.execvp patched; no docker)
  cli.cmd_doctor                              the problem lines

ASSUMPTIONS (the contract is silent; each is the loosest reading):
- A refusal (FA-R1) is a `ValueError`, the type load rejections and the
  host-authority mount check already use. It may come from config load, from
  building the executor, from `private_state()` or from `mounts()`: the helper
  `refusal()` accepts the first of those that raises.
- The `docker exec` of a login names the profile either with
  `--env CODEX_HOME=<container path>` or, if the login action derives it, with
  `--env MULTIAGENTS_PRIVATE_HOME=<container path>`. Never the other
  provider's path under either name.
- The login command is the provider's login action: the exec'd command ends in
  `login` (the action script's argument) or carries `--device-auth`, and is
  never the bare native binary.
- Budget "only its own backing" (FA-R2a) is read as: neither a sibling's
  backing nor the host-side default codex profile
  (`~/.multiagents/profiles/codex`) contributes to a `codex-b` reading.
- Doctor "problem line" is read from the count/exit status: the collision
  config reports more problems than the same project without the collision,
  and the output names both providers and the path.

Stubs: none; the source tree is untouched. Fakes: a fake `codex` (the shared
harness script, with `login status` made to depend on `$CODEX_HOME/auth.json`)
installed at the versions layout, and a fake `docker` for the doctor test.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import sys
import time
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import c2_harness as c2  # noqa: E402
import c3_harness as h3  # noqa: E402
import codex_harness as h  # noqa: E402

from multiagents import cli, manifest, scripts  # noqa: E402
from multiagents.executor import docker as docker_mod  # noqa: E402
from multiagents.executor.docker import DockerExecutor  # noqa: E402
from multiagents.paths import ProjectPaths, global_config_dir  # noqa: E402
from multiagents.providers import load_providers  # noqa: E402
from multiagents.paths import state_root  # noqa: E402

SRC = Path(__file__).resolve().parents[1] / "src" / "multiagents"
SHIPPED_PROVIDERS = SRC / "defaults" / "providers.yaml"

OLD_SHAPE = {"extends": "codex", "family": "codex",
             "env": {"MULTIAGENTS_CODEX_PROFILE": "~/.multiagents/profiles/codex-b"}}
NEW_SHAPE = {"extends": "codex", "family": "codex",
             "container_private_home": [".codex-b"]}

HAPPY = [{"type": "thread.started", "thread_id": "t1"}, {"type": "turn.started"},
         {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}}]


# ---------------------------------------------------------------------------
# the world: a host home with a codex installed at its versions layout
# ---------------------------------------------------------------------------

LOGIN_BY_AUTH_FILE = (
    'status = B.get("status", "logged_in")',
    'status = "logged_in" if os.path.exists(os.path.join('
    'os.environ.get("CODEX_HOME", ""), "auth.json")) else "not_logged_in"')


class World:
    def __init__(self, tmp_path: Path, monkeypatch):
        self.tmp = tmp_path
        self.home = Path(os.environ["HOME"])
        self.releases = self.home / ".codex" / "packages" / "standalone" / "releases"
        self.native_dir = self.releases / "v1" / "bin"
        self.native_dir.mkdir(parents=True)
        old, new = LOGIN_BY_AUTH_FILE
        assert old in h.FAKE_CODEX, "the shared fake codex changed shape"
        self.native = self.native_dir / "codex"
        self.native.write_text(h.FAKE_CODEX.replace(old, new))
        self.native.chmod(0o755)
        self.behaviour = {"secret": h.SECRET, "events": HAPPY}
        self.save()
        self.launcher = self.home / ".local" / "bin" / "codex"
        self.launcher.parent.mkdir(parents=True)
        self.launcher.symlink_to(self.native)
        monkeypatch.setenv("PATH", f"{self.launcher.parent}:{Path(sys.executable).parent}:/usr/bin:/bin")
        self.monkeypatch = monkeypatch

    def save(self):
        (self.native_dir / "behaviour.json").write_text(json.dumps(self.behaviour))

    def calls(self):
        log = self.native_dir / "calls.jsonl"
        return [json.loads(l) for l in log.read_text().splitlines() if l] if log.exists() else []

    def executor(self, raw: dict, **config) -> DockerExecutor:
        providers = load_providers(raw)
        paths = ProjectPaths(self.tmp / "project")
        paths.root.mkdir(exist_ok=True)
        paths.config.mkdir(parents=True, exist_ok=True)
        config.setdefault("image", "img")
        config.setdefault("network", "bridge")
        ex = DockerExecutor(config, paths, providers, global_config_dir())
        self.monkeypatch.setattr(ex, "inside", lambda: False)
        return ex


@pytest.fixture
def world(tmp_path, monkeypatch) -> World:
    return World(tmp_path, monkeypatch)


def shipped(**extra) -> dict:
    raw = c2.raw_shipped_providers()
    raw.update(copy.deepcopy(extra))
    return raw


def two_codex(world, second: dict = NEW_SHAPE) -> DockerExecutor:
    return world.executor(shipped(**{"codex-b": second}))


def refusal(world: World, raw: dict, **config) -> str | None:
    """The ValueError message of the first step that refuses `raw`, else None."""
    try:
        providers = load_providers(raw)
        ex = world.executor(raw, **config)
        ex.private_state()
        ex.mounts()
        del providers
    except ValueError as exc:
        return str(exc)
    return None


def named(text: str, name: str) -> bool:
    """`name` appears as a whole provider name (codex is not inside codex-b)."""
    return re.search(rf"(?<![\w-]){re.escape(name)}(?![\w-])", text) is not None


def covering(mounts, path: Path):
    """The mount that is effective at `path`: the deepest one containing it."""
    hits = [(p, ro) for p, ro in mounts if p == path or p in path.parents]
    return max(hits, key=lambda pair: len(pair[0].parts), default=None)


def backing_root(name: str) -> Path:
    return state_root() / "container-state" / "shared" / name


# ===========================================================================
# FA-R1 / FA-R1a — no silent mount collision
# ===========================================================================

def test_fa_r1_old_shape_codex_b_is_refused_naming_both_providers_and_the_path(world):
    message = refusal(world, shipped(**{"codex-b": OLD_SHAPE}))

    assert message is not None, "codex and codex-b both claim ~/.codex and nothing refused"
    assert named(message, "codex") and named(message, "codex-b"), message
    assert re.search(r"\.codex(?![\w-])", message), message


def test_fa_r1_the_error_names_the_two_fixes(world):
    message = refusal(world, shipped(**{"codex-b": OLD_SHAPE}))

    assert message is not None
    assert "container_private_home" in message, message
    assert "auth_from" in message, message


def test_fa_r1a_the_refusal_is_a_value_error_before_any_mount_map_is_returned(world):
    # The silent replacement is the bug: the map must not come back with one of
    # the two backings dropped.
    ex = world.executor(shipped(**{"codex-b": OLD_SHAPE}))
    with pytest.raises(ValueError):
        ex.private_state()
        ex.mounts()


def test_fa_r1_unrelated_providers_with_the_same_private_path_are_refused(world):
    raw = {"alpha": {"bin": "alpha", "container_private_home": [".shared-state"]},
           "beta": {"bin": "beta", "container_private_home": [".shared-state"]}}

    message = refusal(world, raw)

    assert message is not None
    assert named(message, "alpha") and named(message, "beta"), message
    assert ".shared-state" in message


@pytest.mark.parametrize("spelling", ["./.codex", ".codex/", "./.codex/"])
def test_fa_r1_the_same_path_spelled_differently_is_still_a_collision(world, spelling):
    second = {**NEW_SHAPE, "container_private_home": [spelling]}

    message = refusal(world, shipped(**{"codex-b": second}))

    assert message is not None, f"{spelling!r} resolves to ~/.codex and nothing refused"
    assert named(message, "codex-b")


def test_fa_r1_a_provider_that_claims_a_path_nobody_else_does_is_accepted(world):
    assert refusal(world, shipped(**{"codex-b": NEW_SHAPE})) is None


def test_fa_r1_providers_sharing_an_owner_through_auth_from_are_not_a_collision(world):
    raw = shipped(**{"agy-partner": {"extends": "agy", "auth_from": "agy"}})

    assert refusal(world, raw) is None
    ex = world.executor(raw)
    assert ex.container_credential_owner("agy-partner") == "agy"
    assert list(ex.private_state("agy-partner")) == [world.home / ".gemini"]
    assert ex.private_state("agy-partner") == ex.private_state("agy")


def test_fa_r1a_claude_and_claude_b_under_the_sidecar_are_accepted(world):
    raw = shipped(**{"claude-b": {"extends": "claude", "container_account": "b"}})
    config = {"auth_proxy": True, "mount_cli_from_host": False}

    assert refusal(world, raw, **config) is None
    ex = world.executor(raw, **config)
    assert ex.container_credential_owner("claude-b") == ex.container_credential_owner("claude")
    assert ex.private_state("claude-b") == ex.private_state("claude")


def test_fa_r1a_claude_b_without_the_sidecar_is_a_real_collision(world):
    # Without the sidecar the two have different owners and one path: the
    # owner, not the raw string, is what decides.
    raw = shipped(**{"claude-b": {"extends": "claude", "container_account": "b"}})

    message = refusal(world, raw, auth_proxy=False)

    assert message is not None
    assert named(message, "claude") and named(message, "claude-b"), message


def test_fa_r1_a_provider_with_no_private_home_never_collides(world):
    raw = shipped(**{"codex-b": {"extends": "codex", "family": "codex",
                                 "container_private_home": []}})

    assert refusal(world, raw) is None


# ===========================================================================
# FA-R2 / FA-R2a — a second codex account is declarable without code
# ===========================================================================

def test_fa_r2_the_documented_shape_is_in_providers_yaml_next_to_the_other_examples():
    text = SHIPPED_PROVIDERS.read_text()
    comments = [l for l in text.splitlines() if l.lstrip().startswith("#")]

    assert any(re.search(r"codex-b\s*:", l) for l in comments), \
        "no commented `codex-b:` example in defaults/providers.yaml"
    assert any(".codex-b" in l for l in comments), "the example gives no private path"
    # the shipped config itself still declares exactly the real providers
    assert "codex-b" not in c2.raw_shipped_providers()


def test_fa_r2a_two_codex_accounts_have_distinct_private_paths_and_owner_dirs(world):
    ex = two_codex(world)

    first, second = ex.private_state("codex"), ex.private_state("codex-b")

    assert list(first) == [world.home / ".codex"]
    assert list(second) == [world.home / ".codex-b"]
    (a,), (b,) = first.values(), second.values()
    assert a != b
    assert a not in b.parents and b not in a.parents
    everything = ex.private_state()
    assert everything[world.home / ".codex"] == a
    assert everything[world.home / ".codex-b"] == b


def test_fa_r2a_codex_keeps_the_mapping_it_had_alone(world):
    alone = world.executor(shipped()).private_state("codex")

    assert two_codex(world).private_state("codex") == alone


def test_fa_r2a_both_private_homes_are_mounted_writable(world):
    mounts = dict(two_codex(world).mounts())

    assert mounts[world.home / ".codex"] is False
    assert mounts[world.home / ".codex-b"] is False


@pytest.mark.parametrize("name, expected", [("codex", ".codex"), ("codex-b", ".codex-b")])
def test_fa_r2a_each_run_is_told_its_own_private_home(world, name, expected):
    ex = two_codex(world)

    env = ex.adapter_env(["/x/codex.py"], {"MULTIAGENTS_PROVIDER": name}, name)

    assert env["MULTIAGENTS_PRIVATE_HOME"] == str(world.home / expected)
    assert env["MULTIAGENTS_EXECUTOR"] == "docker"


@pytest.mark.parametrize("name, expected", [("codex", ".codex"), ("codex-b", ".codex-b")])
def test_fa_r2a_a_run_gets_its_own_codex_home(world, name, expected):
    ex = two_codex(world)
    prov = ex.providers[name]
    workdir = world.tmp / "work"
    workdir.mkdir()
    argv = prov.build_command(prompt="p", model="m", workdir=str(workdir), permission="sandbox")
    env = {"PATH": os.environ["PATH"], "HOME": str(world.home), "LANG": "C.UTF-8",
           **ex.adapter_env(argv, {"MULTIAGENTS_PROVIDER": name}, name)}

    done = h.invoke(argv[1:], env, cwd=workdir)

    assert done.returncode == 0, done.stderr
    homes = {c["codex_home"] for c in world.calls() if "exec" in c["argv"]}
    assert homes == {str(world.home / expected)}


@pytest.mark.parametrize("name", ["codex", "codex-b"])
def test_fa_r2a_the_executable_stays_resolvable_and_read_only(world, name):
    ex = two_codex(world)
    env = ex.adapter_env(["/x/codex.py"], {"MULTIAGENTS_PROVIDER": name}, name)
    binary = Path(env["MULTIAGENTS_BIN"])

    assert binary.is_file() and os.access(binary, os.X_OK), env["MULTIAGENTS_BIN"]
    assert world.releases in binary.resolve().parents
    effective = covering(ex.mounts(), binary.resolve())
    assert effective is not None, "the executable is not reachable in the container"
    assert effective[1] is True, f"{binary} would be writable or shadowed: {effective}"
    assert covering(ex.mounts(), world.releases) == (world.releases, True)


def test_fa_r2a_the_versions_root_is_not_mounted_inside_codex_bs_writable_home(world):
    mounts = dict(two_codex(world).mounts())

    assert world.releases in mounts
    assert world.home / ".codex-b" not in world.releases.parents


def _seed_auth(ex, name: str):
    (backing,) = ex.private_state(name).values()
    backing.mkdir(parents=True, exist_ok=True)
    (backing / "auth.json").write_text('{"tokens": "x"}')
    return backing


def _action(ex, name: str, action: str, **extra):
    env = scripts.build_env(name, ex.providers[name], ex)
    env.update(extra)
    return h.invoke([action], env)


@pytest.mark.parametrize("owner, other", [("codex", "codex-b"), ("codex-b", "codex")])
def test_fa_r2a_check_reads_only_its_own_auth_fixture(world, owner, other):
    ex = two_codex(world)
    ex.mounts()                                    # allocate both backings
    _seed_auth(ex, owner)

    own = _action(ex, owner, "check")
    foreign = _action(ex, other, "check")

    assert own.returncode == 0, own.stderr
    assert foreign.returncode != 0, "the sibling's login was read as this account's"
    seen = {c["codex_home"] for c in world.calls()}
    assert seen == {str(next(iter(ex.private_state(owner).values()))),
                    str(next(iter(ex.private_state(other).values())))}


def _rollout(home: Path, name: str, five_h: float, now: float):
    h.write_rollout(home, name, [h.token_count_line(
        now - 5, h.window(five_h, 300, int(now + 3600)),
        h.window(five_h, 10080, int(now + 86400)))])


def _used(done) -> float:
    assert done.returncode == 0, done.stderr
    data = json.loads(done.stdout)
    assert data.get("known") is True, data
    return data["windows"]["5h"]["percent"]


@pytest.mark.parametrize("owner, other", [("codex", "codex-b"), ("codex-b", "codex")])
def test_fa_r2a_budget_reads_only_its_own_backing(world, owner, other):
    ex = two_codex(world)
    ex.mounts()
    now = time.time()
    (mine,) = ex.private_state(owner).values()
    (theirs,) = ex.private_state(other).values()
    _rollout(mine, "a", 20.0, now - 600)
    _rollout(theirs, "b", 70.0, now)               # newer, so a union would pick it

    assert _used(_action(ex, owner, "budget")) == pytest.approx(20.0)


def test_fa_r2a_codex_bs_budget_ignores_a_reading_with_no_backing_of_its_own(world):
    ex = two_codex(world)
    ex.mounts()
    (mine,) = ex.private_state("codex-b").values()
    mine.mkdir(parents=True, exist_ok=True)

    done = _action(ex, "codex-b", "budget")

    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout).get("known") is False


def test_fa_r2a_codex_bs_budget_does_not_fall_back_to_the_host_codex_profile(world):
    # The rollout fallback also scans `~/.multiagents/profiles/codex`, which is
    # the LOCAL codex account's. It must not stand in for codex-b.
    ex = two_codex(world)
    ex.mounts()
    host_profile = world.home / ".multiagents" / "profiles" / "codex"
    now = time.time()
    _rollout(host_profile, "h", 90.0, now)
    (mine,) = ex.private_state("codex-b").values()
    _rollout(mine, "b", 15.0, now - 3600)

    assert _used(_action(ex, "codex-b", "budget")) == pytest.approx(15.0)

    # and with nothing of its own, the host profile alone says nothing
    for f in mine.rglob("*.jsonl"):
        f.unlink()
    done = _action(ex, "codex-b", "budget")
    assert json.loads(done.stdout).get("known") is False


# ===========================================================================
# FA-R3 / FA-R3a — docker login runs the provider's login action
# ===========================================================================

def parse_docker_exec(argv):
    """(env flags, container, command) of `docker exec ...`."""
    assert argv[:2] == ["docker", "exec"], argv
    flags, i = {}, 2
    valued = {"--user", "-u", "--workdir", "-w", "--env", "-e", "--env-file"}
    while i < len(argv):
        arg = argv[i]
        if arg in ("--env", "-e"):
            key, _, value = argv[i + 1].partition("=")
            flags[key] = value
            i += 2
        elif arg in valued:
            i += 2
        elif arg.startswith("-"):
            i += 1
        else:
            break
    return flags, argv[i], argv[i + 1:]


@pytest.fixture
def login(world, monkeypatch):
    """`multiagents docker login <p>` with nothing executed: returns the
    recorded (flags, container, command) of the exec it would have made."""
    root = h3.make_git_repo(world.tmp / "proj")
    paths = ProjectPaths(root)
    paths.ensure()
    paths.config.mkdir(parents=True, exist_ok=True)
    (paths.config / "providers.yaml").write_text(yaml.safe_dump(
        {"providers": {"codex-b": NEW_SHAPE}}))
    (paths.config / "project.yaml").write_text(yaml.safe_dump(
        {"executor": {"kind": "docker", "docker": {"network": "bridge"}}}))
    monkeypatch.setattr(DockerExecutor, "ensure_running",
                        lambda self: {"ok": True, "container": self.container})
    execs = []

    def record(file, argv, env=None):
        execs.append((file, [str(a) for a in argv], env))
        raise SystemExit(0)

    monkeypatch.setattr(os, "execvp", record)
    monkeypatch.setattr(os, "execvpe", record)

    def run(provider: str):
        execs.clear()
        with pytest.raises(SystemExit):
            code = cli.cmd_docker(argparse.Namespace(
                action="login", provider=provider, path=str(root), all=False, force=False))
            raise SystemExit(code)       # a return (no exec) is a failure below
        assert execs, f"`docker login {provider}` never reached an exec"
        _, argv, _ = execs[-1]
        return parse_docker_exec(argv)

    run.root, run.paths = root, paths
    return run


def profile_of(flags: dict) -> str:
    return flags.get("CODEX_HOME") or flags.get("MULTIAGENTS_PRIVATE_HOME") or ""


@pytest.mark.parametrize("name, expected", [("codex", ".codex"), ("codex-b", ".codex-b")])
def test_fa_r3a_login_hands_the_native_cli_the_providers_own_profile(
        world, login, name, expected):
    flags, _, _ = login(name)

    assert profile_of(flags) == str(world.home / expected), flags
    sibling = ".codex-b" if expected == ".codex" else ".codex"
    assert str(world.home / sibling) not in flags.values()


@pytest.mark.parametrize("name", ["codex", "codex-b"])
def test_fa_r3a_login_runs_the_login_action_not_the_bare_binary(world, login, name):
    flags, container, command = login(name)

    assert command, "nothing to run in the container"
    assert command != [str(world.launcher)] and command != [str(world.native)]
    assert command[-1] == "login" or "--device-auth" in command, command


def test_fa_r3a_the_two_logins_run_in_one_container_with_different_profiles(world, login):
    a = login("codex")
    b = login("codex-b")

    assert a[1] == b[1]
    assert profile_of(a[0]) != profile_of(b[0])


def test_fa_r3a_login_keeps_the_path_and_terminal_of_today(world, login):
    flags, _, _ = login("codex-b")

    assert flags.get("PATH") and flags.get("TERM")


def test_fa_r3a_a_user_override_of_the_login_action_is_what_runs(world, login):
    override = login.paths.config / "providers" / "codex.py"
    override.parent.mkdir(parents=True, exist_ok=True)
    override.write_text("#!/bin/sh\necho custom-login\n")
    override.chmod(0o755)

    flags, _, command = login("codex-b")

    assert str(override) in command, command
    assert profile_of(flags) == str(world.home / ".codex-b")


def test_fa_r3a_a_provider_without_a_private_home_still_needs_no_login(
        world, login, capsys):
    # unchanged behaviour: no container_private_home -> nothing to log in to
    login.paths.config.joinpath("providers.yaml").write_text(yaml.safe_dump(
        {"providers": {"codex-b": {**NEW_SHAPE, "container_private_home": []}}}))
    code = cli.cmd_docker(argparse.Namespace(
        action="login", provider="codex-b", path=str(login.root), all=False, force=False))

    assert code == 0
    assert "no container_private_home" in capsys.readouterr().out


# ===========================================================================
# FA-R4 — doctor reports it
# ===========================================================================

@pytest.fixture
def doctor(world, monkeypatch, capsys):
    logdir = world.tmp / "fakedocker"
    logdir.mkdir()
    fake = logdir / "docker"
    fake.write_text(f"#!/bin/sh\necho \"$@\" >> {logdir / 'calls.log'}\nexit 1\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{logdir}:{os.environ['PATH']}")
    monkeypatch.setattr(cli.auth_mod, "check_all", lambda *a: {})
    monkeypatch.setattr(cli, "_driver_host_states", lambda *a: {})
    monkeypatch.setattr(cli, "read_all", lambda *a: {})
    monkeypatch.setattr(cli, "_report_agents", lambda *a: 0)
    monkeypatch.setattr(cli, "find_shadowing", lambda *a: [])
    monkeypatch.setattr(manifest, "cli_dependencies_section", lambda *a: 0)
    counter = {"n": 0}

    def run(extra_providers: dict):
        counter["n"] += 1
        root = h3.make_git_repo(world.tmp / f"doc{counter['n']}")
        paths = ProjectPaths(root)
        paths.ensure()
        paths.config.mkdir(parents=True, exist_ok=True)
        (paths.config / "providers.yaml").write_text(
            yaml.safe_dump({"providers": extra_providers}))
        (paths.config / "project.yaml").write_text(yaml.safe_dump(
            {"executor": {"kind": "docker", "docker": {"network": "bridge"}}}))
        capsys.readouterr()
        code = cli.cmd_doctor(argparse.Namespace(path=str(root), clear=None, force=False))
        out = capsys.readouterr().out
        match = re.search(r"^(\d+) problem\(s\)$", out, re.M)
        return code, int(match.group(1)) if match else 0, out

    run.docker_calls = lambda: (logdir / "calls.log").read_text().splitlines() \
        if (logdir / "calls.log").exists() else []
    return run


def test_fa_r4_doctor_reports_a_collision_as_a_problem(doctor):
    base_code, base_problems, _ = doctor({})
    code, problems, out = doctor({"codex-b": OLD_SHAPE})

    assert problems > base_problems, out
    assert code == 1
    assert named(out, "codex-b") and re.search(r"\.codex(?![\w-])", out), out


def test_fa_r4_doctor_has_no_collision_problem_for_the_documented_shape(doctor):
    base_code, base_problems, _ = doctor({})
    code, problems, out = doctor({"codex-b": NEW_SHAPE})

    assert problems == base_problems, out
    assert code == base_code


def test_fa_r4_doctor_launches_nothing_to_find_a_collision(doctor):
    doctor({"codex-b": OLD_SHAPE})

    started = [c for c in doctor.docker_calls()
               if c.split()[:1] and c.split()[0] in ("run", "create", "start", "exec", "build")]
    assert started == []


# ===========================================================================
# FA-R5 — no regression: current mounts and env, pinned
# ===========================================================================

def test_fa_r5_a_single_codex_keeps_its_mounts_and_env(world):
    ex = world.executor(shipped())

    assert ex.private_state("codex") == {world.home / ".codex": backing_root("codex") / ".codex"}
    mounts = dict(ex.mounts())
    assert mounts[world.home / ".codex"] is False
    assert mounts[world.releases] is True
    env = ex.adapter_env(["/x/codex.py"], {"K": "v"}, "codex")
    assert env == {"K": "v", "MULTIAGENTS_EXECUTOR": "docker",
                   "MULTIAGENTS_BIN": str(world.native),
                   "MULTIAGENTS_PRIVATE_HOME": str(world.home / ".codex")}


def test_fa_r5_claude_and_claude_b_share_one_mount_and_one_sidecar_owner(world):
    raw = shipped(**{"claude-b": {"extends": "claude", "container_account": "b"}})
    ex = world.executor(raw, auth_proxy=True, mount_cli_from_host=False)
    expected = {world.home / ".claude": backing_root("claude") / ".claude"}

    assert ex.private_state("claude") == ex.private_state("claude-b") == expected
    assert ex.container_credential_owner("claude-b") == "claude"
    assert [p for p, _ in ex.mounts()].count(world.home / ".claude") == 1
    assert dict(ex.mounts())[world.home / ".claude"] is False
    assert ex.account_pins() == {"claude-b": "b"}


def test_fa_r5_claude_b_run_env_is_signed_for_the_sidecar(world):
    raw = shipped(**{"claude-b": {"extends": "claude", "container_account": "b",
                                  "env": {"CLAUDE_CONFIG_DIR": "~/.multiagents/profiles/claude-b"}}})
    ex = world.executor(raw, auth_proxy=True, mount_cli_from_host=False)
    ex.project_placeholder()

    env = ex.adapter_env(["claude"], {"CLAUDE_CONFIG_DIR": "/host-only",
                                      "MULTIAGENTS_AGENT_ID": "a1"}, "claude-b")

    assert "CLAUDE_CONFIG_DIR" not in env
    assert env["ANTHROPIC_BASE_URL"].startswith("http://")
    assert env["ANTHROPIC_AUTH_TOKEN"]


AGY_B = {"extends": "agy", "family": "agy",
         "env": {"HOME": "~/.multiagents/profiles/agy-b"},
         "container_private_home": [".multiagents/profiles/agy-b/.gemini"]}


def test_fa_r5_agy_and_agy_b_keep_their_separate_mounts(world):
    ex = world.executor(shipped(**{"agy-b": AGY_B}))
    second = world.home / ".multiagents" / "profiles" / "agy-b" / ".gemini"

    assert ex.private_state("agy") == {world.home / ".gemini": backing_root("agy") / ".gemini"}
    assert ex.private_state("agy-b") == {
        second: backing_root("agy-b") / ".multiagents" / "profiles" / "agy-b" / ".gemini"}
    mounts = dict(ex.mounts())
    assert mounts[world.home / ".gemini"] is False and mounts[second] is False


def test_fa_r5_agy_runs_get_no_private_home_variable_and_an_unchanged_env(world):
    ex = world.executor(shipped(**{"agy-b": AGY_B}))

    for name in ("agy", "agy-b"):
        assert ex.adapter_env(["agy"], {"K": "v"}, name) == {"K": "v"}
