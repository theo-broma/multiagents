"""Host-owned node branches and immutable activation results (NC-R33/R61/R65).

Managed checkouts have independent refs and objects: an agent's git commands
cannot write the scheduler's nodes/* refs. Only verified objects cross back.
All integration uses plumbing on the host, with no agent checkout involved.
"""
from __future__ import annotations

from contextlib import contextmanager, ExitStack
import os
from pathlib import Path
import re
import stat
import tempfile

from .. import gitops
from ..config import matches_any


class MissingCheckout(gitops.GitError):
    """The recorded checkout is absent, rather than present but unreadable."""


def checkout_record(run, authority):
    record = authority.get(run.id) if authority else None
    if not record or record["seeded"]:
        raise MissingCheckout("no host launch authority for result")
    return record


@contextmanager
def pinned_checkout(record, authority):
    with ExitStack() as stack:
        try:
            pinned = stack.enter_context(authority.pinned_worktree(Path(record["worktree"])))
        except FileNotFoundError as exc:
            raise MissingCheckout("recorded checkout is missing") from exc
        yield pinned


def recorded_tip(pinned, branch):
    """Read only the H1-named ref, loose or packed, without running agent git.

    A loose ref takes precedence. A present but unreadable or symbolic ref
    is refused rather than falling back to packed refs or following HEAD.
    """
    parts = tuple(branch.split("/"))
    directories = (".git", "refs", "heads", *parts[:-1])
    loose = False
    try:
        fd = gitops._open_beneath(pinned, directories)
        try:
            entry = os.stat(parts[-1], dir_fd=fd, follow_symlinks=False)
            loose = True
            if not stat.S_ISREG(entry.st_mode):
                raise gitops.GitError("result branch is not a regular commit ref")
        finally:
            os.close(fd)
    except FileNotFoundError:
        pass
    if loose:
        raw = gitops._read_beneath(pinned, directories, parts[-1], 4096)
        commit = (raw or b"").decode().strip()
    else:
        raw = gitops._read_beneath(pinned, (".git",), "packed-refs", 32 * 1024 * 1024)
        matches = []
        for line in (raw or b"").decode().splitlines():
            if not line or line.startswith(("#", "^")):
                continue
            sha, separator, ref = line.partition(" ")
            if separator and ref == "refs/heads/" + branch:
                matches.append(sha)
        commit = matches[0] if len(matches) == 1 else ""
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise gitops.GitError("result branch is not a regular commit ref")
    return commit


def top_node(node, nodes):
    while node["parent"]:
        node = nodes[node["parent"]]
    return node


def input_generation(node, ref, nodes):
    if (not node or node.get("disposed") or node.get("completion_pending")
            or node.get("disposal_pending") or node["state"] != "done"):
        return None
    generations = node["generations"]
    if "generation" in ref:
        generations = [g for g in generations if g["seq"] == ref["generation"]]
    for generation in reversed(generations):
        # Mirrors on a loop and its work child identify the same generation
        # by result run and commit, even when their local sequence numbers differ.
        identity = (generation["run_id"], generation["commit"])
        mirrors = [(owner, other) for owner in nodes.values() for other in owner["generations"]
                   if (other["run_id"], other["commit"]) == identity]
        overridden = any(owner["state"] == "done" and owner["outcome"] == "approved"
                         and owner.get("closure", {}).get("outcome") == "approved"
                         and (owner["closure"].get("generation", {}).get("run_id"),
                              owner["closure"].get("generation", {}).get("commit")) == identity
                         for owner, _ in mirrors)
        if overridden:
            return generation
        loops = [other for owner, other in mirrors if owner["kind"] == "loop"]
        if loops and any(other["verdict"] != "approved" for other in loops):
            continue
        rejected = any(other["verdict"] == "rejected" for _, other in mirrors)
        if rejected:
            continue
        if generation["verdict"] == "approved" or (generation["verdict"] is None
                and node["kind"] != "loop" and node["outcome"] in {"completed", "approved"}):
            return generation
    return None


