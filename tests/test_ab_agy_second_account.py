"""AB — a second agy account under the docker executor.

Contract: `context/specs/agy-second-account.md` (AB-R1..R5, R1b, R3b, R3c, R3d,
with the revision section overriding the earlier wording).

Black box, no real binaries. Two fakes stand in for the outside world:

* a fake `docker` on PATH that understands only `exec`. It runs the command in
  a pretend container: `FAKE_IN_CONTAINER=1`, and `HOME` is whatever `--env
  HOME=...` said, or the container's default home when nothing did (docker does
  not forward the client's environment);
* a fake `agy`. Outside a container it answers `/usage` from the "keyring" (a
  number that does not depend on `$HOME`, as on the real host). Inside, it
  reads the token file under `$HOME/.gemini` and the quota from a file next to
  it, so the reading depends on `$HOME` — the key fixture of this suite.

Three accounts therefore have three distinguishable headrooms:

    HOST_KEYRING   the host's keyring account         (never the right answer
                                                       under docker)
    PRIMARY        the container's own agy login      (agy, agy-partner)
    SECOND         the second profile's login         (agy-b)

Nothing here reads a real credential file: every profile is a tmp directory.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import c1_harness  # noqa: E402
import c2_harness as c2  # noqa: E402
import c3_harness as h3  # noqa: E402
import p0_context_harness as ch  # noqa: E402

from multiagents import budget as budget_mod  # noqa: E402
from multiagents import config as config_mod  # noqa: E402
from multiagents import scripts as scripts_mod  # noqa: E402
from multiagents.paths import ProjectPaths  # noqa: E402
from multiagents.providers import expand_env_value, load_providers  # noqa: E402

HOST_KEYRING = 0.11
PRIMARY = 0.57
SECOND = 0.83
OTHER = 0.31

SECOND_HOME_SPEC = "~/.multiagents/profiles/agy-b"
SECOND_PRIVATE = ".multiagents/profiles/agy-b/.gemini"
TOKEN_REL = ".gemini/antigravity-cli/antigravity-oauth-token"

SRC = Path(__file__).resolve().parents[1] / "src" / "multiagents"
SHIPPED_PROVIDERS = SRC / "defaults" / "providers.yaml"


# ---------------------------------------------------------------------------
# the fakes
# ---------------------------------------------------------------------------

FAKE_DOCKER = r'''#!{python}
import json, os, subprocess, sys

argv = sys.argv[1:]


def note(record):
    log = os.environ.get("FAKE_LOG")
    if log:
        with open(log, "a") as handle:
            handle.write(json.dumps(record) + "\n")


if not argv or argv[0] != "exec":
    note({{"docker_other": argv}})
    sys.stderr.write("fake docker: unsupported: %r\n" % (argv,))
    sys.exit(1)

rest = argv[1:]
flags = []
i = 0
valued = ("--env", "-e", "--user", "-u", "--workdir", "-w", "--env-file")
while i < len(rest):
    arg = rest[i]
    if arg in valued:
        if arg in ("--env", "-e"):
            flags.append(rest[i + 1])
        i += 2
        continue
    if arg.startswith("--env="):
        flags.append(arg.split("=", 1)[1])
        i += 1
        continue
    if arg.startswith("-"):
        i += 1
        continue
    break
container, cmd = rest[i], rest[i + 1:]

child = dict(os.environ)
child["HOME"] = os.environ["FAKE_CONTAINER_HOME"]
child["FAKE_IN_CONTAINER"] = "1"
for flag in flags:
    if "=" in flag:
        key, value = flag.split("=", 1)
        child[key] = value
    elif flag in os.environ:
        child[flag] = os.environ[flag]
note({{"docker_exec": {{"container": container, "env_flags": flags, "cmd": cmd,
                       "home": child["HOME"]}}}})
if os.environ.get("FAKE_DOCKER_DOWN"):
    sys.stderr.write("Error response from daemon: container is not running\n")
    sys.exit(125)
sys.exit(subprocess.call(cmd, env=child))
'''

FAKE_AGY = r'''#!{python}
import json, os, sys

args = sys.argv[1:]
home = os.environ.get("HOME", "")
inside = os.environ.get("FAKE_IN_CONTAINER") == "1"
token_path = os.path.join(home, ".gemini", "antigravity-cli", "antigravity-oauth-token")

log = os.environ.get("FAKE_LOG")
if log:
    with open(log, "a") as handle:
        handle.write(json.dumps({{"agy": args, "home": home, "inside": inside}}) + "\n")


def answer(fraction):
    # Both pools carry the account's one number, so a provider that reads the
    # Gemini pool (agy) and one that reads the third-party pool (agy-partner)
    # report the same account the same way.
    print(json.dumps({{"command": {{"data": {{"groups": [
        {{"name": "Gemini",
         "description": "Models within this group: Gemini Flash, Gemini Pro",
         "buckets": [{{"id": "gemini-weekly", "window": "weekly",
                      "remaining_fraction": fraction,
                      "reset_time": "2031-01-01T00:00:00Z"}}]}},
        {{"name": "Claude and GPT models",
         "description": "Models within this group: Claude, GPT",
         "buckets": [{{"id": "3p-5h", "window": "5h",
                      "remaining_fraction": fraction,
                      "reset_time": "2031-01-01T00:00:00Z"}}]}}]}}}}}}))


def write_atomically(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    scratch = path + ".tmp"
    with open(scratch, "w") as handle:
        handle.write(text)
    os.replace(scratch, path)


if "-p" in args and "/usage" in args:
    if not inside:
        # the host keyring: HOME does not move it
        answer(float(os.environ.get("FAKE_KEYRING_FRACTION", "{host}")))
        sys.exit(0)
    try:
        with open(token_path) as handle:
            token = handle.read().strip()
    except OSError:
        token = ""
    if token in ("", "revoked"):
        sys.stderr.write("authentication required: please log in\n")
        sys.exit(1)
    if token == "expired-refreshable":
        write_atomically(token_path, "refreshed")
    quota = os.path.join(home, ".gemini", "fake-quota")
    try:
        with open(quota) as handle:
            fraction = float(handle.read().strip())
    except OSError:
        fraction = 0.9
    answer(fraction)
    sys.exit(0)

if not args:                                  # interactive: the login flow
    if inside:
        write_atomically(token_path, "login-ok")
    sys.exit(0)
sys.exit(0)
'''


@dataclass
class World:
    root: Path
    home: Path                 # the test's HOME, also the container's default
    second_home: Path
    bindir: Path
    log: Path
    config_dir: Path
    providers: dict

    def records(self, key: str) -> list[dict]:
        if not self.log.is_file():
            return []
        found = []
        for line in self.log.read_text().splitlines():
            if line.strip():
                record = json.loads(line)
                if key in record:
                    found.append(record[key])
        return found

    @property
    def docker_execs(self) -> list[dict]:
        return self.records("docker_exec")

    def token(self, profile: Path) -> Path:
        return profile / TOKEN_REL

    def login(self, profile: Path, token: str = "valid", quota: float | None = None):
        path = self.token(profile)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(token)
        if quota is not None:
            (profile / ".gemini" / "fake-quota").write_text(str(quota))


def _provider_blocks(second_home: str = SECOND_HOME_SPEC) -> dict:
    raw = c2.raw_shipped_providers()
    raw["agy-b"] = {
        "extends": "agy",
        "family": "agy",
        "env": {"HOME": second_home},
        "container_private_home": [SECOND_PRIVATE],
    }
    return raw


@pytest.fixture
def world(tmp_path, monkeypatch) -> World:
    home = Path(os.environ["HOME"])           # conftest's per-test HOME
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    for name, body in (("docker", FAKE_DOCKER), ("agy", FAKE_AGY)):
        script = bindir / name
        script.write_text(body.format(python=sys.executable, host=HOST_KEYRING))
        script.chmod(0o755)
    log = tmp_path / "fake.log"
    monkeypatch.setenv("PATH", f"{bindir}:/usr/bin:/bin")
    monkeypatch.setenv("FAKE_LOG", str(log))
    monkeypatch.setenv("FAKE_CONTAINER_HOME", str(home))
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    second_home = home / ".multiagents" / "profiles" / "agy-b"
    w = World(root=tmp_path, home=home, second_home=second_home, bindir=bindir,
              log=log, config_dir=config_dir,
              providers=load_providers(_provider_blocks()))
    w.login(home, "primary-token", PRIMARY)
    return w


class PerProviderExecutor(c2.FakeExecutor):
    """A docker executor stand-in whose private homes, like the real one's, are
    per provider. The backing IS the container path: the fake docker has no
    mount table, so the identity mapping is what a bind mount looks like."""

    def __init__(self, world: World, kind: str = "docker"):
        super().__init__(kind=kind, container="fake-container")
        self.world = world

    def private_state(self, name: str = "") -> dict[str, str]:
        if name == "agy-b":
            path = str(self.world.second_home / ".gemini")
        else:
            path = str(self.world.home / ".gemini")
        return {path: path}


def read(world: World, name: str, *, kind: str = "docker", use_cache: bool = False,
         providers: dict | None = None):
    providers = providers or world.providers
    return budget_mod.read_provider(
        name, providers[name], PerProviderExecutor(world, kind), world.config_dir,
        providers=providers, use_cache=use_cache)


def headroom_is(budget, expected: float) -> bool:
    return budget.known and budget.headroom is not None \
        and abs(budget.headroom - expected) < 0.005


def shows_no_account_numbers(budget, *numbers: float) -> bool:
    text = json.dumps(budget.to_dict(), default=str)
    for number in numbers:
        if str(number) in text or str(round((1 - number) * 100, 1)) in text:
            return False
    return True


# ---------------------------------------------------------------------------
# AB-R1: docker login honours the provider's HOME
# ---------------------------------------------------------------------------

LOGIN_DRIVER = """
import argparse, sys
from multiagents import cli
from multiagents.executor import docker as docker_mod

