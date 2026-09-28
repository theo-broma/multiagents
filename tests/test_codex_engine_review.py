"""Codex provider contract, engine review of be92256 — CX-C16..C20.

`context/specs/codex-provider.md`, section "Engine review of be92256":

- CX-C16  the executor learns WHICH provider a run belongs to from the runner,
          never by guessing from argv[0]'s file name. An `extends:` instance
          gets its own MULTIAGENTS_BIN and private home; a provider without
          `adapter:` whose `bin` shares a file name with some adapter gets no
          adapter variables; an argv[0] match that fits two providers is never
          silently resolved to one of them.
- CX-C17  `bin_versions_depth` cannot widen the mount: a root that is `/`, the
          home, an ancestor of either, a system prefix, or deeper than the
          path allows is refused as a config error naming the key, and not
          mounted.
- CX-C18  `mount_cli_from_host: false`: MULTIAGENTS_BIN is the bare `bin`,
          no versions root is mounted, no CX-C3 refusal runs.
- CX-C19  the stale-root refusal says "this container lacks the versions root
          <X>" and gives the recreate command — for a moved target and for a
          key added after the container was created.
- CX-C20  `enabled: false` providers add neither a versions root nor an
          adapter mount.

Black box. CX-C16 is driven through `Runner.start`, because the spec leaves
open HOW the runner names the provider to the executor (a parameter or an env
entry); only what the agent process finally sees is asserted. The rest drives
`DockerExecutor.mounts` / `start` / `env_file` against a fake docker CLI, as
`test_codex_engine_executor.py` does.

The providers are called `acme` and `acme-2`: nothing in core may know a name.

Stubs, stated once: `DockerExecutor.inside` is pinned (this suite may itself
run in a container), and for tests about the env of one spawn
`ensure_running` is replaced by "the container is up". The refusal tests do
NOT stub `ensure_running`: they drive a fake daemon, so the check may live
anywhere in the start path.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import multiagents.executor.docker as docker_mod
from multiagents.executor.docker import DockerExecutor
from multiagents.paths import ProjectPaths, global_config_dir
from multiagents.providers import Provider

sys.path.insert(0, str(Path(__file__).parent / "support"))
import c3_harness as h3  # noqa: E402

NAME = "acme"
NAME2 = "acme-2"
ADAPTER = "acme-adapter.py"
SPAWN = {"args": ["run", "--model", "{model}", "--", "{prompt}"]}
ARGS = ["run", "--model", "m1", "--", "do the task"]
STREAM = {"format": "ndjson",
          "rules": [{"match": {"type": "result"}, "as": "result",
                     "fields": {"status": "subtype", "text": "result"}}]}
ADAPTER_VARS = ("MULTIAGENTS_BIN", "MULTIAGENTS_EXECUTOR", "MULTIAGENTS_PRIVATE_HOME")
RECREATE = "multiagents docker rm && multiagents docker up"


# ---------------------------------------------------------------------------
# helpers (same shapes as test_codex_engine_executor.py)
# ---------------------------------------------------------------------------

def _executable(path: Path, body: str = "#!/bin/sh\necho fake\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(0o755)
    return path


def _point(link: Path, target: Path) -> None:
    tmp = link.with_name(link.name + ".tmp-link")
    if tmp.is_symlink() or tmp.exists():
        tmp.unlink()
    link.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(target, tmp)
    os.replace(tmp, link)


def _recorder_body(record: Path) -> str:
    """An executable that records its argv and MULTIAGENTS_* env, then emits
    one result event on STREAM."""
    return (
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        f"with open({str(record)!r}, 'a') as f:\n"
        "    f.write(json.dumps({'argv': sys.argv[1:], 'env': {k: v for k, v in os.environ.items()\n"
        "        if k.startswith('MULTIAGENTS_')}}) + '\\n')\n"
        "print(json.dumps({'type': 'result', 'subtype': 'success', 'result': 'ran'}))\n"
    )


def _records(record: Path) -> list[dict]:
    if not record.exists():
        return []
    return [json.loads(line) for line in record.read_text().splitlines() if line.strip()]


@pytest.fixture
def host(tmp_path_factory, monkeypatch):
    """A host home outside any project, a bin dir on PATH, and a fake docker."""
    root = tmp_path_factory.mktemp("host")
    bin_dir = root / ".local" / "bin"
    bin_dir.mkdir(parents=True)
    dockerbin = tmp_path_factory.mktemp("dockerbin")
    _executable(dockerbin / "docker", "#!/bin/sh\nexit 0\n")
    monkeypatch.setenv("HOME", str(root))
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{dockerbin}{os.pathsep}/usr/bin{os.pathsep}/bin")
    return SimpleNamespace(root=root, bin=bin_dir, launcher=bin_dir / NAME)


def _docker(tmp_path: Path, providers: dict, **config) -> DockerExecutor:
    config.setdefault("image", "img")
    config.setdefault("network", "bridge")
    ex = DockerExecutor(config, ProjectPaths(tmp_path), providers, global_config_dir())
    ex.inside = lambda: False                     # see module docstring
    return ex


def _capture_exec(monkeypatch) -> SimpleNamespace:
    captured = SimpleNamespace(command=None, env=None)

    async def fake_exec(*command, **kw):
        captured.command = [str(c) for c in command]
        captured.env = kw.get("env")
        return SimpleNamespace(pid=4242, stdout=None, stderr=None, returncode=None)

    monkeypatch.setattr(docker_mod.asyncio, "create_subprocess_exec", fake_exec)
    return captured


def _read_env(path: Path) -> dict:
    env = {}
    if path.exists():
        for line in path.read_text().splitlines():
            key, _, value = line.partition("=")
            env[key] = value
    return env


def _start_up(ex, argv, cwd, monkeypatch, agent_id="ag-rv0001"):
    """One host-side docker spawn with the container taken as up."""
    captured = _capture_exec(monkeypatch)
    ex.ensure_running = lambda: {"ok": True, "container": ex.container, "existed": True}
    asyncio.run(ex.start(list(argv), cwd, {"MULTIAGENTS_AGENT_ID": agent_id,
                                           "PATH": os.environ["PATH"]}))
    return captured, _read_env(ex.env_file(agent_id))


class FakeDocker:
    """A daemon holding one running container created from `created_mounts`."""

    def __init__(self, ex, created_mounts):
        private = ex.private_state()
        self.mounts = [(str(p), str(private.get(p, p)), not ro) for p, ro in created_mounts]

    def run(self, argv, *a, **kw):
        argv = [str(x) for x in argv]
        out = ""
        fmt = " ".join(argv)
        if argv[:2] == ["docker", "inspect"]:
            if ".State.Status" in fmt:
                out = "running\n"
            elif ".Destination}}:{{.RW" in fmt:
                out = "".join(f"{d}:{'true' if rw else 'false'}\n" for d, _, rw in self.mounts)
            elif ".Source}}>{{.Destination" in fmt:
                out = "".join(f"{s}>{d}\n" for d, s, _ in self.mounts)
            elif ".State.StartedAt" in fmt:
                out = "2026-09-28T10:00:00.000000000Z\n"
        return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")


@pytest.fixture
def fake_docker(monkeypatch):
    real_run = subprocess.run

    def install(ex, created_mounts):
        fake = FakeDocker(ex, created_mounts)

        def run(argv, *a, **kw):
            if argv and str(argv[0]) == "docker":
                return fake.run(argv, *a, **kw)
            return real_run(argv, *a, **kw)

        monkeypatch.setattr(docker_mod.subprocess, "run", run)
        return fake

    return install


def _refusal(ex, adapter: Path, cwd: Path, monkeypatch, agent_id="ag-rv0009") -> tuple[str, list | None]:
    """Start an adapter run for real (no `ensure_running` stub). Returns the
    refusal message ("" if it started) and the command exec'd, if any."""
    captured = _capture_exec(monkeypatch)
    try:
        asyncio.run(ex.start([str(adapter), *ARGS], cwd, {"MULTIAGENTS_AGENT_ID": agent_id}))
    except Exception as exc:                                  # noqa: BLE001
        return str(exc), captured.command
    return "", captured.command


