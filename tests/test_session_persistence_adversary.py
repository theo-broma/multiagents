"""Adversary tests for session-persistence (SP-R1..SP-R5).

Contract: context/specs/session-persistence.md.
Attacks the implementation merged in cc00870 against edge cases, boundary inputs,
stale states, path traversals, and concurrency.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import sp_harness as h
from c1_harness import make_docker_executor
from multiagents.providers import load_providers

SID = "sp-adv-session-0001"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def wait_until(predicate, timeout: float = 15.0, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def _mounts(argv: list[str]) -> list[tuple[Path, Path, bool]]:
    out = []
    for flag, value in zip(argv, argv[1:]):
        if flag not in ("-v", "--volume"):
            continue
        parts = value.split(":")
        ro = parts[-1] == "ro" and len(parts) == 3
        out.append((Path(parts[0]), Path(parts[1]), ro))
    return out


def _fake_cli(tmp_path: Path, name: str) -> str:
    bindir = tmp_path / "clis"
    bindir.mkdir(exist_ok=True)
    path = bindir / name
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(0o755)
    return str(path)


def _provider(tmp_path: Path, name: str, transcript_dir: str | None) -> dict:
    block = {
        "bin": _fake_cli(tmp_path, name),
        "spawn": {"args": ["{prompt}"]},
        "stream": {"format": "ndjson", "rules": []},
    }
    if transcript_dir is not None:
        block["transcript"] = {"dir": transcript_dir, "glob": "*.jsonl"}
    return block


def _executor(tmp_path: Path, project: str, providers: dict):
    root = tmp_path / project
    root.mkdir(exist_ok=True)
    return make_docker_executor(root, load_providers(providers), network="bridge")


def _steer(proj, agent_id: str, message: str = "carry on") -> dict:
    server = proj.server()
    try:
        return server.call("steer_agent", 90, agent_id=agent_id, message=message)
    finally:
        server.close()


def _snapshot(proj) -> tuple[set[str], set[str], dict[str, Any]]:
    return (
        h.branches(proj.root),
        h.worktrees(proj.root),
        proj.tree.read()["nodes"],
    )


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    home = tmp_path / "userhome"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


@pytest.fixture
def local(tmp_path, monkeypatch):
    proj = h.Project(tmp_path, monkeypatch)
    yield proj
    proj.cleanup()


@pytest.fixture
def docker(tmp_path, monkeypatch):
    proj = h.Project(tmp_path, monkeypatch, executor="docker")
    yield proj
    proj.cleanup()


# ---------------------------------------------------------------------------
# SP-R3: runner.steer pre-check attacks
# ---------------------------------------------------------------------------

def test_sp_r3_empty_session_file_is_refused(local):
    """Attack: session file is present on disk but 0 bytes (empty).

    SP-R3 requires steer_agent to refuse a session it cannot resume before
    changing anything. An empty transcript has no conversation to resume;
    resuming it causes the CLI to fail with 'No conversation found'.
    The pre-check in _missing_session uses path.is_file() which evaluates to
    True on a 0-byte file, causing steer to proceed, stop the agent, mutate
    status and launch an unresumable run.
    """
    node = local.finished_node("ag-sp3emp", SID)
    session_file = local.write_session(
        local.host_transcript_dir(Path(node.worktree)), SID
    )
    session_file.write_text("")
    assert session_file.stat().st_size == 0

    before_branches, before_worktrees, before_nodes = _snapshot(local)
    result = _steer(local, node.id)

    assert result.get("steered") is False, (
        f"steer proceeded on a 0-byte session file: {result}"
    )
    assert SID in str(result.get("error", ""))
    assert local.invocations() == [], "process launched for unresumable 0-byte session"
    assert local.status(node.id) == "done"
    assert _snapshot(local) == (before_branches, before_worktrees, before_nodes)


def test_sp_r3_unterminated_corrupt_session_file_is_refused(local):
    """Attack: session file contains truncated/unterminated JSON.

    A crashed turn may leave a half-written JSON line without newline or
    closing bracket. Attempting to resume this corrupt file fails inside the
    CLI. _missing_session must verify the session transcript is actually
    valid and resumable, not merely that a file exists.
    """
    node = local.finished_node("ag-sp3unterm", SID)
    session_file = local.write_session(
        local.host_transcript_dir(Path(node.worktree)), SID
    )
    session_file.write_text('{"type": "user", "sessionId": "' + SID + '", "prompt":')

    before_branches, before_worktrees, before_nodes = _snapshot(local)
    result = _steer(local, node.id)

    assert result.get("steered") is False, (
        f"steer proceeded on an unterminated session file: {result}"
    )
    assert local.invocations() == []
    assert local.status(node.id) == "done"
    assert _snapshot(local) == (before_branches, before_worktrees, before_nodes)


@pytest.mark.parametrize("status", ["failed", "cancelled", "stuck", "detached"])
def test_sp_r3_refusal_mutates_nothing_on_non_done_statuses(local, status):
    """SP-R3 pre-check on every non-running status: failed, cancelled, stuck, detached.

    When the session file is missing, steer must refuse with steered: False,
    and nothing mutates (node status, branches, worktrees, tree.json).
    """
    node = local.finished_node(f"ag-sp3{status[:3]}", SID, status=status, reason="initial")
    before_branches, before_worktrees, before_nodes = _snapshot(local)

    result = _steer(local, node.id)

    assert result.get("steered") is False
    assert SID in str(result.get("error", ""))
    assert local.invocations() == []
    after_node = local.node(node.id)
    assert after_node.status == status
    assert after_node.reason == "initial"
    assert _snapshot(local) == (before_branches, before_worktrees, before_nodes)


def test_sp_r3_symlinked_worktree_slug_resolution(local):
    """Attack: worktree path accessed through a symlink.

    transcript_source uses str(cwd) rather than canonicalizing the path.
    If the agent runs in a symlinked worktree, Claude Code resolves cwd with
    realpath and writes to the realpath slug, but transcript_source looks
    under the symlink slug, falsely reporting the session missing.
    """
    node = local.finished_node("ag-sp3symwt", SID)
    real_wt = Path(node.worktree)
    sym_wt = local.base / "symlink_worktree"
    sym_wt.symlink_to(real_wt)

    # Transcript written by CLI under realpath slug:
    real_session_dir = local.host_transcript_dir(real_wt)
    local.write_session(real_session_dir, SID)

    # Point node.worktree at the symlink:
    local.tree.update(node.id, worktree=str(sym_wt))

    result = _steer(local, node.id)
    assert result.get("steered") is True, (
        f"steer failed to resolve session in symlinked worktree: {result}"
    )


# ---------------------------------------------------------------------------
# SP-R4: worktree reattach attacks
# ---------------------------------------------------------------------------

def test_sp_r4_stale_non_git_dir_at_worktree_path_is_reattached(local):
    """Attack: worktree was removed from git, but its path is occupied by a
    stale non-git directory.

    In runner.py:
        if workdir is None or not workdir.is_dir():
    If workdir.is_dir() is True, runner.py assumes the worktree is already
    checked out and skips attach_worktree! The agent is launched in a non-git
    directory, completely detached from the node's branch and commits.
    SP-R4 requires: 'When a steered node's worktree is missing, the worktree is
    recreated on the node's existing branch, with its commits intact.'
    """
    node = local.finished_node("ag-sp4stale", SID)
    local.write_session(local.host_transcript_dir(Path(node.worktree)), SID)
    local.remove_worktree(node.id)
    assert not Path(node.worktree).exists()

    # Recreate worktree path as an ordinary non-git directory:
    stale_dir = Path(node.worktree)
    stale_dir.mkdir(parents=True)
    (stale_dir / "leftover.txt").write_text("not a git worktree")

    result = _steer(local, node.id)

    assert result.get("steered") is True, result
    # The directory MUST be an attached git worktree on node.branch:
    assert (stale_dir / ".git").exists(), (
        f"worktree at {stale_dir} is not a git worktree: attach_worktree was bypassed"
    )
    head = h.git(stale_dir, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    assert head == node.branch, f"worktree is on {head!r}, expected {node.branch!r}"
    assert (stale_dir / "work.txt").read_text() == f"work of {node.id}\n"
    assert str(stale_dir) in h.worktrees(local.root)


def test_sp_r4_empty_branch_never_creates_suffixed_branch(local):
    """Attack: a steered node whose branch field was cleared/empty (e.g. truncated
    writes: false agent per bug-97a0c7).

    In runner.py line 2874:
        if branch:
            ...
        else:
            branch = gitops.create_worktree(...)
    create_worktree calls unique_branch, which cuts branch-2 off base!
    SP-R4 explicitly mandates: 'It never cuts a new, suffixed branch off base for
    an existing node.'
    """
    node = local.finished_node("ag-sp4emptybr", SID)
    local.write_session(local.host_transcript_dir(Path(node.worktree)), SID)
    local.remove_worktree(node.id)

    # An existing branch for this agent already exists in git:
    existing_branch = node.branch
    assert h.git(local.root, "rev-parse", "--verify", existing_branch).returncode == 0

    # Simulate node whose branch record was cleared in tree:
    local.tree.update(node.id, branch="")

    result = _steer(local, node.id)

    all_branches = h.branches(local.root)
    suffixed = [b for b in all_branches if b.endswith("-2")]
    assert not suffixed, (
        f"steer cut a suffixed branch {suffixed} off base, violating SP-R4"
    )


def test_sp_r4_branch_checked_out_elsewhere_refuses_without_forking(local):
    """Attack: branch exists but is checked out in another worktree.

    steer must refuse because the branch cannot be checked out at two places,
    and it must never cut a suffixed branch off base instead.
    """
    node = local.finished_node("ag-sp4else", SID)
    local.write_session(local.host_transcript_dir(Path(node.worktree)), SID)
    local.remove_worktree(node.id)

    # Check out the branch in a different directory:
    other_wt = local.base / "other_wt"
    h.git(local.root, "worktree", "add", str(other_wt), node.branch)
    before_branches = h.branches(local.root)

    result = _steer(local, node.id)

    assert result.get("steered") is False, (
        f"steer should refuse when branch is checked out elsewhere: {result}"
    )
    assert h.branches(local.root) == before_branches, "branches changed"
    assert not any(b.endswith("-2") for b in h.branches(local.root))


# ---------------------------------------------------------------------------
# SP-R1 / SP-R2: transcript resolution attacks
# ---------------------------------------------------------------------------

def _assert_escape_refused(tmp_path, home, capfd, name: str, declared: str,
                           escaped: Path) -> None:
    """SP-R1, decided 2026-09-26 and after ag-7697c2: a transcript dir whose
    '..' takes it out of a container-private home is refused. Logged, never
    mounted, no host path for it, and the provider reads as declaring no
    transcript location at all."""
    from multiagents.watchdog import transcript_source

    block = _provider(tmp_path, name, declared)
    block["container_private_home"] = [".claude"]
    providers = {name: block}
    ex = _executor(tmp_path, "proj", providers)
    provider = load_providers(providers)[name]
    worktree = tmp_path / "proj"

    # Never mounted: no store for this provider's transcripts, and nothing in
    # the run arguments over the escaped path or a directory holding it
    # below HOME.
    assert ex.transcript_state(name) == {}, (
        f"refused transcript dir {declared!r} was given a store: "
        f"{ex.transcript_state(name)}"
    )
    covering = [dst for _, dst, _ in _mounts(ex.run_args())
                if (dst == escaped or dst in escaped.parents) and home in dst.parents]
    assert not covering, f"{escaped} is mounted via {covering}"

    # No host path: the escaped path is not relocated into multiagents state.
    state = Path(os.environ["MULTIAGENTS_STATE_DIR"]).resolve()
    resolved = ex.host_path(escaped)
    assert ".." not in resolved.parts, f"{resolved} carries an unnormalised '..'"
    assert state not in resolved.resolve().parents, (
        f"refused path {escaped} was given a host store at {resolved}"
    )

    # Treated as declaring no transcript location, so a reader never looks in
    # the user's host HOME for what the container wrote.
    for executor in (None, ex):
        source = transcript_source(provider, worktree, executor)
        assert source is None, (
            f"refused transcript dir {declared!r} still resolves to {source} "
            f"(executor={executor!r})"
        )

    # Logged: the refusal names the declaration.
    err = capfd.readouterr().err
    assert declared in err, (
        f"no refusal reported on stderr for {declared!r}; stderr was: {err!r}"
    )


def test_sp_r1_path_traversal_escaping_private_home_is_refused(tmp_path, home, capfd):
    """'..' before {slug}: '~/.claude/../secret/{slug}' with '.claude' private.

    Normalised, the static prefix is ~/secret — outside the private home it
    names. Refused rather than mounted (amended per the 2026-09-26 decisions;
    the original version of this test required a mount).
    """
    _assert_escape_refused(tmp_path, home, capfd, "escapebefore",
                           "~/.claude/../secret/{slug}", home / "secret" / "slug")


def test_sp_r1_path_traversal_after_slug_is_refused(tmp_path, home, capfd):
    """'..' after {slug}: '~/.claude/{slug}/../../escaped'.

    The static prefix (~/.claude) looks harmless, but every resolved path
    normalises to ~/escaped, outside the private home and in the host's own
    HOME. Any '..' component in a declared transcript dir is refused.
    """
    _assert_escape_refused(tmp_path, home, capfd, "escapeafter",
                           "~/.claude/{slug}/../../escaped", home / "escaped")


def test_sp_r1_unusual_placeholder_root_does_not_mount_over_container_root(tmp_path, home):
    """Attack: transcript.dir = '/{slug}/transcripts'.

    transcript_prefix breaks before '{slug}', leaving prefix = Path('/').
    DockerExecutor.transcript_state computes relative_to('/') as '.', and mounts
    the host transcript directory over '/' in the container:
    -v <host_path>:/:rw.
    This wipes/corrupts the entire container root filesystem!
    """
    providers = {"rootmount": _provider(tmp_path, "rootmount", "/{slug}/transcripts")}
    ex = _executor(tmp_path, "proj", providers)

    t_state = ex.transcript_state()
    assert Path("/") not in t_state, (
        f"DockerExecutor mounted host directory over container root filesystem '/': {t_state}"
    )
    mount_destinations = [dst for _, dst, _ in _mounts(ex.run_args())]
    assert Path("/") not in mount_destinations, (
        "Docker run arguments contain a mount directly over container root '/'"
    )


def test_sp_r1_unusual_placeholder_home_does_not_mount_over_user_home(tmp_path, home):
    """Attack: transcript.dir = '~/{slug}/transcripts'.

    transcript_prefix breaks before '{slug}', leaving prefix = Path.home().
    DockerExecutor.transcript_state checks 'home in prefix.parents' (False, since
    a path is not in its own parents), falls back to relative_to('/'), and
    mounts the host directory over Path.home() (/home/<user>):
    -v <host_path>:/home/<user>:rw.
    This wipes out the entire user HOME in the container!
    """
    providers = {"homemount": _provider(tmp_path, "homemount", "~/{slug}/transcripts")}
    ex = _executor(tmp_path, "proj", providers)

    t_state = ex.transcript_state()
    assert home not in t_state, (
        f"DockerExecutor mounted host directory over entire user HOME {home}: {t_state}"
    )
    mount_destinations = [dst for _, dst, _ in _mounts(ex.run_args())]
    assert home not in mount_destinations, (
        f"Docker run arguments contain a mount directly over user HOME {home}"
    )


def test_sp_r1_unsupported_placeholder_does_not_raise_unhandled_keyerror(tmp_path):
    """Attack: provider declares transcript directory with a placeholder other
    than {slug}, e.g. {agent_id}.

    In watchdog.py:
        path = Path(directory.format(slug=slug)).expanduser()
    Raises an unhandled KeyError: 'agent_id', crashing transcript_source,
    session_transcript, and runner.steer.
    """
    from multiagents.watchdog import transcript_source

    raw = {"custom": _provider(tmp_path, "custom", "~/.custom/{agent_id}/sessions")}
    provider = load_providers(raw)["custom"]
    try:
        source = transcript_source(provider, tmp_path / "worktree")
    except KeyError as exc:
        pytest.fail(f"transcript_source crashed with unhandled KeyError: {exc}")


def test_sp_r2_session_context_honours_executor_for_docker_agent(docker):
    """Attack: transcripts.session_context queries a docker agent's transcript.

    transcripts.session_context calls session_transcript(provider, cwd, session_id)
    with executor=None. For a docker agent, the transcript lives in the
    host-backed store (SP-R1/R2), not the host's own profile. Calling
    session_context without an executor causes it to look in the wrong directory
    and always return None.
    """
    from multiagents.transcripts import session_context

    node = docker.finished_node("ag-sp2ctx", SID)
    # Write session with a context reading (Anthropic format):
    session_dir = docker.user_home / h.TRANSCRIPT_PREFIX / h.slug(Path(node.worktree))
    ex = docker.docker_executor()
    host_session_dir = ex.host_path(session_dir)
    host_session_dir.mkdir(parents=True, exist_ok=True)
    session_file = host_session_dir / f"{SID}.jsonl"
    session_file.write_text(
        json.dumps({
            "type": "assistant",
            "message": {
                "usage": {
                    "input_tokens": 1200,
                    "output_tokens": 300,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                }
            },
        }) + "\n"
    )

    provider = ex.providers["svstub"]
    # Reading context for this docker agent must find the transcript at its host-backed path:
    context = session_context(provider, Path(node.worktree), SID)
    assert context is not None and context > 0, (
        f"session_context returned {context}: it failed to resolve the docker host-backed path"
    )


# ---------------------------------------------------------------------------
# SP-R5: multiagents docker rm / drift refusal attacks
# ---------------------------------------------------------------------------

def test_sp_r5_docker_rm_refuses_with_nested_children_and_force_proceeds(tmp_path, monkeypatch):
    """Attack: tree contains nested child agents (depth > 1) in active states.

    docker rm must list all active agents (both parents and nested children),
    refuse without --force, and proceed with --force.
    """
    base = tmp_path / "proj_nested"
    base.mkdir()
    proj = h.CliProject(base, monkeypatch)
    try:
        # Parent node running
        proj.seed("ag-parent", "running")
        # Nested child node detached
        proj.seed("ag-child", "detached")

        result = proj.cli("docker", "rm")
        out = result.stdout + result.stderr
        assert result.returncode != 0, f"`docker rm` proceeded with nested agents inside:\n{out}"
        assert "ag-parent" in out
        assert "ag-child" in out

        # With --force, it must proceed:
        force_result = proj.cli("docker", "rm", "--force")
        assert force_result.returncode == 0
        assert h.removed_container(proj.docker_log, proj.container)
    finally:
        proj.close()


def test_sp_r4_concurrent_steers_never_fork_branch(local):
    """Attack: two concurrent steers on the same node whose worktree is missing.

    Both callers try to re-attach the worktree and resume the agent.
    SP-R4 states: 'It never cuts a new, suffixed branch off base for an existing node.'
    Neither steer may create a '-N' branch.
    """
    import threading

    node = local.finished_node("ag-sp4conc", SID)
    local.write_session(local.host_transcript_dir(Path(node.worktree)), SID)
    local.remove_worktree(node.id)

    server1 = local.server()
    server2 = local.server()
    results = [None, None]

    def s1():
        results[0] = server1.call("steer_agent", 45, agent_id=node.id, message="msg1")

    def s2():
        results[1] = server2.call("steer_agent", 45, agent_id=node.id, message="msg2")

    t1 = threading.Thread(target=s1)
    t2 = threading.Thread(target=s2)
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    server1.close()
    server2.close()

    all_branches = h.branches(local.root)
    assert not any(b.endswith("-2") for b in all_branches), (
        f"concurrent steers created a suffixed branch: {all_branches}"
    )


def test_sp_r5_drift_message_never_prescribes_refusing_command_when_stopped(tmp_path, monkeypatch):
    """Attack: drift refusal prescription followed by user.

    SP-R5 specifies:
    'The drift refusal must not prescribe a command that would then refuse.
    With such nodes present, it says to run multiagents stop first, then
    docker rm && docker up.'
    After running multiagents stop, docker rm must proceed without being refused.
    """
    base = tmp_path / "proj_prescribe"
    base.mkdir()
    proj = h.CliProject(base, monkeypatch, drift=True)
    try:
        proj.seed("ag-sp5stk", "stuck")
        proj.seed("ag-sp5det", "detached")

        # 1. Drift message prescribes 'multiagents stop' first, then 'docker rm && docker up'
        up = proj.cli("docker", "up")
        up_out = up.stdout + up.stderr
        assert "multiagents stop" in up_out

        # 2. User follows prescribed command: multiagents stop
        stop_res = proj.cli("stop")
        assert stop_res.returncode == 0

        # 3. User runs next prescribed command: multiagents docker rm
        rm_res = proj.cli("docker", "rm")
        assert rm_res.returncode == 0, (
            f"Prescribed command `multiagents docker rm` refused even after `stop`:\n"
            f"{rm_res.stdout}\n{rm_res.stderr}"
        )
    finally:
        proj.close()

