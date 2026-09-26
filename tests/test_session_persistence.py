"""Session persistence — contract `context/specs/session-persistence.md`.

One test (or a small group) per requirement, named after its id:

- SP-R1  a docker agent's transcripts are written to host-backed storage;
- SP-R2  host tooling reads a docker agent's transcript where it really is;
- SP-R3  `steer_agent` refuses a session it cannot resume, before changing
         anything;
- SP-R4  a steer never forks the node's branch;
- SP-R5  recreating the container says what it will cost.

Black box throughout: the docker run command (`DockerExecutor.run_args`), the
`steer_agent` MCP tool of a real server process, and the `multiagents` CLI run
as a subprocess. No docker daemon is needed except by the one opt-in docker
variant of SP-R1 (`SP_TEST_DOCKER=1`); elsewhere a fake `docker` binary stands
in, as in `tests/test_subagent_mcp.py`.

Assumptions the contract leaves open, recorded so they can be challenged:

- "a docker node" (SP-R2, SP-R5) is a node of a project whose executor is
  `docker`; a tree node records no executor of its own.
- SP-R1 is asserted as the guarantee, per the decision appended to the
  spec: the container path of the transcript location resolves, through the
  docker run command's mounts, to writable host storage under multiagents'
  state (`MULTIAGENTS_STATE_DIR`), not the user's HOME profile, distinct per
  project and per provider. Either mechanism satisfies it.
- SP-R2's host-backed path for a docker node is the declared directory
  followed through `run_args`'s mounts, the same lookup as SP-R1, so either
  mechanism satisfies it; its layout is never guessed.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import sp_harness as h                                          # noqa: E402
from c1_harness import make_docker_executor                     # noqa: E402
from multiagents.providers import load_providers                # noqa: E402

SID = "sp-session-0001"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _mounts(argv: list[str]) -> list[tuple[Path, Path, bool]]:
    """`(source, destination, read_only)` for every `-v` in a docker argv."""
    out = []
    for flag, value in zip(argv, argv[1:]):
        if flag not in ("-v", "--volume"):
            continue
        parts = value.split(":")
        ro = parts[-1] == "ro" and len(parts) == 3
        out.append((Path(parts[0]), Path(parts[1]), ro))
    return out


def _under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _state_root() -> Path:
    return Path(os.environ["MULTIAGENTS_STATE_DIR"])


def _fake_cli(tmp_path: Path, name: str) -> str:
    """An executable a provider's `bin` can name, so it reads as installed."""
    bindir = tmp_path / "clis"
    bindir.mkdir(exist_ok=True)
    path = bindir / name
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(0o755)
    return str(path)


def _provider(tmp_path: Path, name: str, transcript_dir: str | None) -> dict:
    block = {"bin": _fake_cli(tmp_path, name), "spawn": {"args": ["{prompt}"]},
             "stream": {"format": "ndjson", "rules": []}}
    if transcript_dir is not None:
        block["transcript"] = {"dir": transcript_dir, "glob": "*.jsonl"}
    return block


def _executor(tmp_path: Path, project: str, providers: dict):
    root = tmp_path / project
    root.mkdir(exist_ok=True)
    return make_docker_executor(root, load_providers(providers), network="bridge")


def _mount_at(ex, destination: Path) -> tuple[Path, Path, bool] | None:
    for mount in _mounts(ex.run_args()):
        if mount[1] == destination:
            return mount
    return None


def _host_path(ex, container_path: Path) -> tuple[Path, bool] | None:
    """Where `container_path` really lives on the host, read off the docker run
    command: `(host path, writable)` through the deepest `-v` covering it, or
    None when no mount does and it would sit in the container layer — gone
    with the container. Either mechanism of the decision counts: a private
    home mounted over `~/.claude`, or a mount of the prefix itself."""
    best = None
    for source, destination, read_only in _mounts(ex.run_args()):
        if container_path == destination or _under_lexically(container_path, destination):
            if best is None or len(destination.parts) > len(best[1].parts):
                best = (source, destination, read_only)
    if best is None:
        return None
    source, destination, read_only = best
    return source / container_path.relative_to(destination), not read_only


def _under_lexically(path: Path, root: Path) -> bool:
    return path != root and path.is_relative_to(root)


def _transcript_dir(provider, cwd: Path) -> Path:
    """The provider's declared transcript directory for a session run in `cwd`,
    as the container sees it (the executor keeps identical paths)."""
    from multiagents.watchdog import transcript_source
    return transcript_source(provider, cwd)[0]