# ===========================================================================
# CX-C16 — the provider is named, not guessed
# ===========================================================================

def _two_instance_config(tmp_path: Path, host, *, parent_first: bool):
    """`acme` and `acme-2` (extends acme, own bin and private home), sharing
    one adapter file in the project's providers dir.

    `acme-2` is declared its own `family`. Without that, the two are one
    family and the router chooses the instance (ties broken by name), so an
    agent pinned to `acme-2` legitimately runs on `acme` and CX-C16 would be
    asserted about an agent that never ran on `acme-2`. The shared adapter —
    the thing an argv[0] guess would trip over — is unaffected."""
    record = tmp_path.parent / (tmp_path.name + "-adapter-record.jsonl")
    _executable(tmp_path / ".multiagents" / "config" / "providers" / ADAPTER,
                _recorder_body(record))
    bins = {NAME: _executable(host.bin / NAME, "#!/bin/sh\necho acme\n"),
            NAME2: _executable(host.bin / NAME2, "#!/bin/sh\necho acme-2\n")}
    parent = {"bin": NAME, "adapter": ADAPTER, "spawn": SPAWN, "stream": STREAM,
              "container_private_home": [".acme"]}
    child = {"extends": NAME, "family": NAME2, "bin": NAME2,
             "container_private_home": [".acme-2"]}
    blocks = [(NAME, parent), (NAME2, child)]
    providers = dict(blocks if parent_first else reversed(blocks))
    return providers, bins, record


