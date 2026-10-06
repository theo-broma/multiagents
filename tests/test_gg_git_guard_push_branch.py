"""GG-R5: `push_branch` runs the guard before it pushes.

Driven through the Runner (what the MCP tool calls) against a local bare remote
addressed by path, the way the existing push_branch tests do.
"""
from __future__ import annotations

import inspect
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))
import gg_world as gw  # noqa: E402
from gg_world import World, git, said  # noqa: E402

from multiagents import gitops  # noqa: E402
from multiagents.config import Config  # noqa: E402
from multiagents.paths import ProjectPaths  # noqa: E402
from multiagents.runner import Runner  # noqa: E402
from multiagents.tree import Node  # noqa: E402

EMAIL = gw.email()
TOKEN = gw.tokens()["ghp"]


def make(tmp_path, monkeypatch, *, patterns=None, push_agent_branches=False, remote=None):
    w = World(tmp_path, patterns=patterns)
    for key in [k for k in os.environ if k.startswith(("GIT_", "MULTIAGENTS_"))]:
        monkeypatch.delenv(key, raising=False)
    for key in ("HOME", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM", "GIT_TERMINAL_PROMPT",
                "MULTIAGENTS_STATE_DIR", "MULTIAGENTS_CONFIG_DIR"):
        monkeypatch.setenv(key, w.env[key])
    section = {"remote": remote or str(w.bare), "base_branch": "main",
               "push_agent_branches": push_agent_branches,
               "guard": {"patterns_file": str(w.patterns)}}
    w.write_config({"git": section})
    paths = ProjectPaths(w.root)
    paths.ensure()
    runner = Runner(paths, Config(project={"git": section}, providers={}, agents={},
                                  models={}, instruction_dirs=[]))
    return w, runner


def assert_masked(result, *secrets):
    blob = json.dumps(result, default=str)
    for s in secrets:
        assert s not in blob, "unmasked match in the push_branch result"
        assert s[2:] not in blob


def test_gg_r5_a_clean_push_still_pushes(tmp_path, monkeypatch):
    w, r = make(tmp_path, monkeypatch)
    sha = w.commit_file("ok.txt", "fine\n")
    result = r.push_branch(None)
    assert result.get("pushed") is True, result
    assert w.remote_refs()["refs/heads/main"] == sha


@pytest.mark.parametrize("name,body,secrets", [
    ("a.txt", "m " + EMAIL + "\n", [EMAIL]),
    ("a.txt", "k = " + TOKEN + "\n", [TOKEN]),
    ("a.txt", "ssh " + gw.tailnet_ip() + "\n", [gw.tailnet_ip()]),
    ("a.pem", gw.private_key_block() + "\n", ["MIIBplaceholder"]),
])
def test_gg_r5_findings_refuse_with_the_guard_reason_and_the_remote_is_untouched(
        tmp_path, monkeypatch, name, body, secrets):
    w, r = make(tmp_path, monkeypatch)
    before = w.remote_refs()
    w.commit_file(name, body)
    result = r.push_branch(None)
    assert result.get("ok") is False, result
    assert result.get("reason") == "guard", result
    assert isinstance(result.get("findings"), list) and result["findings"], result
    assert result.get("pushed") is not True
    assert_masked(result, *secrets)
    assert w.remote_refs() == before


def test_gg_r5_private_pattern_findings_are_masked_in_the_result(tmp_path, monkeypatch):
    word = "Zebra" + " Quux"
    w, r = make(tmp_path, monkeypatch, patterns=word + "\n")
    before = w.remote_refs()
    w.commit_file("a.txt", "about " + word + "\n")
    result = r.push_branch(None)
    assert result.get("ok") is False and result.get("reason") == "guard", result
    assert_masked(result, word)
    assert w.remote_refs() == before


def test_gg_r5_each_finding_says_what_and_where_without_the_match(tmp_path, monkeypatch):
    w, r = make(tmp_path, monkeypatch)
    sha = w.commit_file("src/a.txt", "x\ny\nm " + EMAIL + "\n")
    findings = r.push_branch(None)["findings"]
    blob = json.dumps(findings, default=str, ensure_ascii=False)
    assert "email" in blob and sha[:7] in blob and "src/a.txt:3" in blob
    assert gw.mask(EMAIL) in blob


