"""Phase 0, contract A, group A — P0-R1: the container executes the CLI the
host would execute today (`context/specs/phase0-runtime-repairs.md`, F200).

Black box over `DockerExecutor`'s public surface: `mounts()`, `stale_mounts()`,
`mount_drift()`, `ensure_running()` and `start()`. No docker daemon: the
docker CLI is faked at the subprocess boundary, and the container is modelled
as "created from the mount list computed at creation time".

The host layout is built in a temp directory, deliberately OUTSIDE the project
root (the project root is mounted, read-only since SG-R2, and would otherwise
"contain" everything):

    host/.local/bin/fakecli            -> host/.local/share/fakecli/versions/1.0.0
    host/.local/bin/unrelated-tool        (a bystander; must never be mounted)
    host/.local/share/fakecli/versions/   (the versions directory)
    host/.local/share/fakecli/settings    (a bystander outside the versions dir)

Versioned launcher, as amended 2026-09-22 (4694b9d): a symlink whose resolved
target is a FILE in a directory other than the launcher's own; that directory
is the versions directory whatever its name, even with a single entry.
Tightened by P0-R1.8 (2056cac): the target's file name must also differ from
the launcher's, so a target nested as `versions/1.0.0/bin/fakecli` is not
versioned and keeps today's behaviour (tested at the end).

The provider is named `fakecli` on purpose: the executor must not know which
provider it is dealing with.
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import multiagents.executor.docker as docker_mod
from multiagents.providers import Provider
sys.path.insert(0, str(Path(__file__).parent / "support"))
import c1_harness as h  # noqa: E402

NAME = "fakecli"


# ---------------------------------------------------------------------------
# host layout
# ---------------------------------------------------------------------------

def _executable(path: Path, body: str = "#!/bin/sh\necho fake\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(0o755)
    return path


def _point(link: Path, target: Path | str) -> None:
    """Retarget `link` atomically, as a CLI installer does."""
    tmp = link.with_name(link.name + ".tmp-link")
    if tmp.is_symlink() or tmp.exists():
        tmp.unlink()
    os.symlink(target, tmp)
    os.replace(tmp, link)


class Layout(SimpleNamespace):
    host: Path
    bin: Path
    launcher: Path
    versions: Path

    def version(self, v: str) -> Path:
        return self.versions / v

    def install(self, v: str) -> Path:
        return _executable(self.versions / v, f"#!/bin/sh\necho {v}\n")

    def retarget(self, v: str) -> None:
        _point(self.launcher, self.versions / v)


@pytest.fixture
def layout(tmp_path_factory, monkeypatch) -> Layout:
    host = tmp_path_factory.mktemp("host")
    bin_dir = host / ".local" / "bin"
    versions = host / ".local" / "share" / NAME / "versions"
    bin_dir.mkdir(parents=True)
    versions.mkdir(parents=True)
    _executable(bin_dir / "unrelated-tool")
    _executable(host / ".local" / "share" / NAME / "settings")
    lay = Layout(host=host, bin=bin_dir, launcher=bin_dir / NAME, versions=versions)
    lay.install("1.0.0")
    lay.retarget("1.0.0")

    # A fake `docker` on PATH too, so `docker_available()` holds; every call
    # it would receive is intercepted at subprocess.run below anyway.
    dockerbin = tmp_path_factory.mktemp("dockerbin")
    _executable(dockerbin / "docker", "#!/bin/sh\nexit 0\n")
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{dockerbin}")
    return lay


def _provider() -> Provider:
    return Provider.from_dict(NAME, {"bin": NAME})


def _executor(tmp_path: Path, **config):
    config.setdefault("network", "bridge")
    return h.make_docker_executor(tmp_path, providers={NAME: _provider()}, **config)


def _under(path: Path, ancestor: Path) -> bool:
    return path == ancestor or ancestor in path.parents


def _layout_mounts(mounts, lay: Layout) -> list[tuple[Path, bool]]:
    """The mounts that concern the CLI: anything touching the host layout."""
    return [(p, ro) for p, ro in mounts if _under(p, lay.host) or _under(lay.host, p)]


# ---------------------------------------------------------------------------
# a fake docker daemon holding one container created from a given mount list
# ---------------------------------------------------------------------------

class FakeDocker:
    """Answers the docker CLI as a daemon holding one running container whose
    mounts were fixed at creation. Records every docker argv it receives."""

    def __init__(self, ex, created_mounts):
        private = ex.private_state()
        self.mounts = [(str(p), str(private.get(p, p)), not ro) for p, ro in created_mounts]
        self.calls: list[list[str]] = []

    def run(self, argv, *a, **kw):
        argv = [str(x) for x in argv]
        self.calls.append(argv)
        out, rc = "", 0
        fmt = " ".join(argv)
        if argv[:2] == ["docker", "inspect"]:
            if ".State.Status" in fmt:
                out = "running\n"
            elif ".Destination}}:{{.RW" in fmt:
                out = "".join(f"{d}:{'true' if rw else 'false'}\n" for d, _, rw in self.mounts)
            elif ".Source}}>{{.Destination" in fmt:
                out = "".join(f"{s}>{d}\n" for d, s, _ in self.mounts)
            elif ".State.StartedAt" in fmt:
                out = "2026-09-22T10:00:00.000000000Z\n"
        return subprocess.CompletedProcess(argv, rc, stdout=out, stderr="")

    def lifecycle_calls(self):
        return [c for c in self.calls
                if c[:2] in (["docker", "run"], ["docker", "rm"], ["docker", "create"])]


@pytest.fixture
def fake_docker(monkeypatch):
    real_run = subprocess.run
    holder = {}

    def install(ex, created_mounts):
        fake = FakeDocker(ex, created_mounts)
        holder["fake"] = fake

        def run(argv, *a, **kw):
            if argv and str(argv[0]) == "docker":
                return fake.run(argv, *a, **kw)
            return real_run(argv, *a, **kw)

        monkeypatch.setattr(docker_mod.subprocess, "run", run)
        return fake

    return install


# ---------------------------------------------------------------------------
# capturing the command a spawn issues
# ---------------------------------------------------------------------------

def _issued_command(ex, argv, cwd, monkeypatch) -> list[str]:
    """The argv `start()` hands to the OS for one agent spawn. The container
    is taken as already up (`ensure_running` is not what R1.2 is about)."""
    captured = {}

    async def fake_exec(*command, **kw):
        captured["command"] = [str(c) for c in command]
        return SimpleNamespace(pid=4242, stdout=None, stderr=None,
                               returncode=None)

    monkeypatch.setattr(docker_mod.asyncio, "create_subprocess_exec", fake_exec)
    ex.ensure_running = lambda: {"ok": True, "container": ex.container, "existed": True}
    asyncio.run(ex.start(list(argv), cwd,
                         {"MULTIAGENTS_AGENT_ID": "ag-test01"}))
    return captured["command"]


AGENT_ARGS = ["-p", "do the task", "--model", "m1"]


# ===========================================================================
# P0-R1.1 — the declared mount list is stable across a CLI update
# ===========================================================================

def test_p0_r1_1_mount_list_is_identical_before_and_after_a_retarget(tmp_path, layout):
    ex = _executor(tmp_path)
    before = ex.mounts()
    layout.install("1.0.1")
    layout.retarget("1.0.1")
    after = ex.mounts()
    assert after == before, (
        "a CLI update (launcher retargeted inside its versions directory) "
        f"changed the declared mount list:\nbefore={before}\nafter={after}")


def test_p0_r1_1_no_mount_path_carries_a_version_component(tmp_path, layout):
    ex = _executor(tmp_path)
    layout.install("1.0.1")
    for version in ("1.0.0", "1.0.1"):
        layout.retarget(version)
        for path, _ in ex.mounts():
            assert "1.0.0" not in path.parts and "1.0.1" not in path.parts, (
                f"mount {path} pins a CLI version into the mount list")


def test_p0_r1_1_single_entry_versions_dir_with_any_name_is_still_stable(tmp_path, layout):
    # Amended contract: the directory counts whatever its name, and even with
    # one entry. Move the launcher to a directory called `releases` holding
    # exactly one build, then replace that build by another one.
    releases = layout.host / "opt" / NAME / "releases"
    first = _executable(releases / "build-a")
    _point(layout.launcher, first)
    ex = _executor(tmp_path)
    before = ex.mounts()

    second = _executable(releases / "build-b")
    _point(layout.launcher, second)
    first.unlink()                          # the old build is gone: one entry again
    after = ex.mounts()

    assert after == before
    for path, _ in after:
        assert path not in (first, second), f"{path} names one build"


# ===========================================================================
# P0-R1.2 — the next spawn executes the new version, without recreation
# ===========================================================================

def test_p0_r1_2_spawn_after_retarget_execs_the_new_version_by_resolved_path(
        tmp_path, layout, monkeypatch):
    ex = _executor(tmp_path)
    # The container is created now, while 1.0.0 is current and 1.0.1 does
    # not even exist yet on disk.
    created = ex.mounts()

    layout.install("1.0.1")
    layout.retarget("1.0.1")
    new, old = layout.version("1.0.1"), layout.version("1.0.0")

    command = _issued_command(ex, [NAME, *AGENT_ARGS], tmp_path, monkeypatch)

    # Decided approach: the host resolves the launcher per spawn and the
    # issued command names that absolute path, followed by the agent's args.
    tail = command[-(len(AGENT_ARGS) + 1):]
    assert tail == [str(new), *AGENT_ARGS], (
        f"the program executed should be the resolved new version {new}; "
        f"command was {command}")
    assert not any(str(old) in token for token in command), (
        f"the superseded version {old} appears in the command {command}")

    # Reachable in a container created BEFORE the new version was installed:
    # it must lie under a directory mounted at creation, not be a new mount.
    covering = [p for p, _ in created if _under(new, p) and p != new]
    assert covering, (
        f"{new} is not under any mount the container was created with "
        f"({created}); running it would need the container recreated")


def test_p0_r1_2_every_spawn_resolves_afresh_including_a_rollback(
        tmp_path, layout, monkeypatch):
    ex = _executor(tmp_path)
    layout.install("1.0.1")

    seen = []
    for version in ("1.0.1", "1.0.0", "1.0.1"):
        layout.retarget(version)
        command = _issued_command(ex, [NAME, *AGENT_ARGS], tmp_path, monkeypatch)
        seen.append(command[-(len(AGENT_ARGS) + 1)])

    assert seen == [str(layout.version(v)) for v in ("1.0.1", "1.0.0", "1.0.1")], (
        "each spawn must execute whatever the launcher points at at that "
        f"moment; executed {seen}")


def test_p0_r1_2_agent_arguments_pass_through_untouched(tmp_path, layout, monkeypatch):
    ex = _executor(tmp_path)
    layout.install("1.0.1")
    layout.retarget("1.0.1")
    tricky = ["-p", f"mention {NAME} and $HOME and {{prompt}}", NAME, "--x=\"y z\""]
    command = _issued_command(ex, [NAME, *tricky], tmp_path, monkeypatch)
    assert command[-len(tricky):] == tricky, (
        "only the program is replaced; arguments that happen to name the "
        f"launcher must be left alone. command={command}")


# ===========================================================================
# P0-R1.3 — `docker up` does not refuse because the CLI updated
# ===========================================================================

def test_p0_r1_3_retarget_produces_no_stale_mount_report(tmp_path, layout, fake_docker):
    ex = _executor(tmp_path)
    fake_docker(ex, ex.mounts())            # container created at 1.0.0
    layout.install("1.0.1")
    layout.retarget("1.0.1")
    assert ex.stale_mounts() == []


def test_p0_r1_3_retarget_produces_no_mount_drift_report(tmp_path, layout, fake_docker):
    ex = _executor(tmp_path)
    fake_docker(ex, ex.mounts())
    layout.install("1.0.1")
    layout.retarget("1.0.1")
    assert ex.mount_drift() == []


def test_p0_r1_3_docker_up_keeps_the_container_across_a_cli_update(
        tmp_path, layout, fake_docker):
    ex = _executor(tmp_path)
    fake = fake_docker(ex, ex.mounts())
    layout.install("1.0.1")
    layout.retarget("1.0.1")

    result = ex.ensure_running()
    assert result.get("ok") is True, f"docker up refused after a CLI update: {result}"
    assert fake.lifecycle_calls() == [], (
        f"the running container was replaced: {fake.lifecycle_calls()}")


def test_p0_r1_3_a_real_mount_addition_is_still_refused_after_a_retarget(
        tmp_path, layout, fake_docker):
    # 538149b behaviour kept: a mount the container lacks is still reported,
    # even when a CLI update happened at the same time.
    ex = _executor(tmp_path)
    fake_docker(ex, ex.mounts())
    layout.install("1.0.1")
    layout.retarget("1.0.1")

    toolchain = tmp_path.parent / (tmp_path.name + "-toolchain")
    toolchain.mkdir()
    ex.config["extra_mounts"] = [{"path": str(toolchain), "read_only": True}]

    stale = ex.stale_mounts()
    assert stale and any(str(toolchain) in s for s in stale), stale
    assert all("1.0.1" not in s and "1.0.0" not in s for s in stale), (
        f"the CLI update must not be part of the report: {stale}")
    result = ex.ensure_running()
    assert result.get("ok") is False and str(toolchain) in result.get("error", "")


def test_p0_r1_3_a_read_only_change_is_still_refused_after_a_retarget(
        tmp_path, layout, fake_docker):
    ex = _executor(tmp_path)
    # The project root is read-only since SG-R2, so the "wrong" state is taken
    # from a path the config still wants writable: the worktrees directory.
    ex.paths.worktrees.mkdir(parents=True, exist_ok=True)
    created = ex.mounts()
    assert dict(created)[ex.paths.worktrees] is False
    # The container got the worktrees read-only; the config wants them writable.
    tampered = [(p, True if p == ex.paths.worktrees else ro) for p, ro in created]
    fake_docker(ex, tampered)
    layout.install("1.0.1")
    layout.retarget("1.0.1")

    stale = ex.stale_mounts()
    assert stale == [f"{ex.paths.worktrees} as writable"], stale


# ===========================================================================
# P0-R1.4 — the provider CLI stays invokable by its declared name
# ===========================================================================

def test_p0_r1_4_launcher_path_stays_mounted_read_only_and_resolvable(tmp_path, layout):
    ex = _executor(tmp_path)
    layout.install("1.0.1")
    for version in ("1.0.0", "1.0.1"):
        layout.retarget(version)
        mounts = dict(ex.mounts())
        assert mounts.get(layout.launcher) is True, (
            f"{layout.launcher} (what `{NAME}` resolves to on PATH) must stay "
            f"mounted read-only at its own path; mounts={sorted(mounts)}")
        # What docker binds at that path is its target at creation: that
        # target must be an executable file the container can reach.
        target = layout.launcher.resolve()
        assert target.is_file() and os.access(target, os.X_OK)
        assert any(_under(target, p) for p in mounts), (
            f"the launcher's target {target} is not inside any mount")


# ===========================================================================
# P0-R1.5 — a launcher that is not versioned is mounted exactly as today
# ===========================================================================

def _todays_list(ex, *cli_paths: Path) -> list[tuple[Path, bool]]:
    """Today's full mount list for this fixture: the project root's SG-R2
    layout (the root read-only, `.multiagents` reopened writable, its config
    closed again; the fixture has no `.git`, so nothing under it), the CLI
    path(s) read-only, sorted; nothing else exists to be mounted."""
    project = [(ex.paths.root, True), (ex.paths.data, False), (ex.paths.config, True)]
    return sorted([*project, *[(p, True) for p in cli_paths]])


def test_p0_r1_5_plain_binary_is_mounted_byte_identically(tmp_path, layout):
    layout.launcher.unlink()
    _executable(layout.launcher)
    ex = _executor(tmp_path)
    assert ex.mounts() == _todays_list(ex, layout.launcher)


@pytest.mark.parametrize("relative", [False, True], ids=["absolute", "relative"])
def test_p0_r1_5_symlink_into_its_own_directory_is_mounted_byte_identically(
        tmp_path, layout, relative):
    real = _executable(layout.bin / f"{NAME}-real")
    _point(layout.launcher, real.name if relative else real)
    ex = _executor(tmp_path)
    assert ex.mounts() == _todays_list(ex, layout.launcher, real)


def test_p0_r1_5_plain_binary_spawn_execs_as_today(tmp_path, layout, monkeypatch):
    layout.launcher.unlink()
    _executable(layout.launcher)
    ex = _executor(tmp_path)
    command = _issued_command(ex, [NAME, *AGENT_ARGS], tmp_path, monkeypatch)
    # Today the agent argv is appended unchanged (bare name first).
    assert command[-(len(AGENT_ARGS) + 1):] == [NAME, *AGENT_ARGS], command


def test_p0_r1_5_a_versioned_provider_does_not_change_a_plain_ones_mounts(
        tmp_path, layout, tmp_path_factory, monkeypatch):
    other_bin = tmp_path_factory.mktemp("otherbin")
    plain = _executable(other_bin / "plaincli")
    monkeypatch.setenv("PATH", f"{os.environ['PATH']}{os.pathsep}{other_bin}")
    providers = {NAME: _provider(),
                 "plaincli": Provider.from_dict("plaincli", {"bin": "plaincli"})}
    ex = h.make_docker_executor(tmp_path, providers=providers, network="bridge")
    mounts = dict(ex.mounts())
    assert mounts.get(plain) is True
    assert [p for p in mounts if _under(p, other_bin) or _under(other_bin, p)] == [plain]


# ===========================================================================
# P0-R1.6 — mount only what the binary resolves into; all read-only
# ===========================================================================

def test_p0_r1_6_launcher_directory_is_never_mounted(tmp_path, layout):
    ex = _executor(tmp_path)
    for path, _ in ex.mounts():
        assert not _under(layout.bin, path), (
            f"mount {path} exposes the launcher's directory {layout.bin} "
            "and every unrelated binary in it")
    unrelated = layout.bin / "unrelated-tool"
    assert not any(_under(unrelated, p) for p, _ in ex.mounts())


def test_p0_r1_6_nothing_above_the_versions_directory_is_mounted(tmp_path, layout):
    ex = _executor(tmp_path)
    settings = layout.versions.parent / "settings"
    for path, _ in ex.mounts():
        assert not (path != layout.versions and _under(layout.versions, path)), (
            f"mount {path} is wider than the versions directory {layout.versions}")
    assert not any(_under(settings, p) for p, _ in ex.mounts()), (
        f"{settings} sits beside the versions directory and must stay unmounted")


def test_p0_r1_6_every_cli_mount_is_read_only(tmp_path, layout):
    ex = _executor(tmp_path)
    layout.install("1.0.1")
    for version in ("1.0.0", "1.0.1"):
        layout.retarget(version)
        cli = _layout_mounts(ex.mounts(), layout)
        assert cli, "the CLI must be mounted at all"
        writable = [p for p, ro in cli if not ro]
        assert writable == [], f"CLI mounts must be read-only: {writable}"


def test_p0_r1_6_a_versions_directory_inside_the_launchers_directory_does_not_widen_to_it(
        tmp_path, layout):
    # The versions directory may sit INSIDE the launcher's directory
    # (bin/versions/1.0.0). The target's directory may be mounted; bin/, with
    # every unrelated binary in it, still may not.
    nested = layout.bin / "versions"
    target = _executable(nested / "1.0.0")
    _point(layout.launcher, target)
    ex = _executor(tmp_path)
    mounts = [p for p, _ in ex.mounts()]
    assert layout.bin not in mounts
    assert any(_under(target, p) for p in mounts)


# ===========================================================================
# P0-R1.7 — no provider name in the executor
# ===========================================================================

# Occurrences, case-insensitive, of the three provider names in
# src/multiagents/executor/*.py at e1e605a (before P0-R1): base.py 6,
# docker.py 9, __init__.py 2, local.py 0.
PROVIDER_NAMES_IN_EXECUTOR_TODAY = 17


def test_p0_r1_7_provider_names_in_the_executor_do_not_grow():
    root = Path(__file__).resolve().parent.parent / "src" / "multiagents" / "executor"
    pattern = re.compile(r"claude|agy|opencode", re.IGNORECASE)
    found = {f.name: len(pattern.findall(f.read_text())) for f in sorted(root.glob("*.py"))}
    total = sum(found.values())
    assert total <= PROVIDER_NAMES_IN_EXECUTOR_TODAY, (
        f"provider names in executor/*.py grew from "
        f"{PROVIDER_NAMES_IN_EXECUTOR_TODAY} to {total}: {found}")


# ===========================================================================
# P0-R1.8 — a target whose file name equals the launcher's is not versioned
# ===========================================================================
#
# Amendment after the review (finding 1): versioned additionally requires the
# resolved file's name to DIFFER from the launcher's. The nested layout
#
#     host/.local/bin/fakecli -> host/.local/share/fakecli/versions/1.0.0/bin/fakecli
#
# is therefore not versioned and keeps today's behaviour: the launcher and its
# resolved file are mounted (as for any symlink, P0-R1.5), and the command
# issued uses the bare name.

def _nested(layout: Layout, relative: bool = False) -> Path:
    layout.version("1.0.0").unlink()        # the fixture's flat build makes way
    target = _executable(layout.version("1.0.0") / "bin" / NAME)
    _point(layout.launcher, os.path.relpath(target, layout.bin) if relative else target)
    return target


@pytest.mark.parametrize("relative", [False, True], ids=["absolute", "relative"])
def test_p0_r1_8_nested_same_name_target_is_mounted_as_today(tmp_path, layout, relative):
    target = _nested(layout, relative)
    ex = _executor(tmp_path)
    mounts = ex.mounts()
    assert mounts == _todays_list(ex, layout.launcher, target), (
        "a target whose file name equals the launcher's is not versioned: "
        f"today's list is the launcher plus its resolved file; got {mounts}")


def test_p0_r1_8_nested_same_name_target_mounts_no_directory_of_the_install(
        tmp_path, layout):
    target = _nested(layout)
    ex = _executor(tmp_path)
    paths = [p for p, _ in ex.mounts()]
    for directory in (target.parent, layout.version("1.0.0"), layout.versions):
        assert directory not in paths, (
            f"{directory} is mounted; the nested layout must mount the "
            f"resolved file {target} only. mounts={paths}")


def test_p0_r1_8_nested_same_name_target_spawn_uses_the_bare_name(
        tmp_path, layout, monkeypatch):
    target = _nested(layout)
    ex = _executor(tmp_path)
    command = _issued_command(ex, [NAME, *AGENT_ARGS], tmp_path, monkeypatch)
    assert command[-(len(AGENT_ARGS) + 1):] == [NAME, *AGENT_ARGS], (
        f"the nested layout is not versioned: the program must stay the bare "
        f"name {NAME!r}, unrewritten. command={command}")
    assert not any(str(target) in token for token in command), command


def test_p0_r1_8_differently_named_target_is_still_versioned(
        tmp_path, layout, monkeypatch):
    # The claude-shaped layout (bin/fakecli -> versions/1.0.0): the file name
    # `1.0.0` differs from `fakecli`, so the tightened definition still applies.
    ex = _executor(tmp_path)
    mounts = ex.mounts()
    assert (layout.versions, True) in mounts, (
        f"the versions directory {layout.versions} must be mounted read-only; "
        f"mounts={mounts}")
    assert layout.version("1.0.0") not in [p for p, _ in mounts]

    command = _issued_command(ex, [NAME, *AGENT_ARGS], tmp_path, monkeypatch)
    assert command[-(len(AGENT_ARGS) + 1):] == [str(layout.version("1.0.0")), *AGENT_ARGS], (
        f"a versioned launcher's program must be its resolved path; command={command}")