def _run_agent(r):
    async def go():
        result = await r.start("worker", "do the task")
        run = r.runs.get(result.get("agent_id", ""))
        if run is not None:
            await asyncio.wait_for(run.done.wait(), timeout=20)
        return result
    return asyncio.run(go())


@pytest.mark.parametrize("parent_first", [True, False], ids=["parent-first", "child-first"])
@pytest.mark.parametrize("which", [NAME, NAME2])
def test_cx_c16_extends_instance_gets_its_own_bin_local(
        tmp_path, monkeypatch, host, which, parent_first):
    providers, bins, record = _two_instance_config(tmp_path, host, parent_first=parent_first)
    spec = h3.AgentSpec(name="worker", provider=which, model="m1")
    r = h3.make_runner(tmp_path, monkeypatch, agents={"worker": spec}, providers=providers)
    result = _run_agent(r)

    runs = _records(record)
    assert runs, f"the shared adapter never ran for {which}; start() returned {result}"
    got = runs[-1]["env"].get("MULTIAGENTS_BIN", "")
    assert got and os.path.samefile(got, bins[which]), (
        f"an agent on {which!r} must get {which}'s own `bin` ({bins[which]}), whatever "
        f"the order of providers sharing its adapter; MULTIAGENTS_BIN={got!r}")


class _Stop(Exception):
    """Raised after a docker spawn wrote its env file, to end the run there."""


