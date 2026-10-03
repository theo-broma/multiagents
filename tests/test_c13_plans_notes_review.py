"""PN-R1a/PN-R2a regressions from the first C13 review."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from multiagents import plans

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))
import sv_harness as h  # noqa: E402


@pytest.mark.parametrize("mode", ["helper-relative", "helper-absolute", "cli"])
def test_plan_commit_accepts_a_repository_under_a_symlinked_workspace(tmp_path, monkeypatch, mode):
    workspace = tmp_path / "real-workspace"
    workspace.mkdir()
    project = h.Project(workspace)
    alias = tmp_path / "workspace"
    alias.symlink_to(workspace, target_is_directory=True)
    root = alias / "proj"
    relative = "context/plans/next.md"
    path = root / relative
    path.parent.mkdir(parents=True)
    path.write_text("---\nstatus: ready\n---\n# Next phase\n")
    for name, value in project.env(session="").items():
        if name.startswith("GIT_"):
            monkeypatch.setenv(name, value)
    if mode == "cli":
        result = subprocess.run([sys.executable, "-m", "multiagents.cli", "plan", "commit", relative],
                                cwd=root, env=project.env(session=""), capture_output=True,
                                text=True, timeout=10)
    else:
        result = plans.commit(root, [str(path) if mode == "helper-absolute" else relative])
    assert result.returncode == 0, result.stdout + result.stderr
    committed = subprocess.run(["git", "-C", str(root), "show", "--name-only", "--format=", "HEAD"],
                               capture_output=True, text=True, check=True)
    assert committed.stdout.splitlines() == [relative]


@pytest.mark.parametrize("header", [
    "status: ready\ntitle: &unused harmless\n",
    "status: *missing\n",
    "status: ready\na: &a [x, x, x, x, x, x, x, x, x, x]\n" +
    "".join(f"v{i}: &v{i} [" + ", ".join([f"*{'a' if i == 0 else 'v' + str(i - 1)}"] * 10) + "]\n"
            for i in range(12)),
], ids=["anchor-without-alias", "alias-without-anchor", "alias-bomb"])
def test_list_plans_reports_yaml_anchors_and_aliases_as_malformed_quickly(tmp_path, header):
    project = h.Project(tmp_path)
    path = project.root / "context/plans/aliases.md"
    path.parent.mkdir(parents=True)
    path.write_text("---\n" + header + "---\n# Untrusted plan\n")
    server = project.server()
    try:
        summary = server.call("list_plans", timeout=3)
        assert "error" not in summary, summary
        entry, = summary["plans"]
        assert entry["malformed"] and entry["status"] is None
        assert "anchor" in entry["reason"].lower() or "alias" in entry["reason"].lower()
    finally:
        server.close()
