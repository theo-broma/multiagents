"""Codex provider contract, ENGINE half — executor-facing ids.

`context/specs/codex-provider.md`, "Interface contract" plus "Amendments after
the contract review" (the amendments win):

- CX-C1  `adapter:` — a provider field naming an executable that is exec'd as
         argv[0] of an agent run, and doubles as the action script.
- CX-C2  (revised) the EXECUTOR sets MULTIAGENTS_BIN / MULTIAGENTS_EXECUTOR /
         MULTIAGENTS_PRIVATE_HOME on agent runs, including `_start_inside`.
- CX-C3  (pinned) `bin_versions_depth: N` — the versions root is
         `resolved.parents[N-1]`, mounted read-only after the private backing;
         a target that leaves the root is refused by name.

Black box: `Provider.from_dict` / `build_command` / `available`,
`scripts.run_action`, `Runner.start`, and `DockerExecutor.mounts` / `start` /
`env_file` / `stale_mounts` / `ensure_running`. No docker daemon (the docker CLI
is faked at the subprocess boundary as in `test_phase0_versioned_mount.py`), no
real codex, no network.

The provider is called `acme` on purpose: nothing in core may know the name.

Stubs, stated once: `DockerExecutor.inside` is pinned per test (this suite may
itself run in a container, where `/.dockerenv` exists), and for tests about the
env/argv of one spawn `ensure_running` is replaced by "the container is up", as
`test_phase0_versioned_mount._issued_command` does. The refusal tests do NOT
stub `ensure_running`: they drive a fake daemon, so the check may live anywhere
in the start path.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import multiagents.executor.docker as docker_mod
from multiagents import scripts
from multiagents.executor.docker import DockerExecutor
from multiagents.executor.local import LocalExecutor
from multiagents.paths import ProjectPaths, global_config_dir
from multiagents.providers import Provider

sys.path.insert(0, str(Path(__file__).parent / "support"))
import c3_harness as h3  # noqa: E402

NAME = "acme"
ADAPTER = "acme-adapter.py"
SPAWN = {"args": ["run", "--model", "{model}", "--", "{prompt}"]}
ARGS = ["run", "--model", "m1", "--", "do the task"]
STREAM = {"format": "ndjson",
          "rules": [{"match": {"type": "result"}, "as": "result",
                     "fields": {"status": "subtype", "text": "result"}}]}


# ---------------------------------------------------------------------------
# helpers
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


def _under(path: Path, ancestor: Path) -> bool:
    return path == ancestor or ancestor in path.parents


def _adapter_body(record: Path, result_text: str = "adapter ran") -> str:
    """A python adapter that records how it was started, then emits one result
    event on the stream format `c3_harness.fake_cli` declares."""
    return (
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        f"with open({str(record)!r}, 'a') as f:\n"
        "    f.write(json.dumps({'argv': sys.argv[1:], 'env': {k: v for k, v in os.environ.items()\n"
        "        if k.startswith('MULTIAGENTS_')}}) + '\\n')\n"
        "if len(sys.argv) > 1 and sys.argv[1] == 'check':\n"
        "    print('adapter-check-ok'); sys.exit(0)\n"
        f"print(json.dumps({{'type': 'result', 'subtype': 'success', 'result': {result_text!r}}}))\n"
    )


def _records(record: Path) -> list[dict]:
    if not record.exists():
        return []
    return [json.loads(line) for line in record.read_text().splitlines() if line.strip()]


@pytest.fixture
def host(tmp_path_factory, monkeypatch):
    """A host home OUTSIDE any project, with `acme` on PATH and a fake docker.

    HOME points here, so `Path.home() / ".acme"` (the private backing's
    container path) lives in this tree.
    """
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


class _Captured(SimpleNamespace):
    command: list[str]
    env: dict | None


def _capture_exec(monkeypatch) -> _Captured:
    captured = _Captured(command=None, env=None)

    async def fake_exec(*command, **kw):
        captured.command = [str(c) for c in command]
        captured.env = kw.get("env")
        return SimpleNamespace(pid=4242, stdout=None, stderr=None, returncode=None)

    monkeypatch.setattr(docker_mod.asyncio, "create_subprocess_exec", fake_exec)
    return captured


def _start_up(ex, argv, cwd, monkeypatch, agent_id="ag-cx0001") -> tuple[_Captured, dict]:
    """One host-side docker spawn with the container taken as up. Returns the
    captured exec and the environment `start` wrote for the agent."""
    captured = _capture_exec(monkeypatch)
    ex.ensure_running = lambda: {"ok": True, "container": ex.container, "existed": True}
    asyncio.run(ex.start(list(argv), cwd, {"MULTIAGENTS_AGENT_ID": agent_id}))
    env = {}
    path = ex.env_file(agent_id)
    if path.exists():
        for line in path.read_text().splitlines():
            key, _, value = line.partition("=")
            env[key] = value
    return captured, env


class FakeDocker:
    """A daemon holding one running container created from `created_mounts`
    (same model as `test_phase0_versioned_mount.FakeDocker`)."""

    def __init__(self, ex, created_mounts):
        private = ex.private_state()
        self.mounts = [(str(p), str(private.get(p, p)), not ro) for p, ro in created_mounts]
        self.calls: list[list[str]] = []

    def run(self, argv, *a, **kw):
        argv = [str(x) for x in argv]
        self.calls.append(argv)
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


# ===========================================================================
# CX-C1 — `adapter:`
# ===========================================================================

def test_cx_c1_adapter_is_argv0_and_spawn_args_follow_unchanged():
    p = Provider.from_dict(NAME, {"bin": NAME, "adapter": ADAPTER, "spawn": SPAWN})
    argv = p.build_command(prompt="do the task", model="m1", workdir="/w")
    assert Path(argv[0]).name == ADAPTER, (
        f"with `adapter:` set the run must exec the adapter as argv[0]; argv={argv}")
    assert argv[1:] == ARGS, f"spawn args must follow the adapter unchanged; argv={argv}"


def test_cx_c1_bin_keeps_its_meaning_when_an_adapter_is_set(host):
    native = _executable(host.launcher)
    p = Provider.from_dict(NAME, {"bin": NAME, "adapter": ADAPTER, "spawn": SPAWN})
    assert p.available() is not None and os.path.samefile(p.available(), native), (
        f"`available()` must still find the native `bin`, not the adapter: {p.available()}")


def test_cx_c1_absent_adapter_is_todays_behaviour_byte_for_byte():
    p = Provider.from_dict(NAME, {"bin": NAME, "spawn": SPAWN})
    argv = p.build_command(prompt="do the task", model="m1", workdir="/w")
    assert argv == [NAME, *ARGS]
    assert p.script_name == f"{NAME}.sh"


@pytest.mark.parametrize("where", ["project", "global"])
def test_cx_c1_adapter_without_script_is_also_the_action_script(tmp_path, where):
    project_config = tmp_path / "project" / ".multiagents" / "config"
    base = (project_config if where == "project" else global_config_dir()) / "providers"
    record = tmp_path / "record.jsonl"
    _executable(base / ADAPTER, _adapter_body(record))
    p = Provider.from_dict(NAME, {"bin": NAME, "adapter": ADAPTER, "spawn": SPAWN})

    code, out, err = scripts.run_action(NAME, p, LocalExecutor(), "check",
                                        global_config_dir(), project_config)
    assert code == 0 and "adapter-check-ok" in out, (
        f"`adapter:` set and `script:` absent: the adapter found in the {where} "
        f"providers dir must answer actions; got rc={code} out={out!r} err={err!r}")


def test_cx_c1_explicit_script_still_wins_over_the_adapter_for_actions(tmp_path):
    base = global_config_dir() / "providers"
    record = tmp_path / "record.jsonl"
    _executable(base / ADAPTER, _adapter_body(record))
    _executable(base / "acme-actions.sh", "#!/bin/sh\necho from-script\n")
    p = Provider.from_dict(NAME, {"bin": NAME, "adapter": ADAPTER,
                                  "script": "acme-actions.sh", "spawn": SPAWN})
    code, out, _ = scripts.run_action(NAME, p, LocalExecutor(), "check",
                                      global_config_dir(), None)
    assert code == 0 and "from-script" in out and not _records(record)


def _adapter_runner(tmp_path, monkeypatch, host):
    """A Runner whose `acme` provider has an adapter in the project's providers
    dir and a fake native CLI on PATH."""
    record = tmp_path.parent / (tmp_path.name + "-adapter-record.jsonl")
    project_providers = tmp_path / ".multiagents" / "config" / "providers"
    _executable(project_providers / ADAPTER, _adapter_body(record))
    native = _executable(host.launcher, "#!/bin/sh\necho native\n")
    block = {"bin": NAME, "adapter": ADAPTER, "spawn": SPAWN, "stream": STREAM}
    spec = h3.AgentSpec(name="worker", provider=NAME, model="m1")
    r = h3.make_runner(tmp_path, monkeypatch, agents={"worker": spec},
                       providers={NAME: block})
    return r, record, native


def _run_once(r):
    async def go():
        result = await r.start("worker", "do the task")
        run = r.runs.get(result.get("agent_id", ""))
        if run is not None:
            await asyncio.wait_for(run.done.wait(), timeout=20)
        return result
    return asyncio.run(go())


def test_cx_c1_c2_runner_execs_the_adapter_with_bin_and_executor_in_env(
        tmp_path, monkeypatch, host):
    r, record, native = _adapter_runner(tmp_path, monkeypatch, host)
    result = _run_once(r)

    runs = _records(record)
    assert runs, (f"the adapter was never executed for the agent run; "
                  f"start() returned {result}")
    agent_run = runs[-1]
    assert agent_run["argv"][-len(ARGS):] == ARGS, agent_run["argv"]
    env = agent_run["env"]
    assert env.get("MULTIAGENTS_BIN") and os.path.samefile(env["MULTIAGENTS_BIN"], native), (
        f"MULTIAGENTS_BIN must be the absolute path of the native `bin`: {env}")
    assert os.path.isabs(env["MULTIAGENTS_BIN"])
    assert env.get("MULTIAGENTS_EXECUTOR") == "local", env
    node = r.tree.read()["nodes"][result["agent_id"]]
    assert node.get("status") == "done", node


def test_cx_c2_local_bin_is_resolved_at_each_exec_not_once(tmp_path, monkeypatch, host):
    r, record, native = _adapter_runner(tmp_path, monkeypatch, host)
    _run_once(r)
    other = _executable(host.root / "other-bin" / NAME, "#!/bin/sh\necho other\n")
    monkeypatch.setenv("PATH", f"{other.parent}{os.pathsep}{os.environ['PATH']}")
    _run_once(r)

    runs = _records(record)
    assert len(runs) >= 2, f"expected two adapter runs, got {runs}"
    first, second = runs[0]["env"].get("MULTIAGENTS_BIN"), runs[-1]["env"].get("MULTIAGENTS_BIN")
    assert first and os.path.samefile(first, native), runs[0]
    assert second and os.path.samefile(second, other), (
        f"MULTIAGENTS_BIN must be resolved at this exec: PATH changed between "
        f"runs but the second run still got {second}")


def test_cx_c1_docker_adapter_outside_every_mount_is_made_visible_read_only(tmp_path, host):
    adapter = _executable(global_config_dir() / "providers" / ADAPTER, "#!/bin/sh\n")
    _executable(host.launcher)
    p = Provider.from_dict(NAME, {"bin": NAME, "adapter": ADAPTER, "spawn": SPAWN})
    ex = _docker(tmp_path, {NAME: p})
    covering = [(m, ro) for m, ro in ex.mounts() if _under(adapter, m)]
    assert covering, (
        f"the adapter {adapter} must run at its host path in the container, but "
        f"no mount covers it: {ex.mounts()}")
    deepest = max(covering, key=lambda pair: len(pair[0].parts))
    assert deepest[1] is True, f"the adapter must be visible read-only: {deepest}"


# ===========================================================================
# CX-C2 — the executor sets MULTIAGENTS_BIN / _EXECUTOR / _PRIVATE_HOME
# ===========================================================================

def _adapter_provider(**extra) -> Provider:
    return Provider.from_dict(NAME, {"bin": NAME, "adapter": ADAPTER, "spawn": SPAWN,
                                     "container_private_home": [".acme"], **extra})


def test_cx_c2_docker_adapter_run_gets_bin_executor_and_private_home(
        tmp_path, host, monkeypatch):
    adapter = _executable(global_config_dir() / "providers" / ADAPTER, "#!/bin/sh\n")
    native = _executable(host.launcher)
    ex = _docker(tmp_path, {NAME: _adapter_provider()})
    mounts = ex.mounts()
    captured, env = _start_up(ex, [str(adapter), *ARGS], tmp_path, monkeypatch)

    assert captured.command is not None, "no docker exec was issued"
    assert env.get("MULTIAGENTS_EXECUTOR") == "docker", env
    bin_ = env.get("MULTIAGENTS_BIN", "")
    assert bin_ and os.path.isabs(bin_) and os.path.samefile(bin_, native), (
        f"MULTIAGENTS_BIN must name the native `bin`: env={env}")
    assert any(_under(Path(bin_), m) for m, _ in mounts), (
        f"MULTIAGENTS_BIN={bin_} must exist inside the container (under a mount): {mounts}")
    assert env.get("MULTIAGENTS_PRIVATE_HOME") == str(host.root / ".acme"), (
        f"an agent run gets MULTIAGENTS_PRIVATE_HOME when the executor has one: {env}")


def test_cx_c2_docker_adapter_argv0_is_left_alone(tmp_path, host, monkeypatch):
    adapter = _executable(global_config_dir() / "providers" / ADAPTER, "#!/bin/sh\n")
    versions = host.root / ".local" / "share" / NAME / "versions"
    _point(host.launcher, _executable(versions / "1.0.0"))    # P0-R1 versioned
    ex = _docker(tmp_path, {NAME: _adapter_provider()})
    captured, _ = _start_up(ex, [str(adapter), *ARGS], tmp_path, monkeypatch)
    tail = captured.command[-(len(ARGS) + 1):]
    assert tail == [str(adapter), *ARGS], (
        f"with `adapter:` argv[0] is the adapter and stays so: {captured.command}")


def test_cx_c2_docker_non_adapter_provider_keeps_p0_r1_argv(tmp_path, host, monkeypatch):
    # Guard: `_versioned_argv` keeps working without `adapter:`.
    versions = host.root / ".local" / "share" / NAME / "versions"
    target = _executable(versions / "1.0.0")
    _point(host.launcher, target)
    ex = _docker(tmp_path, {NAME: Provider.from_dict(NAME, {"bin": NAME, "spawn": SPAWN})})
    captured, _ = _start_up(ex, [NAME, *ARGS], tmp_path, monkeypatch)
    assert captured.command[-(len(ARGS) + 1):] == [str(target), *ARGS]


def test_cx_c2_start_inside_sets_bin_executor_and_private_home(tmp_path, host, monkeypatch):
    adapter = _executable(global_config_dir() / "providers" / ADAPTER, "#!/bin/sh\n")
    native = _executable(host.launcher)
    ex = _docker(tmp_path, {NAME: _adapter_provider()})
    ex.inside = lambda: True                      # a depth>=2 spawn, from in the container
    captured = _capture_exec(monkeypatch)
    asyncio.run(ex.start([str(adapter), *ARGS], tmp_path,
                         {"MULTIAGENTS_AGENT_ID": "ag-cx0002", "PATH": os.environ["PATH"]}))

    env = captured.env or {}
    assert env.get("MULTIAGENTS_EXECUTOR") == "docker", env
    bin_ = env.get("MULTIAGENTS_BIN", "")
    assert bin_ and os.path.samefile(bin_, native), (
        f"_start_inside must set MULTIAGENTS_BIN too: {env}")
    assert env.get("MULTIAGENTS_PRIVATE_HOME") == str(host.root / ".acme"), env


# ===========================================================================
# CX-C3 — `bin_versions_depth:`
# ===========================================================================

class Nested(SimpleNamespace):
    """`<root>/<version>-x/bin/acme` (N = 3) under host/.acme/packages/releases."""

    def install(self, version: str) -> Path:
        return _executable(self.root / f"{version}-x86_64" / "bin" / NAME,
                           f"#!/bin/sh\necho {version}\n")

    def retarget(self, target: Path) -> None:
        _point(self.launcher, target)


@pytest.fixture
def nested(host):
    root = host.root / ".acme" / "packages" / "releases"
    lay = Nested(host=host.root, root=root, launcher=host.launcher)
    lay.first = lay.install("1.0.0")
    lay.retarget(lay.first)
    _executable(host.root / ".acme" / "config.toml")          # a bystander above the root
    return lay


def _versioned(depth=3, adapter=True, private=True) -> Provider:
    data = {"bin": NAME, "spawn": SPAWN, "bin_versions_depth": depth}
    if adapter:
        data["adapter"] = ADAPTER
    if private:
        data["container_private_home"] = [".acme"]
    return Provider.from_dict(NAME, data)


def test_cx_c3_depth3_mounts_the_versions_root_read_only_and_nothing_above(tmp_path, nested):
    ex = _docker(tmp_path, {NAME: _versioned(3)})
    mounts = ex.mounts()
    assert dict(mounts).get(nested.root) is True, (
        f"bin_versions_depth: 3 must mount resolved.parents[2] = {nested.root} "
        f"read-only; mounts={mounts}")
    private = set(ex.private_state())
    above = [m for m, _ in mounts
             if m not in private and _under(nested.root, m) and m != nested.root
             and _under(m, nested.host)]
    assert above == [], f"nothing above the versions root may be mounted: {above}"


def test_cx_c3_mount_order_is_backing_then_versions_root(tmp_path, nested):
    ex = _docker(tmp_path, {NAME: _versioned(3)})
    order = [m for m, _ in ex.mounts()]
    backing = nested.host / ".acme"
    assert backing in order, f"the private backing {backing} is not mounted: {order}"
    assert nested.root in order, f"the versions root {nested.root} is not mounted: {order}"
    assert order.index(backing) < order.index(nested.root), (
        "the versions root nests inside the backing, so the backing must be "
        f"mounted first: {order}")


def test_cx_c3_depth1_root_is_the_targets_own_directory(tmp_path, host):
    releases = host.root / "opt" / NAME / "releases"
    target = _executable(releases / "acme-1.0.0")
    _point(host.launcher, target)
    ex = _docker(tmp_path, {NAME: _versioned(1, private=False)})
    mounts = dict(ex.mounts())
    assert mounts.get(releases) is True, mounts
    assert target not in mounts, "the root is mounted instead of only the resolved file"


def test_cx_c3_depth1_new_version_is_named_by_multiagents_bin(tmp_path, host, monkeypatch):
    adapter = _executable(global_config_dir() / "providers" / ADAPTER, "#!/bin/sh\n")
    releases = host.root / "opt" / NAME / "releases"
    _point(host.launcher, _executable(releases / "acme-1.0.0"))
    ex = _docker(tmp_path, {NAME: _versioned(1, private=False)})
    before = ex.mounts()
    newer = _executable(releases / "acme-1.0.1")
    _point(host.launcher, newer)
    _, env = _start_up(ex, [str(adapter), *ARGS], tmp_path, monkeypatch)
    assert ex.mounts() == before
    assert env.get("MULTIAGENTS_BIN") == str(newer), (
        f"after a self-update under the root MULTIAGENTS_BIN names the new target: {env}")


def test_cx_c3_depth3_retarget_under_root_changes_no_mount_and_updates_bin(
        tmp_path, nested, monkeypatch):
    adapter = _executable(global_config_dir() / "providers" / ADAPTER, "#!/bin/sh\n")
    ex = _docker(tmp_path, {NAME: _versioned(3)})
    before = ex.mounts()
    second = nested.install("1.0.1")
    nested.retarget(second)
    assert ex.mounts() == before, (
        f"a new version under the root must not change the mount list:\n"
        f"before={before}\nafter={ex.mounts()}")
    _, env = _start_up(ex, [str(adapter), *ARGS], tmp_path, monkeypatch)
    assert env.get("MULTIAGENTS_BIN") == str(second), (
        f"MULTIAGENTS_BIN must be re-resolved on the host at this exec: {env}")


def test_cx_c3_depth3_retarget_under_root_is_not_stale(tmp_path, nested, fake_docker):
    ex = _docker(tmp_path, {NAME: _versioned(3)})
    fake_docker(ex, ex.mounts())
    nested.retarget(nested.install("1.0.1"))
    assert ex.stale_mounts() == [], "a self-update under the root needs no recreate"


def test_cx_c3_target_leaving_the_root_is_refused_by_name(
        tmp_path, nested, fake_docker, monkeypatch):
    adapter = _executable(global_config_dir() / "providers" / ADAPTER, "#!/bin/sh\n")
    ex = _docker(tmp_path, {NAME: _versioned(3)})
    fake_docker(ex, ex.mounts())                   # created while the root held it
    elsewhere = _executable(nested.host / "elsewhere" / "2.0.0-x86_64" / "bin" / NAME)
    nested.retarget(elsewhere)
    captured = _capture_exec(monkeypatch)

    with pytest.raises(Exception) as err:
        asyncio.run(ex.start([str(adapter), *ARGS], tmp_path,
                             {"MULTIAGENTS_AGENT_ID": "ag-cx0003"}))
    message = str(err.value)
    assert "bin_versions_depth" in message, (
        f"the refusal must name the key: {message!r}")
    assert "multiagents docker rm && multiagents docker up" in message, message
    assert captured.command is None, f"nothing may be exec'd: {captured.command}"


def test_cx_c3_absent_key_keeps_p0_r1_for_the_nested_layout(tmp_path, nested):
    # Guard: without the key, today's P0-R1.8 behaviour — the nested target is
    # not versioned, so the launcher and its resolved file are mounted.
    ex = _docker(tmp_path, {NAME: Provider.from_dict(NAME, {"bin": NAME, "spawn": SPAWN})})
    mounts = dict(ex.mounts())
    assert mounts.get(nested.launcher) is True
    assert mounts.get(nested.first) is True
    assert nested.root not in mounts