@pytest.mark.parametrize("parent_first", [True, False], ids=["parent-first", "child-first"])
@pytest.mark.parametrize("which", [NAME, NAME2])
def test_cx_c16_extends_instance_gets_its_own_bin_and_private_home_docker(
        tmp_path, monkeypatch, host, which, parent_first):
    providers, bins, _ = _two_instance_config(tmp_path, host, parent_first=parent_first)
    spec = h3.AgentSpec(name="worker", provider=which, model="m1")
    project = {"executor": {"kind": "docker",
                            "docker": {"image": "img", "network": "bridge"}}}
    r = h3.make_runner(tmp_path, monkeypatch, agents={"worker": spec},
                       providers=providers, project=project)

    # The container is up, and the spawn ends right after `start` has written
    # the agent's environment: what reaches the agent is all this test reads.
    monkeypatch.setattr(DockerExecutor, "inside", lambda self: False)
    monkeypatch.setattr(DockerExecutor, "ensure_running",
                        lambda self: {"ok": True, "container": self.container, "existed": True})
    real_start = DockerExecutor.start

    async def start_then_stop(self, argv, cwd, env, **kw):
        kw.pop("run_dir", None)
        kw.pop("deadline", None)
        _capture_exec(monkeypatch)
        await real_start(self, argv, cwd, env, **kw)
        raise _Stop()

    monkeypatch.setattr(DockerExecutor, "start", start_then_stop)
    try:
        asyncio.run(r.start("worker", "do the task"))
    except _Stop:
        pass
    except Exception as exc:                                   # noqa: BLE001
        if not isinstance(exc.__cause__ or exc.__context__, _Stop) and "_Stop" not in repr(exc):
            raise

    env_files = sorted((r.paths.data / "env").glob("*.env"))
    assert env_files, "no docker spawn wrote an agent environment"
    env = _read_env(env_files[-1])
    got = env.get("MULTIAGENTS_BIN", "")
    assert got and os.path.samefile(got, bins[which]), (
        f"a docker agent on {which!r} must get {which}'s own `bin`; env={env}")
    assert env.get("MULTIAGENTS_PRIVATE_HOME") == str(host.root / f".{which}"), (
        f"a docker agent on {which!r} must get {which}'s own private home "
        f"~/.{which}, not its sibling's; env={env}")


def test_cx_c16_bin_colliding_with_an_adapter_name_gets_no_adapter_vars(
        tmp_path, monkeypatch, host):
    # `acme` has an adapter; `plain` has none, but its `bin` is an executable
    # whose FILE NAME is acme's adapter name, at some absolute path.
    _executable(tmp_path / ".multiagents" / "config" / "providers" / ADAPTER,
                "#!/bin/sh\necho the-real-adapter\n")
    _executable(host.launcher)
    record = tmp_path.parent / (tmp_path.name + "-plain-record.jsonl")
    plain_bin = _executable(host.root / "tools" / ADAPTER, _recorder_body(record))
    providers = {
        NAME: {"bin": NAME, "adapter": ADAPTER, "spawn": SPAWN, "stream": STREAM},
        "plain": {"bin": str(plain_bin), "spawn": SPAWN, "stream": STREAM},
    }
    spec = h3.AgentSpec(name="worker", provider="plain", model="m1")
    r = h3.make_runner(tmp_path, monkeypatch, agents={"worker": spec}, providers=providers)
    result = _run_agent(r)

    runs = _records(record)
    assert runs, f"`plain`'s bin never ran; start() returned {result}"
    leaked = {k: v for k, v in runs[-1]["env"].items() if k in ADAPTER_VARS}
    assert leaked == {}, (
        f"a provider without `adapter:` gets no adapter variables, even when its "
        f"`bin` shares a file name with another provider's adapter: {leaked}")