def test_gg_r5_only_the_commits_the_push_would_add_are_scanned(tmp_path, monkeypatch):
    w, r = make(tmp_path, monkeypatch)
    w.commit_file("a.txt", "m " + EMAIL + "\n")
    git(w.root, "push", "-q", "origin", "main", env=w.env)        # already published
    sha = w.commit_file("later.txt", "fine\n")
    result = r.push_branch(None)
    assert result.get("pushed") is True, result
    assert w.remote_refs()["refs/heads/main"] == sha


def test_gg_r5_a_message_or_author_finding_refuses_too(tmp_path, monkeypatch):
    w, r = make(tmp_path, monkeypatch)
    before = w.remote_refs()
    w.commit("by " + EMAIL, add=False)
    result = r.push_branch(None)
    assert result.get("reason") == "guard" and result.get("ok") is False, result
    assert w.remote_refs() == before


def test_gg_r5_an_explicit_remote_is_scanned_as_well(tmp_path, monkeypatch):
    w, r = make(tmp_path, monkeypatch)
    other = tmp_path / "other.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(other)], check=True)
    w.commit_file("a.txt", "m " + EMAIL + "\n")
    result = r.push_branch(None, str(other))
    assert result.get("reason") == "guard" and result.get("ok") is False, result
    refs = subprocess.run(["git", "-C", str(other), "for-each-ref"], capture_output=True,
                          text=True).stdout
    assert refs.strip() == ""


def test_gg_r5_an_agent_branch_is_scanned_before_it_is_pushed(tmp_path, monkeypatch):
    w, r = make(tmp_path, monkeypatch, push_agent_branches=True)
    wt = r.paths.worktree("ag-one")
    gitops.create_worktree(w.root, wt, "agents/worker/one", base="main", unique=False)
    (wt / "agent.txt").write_text("m " + EMAIL + "\n")
    git(wt, "add", "-A", env=w.env)
    git(wt, "commit", "-q", "-m", "agent", env=w.env)
    r.tree.add(Node(id="ag-one", agent="worker", provider="p", model="m", parent=None,
                    depth=1, branch="agents/worker/one", worktree=str(wt),
                    status="done", task="work"))
    before = w.remote_refs()
    result = r.push_branch("ag-one")
    assert result.get("ok") is False and result.get("reason") == "guard", result
    assert_masked(result, EMAIL)
    assert w.remote_refs() == before


def test_gg_r5_a_clean_agent_branch_pushes(tmp_path, monkeypatch):
    w, r = make(tmp_path, monkeypatch, push_agent_branches=True)
    wt = r.paths.worktree("ag-one")
    gitops.create_worktree(w.root, wt, "agents/worker/one", base="main", unique=False)
    (wt / "agent.txt").write_text("fine\n")
    git(wt, "add", "-A", env=w.env)
    git(wt, "commit", "-q", "-m", "agent", env=w.env)
    r.tree.add(Node(id="ag-one", agent="worker", provider="p", model="m", parent=None,
                    depth=1, branch="agents/worker/one", worktree=str(wt),
                    status="done", task="work"))
    result = r.push_branch("ag-one")
    assert result.get("pushed") is True, result
    assert "refs/heads/agents/worker/one" in w.remote_refs()


def test_gg_r5_the_tool_offers_no_way_to_skip_the_scan():
    from multiagents import server
    params = set(inspect.signature(server.push_branch).parameters)
    assert not {p for p in params
                if any(w in p.lower() for w in ("skip", "force", "guard", "verify",
                                                 "bypass", "unsafe", "ignore", "scan"))}, params


def test_gg_r5_a_missing_patterns_file_still_lets_a_clean_push_through(tmp_path, monkeypatch):
    w, r = make(tmp_path, monkeypatch)
    assert not w.patterns.exists()
    w.commit_file("ok.txt", "fine\n")
    assert r.push_branch(None).get("pushed") is True


def test_gg_r5_a_failing_scan_refuses_rather_than_pushes(tmp_path, monkeypatch):
    w, r = make(tmp_path, monkeypatch, patterns="whatever\n")
    os.chmod(w.patterns, 0o644)
    before = w.remote_refs()
    w.commit_file("ok.txt", "fine\n")
    result = r.push_branch(None)
    assert result.get("pushed") is not True, result
    assert result.get("ok") is False, result
    assert w.remote_refs() == before
