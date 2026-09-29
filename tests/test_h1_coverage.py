"""H1 behaviours that adversary ag-387314's mutations showed no test pinned.

Each test names the requirement in context/specs/h1-host-authority.md it
covers. The surface is Runner's host actions, `reap_pending_branches`, git
and the filesystem, plus the public `HostAuthority` record (`get`, `add`,
`rebind`, `remove_worktree`, `pinned_worktree`) where no host action reaches
the behaviour on its own.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent / "support"))
import c3_harness as h3  # noqa: E402

from multiagents import gitops
from multiagents.config import AgentSpec
from multiagents.executor.docker import DockerExecutor
from multiagents.runner import Runner, reap_pending_branches
from multiagents.tree import Node, Tree

from test_h1_host_authority import (  # noqa: F401
    IDENTITY, commit, exists, forge, git, project, start)


def mismatches(r: Runner, node_id: str) -> list[dict]:
    return [row for row in (json.loads(line) for line in
                            r.paths.events_file.read_text().splitlines())
            if row.get("kind") == "host_authority_mismatch" and row.get("node") == node_id]


def unrecorded(r: Runner, node_id: str, *, branch: str, worktree: Path | str = "",
               status: str = "done", parent: str | None = None) -> Node:
    """A node that appears only in container-written tree.json, after the seed."""
    node = Node(id=node_id, agent="worker", provider="fake", model="m",
                parent=parent, depth=2 if parent else 1, status=status,
                task="forged", branch=branch, worktree=str(worktree))
    r.tree.add(node)
    return node


def head(path: Path) -> str:
    return git(path, "rev-parse", "HEAD")


# --- a project whose tree.json predates the host record (HA-R7) -------------

@pytest.fixture
def legacy(tmp_path, monkeypatch):
    """A git project and tree.json, with no Runner started yet."""
    monkeypatch.setattr(DockerExecutor, "inside", lambda self: False)
    for key, value in IDENTITY.items():
        monkeypatch.setenv(key, value)
    h3.as_root(monkeypatch)
    paths = h3.make_paths(tmp_path)
    h3.make_git_repo(tmp_path)
    git(tmp_path, "branch", "-M", "main")
    (tmp_path / ".gitignore").write_text(".multiagents/\n")
    git(tmp_path, "add", ".gitignore")
    git(tmp_path, "commit", "-qm", "ignore project state")
    return paths, Tree(paths.tree_file, paths.events_file)


def host_runner(paths) -> Runner:
    provider = h3.fake_cli(paths.root.parent, events=[{"type": "result",
        "subtype": "success", "result": "done"}], delay=0.35)
    return Runner(paths, h3.make_config(agents={"worker": AgentSpec("worker", "fake", "m")},
                                        providers={"fake": provider}))


def legacy_node(paths, tree: Tree, node_id: str, *, parent: str | None = None,
                session_id: str = "") -> Node:
    """A pre-upgrade node with a real branch, checkout and one commit."""
    branch = f"agents/worker/{node_id.removeprefix('ag-')}"
    worktree = paths.worktree(node_id)
    gitops.create_worktree(paths.root, worktree, branch, unique=False)
    node = Node(id=node_id, agent="worker", provider="fake", model="m", parent=parent,
                depth=2 if parent else 1, status="done", task="legacy",
                branch=branch, worktree=str(worktree), session_id=session_id)
    tree.add(node)
    commit(node, f"{node_id}.txt")
    return node


def test_ha_r7_seeded_child_is_not_merged_when_its_host_parent_ends(legacy, monkeypatch):
    """A seeded entry authorises no merge of pending children, even when its
    recorded parent is exactly the host node whose run just ended."""
    paths, tree = legacy
    child = legacy_node(paths, tree, "ag-seedch", parent="ag-hostpar")
    runner = host_runner(paths)
    # The host parent gets the id the pre-upgrade child already names, so the
    # seeded record's parent agrees with the ending node and only its seeded
    # provenance stands between it and the merge.
    import multiagents.runner as runner_module
    monkeypatch.setattr(runner_module, "new_id", lambda: "ag-hostpar")

    async def run():
        parent = await start(runner, wait=False)
        assert parent.id == "ag-hostpar"
        with runner.tree.transaction() as data:
            data["nodes"][parent.id].setdefault("children", []).append(child.id)
        await asyncio.wait_for(runner.runs[parent.id].done.wait(), timeout=20)
        return parent

    parent = asyncio.run(run())

    assert not (Path(parent.worktree) / "ag-seedch.txt").exists()
    assert runner.tree.get(child.id).status == "done"
    assert exists(paths.root, child.branch)
    assert Path(child.worktree).is_dir()


def test_ha_r7_host_child_is_not_auto_merged_into_a_seeded_parent(legacy, monkeypatch):
    """No auto-merge (HA-R3) and no pending-children merge (HA-R6) into a
    seeded node's worktree, however the merge is reached."""
    paths, tree = legacy
    seeded = legacy_node(paths, tree, "ag-seedpa", session_id="sess-seed")
    runner = host_runner(paths)
    before = head(Path(seeded.worktree))

    async def spawn_child():
        h3.as_subagent(monkeypatch, agent_id=seeded.id, depth=1, can_spawn=True)
        result = await runner.start("worker", "child of a seeded node")
        assert result.get("agent_id"), result
        child = runner.tree.get(result["agent_id"])
        commit(child, "child.txt")
        await asyncio.wait_for(runner.runs[child.id].done.wait(), timeout=20)
        return child

    child = asyncio.run(spawn_child())
    assert child.parent == seeded.id
    assert head(Path(seeded.worktree)) == before
    assert not (Path(seeded.worktree) / "child.txt").exists()
    assert exists(paths.root, child.branch)
    assert runner.tree.get(child.id).status == "done"

    # The seeded node's own run ending on the host must not pull the waiting
    # child in either.
    h3.as_root(monkeypatch)

    async def steer_seeded():
        result = await runner.steer(seeded.id, "continue")
        run = runner.runs.get(seeded.id)
        if run is not None:
            await asyncio.wait_for(run.done.wait(), timeout=20)
        return result

    asyncio.run(steer_seeded())
    assert head(Path(seeded.worktree)) == before
    assert not (Path(seeded.worktree) / "child.txt").exists()
    assert exists(paths.root, child.branch)