def test_cx_c16_ambiguous_argv0_fallback_never_picks_one(tmp_path, host, monkeypatch):
    # Called with no provider named, the executor may only fall back to the
    # argv[0] match; here that match fits both providers. Either it refuses the
    # run, or it starts it without claiming to know whose `bin` it drives.
    adapter = _executable(global_config_dir() / "providers" / ADAPTER, "#!/bin/sh\n")
    _executable(host.bin / NAME)
    _executable(host.bin / NAME2)
    providers = {
        NAME: Provider.from_dict(NAME, {"bin": NAME, "adapter": ADAPTER, "spawn": SPAWN,
                                        "container_private_home": [".acme"]}),
        NAME2: Provider.from_dict(NAME2, {"bin": NAME2, "adapter": ADAPTER, "spawn": SPAWN,
                                          "container_private_home": [".acme-2"]}),
    }
    ex = _docker(tmp_path, providers)
    captured = _capture_exec(monkeypatch)
    ex.ensure_running = lambda: {"ok": True, "container": ex.container, "existed": True}
    try:
        asyncio.run(ex.start([str(adapter), *ARGS], tmp_path,
                             {"MULTIAGENTS_AGENT_ID": "ag-rv0002", "PATH": os.environ["PATH"]}))
    except Exception:                                          # noqa: BLE001
        assert captured.command is None, (
            f"a refused ambiguous run must exec nothing: {captured.command}")
        return
    env = _read_env(ex.env_file("ag-rv0002"))
    guessed = {k: env[k] for k in ("MULTIAGENTS_BIN", "MULTIAGENTS_PRIVATE_HOME") if k in env}
    assert guessed == {}, (
        f"argv[0] {adapter.name} is the adapter of both {NAME} and {NAME2}: the "
        f"executor must not pick one of them; it gave {guessed}")


# ===========================================================================
# CX-C17 — `bin_versions_depth` cannot widen the mount
# ===========================================================================

@pytest.fixture
def deep(host):
    """A codex-like layout, one level deeper than CX-C3's `nested`:
    `~/.acme/packages/standalone/releases/<version>-<triple>/bin/acme`.
    parents[N-1]: N=3 releases, N=6 ~/.acme, N=7 the home, N=8 above it."""
    root = host.root / ".acme" / "packages" / "standalone" / "releases"
    target = _executable(root / "1.0.0-x86_64" / "bin" / NAME)
    _point(host.launcher, target)
    return SimpleNamespace(host=host.root, root=root, target=target, launcher=host.launcher)


def _depth_provider(depth: int, **extra) -> Provider:
    return Provider.from_dict(NAME, {"bin": NAME, "adapter": ADAPTER, "spawn": SPAWN,
                                     "bin_versions_depth": depth, **extra})


def _home_cases(deep) -> dict[str, tuple[int, Path | None]]:
    target = deep.target
    return {
        "home": (7, deep.host),
        "home-ancestor": (8, deep.host.parent),
        "root": (len(target.parents), Path("/")),
        "too-deep": (len(target.parents) + 5, None),
    }


@pytest.mark.parametrize("case", ["home", "home-ancestor", "root", "too-deep"])
def test_cx_c17_widening_root_is_not_mounted(tmp_path, deep, case):
    depth, widened = _home_cases(deep)[case]
    ex = _docker(tmp_path, {NAME: _depth_provider(depth)})
    mounts = dict(ex.mounts())
    for forbidden in {deep.host, *deep.host.parents}:
        assert forbidden not in mounts, (
            f"bin_versions_depth: {depth} ({case}) must mount nothing that "
            f"contains the home: {forbidden} is mounted; mounts={sorted(mounts)}")
    if widened is not None:
        assert widened not in mounts


@pytest.mark.parametrize("case", ["home", "home-ancestor", "root", "too-deep"])
def test_cx_c17_widening_root_is_refused_naming_the_key(
        tmp_path, deep, fake_docker, monkeypatch, case):
    depth, _ = _home_cases(deep)[case]
    adapter = _executable(global_config_dir() / "providers" / ADAPTER, "#!/bin/sh\n")
    ex = _docker(tmp_path, {NAME: _depth_provider(depth)})
    fake_docker(ex, ex.mounts())          # a container created from today's config
    message, command = _refusal(ex, adapter, tmp_path, monkeypatch)
    assert message, (
        f"bin_versions_depth: {depth} ({case}) is a config error: the run must be refused")
    assert "bin_versions_depth" in message, f"the refusal must name the key: {message!r}"
    assert command is None, f"nothing may be exec'd: {command}"