class Results:
    def __init__(self, paths, config):
        self.paths, self.config = paths, config
        self.root = paths.root

    def git(self, *args, **kwargs):
        with gitops._host_scope(self.root):
            return gitops.run(self.root, *args, **kwargs)

    def tip(self, ref):
        if ref.startswith("refs/") and self.git("symbolic-ref", "-q", ref).ok:
            raise gitops.GitError("scheduler ref is symbolic: " + ref)
        return self.git("rev-parse", "--verify", "-q", ref + "^{commit}").out

    def move(self, ref, commit, expected):
        # Retention refs are replayable after a crash between git and sqlite.
        if self.tip(ref) == commit:
            return
        self.git("update-ref", "--no-deref", ref, commit, expected or "0" * 40, check=True)

    def merge_commit(self, left, right, message):
        if self.git("merge-base", "--is-ancestor", right, left).ok:
            return left
        if self.git("merge-base", "--is-ancestor", left, right).ok:
            return right
        merged = self.git("merge-tree", "--write-tree", left, right)
        if not merged.ok:
            raise gitops.GitError(merged.out or merged.err)
        return self.commit(merged.out.splitlines()[0], [left, right], message)

    def commit(self, tree, parents, message):
        args = ["-c", "user.name=multiagents", "-c", "user.email=scheduler@multiagents.invalid",
                "-c", "commit.gpgSign=false", "commit-tree", tree]
        for parent in parents:
            args.extend(["-p", parent])
        return self.git(*args, "-m", message, check=True).out

    def prepare(self, node, nodes):
        top = top_node(node, nodes)
        branch = "refs/heads/nodes/" + top["id"]
        tip = top.get("branch_tip")
        if tip is None:
            base = self.config.base_branch or gitops.current_branch(self.root)
            tip = self.tip(base)
        elif self.tip(branch) not in {"", tip}:
            raise gitops.GitError("node branch differs from its host-recorded tip")
        refs = []
        cur = node
        while cur:
            refs[0:0] = cur["inputs"]
            cur = nodes.get(cur["parent"])
        commit = tip
        if refs:
            inputs = [input_generation(nodes.get(ref["node"]), ref, nodes) for ref in refs]
            if any(generation is None for generation in inputs):
                raise gitops.GitError("input generation is unavailable")
            commit = inputs[0]["commit"]
            for generation in inputs[1:]:
                commit = self.merge_commit(commit, generation["commit"], "multiagents: combined node inputs")
        top.update(branch=branch, branch_tip=tip)
        return {"input_commit": commit, "branch_node": top["id"]}

    def ensure_branch(self, node):
        actual = self.tip(node["branch"])
        if not actual:
            self.move(node["branch"], node["branch_tip"], "")
        elif actual != node["branch_tip"]:
            raise gitops.GitError("node branch differs from its host-recorded tip")

    def create_checkout(self, path, branch, commit):
        path.parent.mkdir(parents=True, exist_ok=True)
        # Only objects are shared, through a host-authored alternate. Refs,
        # HEAD, index and config belong to this checkout, independently.
        gitops.run(self.root, "-c", "core.hooksPath=/dev/null", "clone", "--shared", "--no-checkout",
                   "--", str(self.root), str(path), env=gitops._host_env(None), check=True)
        with gitops._host_scope(path):
            gitops.run(path, "checkout", "-b", branch, commit, check=True)

    def capture(self, run, attempt, authority):
        record = checkout_record(run, authority)
        branch = record["branch"]
        with pinned_checkout(record, authority) as pinned:
            metadata = pinned / ".git"
            if not metadata.is_dir() or metadata.is_symlink():
                raise gitops.GitError("result git directory was replaced")
            # Read the recorded branch, never the agent's HEAD or .git pointer.
            commit = recorded_tip(pinned, branch)
            checkout_commit = commit
            # Quarantine and verify before adding immutable objects to the
            # project. An agent cannot overwrite an existing host object.
            with tempfile.TemporaryDirectory(dir=self.paths.scheduler) as tmp:
                quarantine = Path(tmp) / "repo"
                gitops.run(self.root, "init", "--bare", str(quarantine), check=True)
                gitops._write_beneath(quarantine, ("objects", "info"), "alternates",
                                     (str(self.root / ".git" / "objects") + "\n").encode())
                objects = metadata / "objects"
                if objects.is_symlink() or not objects.is_dir():
                    raise gitops.GitError("result object directory was replaced")
                copied = []
                for directory in objects.iterdir():
                    if not re.fullmatch(r"[0-9a-f]{2}|pack", directory.name):
                        continue
                    if directory.is_symlink() or not directory.is_dir():
                        raise gitops.GitError("result object directory was replaced")
                    for source in directory.iterdir():
                        pattern = r"[0-9a-f]{38}" if directory.name != "pack" else r"pack-[0-9a-f]{40}\.(pack|idx)"
                        if not re.fullmatch(pattern, source.name):
                            continue
                        data = gitops._read_beneath(pinned, (".git", "objects", directory.name),
                                                   source.name, 512 * 1024 * 1024)
                        if data is None:
                            raise gitops.GitError("unreadable result object")
                        gitops._write_beneath(quarantine, ("objects", directory.name), source.name,
                                             data, create=True)
                        copied.append((directory.name, source.name))
                gitops.run(self.root, "--git-dir=" + str(quarantine), "fsck", "--full", "--strict",
                           "--no-reflogs", "--no-dangling", commit, check=True)
                for directory, name in copied:
                    target = self.root / ".git" / "objects" / directory / name
                    if target.exists():
                        continue
                    data = gitops._read_beneath(quarantine, ("objects", directory), name, 512 * 1024 * 1024)
                    gitops._write_beneath(self.root / ".git", ("objects", directory), name, data, create=True)
        if not self.git("merge-base", "--is-ancestor", attempt["input_commit"], commit).ok:
            raise gitops.GitError("result does not descend from input commit")
        retention_ref = "refs/heads/node-results/" + attempt["attempt_id"]
        retained = self.tip(retention_ref)
        patterns = attempt.get("readonly_paths", [])
        changed = self.git("diff", "--no-renames", "--name-only", "--diff-filter=MDT", "-z",
                           attempt["input_commit"], commit, strip=False).out.split("\0")
        reverted = [path for path in changed if path and matches_any(patterns, path)]
        if reverted:
            with tempfile.TemporaryDirectory(dir=self.paths.scheduler) as tmp:
                env = {"GIT_INDEX_FILE": str(Path(tmp) / "index")}
                self.git("read-tree", commit, env=env, check=True)
                self.git("restore", "--source=" + attempt["input_commit"], "--staged", "--",
                         *reverted, env=env, check=True)
                tree = self.git("write-tree", env=env, check=True).out
                if retained and self.git("rev-parse", retained + "^{tree}", check=True).out != tree:
                    raise gitops.GitError("captured result changed after run completion")
                commit = retained or self.commit(tree, [commit], "multiagents: restore readonly paths")
        if retained and not reverted and retained != commit:
            raise gitops.GitError("captured result changed after run completion")
        self.move(retention_ref, commit, "")
        return {"status": run.status, "session_id": run.session_id, "branch": branch,
                "run_dir": str(self.paths.run_dir(run.id)), "commit": commit, "checkout_commit": checkout_commit,
                "readonly_reverted": reverted}

    def integrate(self, node, nodes, attempt):
        top = nodes[attempt["branch_node"]]
        before = top["branch_tip"]
        ref = top["branch"]
        if self.tip(ref) != before:
            raise gitops.GitError("node branch differs from its host-recorded tip")
        commit = self.merge_commit(before, attempt["result"]["commit"], "multiagents: integrate " + node["id"])
        seq = len(node["generations"]) + 1
        # The prepared commit is journalled before moving refs by Engine.
        return {"seq": seq, "commit": commit, "run_id": attempt["run_id"], "verdict": None,
                **({"readonly_reverted": attempt["result"]["readonly_reverted"]}
                   if attempt["result"].get("readonly_reverted") else {})}, before

    def prepare_publication(self, node):
        from .model import Refused
        if self.tip(node["branch"]) != node["branch_tip"]:
            raise Refused("merge_conflict", detail="node branch differs from host record")
        if self.git("status", "--porcelain").out:
            raise Refused("merge_conflict", detail="target worktree has uncommitted changes; commit or stash first")
        ref = self.git("symbolic-ref", "-q", "HEAD", check=True).out
        before = self.tip(ref)
        merged = self.git("merge-tree", "--write-tree", before, node["branch_tip"])
        if not merged.ok:
            raise Refused("merge_conflict", detail=merged.err or merged.out)
        tree = merged.out.splitlines()[0]
        target = before if tree == self.git("rev-parse", before + "^{tree}", check=True).out else self.commit(
            tree, [before], "multiagents: publish " + node["id"])
        return {"ref": ref, "before": before, "target": target}

    def apply_publication(self, intent):
        from .model import Refused
        ref, before, target = intent["ref"], intent["before"], intent["target"]
        actual = self.tip(ref)
        if actual not in {before, target}:
            # A replay after update-ref may find later commits on top of the
            # journalled one: only this process could have made it reachable.
            if actual and target != before and self.git("merge-base", "--is-ancestor", target, actual).ok:
                return
            raise Refused("merge_conflict", detail="publication target changed")
        try:
            self.move(ref, target, before)
        except gitops.GitError as exc:
            raise Refused("merge_conflict", detail=str(exc)) from exc
        # Replaying after update-ref repairs the index/checkout as well. The
        # two-tree merge refuses intervening work instead of deleting it, and
        # main goes back to where it was so a retry starts from it (NC-R86).
        checkout = self.git("read-tree", "-u", "-m", before, target)
        if not checkout.ok:
            if target != before:
                self.git("update-ref", "--no-deref", ref, before, target)
            raise Refused("merge_conflict", detail=checkout.err or checkout.out)


def checkout_branch(run, authority):
    """Read a managed checkout's branch through its host-recorded directory."""
    record = authority.get(run.id) if authority else None
    if not record or record["seeded"]:
        return None
    try:
        with authority.pinned_worktree(Path(record["worktree"])) as pinned:
            head = gitops._read_beneath(pinned, (".git",), "HEAD", 4096)
            if head != ("ref: refs/heads/" + record["branch"] + "\n").encode():
                return None
            return record["branch"]
    except (OSError, ValueError):
        return None


def checkout_tip(run, authority):
    """Read the recorded run ref without consulting its HEAD or git pointer."""
    record = checkout_record(run, authority)
    with pinned_checkout(record, authority) as pinned:
        return recorded_tip(pinned, record["branch"])
