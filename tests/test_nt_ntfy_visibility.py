"""NT-R2, "who can call it": the `notify` tool is offered to the root
orchestrator only. It is in no subagent's tool list, legacy runs (a subagent
with MULTIAGENTS_AGENT_ID but no node permissions) included.

The tool list is fixed when the server module is imported, from the
environment, so each case lists the tools in a fresh interpreter.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SRC = str(Path(__file__).resolve().parents[1] / "src")
LISTING = (
    "import asyncio, json, sys; sys.path.insert(0, sys.argv[1]);"
    "from multiagents import server;"
    "print(json.dumps(sorted(t.name for t in asyncio.run(server.mcp.list_tools()))))"
)
LIST_TIMEOUT = 60


def tools_listed(tmp_path, **env) -> set[str]:
    clean = {k: v for k, v in os.environ.items() if not k.startswith(("MULTIAGENTS_", "CLAUDE_"))}
    clean["HOME"] = str(tmp_path)
    run = subprocess.run([sys.executable, "-c", LISTING, SRC], cwd=tmp_path, env={**clean, **env},
                         capture_output=True, text=True, timeout=LIST_TIMEOUT)
    assert run.returncode == 0, run.stderr
    return set(json.loads(run.stdout.strip().splitlines()[-1]))


@pytest.fixture(scope="module")
def root_tools(tmp_path_factory):
    return tools_listed(tmp_path_factory.mktemp("root"))


def test_nt_r2_the_root_orchestrator_is_offered_the_notify_tool(root_tools):
    assert "notify" in root_tools
    assert "list_questions" in root_tools          # the listing itself is sane


SUBAGENTS = {
    "legacy run, no node permissions": {"MULTIAGENTS_AGENT_ID": "ag-sub"},
    "node run, no permissions granted": {"MULTIAGENTS_AGENT_ID": "run-1",
                                         "MULTIAGENTS_NODE_PERMISSIONS": ""},
    "node run with delegate": {"MULTIAGENTS_AGENT_ID": "run-1",
                               "MULTIAGENTS_NODE_PERMISSIONS": "delegate"},
    "node run with delegate and verdict": {"MULTIAGENTS_AGENT_ID": "run-1",
                                           "MULTIAGENTS_NODE_PERMISSIONS": "delegate,verdict"},
}


@pytest.mark.parametrize("case", list(SUBAGENTS))
def test_nt_r2_no_subagent_is_offered_the_notify_tool(tmp_path, root_tools, case):
    assert "notify" in root_tools                   # so this is red until the tool exists
    listed = tools_listed(tmp_path, **SUBAGENTS[case])
    assert "notify" not in listed
    assert "list_questions" in listed or "get_node" in listed   # the listing itself is sane