# --- HA-R4 case 1 and case 4 ------------------------------------------------

@pytest.mark.parametrize("finish", ["discard", "merge"])
def test_ha_r4_case4_record_shows_the_host_completion(project, finish):
    node = asyncio.run(start(project))
    commit(node, "work.txt")
    if finish == "discard":
        result = project.discard_agent(node.id, force=True)
        assert result.get("discarded") is True, result
    else:
        result = project.merge_agent(node.id)
        assert result.get("result") == "merged", result
    assert not exists(project.paths.root, node.branch)

    completion = Runner(project.paths, project.config).authority.get(node.id)["completion"]
    assert completion, "the host record must show the completion"
    assert completion["status"] == {"discard": "discarded", "merge": "merged"}[finish]
    assert completion["time"]
    if finish == "merge":
        assert completion["commit"] == head(project.paths.root)
        assert git(project.paths.root, "show", "--stat", "--format=", "HEAD").find(
            "work.txt") >= 0


@pytest.mark.parametrize("tree_branch", ["cleared", "still_named"])
def test_ha_r4_case1_reap_deletes_a_host_completed_branch(project, tree_branch):
    """The host completed the node and its record binds the branch, so a
    pending deletion of that branch is carried out whatever tree.json says
    about the node's current `branch`."""
    node = asyncio.run(start(project))
    assert project.discard_agent(node.id, force=True).get("discarded") is True
    # The deletion did not stick (for instance the ref reappeared from a
    # packed-refs lock); tree.json still asks for it.
    git(project.paths.root, "branch", node.branch, "main")
    forge(project, node, status="discarded", worktree="",
          branch="" if tree_branch == "cleared" else node.branch,
          branch_pending_delete=node.branch)

    reap_pending_branches(project.paths.root, project.tree, project.authority)

    assert not exists(project.paths.root, node.branch)