@pytest.fixture
def usr_bin(host):
    """`acme` on PATH pointing at a real executable file in /usr/bin."""
    usr_bin = Path("/usr/bin")
    for name in ("python3", "git", "sh", "env", "ls", "cat"):
        found = shutil.which(name, path=str(usr_bin))
        if found and Path(found).resolve().parent == usr_bin:
            real = Path(found).resolve()
            break
    else:
        real = next((p for p in sorted(usr_bin.iterdir()) if not p.is_symlink()
                     and p.is_file() and os.access(p, os.X_OK)), None)
        if real is None:
            pytest.skip("no regular executable file in /usr/bin on this machine")
    _point(host.launcher, real)
    return real


@pytest.mark.parametrize("depth,widened", [(2, Path("/usr")), (3, Path("/"))],
                         ids=["usr", "root"])
def test_cx_c17_system_prefix_root_is_not_mounted_and_refused(
        tmp_path, usr_bin, fake_docker, monkeypatch, depth, widened):
    adapter = _executable(global_config_dir() / "providers" / ADAPTER, "#!/bin/sh\n")
    ex = _docker(tmp_path, {NAME: _depth_provider(depth)})
    mounts = ex.mounts()
    assert widened not in dict(mounts), (
        f"bin_versions_depth: {depth} on {usr_bin} computes {widened}, which must "
        f"never be mounted; mounts={mounts}")
    fake_docker(ex, mounts)
    message, command = _refusal(ex, adapter, tmp_path, monkeypatch)
    assert "bin_versions_depth" in message, (
        f"a root of {widened} must be refused naming the key: {message!r}")
    assert command is None


# ===========================================================================
# CX-C18 — `mount_cli_from_host: false` is honoured
# ===========================================================================

@pytest.fixture
def nested(host):
    """CX-C3's `<root>/<version>-x86_64/bin/acme` (N = 3)."""
    root = host.root / ".acme" / "packages" / "releases"
    first = _executable(root / "1.0.0-x86_64" / "bin" / NAME)
    _point(host.launcher, first)
    return SimpleNamespace(host=host.root, root=root, first=first, launcher=host.launcher)


def test_cx_c18_flag_off_bin_is_the_bare_name(tmp_path, nested, monkeypatch):
    adapter = _executable(global_config_dir() / "providers" / ADAPTER, "#!/bin/sh\n")
    ex = _docker(tmp_path, {NAME: _depth_provider(3)}, mount_cli_from_host=False)
    _, env = _start_up(ex, [str(adapter), *ARGS], tmp_path, monkeypatch)
    assert env.get("MULTIAGENTS_BIN") == NAME, (
        f"with mount_cli_from_host: false the host binary is not in the container: "
        f"MULTIAGENTS_BIN must be the bare `bin`, found by PATH in there; env={env}")


def test_cx_c18_flag_off_bin_is_the_bare_name_inside(tmp_path, nested, monkeypatch):
    adapter = _executable(global_config_dir() / "providers" / ADAPTER, "#!/bin/sh\n")
    ex = _docker(tmp_path, {NAME: _depth_provider(3)}, mount_cli_from_host=False)
    ex.inside = lambda: True
    captured = _capture_exec(monkeypatch)
    asyncio.run(ex.start([str(adapter), *ARGS], tmp_path,
                         {"MULTIAGENTS_AGENT_ID": "ag-rv0003", "PATH": os.environ["PATH"]}))
    env = captured.env or {}
    assert env.get("MULTIAGENTS_BIN") == NAME, env


def test_cx_c18_flag_off_mounts_no_versions_root(tmp_path, nested):
    ex = _docker(tmp_path, {NAME: _depth_provider(3)}, mount_cli_from_host=False)
    mounts = dict(ex.mounts())
    assert nested.root not in mounts, mounts
    assert nested.launcher not in mounts and nested.first not in mounts, mounts


