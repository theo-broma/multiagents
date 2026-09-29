"""HA-R12, the `clean` clause: a malformed tree entry never aborts `clean`.

tree.json is writable from the container, so any entry can have the wrong
shape. `multiagents clean` must skip such an entry with an event, touch
nothing it names, and still clean the honest entries around it.
"""
from __future__ import annotations

import argparse
import json

import pytest

from multiagents import cli
from multiagents.tree import Node

from test_h1h3_round2_stop import _project, _spawn
from test_h3_host_git import git, isolated_git  # noqa: F401


MALFORMED = {
    "not-a-mapping": lambda entry: "junk",
    "a-list": lambda entry: [entry],
    "int-worktree": lambda entry: {**entry, "worktree": 1},
    "list-worktree": lambda entry: {**entry, "worktree": [entry["worktree"]]},
    "int-branch": lambda entry: {**entry, "branch": 1},
    "list-branch": lambda entry: {**entry, "branch": [entry["branch"]]},
}

PASSES = {
    "branches": dict(branches=True, homes=False, tree=False),
    "tree-and-homes": dict(branches=False, homes=True, tree=True),
    "all": dict(branches=True, homes=True, tree=True),
}


def _events_about(paths, node_id):
    if not paths.events_file.exists():
        return 0
    count = 0
    for line in paths.events_file.read_text().splitlines():
        event = json.loads(line)
        if node_id in (event.get("agent"), event.get("node")):
            count += 1
    return count


def _branch_exists(root, branch):
    return git(root, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}",
               check=False).returncode == 0


@pytest.mark.parametrize("flags", list(PASSES.values()), ids=list(PASSES))
@pytest.mark.parametrize("forge", list(MALFORMED.values()), ids=list(MALFORMED))
def test_ha_r12_clean_skips_a_malformed_entry_and_cleans_the_others(
        tmp_path, monkeypatch, forge, flags):
    root, paths, tree, authority = _project(tmp_path, monkeypatch)
    bad_wt = _spawn(root, paths, tree, authority, "ag-bad", "agents/worker/bad")
    good_wt = _spawn(root, paths, tree, authority, "ag-good", "agents/worker/good")
    for node_id in ("ag-bad", "ag-good"):
        tree.set_status(node_id, "done", "finished")
        paths.home(node_id).mkdir(parents=True, exist_ok=True)
    tree.add(Node(id="ag-spent", agent="worker", provider="p", model="m",
                  parent=None, depth=1, status="done", task="work"))

    # From the container: the malformed entry is listed first, so a pass that
    # aborts on it never reaches the honest ones.
    data = json.loads(paths.tree_file.read_text())
    bad = forge(data["nodes"].pop("ag-bad"))
    data["nodes"] = {"ag-bad": bad, **data["nodes"]}
    paths.tree_file.write_text(json.dumps(data))
    events_before = _events_about(paths, "ag-bad")

    try:
        result = cli.cmd_clean(argparse.Namespace(path=str(root), force=False, **flags))
    except Exception as exc:                  # noqa: BLE001
        raise AssertionError(f"`multiagents clean` crashed on one entry: "
                             f"{type(exc).__name__}: {exc}")

    assert result == 0
    assert _events_about(paths, "ag-bad") > events_before, \
        "the malformed entry was skipped without an event"
    # Mismatched: no host mutation for what the malformed entry names.
    assert _branch_exists(root, "agents/worker/bad")
    assert bad_wt.is_dir()
    # The honest entries are still processed.
    if flags["branches"]:
        assert not _branch_exists(root, "agents/worker/good"), \
            "clean --branches never reached the honest node's branch"
        assert not good_wt.exists(), "clean --branches never removed the honest worktree"
    if flags["tree"]:
        remaining = json.loads(paths.tree_file.read_text())["nodes"]
        assert "ag-spent" not in remaining, "clean --tree never pruned the finished node"
    if flags["homes"]:
        assert not paths.home("ag-good").exists(), \
            "clean --homes never removed the honest node's home"