def _backed(ex, container_path: Path) -> Path:
    """The SP-R1 guarantee for one path: it resolves to writable host storage
    under multiagents' state, and not to the user's own HOME profile."""
    resolved = _host_path(ex, container_path)
    assert resolved is not None, (
        f"{container_path} is covered by no mount: a transcript written there "
        f"lives in the container layer and dies with the container. Mounts: "
        f"{_mounts(ex.run_args())}")
    host, writable = resolved
    assert writable, f"{container_path} resolves to a read-only mount ({host})"
    assert host != container_path, (
        f"{container_path} is mounted from the user's own path: that is the host "
        f"profile, not multiagents' state")
    profile = Path.home() / container_path.relative_to(Path.home()).parts[0]
    assert not _under(host, profile), (
        f"{container_path} resolves to {host}, inside the user's HOME profile {profile}")
    assert _under(host, _state_root()), (
        f"{container_path} resolves to {host}, outside multiagents' state {_state_root()}")
    return host


def _worktree(tmp_path: Path, project: str, agent_id: str = "ag-sp1wt") -> Path:
    from multiagents.paths import ProjectPaths
    return ProjectPaths(tmp_path / project).worktree(agent_id)


def _loaded(providers: dict, name: str):
    return load_providers(providers)[name]


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    home = tmp_path / "userhome"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


# ---------------------------------------------------------------------------
# SP-R1 — a docker agent's transcript lands on host-backed storage
#
# Asserted as the decision of 2026-09-26 words it: the container path of the
# declared transcript location resolves, through the docker run command, to
# host storage under multiagents' state that is not the user's HOME profile,
# and is distinct per project and per provider. HOW (a container-private home
# over the profile, or a mount of the prefix) is the executor's business.
# ---------------------------------------------------------------------------

def test_sp_r1_run_args_back_the_static_prefix_of_a_declared_transcript_dir(tmp_path, home):
    providers = {"fakecli": _provider(tmp_path, "fakecli", "~/.fakecli/sessions/{slug}")}
    ex = _executor(tmp_path, "proj", providers)
    _backed(ex, home / ".fakecli" / "sessions")
    _backed(ex, _transcript_dir(_loaded(providers, "fakecli"), _worktree(tmp_path, "proj")))


def test_sp_r1_prefix_stops_at_the_first_component_holding_a_placeholder(tmp_path, home):
    """`~/.fakecli/{slug}/sessions`: the static part is `~/.fakecli`, and every
    session directory below it, whatever the slug, is backed."""
    providers = {"fakecli": _provider(tmp_path, "fakecli", "~/.fakecli/{slug}/sessions")}
    ex = _executor(tmp_path, "proj", providers)
    _backed(ex, home / ".fakecli")
    for agent_id in ("ag-sp1a", "ag-sp1b"):
        _backed(ex, _transcript_dir(_loaded(providers, "fakecli"),
                                    _worktree(tmp_path, "proj", agent_id)))


def test_sp_r1_a_dir_without_placeholder_is_backed_whole(tmp_path, home):
    ex = _executor(tmp_path, "proj", {"fakecli": _provider(
        tmp_path, "fakecli", "~/.fakecli/sessions")})
    _backed(ex, home / ".fakecli" / "sessions")


def test_sp_r1_no_mount_for_a_provider_that_declares_no_transcript(tmp_path, home):
    bare = _executor(tmp_path, "proj", {})
    ex = _executor(tmp_path, "proj", {"quiet": _provider(tmp_path, "quiet", None)})
    before = set(_mounts(bare.run_args()))
    extra = set(_mounts(ex.run_args())) - before
    # The provider's own binary is mounted read-only at its path; nothing else.
    binary = Path(_fake_cli(tmp_path, "quiet"))
    assert all(src == dst == binary for src, dst, _ in extra), (
        f"a provider with no transcript declaration gained mounts: {extra}")


def test_sp_r1_the_location_is_per_project(tmp_path, home):
    providers = {"fakecli": _provider(tmp_path, "fakecli", "~/.fakecli/sessions/{slug}")}
    provider = _loaded(providers, "fakecli")
    a = _backed(_executor(tmp_path, "proj-a", providers),
                _transcript_dir(provider, _worktree(tmp_path, "proj-a")))
    b = _backed(_executor(tmp_path, "proj-b", providers),
                _transcript_dir(provider, _worktree(tmp_path, "proj-b")))
    assert a != b, f"two projects' sessions resolve to one directory: {a}"