def test_cx_c18_flag_off_target_leaving_the_root_is_not_refused(
        tmp_path, nested, fake_docker, monkeypatch):
    adapter = _executable(global_config_dir() / "providers" / ADAPTER, "#!/bin/sh\n")
    ex = _docker(tmp_path, {NAME: _depth_provider(3)}, mount_cli_from_host=False)
    fake_docker(ex, ex.mounts())
    _point(nested.launcher,
           _executable(nested.host / "elsewhere" / "2.0.0-x86_64" / "bin" / NAME))
    message, command = _refusal(ex, adapter, tmp_path, monkeypatch)
    assert message == "" and command is not None, (
        f"with the flag off no CX-C3 refusal runs; got {message!r}")


# ===========================================================================
# CX-C19 — the stale-root refusal is worded neutrally
# ===========================================================================

def _lacks(message: str, root: Path) -> bool:
    return re.search(r"[Tt]his container lacks the versions root "
                     + re.escape(str(root)), message) is not None


def test_cx_c19_moved_target_says_the_container_lacks_the_root(
        tmp_path, nested, fake_docker, monkeypatch):
    adapter = _executable(global_config_dir() / "providers" / ADAPTER, "#!/bin/sh\n")
    ex = _docker(tmp_path, {NAME: _depth_provider(3)})
    fake_docker(ex, ex.mounts())
    new_root = nested.host / "elsewhere"
    _point(nested.launcher, _executable(new_root / "2.0.0-x86_64" / "bin" / NAME))
    message, command = _refusal(ex, adapter, tmp_path, monkeypatch)
    assert _lacks(message, new_root), (
        f"the refusal must say 'this container lacks the versions root {new_root}': "
        f"{message!r}")
    assert RECREATE in message, message
    assert command is None


def test_cx_c19_key_added_after_creation_says_the_container_lacks_the_root(
        tmp_path, nested, fake_docker, monkeypatch):
    adapter = _executable(global_config_dir() / "providers" / ADAPTER, "#!/bin/sh\n")
    before = _docker(tmp_path, {NAME: Provider.from_dict(
        NAME, {"bin": NAME, "adapter": ADAPTER, "spawn": SPAWN})})
    ex = _docker(tmp_path, {NAME: _depth_provider(3)})
    fake_docker(ex, before.mounts())       # created before the key was added
    message, command = _refusal(ex, adapter, tmp_path, monkeypatch)
    assert _lacks(message, nested.root), (
        f"the refusal must say 'this container lacks the versions root "
        f"{nested.root}': {message!r}")
    assert RECREATE in message, message
    assert command is None


# ===========================================================================
# CX-C20 — disabled providers add no mounts
# ===========================================================================

def test_cx_c20_disabled_provider_adds_no_versions_root(tmp_path, nested):
    ex = _docker(tmp_path, {NAME: _depth_provider(3, enabled=False)})
    assert nested.root not in dict(ex.mounts()), ex.mounts()


def test_cx_c20_disabled_provider_adds_no_adapter_mount(tmp_path, host):
    adapter = _executable(global_config_dir() / "providers" / ADAPTER, "#!/bin/sh\n")
    _executable(host.launcher)
    p = Provider.from_dict(NAME, {"bin": NAME, "adapter": ADAPTER, "spawn": SPAWN,
                                  "enabled": False})
    ex = _docker(tmp_path, {NAME: p})
    assert adapter not in dict(ex.mounts()), ex.mounts()


def test_cx_c20_enabled_sibling_still_gets_its_mounts(tmp_path, nested):
    # Guard: the rule is per provider. A disabled `acme-2` takes nothing away
    # from an enabled `acme`.
    adapter = _executable(global_config_dir() / "providers" / ADAPTER, "#!/bin/sh\n")
    other = Provider.from_dict(NAME2, {"bin": NAME2, "adapter": "acme-2-adapter.py",
                                       "spawn": SPAWN, "enabled": False})
    ex = _docker(tmp_path, {NAME: _depth_provider(3), NAME2: other})
    mounts = dict(ex.mounts())
    assert mounts.get(nested.root) is True, mounts
    assert any(adapter == m or m in adapter.parents for m in mounts), mounts