def test_ha_r4_case1_completion_releases_only_the_recorded_branch(project):
    """A host-completed node's pending deletion naming a branch its record does
    not bind is not case 1: another host node's live branch survives."""
    done = asyncio.run(start(project))
    victim = asyncio.run(start(project))
    assert project.discard_agent(done.id, force=True).get("discarded") is True
    # The victim's checkout is gone (container-writable), so git itself would
    # not refuse to delete its branch; only the host's rule protects it.
    git(project.paths.root, "worktree", "remove", "--force", victim.worktree)
    forge(project, done, status="discarded", worktree="", branch=victim.branch,
          branch_pending_delete=victim.branch)

    reap_pending_branches(project.paths.root, project.tree, project.authority)
    Runner(project.paths, project.config)

    assert exists(project.paths.root, victim.branch)


# --- HA-R2a: refspec-shaped and malformed branch names -----------------------

@pytest.fixture
def remote(project, tmp_path):
    bare = tmp_path.parent / f"{tmp_path.name}-remote.git"
    subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True)
    project.config.project.setdefault("git", {}).update(
        remote=str(bare), push_agent_branches=True)
    return bare


def remote_refs(bare: Path) -> str:
    return subprocess.run(["git", "-C", str(bare), "for-each-ref"],
                          capture_output=True, text=True).stdout


@pytest.mark.parametrize("branch", [
    "agents/x:refs/heads/main",
    "+agents/x:refs/heads/main",
    "agents/x:main",
    "+agents/x",
    "agents/x..y",
    "agents/x.lock",
    "agents/x y",
    "agents/",
])
def test_ha_r2a_push_refuses_a_branch_that_is_not_a_plain_agents_ref(
        project, remote, branch):
    git(project.paths.root, "branch", "agents/x", "main")
    node = unrecorded(project, "ag-refspec", branch=branch)

    result = project.push_branch(node.id)

    assert result.get("pushed") is not True, result
    assert remote_refs(remote) == "", "nothing may reach the remote"
    rows = mismatches(project, node.id)
    assert rows and all("branch" in row.get("fields", []) for row in rows), rows


def test_ha_r2a_push_still_publishes_a_plain_unrecorded_agents_branch(project, remote):
    """Control for the refusals above: a well-formed nested branch is in D."""
    git(project.paths.root, "branch", "agents/x", "main")
    node = unrecorded(project, "ag-plain", branch="agents/x")

    result = project.push_branch(node.id)

    assert result.get("pushed") is True, result
    assert "refs/heads/agents/x" in remote_refs(remote)
    assert "refs/heads/main" not in remote_refs(remote)


# --- a branch in a node's history stays protected after rebind ---------------

def rebound(project) -> tuple[Node, str]:
    """A host node whose record moved to a new branch; the old one still exists."""
    node = asyncio.run(start(project))
    replacement = f"{node.branch}-next"
    git(project.paths.root, "branch", replacement, "main")
    project.authority.rebind(node.id, replacement, Path(node.worktree))
    forge(project, node, branch=replacement)
    # Nothing has the old branch checked out, so git would delete it if asked.
    git(Path(node.worktree), "switch", "-q", "--detach")
    return node, node.branch


def test_ha_r4_previous_branch_of_a_rebound_node_is_never_reaped(project):
    node, old = rebound(project)
    forged = unrecorded(project, "ag-oldref", branch=old, status="discarded")
    forge(project, forged, branch_pending_delete=old)

    reap_pending_branches(project.paths.root, project.tree, project.authority)
    Runner(project.paths, project.config)

    assert exists(project.paths.root, old)