def test_sp_r1_the_location_is_per_provider(tmp_path, home):
    providers = {"one": _provider(tmp_path, "one", "~/.one/sessions/{slug}"),
                 "two": _provider(tmp_path, "two", "~/.two/sessions/{slug}")}
    ex = _executor(tmp_path, "proj", providers)
    cwd = _worktree(tmp_path, "proj")
    a = _backed(ex, _transcript_dir(_loaded(providers, "one"), cwd))
    b = _backed(ex, _transcript_dir(_loaded(providers, "two"), cwd))
    assert a != b and not _under(a, b) and not _under(b, a), (
        f"two providers' sessions share one store: {a} and {b}")


def test_sp_r1_a_prefix_inside_a_private_home_is_backed(tmp_path, home):
    """The first mechanism, for a provider other than claude: its transcript
    prefix lies inside its own container-private home."""
    block = _provider(tmp_path, "fakecli", "~/.fakecli/sessions/{slug}")
    block["container_private_home"] = [".fakecli"]
    providers = {"fakecli": block}
    ex = _executor(tmp_path, "proj", providers)
    _backed(ex, _transcript_dir(_loaded(providers, "fakecli"), _worktree(tmp_path, "proj")))


def test_sp_r1_the_shipped_claude_provider_backs_its_projects_dir(tmp_path, home):
    """Guard for today's path: claude declares `~/.claude/projects/{slug}`,
    inside its host-backed container-private home `~/.claude`. That home is
    multiagents' own container profile (shared across projects by default,
    `credential_scope`), never the user's `~/.claude`; the per-project split
    comes from the slug, which names the worktree. And no second mount is laid
    over a prefix the private home already backs."""
    from multiagents.config import load as load_config
    from multiagents.paths import ProjectPaths

    (tmp_path / "proj").mkdir()
    shipped = load_config(ProjectPaths(tmp_path / "proj"), seed=False).providers
    claude = dict(shipped["claude"])
    claude["bin"] = _fake_cli(tmp_path, "claude")
    (home / ".claude").mkdir()
    (home / ".claude" / ".credentials.json").write_text("{}")
    provider = _loaded({"claude": claude}, "claude")

    ex_a = _executor(tmp_path, "proj", {"claude": claude})
    ex_b = _executor(tmp_path, "proj-b", {"claude": claude})
    _backed(ex_a, home / ".claude" / "projects")
    a = _backed(ex_a, _transcript_dir(provider, _worktree(tmp_path, "proj")))
    b = _backed(ex_b, _transcript_dir(provider, _worktree(tmp_path, "proj-b")))
    assert a != b, f"two projects' claude sessions resolve to one directory: {a}"
    destinations = [m[1] for m in _mounts(ex_a.run_args())]
    assert home / ".claude" / "projects" not in destinations, (
        "a second mount was laid over ~/.claude/projects, which the private home "
        "already backs")


def test_sp_r1_docker_variant_a_transcript_survives_container_recreation(tmp_path, monkeypatch):
    why = _docker_ready()
    if why:
        pytest.skip(why)
    proj = h.Project(tmp_path, monkeypatch, executor="docker")
    # The real daemon, not the harness's fake.
    monkeypatch.setenv("PATH", os.environ["PATH"].split(os.pathsep, 1)[1])
    worktree = proj.root / "wt"
    target = proj.host_transcript_dir(worktree) / f"{SID}.jsonl"
    container = f"multiagents-{proj.paths.slug}"
    try:
        up = proj.cli("docker", "up", timeout=600)
        assert up.returncode == 0, up.stdout + up.stderr
        wrote = subprocess.run(
            ["docker", "exec", container, "sh", "-c",
             f'mkdir -p "$(dirname "$0")" && echo kept > "$0"', str(target)],
            capture_output=True, text=True)
        assert wrote.returncode == 0, wrote.stderr
        rm = proj.cli("docker", "rm", "--force")
        assert rm.returncode == 0, rm.stdout + rm.stderr
        up = proj.cli("docker", "up", timeout=600)
        assert up.returncode == 0, up.stdout + up.stderr
        read = subprocess.run(["docker", "exec", container, "cat", str(target)],
                              capture_output=True, text=True)
        assert read.stdout.strip() == "kept", (
            f"the transcript did not survive `docker rm && docker up`: {read.stderr}")
    finally:
        subprocess.run(["docker", "rm", "-f", container], capture_output=True)
        proj.cleanup()