# The container is not real: whether it is "up" is not what is under test.
docker_mod.DockerExecutor.ensure_running = lambda self: {
    "ok": True, "container": self.container}
raise SystemExit(cli.cmd_docker(argparse.Namespace(
    action="login", provider=sys.argv[1], path=sys.argv[2],
    all=False, force=False)))
"""


def _project(world: World, extra_providers: dict | None = None,
             project: dict | None = None) -> Path:
    root = h3.make_git_repo(world.root / "proj")
    paths = ProjectPaths(root)
    paths.ensure()
    paths.config.mkdir(parents=True, exist_ok=True)
    blocks = extra_providers if extra_providers is not None else {
        "agy-b": _provider_blocks()["agy-b"]}
    (paths.config / "providers.yaml").write_text(
        yaml.safe_dump({"providers": blocks}))
    (paths.config / "project.yaml").write_text(yaml.safe_dump(
        project or {"executor": {"kind": "docker", "docker": {"network": "bridge"}}}))
    return root


def docker_login(world: World, provider: str, root: Path | None = None):
    """`multiagents docker login <provider>` through the CLI code path, as a
    subprocess because the command replaces itself with `docker exec`."""
    root = root or _project(world)
    before = len(world.docker_execs)
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC.parent) + os.pathsep + env.get("PYTHONPATH", "")
    done = subprocess.run(
        [sys.executable, "-c", LOGIN_DRIVER, provider, str(root)],
        capture_output=True, text=True, env=env, timeout=60, cwd=str(root))
    execs = world.docker_execs[before:]
    return done, execs


def test_ab_r1_login_runs_with_the_providers_own_expanded_home(world):
    done, execs = docker_login(world, "agy-b")

    assert done.returncode == 0, done.stderr
    assert len(execs) == 1, (done.stdout, done.stderr)
    assert execs[0]["home"] == str(world.second_home)
    assert "~" not in execs[0]["home"]


def test_ab_r1_the_login_home_is_expanded_as_a_launch_expands_it(world, monkeypatch):
    # `providers.expand_env_value`: both `$VAR` and `~`.
    monkeypatch.setenv("AB_PROFILES", str(world.root / "profiles"))
    blocks = {"agy-b": {**_provider_blocks()["agy-b"],
                        "env": {"HOME": "$AB_PROFILES/agy-b"},
                        "container_private_home": [
                            os.path.relpath(world.root / "profiles" / "agy-b" / ".gemini",
                                            world.home)]}}
    root = _project(world, blocks)

    done, execs = docker_login(world, "agy-b", root)

    assert done.returncode == 0, done.stderr
    assert execs and execs[0]["home"] == expand_env_value("$AB_PROFILES/agy-b")
    assert execs[0]["home"] == str(world.root / "profiles" / "agy-b")


def test_ab_r1_a_provider_without_env_home_logs_in_as_today(world):
    done, execs = docker_login(world, "agy")

    assert done.returncode == 0, done.stderr
    assert len(execs) == 1
    assert execs[0]["home"] == str(world.home)
    flags = " ".join(execs[0]["env_flags"])
    assert "PATH=" in flags and "TERM=" in flags      # unchanged call shape


def test_ab_r1_the_base_login_is_not_affected_by_the_second_providers_home(world):
    docker_login(world, "agy-b")
    _, execs = docker_login(world, "agy")

    assert execs[0]["home"] == str(world.home)
    assert execs[0]["home"] != str(world.second_home)


# ---------------------------------------------------------------------------
# AB-R1b: one HOME for everything
# ---------------------------------------------------------------------------

def _launch_home(world: World, monkeypatch, tmp_path) -> str:
    """HOME as the launch action of a provider with this `env:` receives it,
    through the real driver launch path."""
    from multiagents import driver

    root = h3.make_git_repo((tmp_path / "launchproj").resolve())
    paths = ProjectPaths(root)
    paths.ensure()
    fake = ch.FakeProvider(paths.config, tmp_path)
    # The stock fake records only MULTIAGENTS_* variables; HOME is the point here.
    marker = 'if k.startswith("MULTIAGENTS_")}'
    text = fake.script.read_text()
    assert marker in text, "the shared fake provider changed shape"
    fake.script.write_text(text.replace(
        marker, 'if k.startswith("MULTIAGENTS_") or k == "HOME"}'))
    fake.control(turns=[{"exit": 0}])
    (paths.config / "providers.yaml").write_text(yaml.safe_dump({"providers": {
        "fakeprov": {"bin": "true", "script": fake.name,
                     "env": {"HOME": SECOND_HOME_SPEC},
                     "spawn": {"args": ["x"]}}}}))
    (paths.config / "agents.yaml").write_text(yaml.safe_dump({"agents": {
        "orchestrator": {"provider": "fakeprov", "model": "m", "launch": True,
                         "role": "orchestrator"}}}))
    monkeypatch.setenv("FAKE_CTL", str(fake.ctl))
    monkeypatch.setenv("FAKE_LOG", str(fake.log))
    monkeypatch.setenv("MULTIAGENTS_PROJECT", str(root))
    monkeypatch.chdir(root)
    config = config_mod.load(paths)
    driver._launch_agent(paths, config, "orchestrator", resume=False, unattended=1)
    launches = fake.calls("launch")
    assert launches, fake.calls()
    return launches[0]["env"]["HOME"]


def test_ab_r1b_login_auth_budget_and_launch_use_one_home(world, monkeypatch, tmp_path):
    provider = world.providers["agy-b"]
    world.login(world.second_home, "valid", SECOND)
    expected = expand_env_value(SECOND_HOME_SPEC)

    _, login_execs = docker_login(world, "agy-b")
    login_home = login_execs[0]["home"]

    auth_home = scripts_mod.build_env(
        "agy-b", provider, PerProviderExecutor(world))["HOME"]

    before = len(world.docker_execs)
    read(world, "agy-b")
    budget_execs = world.docker_execs[before:]
    assert budget_execs, "the budget must run inside the container"
    budget_home = budget_execs[-1]["home"]

    launch_home = _launch_home(world, monkeypatch, tmp_path)

    assert {login_home, auth_home, budget_home, launch_home} == {expected}


# ---------------------------------------------------------------------------
# AB-R2: the budget reads the account the provider's agents run on
# ---------------------------------------------------------------------------

def test_ab_r2_the_second_provider_reports_the_second_profiles_numbers(world):
    world.login(world.second_home, "valid", SECOND)

    budget = read(world, "agy-b")

    assert headroom_is(budget, SECOND), budget


def test_ab_r2_the_base_provider_reads_the_container_account_not_the_host_keyring(world):
    world.login(world.second_home, "valid", SECOND)

    budget = read(world, "agy")

    assert headroom_is(budget, PRIMARY), budget


def test_ab_r2_agy_partner_reads_the_primary_container_account(world):
    budget = read(world, "agy-partner")

    assert headroom_is(budget, PRIMARY), budget


def test_ab_r2_every_agy_family_read_runs_inside_the_container(world):
    world.login(world.second_home, "valid", SECOND)
    for name in ("agy", "agy-partner", "agy-b"):
        read(world, name)

    assert world.docker_execs, "no read went through docker exec"
    answered = [record for record in map(json.loads, world.log.read_text().splitlines())
                if "agy" in record]
    assert answered, "the fake agy was never run"
    # no /usage was answered by the host keyring
    usage = [entry for entry in answered if "/usage" in entry["agy"]]
    assert usage and all(entry["inside"] for entry in usage), usage


def test_ab_r2_a_failed_container_read_is_unknown_never_host_usage(world, monkeypatch):
    world.login(world.second_home, "valid", SECOND)
    monkeypatch.setenv("FAKE_DOCKER_DOWN", "1")

    for name in ("agy", "agy-partner", "agy-b"):
        budget = read(world, name)
        assert budget.known is False, (name, budget)
        assert shows_no_account_numbers(budget, HOST_KEYRING, PRIMARY, SECOND), \
            (name, budget)


def test_ab_r2_under_the_local_executor_the_budget_is_unchanged(world):
    world.login(world.second_home, "valid", SECOND)

    for name in ("agy", "agy-partner", "agy-b"):
        budget = read(world, name, kind="local")
        assert headroom_is(budget, HOST_KEYRING), (name, budget)

    assert world.docker_execs == [], "the local executor must not reach docker"


# ---------------------------------------------------------------------------
# AB-R3: never the wrong account's numbers
# ---------------------------------------------------------------------------

def test_ab_r3_an_empty_second_profile_is_unknown_with_a_note_and_no_numbers(world):
    assert not world.second_home.exists()

    budget = read(world, "agy-b")

    assert budget.known is False
    assert budget.headroom is None
    assert "log" in (budget.note or "").lower(), budget.note
    assert shows_no_account_numbers(budget, HOST_KEYRING, PRIMARY), budget


@pytest.mark.parametrize("token", ["", "revoked"])
def test_ab_r3_a_present_but_unusable_token_is_unknown(world, token):
    # R3b: "token file present" is not "valid".
    world.login(world.second_home, token, SECOND)

    budget = read(world, "agy-b")

    assert budget.known is False
    assert budget.headroom is None
    assert shows_no_account_numbers(budget, HOST_KEYRING, PRIMARY), budget


def test_ab_r3_both_providers_read_in_turn_keep_their_own_values(world):
    world.login(world.second_home, "valid", SECOND)

    first = read(world, "agy")
    second = read(world, "agy-b")
    first_again = read(world, "agy")
    second_again = read(world, "agy-b")

    assert headroom_is(first, PRIMARY) and headroom_is(first_again, PRIMARY)
    assert headroom_is(second, SECOND) and headroom_is(second_again, SECOND)


def test_ab_r3_the_cache_does_not_collide_between_the_two_providers(world):
    world.login(world.second_home, "valid", SECOND)

    # Cached reads, inside one TTL, in both orders.
    a1 = read(world, "agy", use_cache=True)
    b1 = read(world, "agy-b", use_cache=True)
    a2 = read(world, "agy", use_cache=True)
    b2 = read(world, "agy-b", use_cache=True)

    assert headroom_is(a1, PRIMARY) and headroom_is(a2, PRIMARY)
    assert headroom_is(b1, SECOND) and headroom_is(b2, SECOND)


def test_ab_r3_an_unknown_second_profile_does_not_poison_a_cached_primary(world):
    first = read(world, "agy", use_cache=True)
    unknown = read(world, "agy-b", use_cache=True)
    again = read(world, "agy", use_cache=True)

    assert headroom_is(first, PRIMARY) and headroom_is(again, PRIMARY)
    assert unknown.known is False


# ---------------------------------------------------------------------------
# AB-R3b: token persistence
# ---------------------------------------------------------------------------

def test_ab_r3b_login_writes_only_into_the_providers_own_profile(world):
    primary_token = world.token(world.home)
    before = (primary_token.read_text(), primary_token.stat().st_mtime_ns)
    assert not world.token(world.second_home).exists()

    done, _ = docker_login(world, "agy-b")

    assert done.returncode == 0, done.stderr
    assert world.token(world.second_home).is_file(), "login wrote nowhere under agy-b's HOME"
    assert world.token(world.second_home).read_text().strip() != ""
    assert (primary_token.read_text(), primary_token.stat().st_mtime_ns) == before


def test_ab_r3b_a_login_through_the_cli_makes_the_second_budget_known(world):
    docker_login(world, "agy-b")
    (world.second_home / ".gemini").mkdir(parents=True, exist_ok=True)
    (world.second_home / ".gemini" / "fake-quota").write_text(str(SECOND))

    assert headroom_is(read(world, "agy-b"), SECOND)


def test_ab_r3b_an_expired_but_refreshable_token_is_not_unknown(world):
    world.login(world.second_home, "expired-refreshable", SECOND)

    budget = read(world, "agy-b")

    assert headroom_is(budget, SECOND), budget


def test_ab_r3b_a_refresh_replaces_the_token_in_the_second_profile_only(world):
    world.login(world.second_home, "expired-refreshable", SECOND)
    primary_token = world.token(world.home)
    before = (primary_token.read_text(), primary_token.stat().st_mtime_ns)

    read(world, "agy-b")

    assert world.token(world.second_home).read_text().strip() == "refreshed"
    assert (primary_token.read_text(), primary_token.stat().st_mtime_ns) == before


def test_ab_r3b_refreshing_the_primary_leaves_the_second_profile_alone(world):
    world.login(world.home, "expired-refreshable", PRIMARY)
    world.login(world.second_home, "second-token", SECOND)
    second_token = world.token(world.second_home)
    before = (second_token.read_text(), second_token.stat().st_mtime_ns)

    budget = read(world, "agy")

    assert headroom_is(budget, PRIMARY)
    assert world.token(world.home).read_text().strip() == "refreshed"
    assert (second_token.read_text(), second_token.stat().st_mtime_ns) == before


def test_ab_r3b_each_profile_has_its_own_persistent_backing_and_mount(tmp_path):
    from multiagents.executor.docker import DockerExecutor
    providers = load_providers(_provider_blocks())
    ex = c1_harness.make_docker_executor(tmp_path, providers)
    assert isinstance(ex, DockerExecutor)

    primary = ex.private_state("agy")
    second = ex.private_state("agy-b")

    assert primary and second
    (primary_dest, primary_backing), = primary.items()
    (second_dest, second_backing), = second.items()
    assert primary_dest != second_dest and primary_backing != second_backing
    # The backing is on the host, outside the container's own filesystem layer,
    # so it survives `docker rm`; and neither is nested in the other.
    assert second_dest == Path.home() / SECOND_PRIVATE
    for a, b in ((primary_backing, second_backing), (second_backing, primary_backing)):
        assert a not in b.parents
    mounted = {Path(path) for path, _ro in ex.mounts()}
    assert second_dest in mounted and primary_dest in mounted


# ---------------------------------------------------------------------------
# AB-R3c: honest identity in doctor
# ---------------------------------------------------------------------------

def _doctor(world: World, capsys, *, kind: str) -> str:
    import multiagents.cli as cli
    project = {"executor": {"kind": kind, "docker": {"network": "bridge"}}} \
        if kind == "docker" else {"executor": {"kind": "local"}}
    root = _project(world, project=project)
    capsys.readouterr()
    cli.cmd_doctor(argparse.Namespace(path=str(root), clear=None, force=False))
    return capsys.readouterr().out


@pytest.mark.parametrize("kind", ["local", "docker"])
def test_ab_r3c_doctor_shows_the_profile_path_of_the_second_provider(
        world, capsys, kind):
    world.login(world.second_home, "valid", SECOND)

    out = _doctor(world, capsys, kind=kind)

    assert str(world.second_home) in out, out


@pytest.mark.parametrize("kind", ["local", "docker"])
def test_ab_r3c_a_token_file_never_makes_the_identity_confirmed(world, capsys, kind):
    world.login(world.second_home, "valid", SECOND)

    out = _doctor(world, capsys, kind=kind)

    lowered = out.lower()
    assert "identity unverified" in lowered, out
    assert "confirmed" not in lowered
    assert "verified account" not in lowered.replace("unverified account", "")


# ---------------------------------------------------------------------------
# AB-R4: under the local executor nothing pretends
# ---------------------------------------------------------------------------

def _load(world: World, executor: dict | None, extra: dict | None = None):
    blocks = {"agy-b": _provider_blocks()["agy-b"], **(extra or {})}
    root = _project(world, blocks, project={"executor": executor} if executor else {})
    return config_mod.load(ProjectPaths(root))


def _mentions(config, name: str) -> list[str]:
    return [w for w in config.warnings if re.search(rf"\b{re.escape(name)}\b", w)]


def test_ab_r4_local_executor_warns_that_the_provider_uses_the_primary_account(world):
    config = _load(world, {"kind": "local"})

    found = _mentions(config, "agy-b")

    assert found, config.warnings
    text = " ".join(found).lower()
    assert "primary" in text and "local" in text, found


def test_ab_r4_docker_executor_does_not_warn(world):
    config = _load(world, {"kind": "docker", "docker": {"network": "bridge"}})

    assert _mentions(config, "agy-b") == [], config.warnings


def test_ab_r4_launching_is_not_refused_under_local(world):
    # The warning is advice: the config still loads and the provider survives.
    config = _load(world, {"kind": "local"})
    providers = load_providers(config.providers)

    assert "agy-b" in providers


def test_ab_r4_a_provider_that_does_not_relocate_home_does_not_warn(world):
    config = _load(world, {"kind": "local"}, extra={
        "agy-c": {"extends": "agy", "family": "agy"}})

    assert _mentions(config, "agy-c") == []
    assert _mentions(config, "agy-partner") == []
    assert _mentions(config, "agy") == []


def test_ab_r4_the_warning_follows_extends_through_more_than_one_hop(world):
    config = _load(world, {"kind": "local"}, extra={
        "agy-c": {"extends": "agy-b", "family": "agy",
                  "env": {"HOME": "~/.multiagents/profiles/agy-c"},
                  "container_private_home": [".multiagents/profiles/agy-c/.gemini"]}})

    assert _mentions(config, "agy-c"), config.warnings


def test_ab_r4_an_unrelated_provider_with_its_own_home_is_not_warned_about(world):
    # Metadata, not "HOME is set": the limitation belongs to the provider that
    # declares it, and is inherited only through `extends`.
    config = _load(world, {"kind": "local"}, extra={
        "other-b": {"extends": "opencode", "family": "opencode",
                    "env": {"HOME": "~/.multiagents/profiles/other-b"}}})

    assert _mentions(config, "other-b") == [], config.warnings


def test_ab_r4_doctor_shows_the_warning_under_local(world, capsys):
    out = _doctor(world, capsys, kind="local")

    lines = [line for line in out.splitlines() if "agy-b" in line]
    assert any("primary" in line.lower() for line in lines), out


def test_ab_r4_doctor_does_not_show_the_warning_under_docker(world, capsys):
    out = _doctor(world, capsys, kind="docker")

    assert not any("primary" in line.lower() and "agy-b" in line for line in out.splitlines()), out


def test_ab_r4_the_limitation_is_declared_as_metadata_in_the_shipped_defaults():
    # Which key it is called is the implementer's choice; that the agy block
    # carries a declaration beyond what it had before this contract is not.
    before = {"agent_guidance", "auth", "billing", "bin", "budget_windows",
              "container_private_home", "effort_suffixes", "home_links", "mcp",
              "models_cmd", "models_include", "models_parse", "notes",
              "opaque_tool_args", "opaque_tools", "refusal_markers", "spawn",
              "stream", "truncation_markers", "usage_mode"}
    shipped = yaml.safe_load(SHIPPED_PROVIDERS.read_text())["providers"]

    assert set(shipped["agy"]) - before, \
        "the agy block declares nothing new for the core to read"


def _agy_mentions(path: Path) -> int:
    """Occurrences of the provider name in code and strings, not in comments or
    docstrings, which may explain without hard-coding."""
    tree = ast.parse(path.read_text())
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) \
                    and isinstance(first.value.value, str):
                docstrings.add(id(first.value))
    count = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and id(node) not in docstrings:
            count += len(re.findall(r"\bagy\b", node.value))
        elif isinstance(node, ast.Name) and re.search(r"\bagy\b|agy_", node.id):
            count += 1
    return count


def test_ab_r4_no_new_provider_names_in_the_package_source():
    # P0-R8: providers are plugins. The second account's name appears nowhere,
    # and the existing mentions of the first (measured before this contract:
    # auth.py 1, budget.py 2, cli.py 2) do not grow.
    baseline = {"auth.py": 1, "budget.py": 2, "cli.py": 2}
    offenders = []
    for path in sorted(SRC.glob("*.py")):
        source = path.read_text()
        if re.search(r"\bagy[-_]b\b|agy-partner", source):
            offenders.append(f"{path.name}: second-account name")
        if _agy_mentions(path) > baseline.get(path.name, 0):
            offenders.append(f"{path.name}: more `agy` than before")
    assert not offenders, offenders


# ---------------------------------------------------------------------------
# AB-R3d: cache identity includes the resolved HOME
# ---------------------------------------------------------------------------

def test_ab_r3d_a_changed_home_within_the_ttl_is_a_different_cache_identity(world):
    world.login(world.second_home, "valid", SECOND)
    other_home = world.home / ".multiagents" / "profiles" / "agy-other"
    world.login(other_home, "valid", OTHER)

    first = read(world, "agy-b", use_cache=True)
    relocated = load_providers(_provider_blocks(str(other_home)))
    second = read(world, "agy-b", use_cache=True, providers=relocated)

    assert headroom_is(first, SECOND)
    assert headroom_is(second, OTHER), "the cache served the old profile's reading"


def test_ab_r3d_two_spellings_of_one_home_are_one_identity(world):
    world.login(world.second_home, "valid", SECOND)

    read(world, "agy-b", use_cache=True)
    first_calls = len(world.docker_execs)
    absolute = load_providers(_provider_blocks(str(world.second_home)))
    again = read(world, "agy-b", use_cache=True, providers=absolute)

    assert headroom_is(again, SECOND)
    assert len(world.docker_execs) == first_calls, "an equal HOME re-read the profile"


# ---------------------------------------------------------------------------
# AB-R5: no regression
# ---------------------------------------------------------------------------

def test_ab_r5_build_env_leaves_home_alone_for_providers_that_do_not_set_it(world):
    for name in ("agy", "agy-partner", "claude", "codex", "opencode"):
        env = scripts_mod.build_env(name, world.providers[name], PerProviderExecutor(world))
        assert env["HOME"] == os.environ["HOME"], name


def test_ab_r5_claude_b_style_instances_keep_their_own_profile_variable(world):
    raw = c2.raw_shipped_providers()
    raw["claude-b"] = {"extends": "claude", "family": "claude",
                       "env": {"CLAUDE_CONFIG_DIR": "~/.multiagents/profiles/claude-b"}}
    providers = load_providers(raw)

    env = scripts_mod.build_env("claude-b", providers["claude-b"],
                                PerProviderExecutor(world))

    assert env["CLAUDE_CONFIG_DIR"] == str(Path.home() / ".multiagents/profiles/claude-b")
    assert env["HOME"] == os.environ["HOME"]


def test_ab_r5_the_second_provider_does_not_change_the_base_providers_env(world):
    env = scripts_mod.build_env("agy", world.providers["agy"], PerProviderExecutor(world))
    second = scripts_mod.build_env("agy-b", world.providers["agy-b"],
                                   PerProviderExecutor(world))

    assert env["HOME"] == str(world.home)
    assert second["HOME"] == str(world.second_home)
    assert second["MULTIAGENTS_PRIVATE_BACKING"] != env["MULTIAGENTS_PRIVATE_BACKING"]


def test_ab_r5_a_provider_without_a_private_home_still_needs_no_login(world):
    done, execs = docker_login(world, "opencode")

    assert done.returncode == 0
    assert execs == []
    assert "no container_private_home" in done.stdout


def test_ab_r5_login_for_an_unknown_provider_is_still_refused(world):
    done, execs = docker_login(world, "no-such-provider")

    assert done.returncode == 2
    assert execs == []
    assert "unknown provider" in done.stderr