def test_ha_r2a_previous_branch_of_a_rebound_node_is_not_an_unrecorded_operand(
        project, remote):
    node, old = rebound(project)
    forged = unrecorded(project, "ag-oldpush", branch=old)

    result = project.push_branch(forged.id)

    assert result.get("pushed") is not True, result
    assert remote_refs(remote) == ""
    assert any("branch" in row.get("fields", []) for row in mismatches(project, forged.id))


# --- HA-R5: nested rule, inside or around a recorded worktree ----------------

def nested_branch(project, node_id: str) -> str:
    branch = f"agents/worker/{node_id.removeprefix('ag-')}"
    git(project.paths.root, "branch", branch, "main")
    return branch


def test_ha_r5_path_inside_a_recorded_worktree_is_refused(project):
    victim = asyncio.run(start(project))
    inner = Path(victim.worktree) / "sub"
    inner.mkdir()
    (inner / "keep.txt").write_text("keep\n")
    node = unrecorded(project, "ag-inside", branch=nested_branch(project, "ag-inside"),
                      worktree=inner)

    project.discard_agent(node.id, force=True)

    assert (inner / "keep.txt").read_text() == "keep\n"
    assert Path(victim.worktree).is_dir()
    assert any("worktree" in row.get("fields", []) for row in mismatches(project, node.id))


def test_ha_r5_path_containing_a_recorded_worktree_is_refused(project):
    host = asyncio.run(start(project))
    group = project.paths.worktrees / "group"
    held = group / "inner"
    held.mkdir(parents=True)
    (held / "keep.txt").write_text("keep\n")
    project.authority.rebind(host.id, host.branch, held)
    node = unrecorded(project, "ag-around", branch=nested_branch(project, "ag-around"),
                      worktree=group)

    project.discard_agent(node.id, force=True)

    assert (held / "keep.txt").read_text() == "keep\n"
    assert any("worktree" in row.get("fields", []) for row in mismatches(project, node.id))


def test_ha_r5_worktree_root_itself_is_refused(project):
    victim = asyncio.run(start(project))
    root = project.paths.worktrees
    (root / "loose").mkdir()
    (root / "loose" / "keep.txt").write_text("keep\n")
    node = unrecorded(project, "ag-rootwt", branch=nested_branch(project, "ag-rootwt"),
                      worktree=root)

    project.discard_agent(node.id, force=True)

    assert (root / "loose" / "keep.txt").read_text() == "keep\n"
    assert Path(victim.worktree).is_dir()
    assert any("worktree" in row.get("fields", []) for row in mismatches(project, node.id))


def test_ha_r5_dotdot_path_never_reaches_a_recorded_worktree(project):
    """A pathname that walks `..` back onto a recorded checkout is refused at
    the moment of use, both for removal and for a pinned git operation."""
    victim = asyncio.run(start(project))
    (project.paths.worktrees / "sub").mkdir()
    sideways = project.paths.worktrees / "sub" / ".." / victim.id
    assert sideways.resolve() == Path(victim.worktree).resolve()

    assert project.authority.remove_worktree(sideways) is False
    assert project.authority.remove_worktree(sideways, recorded=True) is False
    assert Path(victim.worktree).is_dir()
    assert exists(project.paths.root, victim.branch)

    with pytest.raises((OSError, ValueError)):
        with project.authority.pinned_worktree(sideways):
            pass


def test_ha_r5_remove_worktree_refuses_a_path_it_is_not_authorised_for(project):
    victim = asyncio.run(start(project))
    stray = project.paths.worktree("ag-stray")
    stray.mkdir()
    (stray / "keep.txt").write_text("keep\n")

    # A recorded removal of a path no record holds, and a nested removal of a
    # path a record does hold, are both refused and delete nothing.
    assert project.authority.remove_worktree(stray, recorded=True) is False
    assert (stray / "keep.txt").is_file()
    assert project.authority.remove_worktree(Path(victim.worktree)) is False
    assert Path(victim.worktree).is_dir()
    assert git(Path(victim.worktree), "symbolic-ref", "--short", "HEAD") == victim.branch


