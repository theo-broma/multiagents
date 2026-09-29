"""Adversary round 2 (held): HG-R8's compare-and-swap when the ref moved meanwhile.

A host checkpoint runs detached at the branch tip and then advances the
branch with `update-ref --no-deref <ref> <new> <old>`. If the branch moved
legitimately during the call, the host must neither overwrite that move nor
report a checkpoint that is not on the branch. No existing test moves the
ref mid-call: ignoring the failed `update-ref` survived mutation.
"""
from __future__ import annotations

from multiagents import gitops

from test_h3_host_git import agent_branch, git, isolated_git, repo  # noqa: F401


def test_checkpoint_does_not_claim_success_when_the_branch_moved_mid_call(tmp_path, monkeypatch):
    root = repo(tmp_path)
    wt = agent_branch(root, tmp_path)
    (wt / "pending.txt").write_text("pending\n")
    concurrent = {}
    real = gitops._commit_all

    def racing(*args, **kwargs):
        result = real(*args, **kwargs)
        # Meanwhile, the agent's own commit lands on its branch.
        tree = git(root, "rev-parse", "agents/worker/one^{tree}").stdout.strip()
        sha = git(root, "commit-tree", tree, "-p", "agents/worker/one",
                  "-m", "agent's concurrent commit").stdout.strip()
        git(root, "update-ref", "refs/heads/agents/worker/one", sha)
        concurrent["sha"] = sha
        return result

    monkeypatch.setattr(gitops, "_commit_all", racing)
    try:
        result = gitops.commit_all(wt, "checkpoint", root=root, branch="agents/worker/one")
        ok = result.ok
    except gitops.GitError:
        ok = False
    tip = git(root, "rev-parse", "agents/worker/one").stdout.strip()
    assert tip == concurrent["sha"], "the host overwrote a concurrent move of the branch"
    assert not ok, "commit_all reported a checkpoint that is not on the branch"
