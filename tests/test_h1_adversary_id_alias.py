"""Adversary: tree.json entries whose inner `id` differs from their key.

`Tree.get(key)` builds the Node from the entry's own `id` field, and the host
looks up its record by `node.id`. Both the key and the `id` field are
container-written, so a container can make the host use one node's record
for another node, or no record at all for a host-created node.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import subprocess
import json
from pathlib import Path

from multiagents import cli

from test_h1_host_authority import commit, exists, git, project, start  # noqa: F401


def events(r, kind: str) -> list[dict]:
    return [row for row in (json.loads(line) for line in
                            r.paths.events_file.read_text().splitlines())
            if row.get("kind") == kind]


def test_recorded_node_with_rewritten_inner_id_merges_forged_branch(project):
    """HA-R2/HA-R9: an entry whose inner id differs from its key is a
    mismatch, so merge_agent on it is refused outright. Neither the forged
    branch nor the recorded one lands on main, and the node is not completed."""
    victim = asyncio.run(start(project))
    commit(victim, "genuine.txt")
    base = git(project.paths.root, "rev-parse", "main")

    # Container side: an agents/* branch it made, plus a rewrite of the
    # victim's entry so its inner id no longer names a recorded node.
    evil = "agents/worker/evil-alias"
    git(project.paths.root, "branch", evil, "main")
    evil_checkout = project.paths.worktree("ag-evil-alias")
    git(project.paths.root, "worktree", "add", "-q", str(evil_checkout), evil)
    (evil_checkout / "evil.txt").write_text("forged\n")
    git(evil_checkout, "add", "evil.txt")
    git(evil_checkout, "commit", "-qm", "evil")
    git(project.paths.root, "worktree", "remove", "--force", str(evil_checkout))
    with project.tree.transaction() as data:
        entry = data["nodes"][victim.id]
        entry["id"] = "ag-ghost-alias"
        entry["branch"] = evil
        entry["worktree"] = ""

    result = None
    with contextlib.suppress(Exception):
        result = project.merge_agent(victim.id)

    assert not (project.paths.root / "evil.txt").exists(), result
    assert git(project.paths.root, "rev-parse", "main") == base, result
    assert not (project.paths.root / "genuine.txt").exists(), result
    assert not (result or {}).get("merged"), result
    assert (result or {}).get("result") != "merged", result
    assert project.authority.get(victim.id)["completion"] is None, result
    assert exists(project.paths.root, victim.branch), result
    assert any(row.get("node") == victim.id and "id" in row.get("fields", [])
               for row in events(project, "host_authority_mismatch")), result


def test_recorded_node_with_rewritten_inner_id_pushes_forged_branch(project, tmp_path):
    """HA-R2: push_branch on a host-created node must push the recorded ref."""
    victim = asyncio.run(start(project))
    commit(victim, "genuine.txt")
    evil = "agents/worker/evil-push"
    git(project.paths.root, "branch", evil, "main")
    remote = tmp_path.parent / "alias-remote.git"
    subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
    project.config.project.setdefault("git", {}).update(
        remote=str(remote), push_agent_branches=True)
    with project.tree.transaction() as data:
        data["nodes"][victim.id]["id"] = "ag-ghost-push"
        data["nodes"][victim.id]["branch"] = evil

    result = project.push_branch(victim.id)

    pushed_evil = subprocess.run(["git", "-C", str(remote), "show-ref", "--verify",
        "--quiet", f"refs/heads/{evil}"], capture_output=True).returncode == 0
    assert not pushed_evil, result


def test_clean_uses_inner_id_to_delete_live_host_branch(project):
    """HA-R2/HA-R4: `clean --branches` reads the record by the entry's inner
    id, so a second entry claiming a live host node's id and a terminal status
    deletes that node's branch and checkout while its own entry says running."""
    victim = asyncio.run(start(project))
    attacker = asyncio.run(start(project))
    marker = Path(victim.worktree) / "uncommitted-work.txt"
    marker.write_text("only copy\n")
    with project.tree.transaction() as data:
        data["nodes"][victim.id]["status"] = "running"
        data["nodes"][attacker.id]["id"] = victim.id
        data["nodes"][attacker.id]["status"] = "done"

    cli.cmd_clean(argparse.Namespace(path=str(project.paths.root),
        branches=True, homes=False, tree=False, force=True))

    assert exists(project.paths.root, victim.branch), "live victim branch deleted"
    assert Path(victim.worktree).is_dir(), "live victim worktree removed"


