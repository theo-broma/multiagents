"""Adversary round 2: `reap_pending_branches` raises on a container-written type.

Its docstring promises it "Never raises: it runs on every Runner start". It
builds the set of held branches from every entry's `branch` outside its
try-block, so one entry whose `branch` is a list (unhashable) raises
`TypeError` -- in every `Runner()` construction, which `multiagents stop`
does before it stops anything or checkpoints anyone (HG-R9). An entry that
lacks Node's required fields does the same one step earlier, in
`Tree.active()`.
"""
from __future__ import annotations

import argparse
import json

import pytest

from multiagents import cli
from multiagents.runner import Runner, reap_pending_branches

from test_h1h3_round2_stop import _forge, _project, _spawn
from test_h3_host_git import git, isolated_git  # noqa: F401


FULL = {"id": "ag-evil", "agent": "worker", "provider": "p", "model": "m",
        "parent": None, "depth": 1, "status": "running", "task": "t"}


def _poison(paths, entry):
    data = json.loads(paths.tree_file.read_text())
    data["nodes"]["ag-evil"] = entry
    paths.tree_file.write_text(json.dumps(data))


LIST_BRANCH = dict(FULL, branch=["agents/x"], worktree="", branch_pending_delete="agents/x")


def test_reap_never_raises_on_a_non_string_branch(tmp_path, monkeypatch):
    root, paths, tree, authority = _project(tmp_path, monkeypatch)
    _spawn(root, paths, tree, authority, "ag-two", "agents/worker/two")
    _poison(paths, LIST_BRANCH)
    reap_pending_branches(root, tree, authority)


@pytest.mark.parametrize("entry", [LIST_BRANCH, {"id": "ag-evil", "status": "running"}],
                         ids=["list-branch", "missing-fields"])
def test_stop_still_checkpoints_when_one_entry_is_malformed(tmp_path, monkeypatch, entry):
    root, paths, tree, authority = _project(tmp_path, monkeypatch)
    good = _spawn(root, paths, tree, authority, "ag-two", "agents/worker/two")
    (good / "honest.txt").write_text("honest work in progress\n")
    _poison(paths, entry)
    try:
        cli.cmd_stop(argparse.Namespace(path=str(root), keep_containers=True))
    except Exception as exc:                  # noqa: BLE001
        raise AssertionError(f"`multiagents stop` crashed: {type(exc).__name__}: {exc}")
    assert git(root, "show", "agents/worker/two:honest.txt", check=False).stdout == \
        "honest work in progress\n", "the honest agent's work was never checkpointed"