def test_ha_r5_removal_does_not_descend_into_another_filesystem(project, monkeypatch):
    """A mount inside a checkout being removed is outside `D`: its contents
    survive the host's removal of the checkout."""
    node = asyncio.run(start(project))
    mount = Path(node.worktree) / "mnt"
    mount.mkdir()
    (mount / "other-device.txt").write_text("not ours\n")
    inode = os.stat(mount).st_ino

    # No privileges to mount here, so report `mnt` as a different device.
    def shifted(real):
        def call(*args, **kwargs):
            result = real(*args, **kwargs)
            if result.st_ino != inode:
                return result
            fields = list(result)
            fields[2] += 1                    # st_dev
            return os.stat_result(fields)
        return call

    for name in ("stat", "lstat", "fstat"):
        monkeypatch.setattr(os, name, shifted(getattr(os, name)))
    with contextlib.suppress(Exception):
        project.discard_agent(node.id, force=True)
    monkeypatch.undo()

    assert (mount / "other-device.txt").read_text() == "not ours\n"


# --- the record: rebind and add ---------------------------------------------

def test_ha_r1_rebind_clears_a_previous_completion(project):
    node = asyncio.run(start(project))
    assert project.discard_agent(node.id, force=True).get("discarded") is True
    assert project.authority.get(node.id)["completion"]
    fresh = f"{node.branch}-again"
    git(project.paths.root, "branch", fresh, "main")

    project.authority.rebind(node.id, fresh, project.paths.worktree(node.id))

    assert project.authority.get(node.id)["completion"] is None
    # The new branch is a host-created branch the host has not completed, so a
    # pending deletion of it is not honoured (HA-R4).
    forge(project, node, status="discarded", branch=fresh, branch_pending_delete=fresh)
    reap_pending_branches(project.paths.root, project.tree, project.authority)
    assert exists(project.paths.root, fresh)


def test_ha_r1_adding_an_existing_id_does_not_overwrite_its_record(project):
    node = asyncio.run(start(project))
    before = project.authority.get(node.id)
    forged = Node(id=node.id, agent="worker", provider="fake", model="m",
                  parent="ag-elsewhere", depth=1, status="running", task="forged",
                  branch="agents/worker/forged", worktree=str(project.paths.worktree("ag-x")))

    with contextlib.suppress(Exception):
        project.authority.add(forged)

    assert project.authority.get(node.id) == before
    assert Runner(project.paths, project.config).authority.get(node.id) == before


# --- HA-R9: an inner id that differs from its key ---------------------------

@pytest.mark.parametrize("action", ["merge_agent", "push_branch", "discard_agent"])
def test_ha_r9_inner_id_mismatch_is_reported_and_nothing_is_mutated(
        project, remote, action):
    victim = asyncio.run(start(project))
    commit(victim, "genuine.txt")
    base = head(project.paths.root)
    with project.tree.transaction() as data:
        data["nodes"][victim.id]["id"] = "ag-ghost-r9"

    with contextlib.suppress(Exception):
        {"merge_agent": lambda: project.merge_agent(victim.id),
         "push_branch": lambda: project.push_branch(victim.id),
         "discard_agent": lambda: project.discard_agent(victim.id, force=True)}[action]()

    rows = mismatches(project, victim.id)
    assert any("id" in row.get("fields", []) for row in rows), rows
    assert head(project.paths.root) == base
    assert not (project.paths.root / "genuine.txt").exists()
    assert exists(project.paths.root, victim.branch)
    assert Path(victim.worktree).is_dir()
    assert remote_refs(remote) == ""
    assert project.authority.get(victim.id)["completion"] is None