def test_auto_merge_with_rewritten_inner_id_lands_in_unrelated_worktree(project):
    """HA-R3: an ending host-created depth-1 node is never merged into any
    worktree other than its recorded parent's (it has none)."""
    bystander = asyncio.run(start(project))
    bystander_head = git(Path(bystander.worktree), "rev-parse", "HEAD")

    async def run():
        node = await start(project, wait=False)
        evil = "agents/worker/evil-auto"
        git(project.paths.root, "branch", evil, "main")
        checkout = project.paths.worktree("ag-evil-auto")
        git(project.paths.root, "worktree", "add", "-q", str(checkout), evil)
        (checkout / "evil.txt").write_text("forged\n")
        git(checkout, "add", "evil.txt")
        git(checkout, "commit", "-qm", "evil")
        git(project.paths.root, "worktree", "remove", "--force", str(checkout))
        with project.tree.transaction() as data:
            entry = data["nodes"][node.id]
            entry["id"] = "ag-ghost-auto"
            entry["parent"] = bystander.id
            entry["branch"] = evil
        await asyncio.wait_for(project.runs[node.id].done.wait(), timeout=20)
        return node

    asyncio.run(run())

    assert not (Path(bystander.worktree) / "evil.txt").exists()
    assert git(Path(bystander.worktree), "rev-parse", "HEAD") == bystander_head


def test_read_only_agent_aliasing_live_node_gets_it_dropped(tmp_path, monkeypatch):
    """HA-R2/HA-R4: when a `writes: false` agent ends, `_drop_if_empty` must
    act on that agent's own record. Rewriting its entry's inner id to another
    host node's id makes the host discard that other node: its host-created
    branch and checkout are deleted and its record marked discarded, although
    the host never completed it."""
    import sys
    sys.path.insert(0, str(Path(__file__).parent / "support"))
    import c3_harness as h3
    from multiagents.config import AgentSpec
    from multiagents.executor.docker import DockerExecutor
    from test_h1_host_authority import IDENTITY

    monkeypatch.setattr(DockerExecutor, "inside", lambda self: False)
    for key, value in IDENTITY.items():
        monkeypatch.setenv(key, value)
    provider = h3.fake_cli(tmp_path.parent, events=[{"type": "result",
        "subtype": "success", "result": "done"}], delay=0.35)
    runner = h3.make_runner(tmp_path, monkeypatch,
        agents={"worker": AgentSpec("worker", "fake", "m"),
                "reader": AgentSpec("reader", "fake", "m", writes=False)},
        providers={"fake": provider})
    git(tmp_path, "branch", "-M", "main")
    (tmp_path / ".gitignore").write_text(".multiagents/\n")
    git(tmp_path, "add", ".gitignore")
    git(tmp_path, "commit", "-qm", "ignore project state")

    victim = asyncio.run(start(runner))           # done, awaiting review
    marker = Path(victim.worktree) / "uncommitted-work.txt"
    marker.write_text("only copy\n")

    async def run():
        result = await runner.start("reader", "look around")
        reader_id = result["agent_id"]
        with runner.tree.transaction() as data:
            data["nodes"][reader_id]["id"] = victim.id
        await asyncio.wait_for(runner.runs[reader_id].done.wait(), timeout=20)
        return reader_id

    asyncio.run(run())

    assert exists(tmp_path, victim.branch), "victim's host-created branch deleted"
    assert Path(victim.worktree).is_dir(), "victim's checkout removed"
    assert runner.authority.get(victim.id)["completion"] is None