def _docker_ready() -> str:
    import shutil
    if os.environ.get("SP_TEST_DOCKER") != "1":
        return "docker cases are opt-in: set SP_TEST_DOCKER=1 with a working container setup"
    if not shutil.which("docker"):
        return "no docker binary"
    if subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
        return "docker daemon not reachable"
    return ""


# ---------------------------------------------------------------------------
# SP-R3 — steer refuses a session it cannot resume, before changing anything
# ---------------------------------------------------------------------------

@pytest.fixture
def local(tmp_path, monkeypatch):
    proj = h.Project(tmp_path, monkeypatch)
    yield proj
    proj.cleanup()


def _steer(proj, agent_id: str, message: str = "carry on") -> dict:
    server = proj.server()
    try:
        return server.call("steer_agent", 90, agent_id=agent_id, message=message)
    finally:
        server.close()


def _snapshot(proj) -> tuple[set[str], set[str]]:
    return h.branches(proj.root), h.worktrees(proj.root)


def test_sp_r3_missing_session_is_refused_and_nothing_changes(local):
    node = local.finished_node("ag-sp3a01", SID)
    before = _snapshot(local)
    result = _steer(local, node.id)
    looked = local.host_transcript_dir(Path(node.worktree))
    assert isinstance(result, dict) and result.get("steered") is False, result
    error = str(result.get("error", ""))
    assert SID in error, f"the error does not name the session id: {error!r}"
    assert str(looked) in error, f"the error does not name where it looked ({looked}): {error!r}"
    assert "fresh" in error.lower() or "start_agent" in error, \
        f"the error does not suggest a fresh run: {error!r}"
    assert local.invocations() == [], "a process was launched for an unresumable session"
    after = local.node(node.id)
    assert (after.status, after.reason) == ("done", "finished"), local.describe(node.id)
    assert _snapshot(local) == before, "branches or worktrees changed"


def test_sp_r3_missing_session_and_missing_worktree_creates_no_branch_or_worktree(local):
    node = local.finished_node("ag-sp3b01", SID)
    local.remove_worktree(node.id)
    before = _snapshot(local)
    result = _steer(local, node.id)
    assert result.get("steered") is False, result
    assert SID in str(result.get("error", "")), result
    assert local.invocations() == []
    assert _snapshot(local) == before, (
        f"the refusal still cut a branch or worktree: {_snapshot(local)} vs {before}")
    assert not Path(node.worktree).exists()
    assert local.status(node.id) == "done"


def test_sp_r3_a_session_in_another_worktrees_directory_does_not_count(local):
    """The check is for THIS node's session where it should be, not any file
    with that name: the directory is keyed by the node's worktree."""
    node = local.finished_node("ag-sp3c01", SID)
    local.write_session(local.host_transcript_dir(local.root), SID)
    result = _steer(local, node.id)
    assert result.get("steered") is False, result
    assert local.invocations() == []


def test_sp_r3_present_session_is_resumed(local):
    """Control: the check must not refuse a session that is there."""
    node = local.finished_node("ag-sp3d01", SID)
    local.write_session(local.host_transcript_dir(Path(node.worktree)), SID)
    result = _steer(local, node.id)
    assert result.get("steered") is True, result
    assert local.resumed(SID), f"the session was not resumed: {local.invocations()}"


def test_sp_r3_provider_without_transcript_declaration_is_unaffected(tmp_path, monkeypatch):
    """Control: no declaration, no check — the steer goes ahead as before."""
    proj = h.Project(tmp_path, monkeypatch, transcript=False)
    try:
        node = proj.finished_node("ag-sp3e01", SID)
        result = _steer(proj, node.id)
        assert result.get("steered") is True, result
        assert proj.resumed(SID)
    finally:
        proj.cleanup()


# ---------------------------------------------------------------------------
# SP-R4 — a steer never forks the node's branch
# ---------------------------------------------------------------------------

