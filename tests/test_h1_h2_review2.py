"""Regressions from review ag-64181b for HA-R2a and RF-R1/RF-R3."""
from __future__ import annotations

import argparse
import asyncio
import yaml

from multiagents import cli, gitops
from test_h1_host_authority import git, project  # noqa: F401 (fixture import)
from test_h1_review_findings import mismatch
from test_h1_unrecorded_nodes import unrecorded
from test_agent_survival import (project as survival_project, start_and_detach,
                                 go, terminal, SID)  # noqa: F401 (fixture import)
import sv_harness as sv
from test_h2_refusal_status import SHIPPED


def test_ha_r2a_resume_never_commits_unrecorded_main_checkout(project, monkeypatch):
    checkout = project.paths.worktree("ag-forged-main-resume")
    # Git permits an explicit forced second checkout of a branch. This is a
    # valid in-domain worktree, but main is not a valid unrecorded operand.
    git(project.paths.root, "worktree", "add", "--force", str(checkout), "main")
    marker = checkout / "interrupted.txt"
    marker.write_text("unfinished work\n")
    node = unrecorded(project, "ag-forged-main-resume", branch="main",
                      worktree=checkout, status="running")
    project.tree.update(node.id, pid=999999999, pid_start="")
    before = git(project.paths.root, "rev-parse", "main")
    monkeypatch.setattr(cli, "_executor_problems", lambda *args: [])

    result = cli.cmd_resume(argparse.Namespace(
        path=str(project.paths.root), no_launch=True, resume=True, wait=False,
        unattended=0, team="", supervise=True))

    assert result == 0
    assert git(project.paths.root, "rev-parse", "main") == before
    assert git(checkout, "rev-parse", "HEAD") == before
    assert marker.read_text() == "unfinished work\n"
    assert git(checkout, "status", "--porcelain", "--", marker.name) == "?? interrupted.txt"
    mismatch(project, node.id, "resume", ["branch"])


def test_ha_r2a_steer_rechecks_parent_before_replacement_checkout(
        project, tmp_path, monkeypatch):
    node_id = "ag-steer-replacement-swap"
    branch = "agents/worker/steer-replacement-swap"
    git(project.paths.root, "branch", branch, "main")
    parent = project.paths.worktrees / "nested-replacement"
    parent.mkdir()
    checkout = parent / node_id
    checkout.mkdir()  # Occupied by a plain directory, so steer moves it aside.
    node = unrecorded(project, node_id, branch=branch, worktree=checkout,
                      status="running", session_id="session-for-test")
    outside_parent = tmp_path / "outside-replacement"
    outside_parent.mkdir()
    marker = outside_parent / "keep.txt"
    marker.write_text("untouched\n")
    original_move = gitops.move_aside
    swapped = False

    def move_then_swap(repo, path):
        nonlocal swapped
        aside = original_move(repo, path)
        # Narrow seam: after steer's move_aside returns, before its replacement
        # attach_worktree uses the node's original pathname.
        parent.rename(project.paths.worktrees / "saved-replacement-parent")
        parent.symlink_to(outside_parent, target_is_directory=True)
        swapped = True
        return aside

    monkeypatch.setattr(gitops, "move_aside", move_then_swap)
    asyncio.run(project.steer(node.id, "continue"))

    assert swapped, "steer did not reach the replacement-checkout seam"
    assert marker.read_text() == "untouched\n"
    assert sorted(p.name for p in outside_parent.iterdir()) == ["keep.txt"]
    assert not (outside_parent / node_id).exists()


def test_rf_r3_r1_adopted_opencode_filter_without_exit_status_is_refused(
        survival_project):
    p = survival_project()
    # Synthetic opencode-shaped stream, using the shipped opencode declaration.
    # The stub is only an event source; its output is consumed by a real server.
    config_path = p.root / ".multiagents" / "config" / "providers.yaml"
    config = yaml.safe_load(config_path.read_text())
    shipped = yaml.safe_load(SHIPPED.read_text())["providers"]["opencode"]
    opencode = dict(config["providers"].pop("svstub"))
    opencode["stream"] = shipped["stream"]
    opencode["usage_mode"] = shipped["usage_mode"]
    opencode["spawn"] = {**opencode["spawn"], "prompt_transport": "argv"}  # C16 TG-R4 opt-in
    config["providers"]["opencode"] = opencode
    config_path.write_text(yaml.safe_dump(config))
    agents_path = p.root / ".multiagents" / "config" / "agents.yaml"
    agents = yaml.safe_load(agents_path.read_text())
    for agent in agents["agents"].values():
        agent["provider"] = "opencode"
        agent["model"] = "opencode/m"
    agents_path.write_text(yaml.safe_dump(agents))

    steps = [["touch", str(p.marker("started"))],
             ["wait_for", str(p.marker("go")), 60],
             ["emit", {"type": "text", "sessionID": SID,
                       "part": {"text": "Request filtered"}}],
             ["emit", {"type": "step_finish", "sessionID": SID,
                       "part": {"reason": "content-filter", "tokens": {}, "cost": 0}}],
             ["touch", str(p.marker("finished"))], ["exit", 0]]
    _, node_id = start_and_detach(p, steps, how="sigkill")
    before_health = p.tree.provider_health()
    go(p)
    assert sv.wait_until(p.marker("finished").is_file, 15)
    assert sv.wait_until(p.exit_status(node_id).is_file, 5)
    p.exit_status(node_id).unlink()
    assert not p.exit_status(node_id).exists()
    assert "content-filter" in (p.run_dir(node_id) / "output.ndjson").read_text()

    p.server()
    assert terminal(p, node_id, 15) == "refused", p.describe(node_id)
    assert "content-filter" in p.node(node_id).reason
    assert p.tree.provider_health() == before_health