def test_sp_r4_missing_worktree_is_recreated_on_the_same_branch_with_its_commit(local):
    node = local.finished_node("ag-sp4a01", SID)
    commit = local.commit_of(node.id)
    local.write_session(local.host_transcript_dir(Path(node.worktree)), SID)
    local.remove_worktree(node.id)
    before = h.branches(local.root)

    result = _steer(local, node.id)

    assert result.get("steered") is True, result
    assert h.branches(local.root) == before, (
        f"the steer created branches {h.branches(local.root) - before}")
    assert not any(b.startswith(node.branch + "-") for b in h.branches(local.root))
    after = local.node(node.id)
    assert after.branch == node.branch, f"node moved to branch {after.branch!r}"
    worktree = Path(after.worktree)
    assert worktree.is_dir(), f"no worktree at {worktree}"
    head = h.git(worktree, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    assert head == node.branch, f"worktree is on {head!r}, not {node.branch!r}"
    assert h.git(worktree, "merge-base", "--is-ancestor", commit, "HEAD",
                 check=False).returncode == 0, "the node's commit is not in the worktree"
    assert (worktree / "work.txt").read_text() == f"work of {node.id}\n"
    assert local.resumed(SID)


def test_sp_r4_deleted_branch_is_refused_with_that_reason(local):
    node = local.finished_node("ag-sp4b01", SID)
    local.write_session(local.host_transcript_dir(Path(node.worktree)), SID)
    local.remove_worktree(node.id)
    h.git(local.root, "branch", "-D", node.branch)
    before = _snapshot(local)

    result = _steer(local, node.id)

    assert result.get("steered") is False, result
    error = str(result.get("error", ""))
    assert node.branch in error, f"the error does not name the missing branch: {error!r}"
    assert _snapshot(local) == before, (
        f"a branch or worktree was created: {_snapshot(local)} vs {before}")
    assert local.invocations() == [], "a process was launched"
    assert local.status(node.id) == "done"


# ---------------------------------------------------------------------------
# SP-R2 — host tooling reads a docker node's transcript where it really is
# ---------------------------------------------------------------------------

def test_sp_r2_local_node_resolves_the_declared_host_path(local):
    """The local executor is unchanged: the declared dir, expanded on the host."""
    from multiagents.watchdog import transcript_source
    from multiagents.transcripts import session_transcript

    provider = local.docker_executor().providers["svstub"]
    worktree = local.paths.worktree("ag-sp2a01")
    directory, pattern = transcript_source(provider, worktree)
    assert directory == local.host_transcript_dir(worktree)
    assert pattern == "*.jsonl"
    assert session_transcript(provider, worktree, SID) == \
        local.host_transcript_dir(worktree) / f"{SID}.jsonl"


@pytest.fixture
def docker(tmp_path, monkeypatch):
    proj = h.Project(tmp_path, monkeypatch, executor="docker")
    yield proj
    proj.cleanup()


def _docker_session_dir(proj, worktree: Path) -> Path:
    """Where a docker agent in `worktree` really writes its session: the
    declared directory as the container sees it, followed through the docker
    run command's mounts (either SP-R1 mechanism) to host storage."""
    container_dir = proj.user_home / h.TRANSCRIPT_PREFIX / h.slug(worktree)
    ex = proj.docker_executor()
    assert _host_path(ex, container_dir) is not None, (
        f"the docker executor backs no transcript directory for the stub provider "
        f"(SP-R1): {container_dir} is covered by no mount, so there is no "
        f"host-backed path to read")
    return _backed(ex, container_dir)


def test_sp_r2_docker_node_steer_looks_in_the_host_backed_directory(docker):
    node = docker.finished_node("ag-sp2b01", SID)
    # A session file at the HOST's own declared location must not satisfy a
    # docker node: the agent never wrote there.
    docker.write_session(docker.host_transcript_dir(Path(node.worktree)), SID)
    result = _steer(docker, node.id)
    assert result.get("steered") is False, (
        f"a docker node was resumed on the strength of a host-profile file: {result}")
    error = str(result.get("error", ""))
    expected = _docker_session_dir(docker, Path(node.worktree))
    assert str(expected) in error, f"expected the host-backed {expected} in {error!r}"
    assert str(docker.user_home / h.TRANSCRIPT_PREFIX) not in error


def test_sp_r2_docker_node_session_at_the_host_backed_path_is_resumed(docker):
    node = docker.finished_node("ag-sp2c01", SID)
    docker.write_session(_docker_session_dir(docker, Path(node.worktree)), SID)
    result = _steer(docker, node.id)
    assert result.get("steered") is True, result
    assert h.wait_until(lambda: docker.resumed(SID), 15), (
        f"the session was not resumed: {docker.invocations()}")


# ---------------------------------------------------------------------------
# SP-R5 — recreating the container says what it will cost
# ---------------------------------------------------------------------------

ACTIVE = {"ag-sp5run": "running", "ag-sp5det": "detached", "ag-sp5stk": "stuck"}
ENDED = {"ag-sp5don": "done", "ag-sp5fai": "failed"}


@pytest.fixture
def cli_project(tmp_path, monkeypatch):
    made = []

    def make(drift: bool = False):
        base = tmp_path / f"p{len(made)}"
        base.mkdir()
        proj = h.CliProject(base, monkeypatch, drift=drift)
        made.append(proj)
        return proj
    yield make
    for proj in made:
        proj.close()


def _seed(proj, nodes: dict[str, str]) -> None:
    for agent_id, status in nodes.items():
        proj.seed(agent_id, status)


def test_sp_r5_docker_rm_refuses_while_agents_are_inside_and_lists_them(cli_project):
    proj = cli_project()
    _seed(proj, {**ACTIVE, **ENDED})
    result = proj.cli("docker", "rm")
    out = result.stdout + result.stderr
    assert result.returncode != 0, f"`docker rm` proceeded with agents inside:\n{out}"
    for agent_id in ACTIVE:
        assert agent_id in out, f"{agent_id} not listed:\n{out}"
    for agent_id in ENDED:
        assert agent_id not in out, f"ended node {agent_id} listed as at risk:\n{out}"
    assert "multiagents stop" in out, f"the refusal does not suggest `multiagents stop`:\n{out}"
    assert not h.removed_container(proj.docker_log, proj.container), \
        "the container was removed anyway"


def test_sp_r5_docker_rm_force_proceeds(cli_project):
    proj = cli_project()
    _seed(proj, ACTIVE)
    result = proj.cli("docker", "rm", "--force")
    assert result.returncode == 0, result.stdout + result.stderr
    assert h.removed_container(proj.docker_log, proj.container), \
        f"`--force` did not remove the container: {h.docker_calls(proj.docker_log)}"


def test_sp_r5_docker_rm_with_only_ended_nodes_behaves_as_today(cli_project):
    proj = cli_project()
    _seed(proj, ENDED)
    result = proj.cli("docker", "rm")
    assert result.returncode == 0, result.stdout + result.stderr
    assert h.removed_container(proj.docker_log, proj.container)


@pytest.mark.parametrize("status", sorted(set(ACTIVE.values())))
def test_sp_r5_each_active_status_alone_blocks_docker_rm(cli_project, status):
    proj = cli_project()
    proj.seed("ag-sp5one", status)
    result = proj.cli("docker", "rm")
    assert result.returncode != 0, f"a {status} node did not block `docker rm`"
    assert "ag-sp5one" in result.stdout + result.stderr
    assert not h.removed_container(proj.docker_log, proj.container)


def _drift_messages(proj) -> dict[str, str]:
    """The mount-drift refusal from both places that give it: `run`'s
    preflight (its `executor` section; the tree it prints above that lists
    every node and proves nothing) and `docker up`."""
    run = proj.cli("run", "--no-launch")
    up = proj.cli("docker", "up")
    run_out = run.stdout + run.stderr
    marker = run_out.find("\nexecutor")
    return {"run": run_out[marker:] if marker >= 0 else "",
            "docker up": up.stdout + up.stderr}


def test_sp_r5_drift_refusal_with_agents_inside_says_stop_first(cli_project):
    proj = cli_project(drift=True)
    _seed(proj, {**ACTIVE, **ENDED})
    messages = _drift_messages(proj)
    for agent_id, status in ACTIVE.items():
        assert proj.tree.get(agent_id).status == status, (
            f"fixture: {agent_id} was reconciled away to {proj.tree.get(agent_id).status}")
    for where, out in messages.items():
        assert "docker rm" in out, f"{where}: no drift refusal at all:\n{out}"
        for agent_id in ACTIVE:
            assert agent_id in out, f"{where}: {agent_id} not listed:\n{out}"
        for agent_id in ENDED:
            assert agent_id not in out, f"{where}: ended {agent_id} listed as at risk:\n{out}"
        assert "multiagents stop" in out, f"{where}: does not say to stop first:\n{out}"
        assert out.index("multiagents stop") < out.index("docker rm"), (
            f"{where}: `multiagents stop` must come before `docker rm`:\n{out}")


def test_sp_r5_drift_refusal_without_agents_keeps_todays_prescription(cli_project):
    proj = cli_project(drift=True)
    _seed(proj, ENDED)
    for where, out in _drift_messages(proj).items():
        assert "multiagents docker rm && multiagents docker up" in out, \
            f"{where}: today's prescription is gone:\n{out}"
        assert "multiagents stop" not in out, \
            f"{where}: tells the user to stop agents when none are inside:\n{out}"
        for agent_id in ENDED:
            assert agent_id not in out, f"{where}: ended {agent_id} listed as at risk:\n{out}"
