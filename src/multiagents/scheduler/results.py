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


class RecoveryConflict(gitops.GitError):
    """Captured WIP cannot safely be used with the current inputs."""


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
    if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", commit):
        raise gitops.GitError("result branch is not a regular commit ref")
    return commit


def top_node(node, nodes):
    while node["parent"]:
        node = nodes[node["parent"]]
    return node


def root_approved_generation(node, identity):
    if node["state"] != "done" or node["outcome"] != "approved":
        return False
    closure = node.get("closure")
    if closure is not None:
        if closure.get("outcome") != "approved":
            return False
        generation = closure.get("generation", {})
    elif node["kind"] == "loop" and node.get("closed_by") == "root":
        # NC-R97: records carrying only NC-R37's root marker approve the
        # latest generation, never every generation in the loop's history.
        generation = node["generations"][-1] if node["generations"] else {}
    else:
        return False
    return (generation.get("run_id"), generation.get("commit")) == identity


def recording_loops(node, nodes):
    """Enclosing loops that record and review this node's generations.

    A loop reviews work produced anywhere in its work subtree, nearest loop
    first; its verdict child's subtree is review, never recorded work (NC-R97).
    """
    loops, child = [], node
    while child["parent"]:
        parent = nodes[child["parent"]]
        if parent["kind"] == "loop" and parent["loop"]["verdict_child"] != child["id"]:
            loops.append(parent)
        child = parent
    return loops


def input_generation(node, ref, nodes, consumer=None):
    """Resolve for the consuming node; no consumer means an external reader."""
    if (not node or node.get("disposed") or node.get("completion_pending")
            or node.get("disposal_pending") or node["state"] != "done"):
        return None
    inside = set()
    cur = consumer
    while cur and cur["parent"]:
        cur = nodes[cur["parent"]]
        inside.add(cur["id"])
    # Every enclosing loop judges this node's work, including one whose
    # verdict child holds it: review output descends from unjudged work.
    enclosing = [node] if node["kind"] == "loop" else []
    cur = node
    while cur["parent"]:
        cur = nodes[cur["parent"]]
        if cur["kind"] == "loop":
            enclosing.append(cur)
    generations = node["generations"]
    if "generation" in ref:
        generations = [g for g in generations if g["seq"] == ref["generation"]]
    for generation in reversed(generations):
        # Mirrors on a loop and its work child identify the same generation
        # by result run and commit, even when their local sequence numbers differ.
        identity = (generation["run_id"], generation["commit"])
        mirrors = [(owner, other) for owner in nodes.values() for other in owner["generations"]
                   if (other["run_id"], other["commit"]) == identity]
        root_approved = {loop["id"] for loop in enclosing if root_approved_generation(loop, identity)}
        # Tree membership precedes mirrored records: finished work is still
        # unjudged if its loop has not recorded the generation yet (NC-R97).
        # Each loop gates only consumers outside its subtree; its own work
        # and reviewer must be able to consume pending results.
        if any(loop["id"] not in inside and loop["id"] not in root_approved
               and not any(owner["id"] == loop["id"] and other["verdict"] == "approved"
                           for owner, other in mirrors) for loop in enclosing):
            continue
        # A rejection blocks every consumer. Only the rejecting loop's own
        # root close overturns it: never a close of a node inside it, nor an
        # inner loop's approval. Rejections copied onto plain records yield
        # to any enclosing loop's root approval.
        if any(other["verdict"] == "rejected"
               and (owner["id"] not in root_approved if owner["kind"] == "loop" else not root_approved)
               for owner, other in mirrors):
            continue
        if (root_approved or generation["verdict"] == "approved" or (generation["verdict"] is None
                and node["kind"] != "loop" and node["outcome"] in {"completed", "approved"}
                and not (node.get("closure") or {}).get("via"))):
            return generation
    return None


class Results:
    def __init__(self, paths, config, *, generation_commits=(), generation_policies=None,
                 persist_capture=None):
        self.paths, self.config = paths, config
        self.root = paths.root
        self.generation_commits = set(generation_commits)
        self.generation_policies = generation_policies or {}
        self.persist_capture = persist_capture

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
            inputs = [input_generation(nodes.get(ref["node"]), ref, nodes, consumer=node) for ref in refs]
            if any(generation is None for generation in inputs):
                raise gitops.GitError("input generation is unavailable")
            commit = inputs[0]["commit"]
            for generation in inputs[1:]:
                commit = self.merge_commit(commit, generation["commit"], "multiagents: combined node inputs")
        recovery = node.get("recovery")
        if recovery and recovery.get("pending"):
            # A reviewer resumes from the generation it currently reviews.
            parent = nodes.get(node["parent"])
            if (parent and parent["kind"] == "loop" and parent["loop"]["verdict_child"] == node["id"]
                    and parent["generations"]):
                commit = parent["generations"][-1]["commit"]
            try:
                captured = recovery["commit"]
                if self.tip("refs/heads/node-results/" + recovery["attempt"]) != captured:
                    raise gitops.GitError("captured ref is missing or differs from the recorded commit")
                if not self.git("merge-base", "--is-ancestor", recovery["input_commit"], captured).ok:
                    raise gitops.GitError("captured commit does not descend from the attempt's input")
                commit = self.merge_commit(commit, captured, "multiagents: recover " + node["id"])
            except gitops.GitError as exc:
                raise RecoveryConflict(str(exc)) from exc
        top.update(branch=branch, branch_tip=tip)
        prepared = {"input_commit": commit, "branch_node": top["id"],
                    "prepared_branch": branch, "prepared_tip": tip}
        if recovery and recovery.get("pending"):
            prepared["recovery_source"] = {k: recovery[k] for k in ("attempt", "commit")}
        return prepared

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

    def tree_entries(self, commit, paths):
        # One query for all paths in a source, including modes, symlinks,
        # gitlinks and directories. Missing entries are represented by None.
        raw = self.git("--literal-pathspecs", "ls-tree", "-r", "-t", "-z", commit,
                       "--", *paths, strip=False, check=True).out
        entries = {}
        for item in raw.split("\0"):
            if item:
                entry, path = item.split("\t", 1)
                entries[path] = entry.split(" ")
        return {path: entries.get(path) for path in paths}

    def readonly_decision(self, attempt, commit, paths):
        base = self.config.base_branch or gitops.current_branch(self.root)
        base_commit = self.tip(base) if base else ""
        if not base_commit:
            raise gitops.GitError("readonly capture base branch cannot be resolved")
        # Intersect the two host-owned source sets with the attempt's new
        # ancestry. Neither a generation's ancestors nor base side parents
        # acquire authority from their descendant's record.
        base_chain = set(self.git("rev-list", "--first-parent", base_commit, check=True).out.splitlines())
        input_commit = attempt["input_commit"]
        history = [line.split() for line in self.git(
            "rev-list", "--topo-order", "--parents", commit, "--not", input_commit,
            check=True).out.splitlines()]
        eligible = base_chain | self.generation_commits
        candidates = dict.fromkeys(source for source, *parents in history if source in eligible)
        merge_bases = {}
        for source in candidates:
            pair = (source, input_commit)
            bases = self.git("merge-base", "--all", *pair)
            if not bases.ok and not (bases.code == 1 and not bases.out and not bases.err):
                raise gitops.GitError(bases.err or bases.out or "readonly source merge-base failed")
            merge_bases[pair] = bases.out.splitlines()
        # Batch all protected paths per commit, caching sources and shared
        # merge bases alike; no Git call depends on an individual path.
        commits = dict.fromkeys([input_commit, commit, *candidates,
                                *(base for bases in merge_bases.values() for base in bases)])
        entries = {source: self.tree_entries(source, paths) for source in commits}
        tip = entries[commit]
        producer_policies = {}

        def policy_allows(source, path):
            # Base first-parent commits are exempt even when a generation
            # record also names them (RP10, Decision 13).
            if source in base_chain:
                return True
            if source not in producer_policies:
                policies = self.generation_policies.get(source)
                if not policies or any(not records or any(
                        not isinstance(policy, list) or any(not isinstance(pattern, str) for pattern in policy)
                        for policy in records) for records in policies.values()):
                    raise gitops.GitError("readonly generation producer policy is unavailable: " + source)
                producer_policies[source] = policies
            return all(not matches_any(policy, path)
                       for records in producer_policies[source].values() for policy in records)

        decisions = []
        for path in paths:
            could_vouch = {source for source in candidates if merge_bases[(source, input_commit)]
                           and all(entries[source][path] != entries[base][path]
                                   and entries[input_commit][path] == entries[base][path]
                                   for base in merge_bases[(source, input_commit)])}
            allowed, ancestors = set(), {}
            # Parents precede descendants here. A policy matters only for
            # a possible vouch or a source that could supersede an allowed
            # vouching ancestor, including a revert to the input (RP13).
            for source, *parents in reversed(history):
                inherited = any(ancestors.get(parent, False) for parent in parents)
                if (source in candidates and (source in could_vouch or inherited)
                        and policy_allows(source, path)):
                    allowed.add(source)
                ancestors[source] = inherited or (source in allowed and source in could_vouch)
            sources, superseded = [], set()
            # Descendants precede parents. Only a source allowed on this
            # path can supersede an older eligible snapshot (RP7/RP10).
            for source, *parents in history:
                if source in superseded:
                    superseded.update(parents)
                elif source in allowed:
                    sources.append(source)
                    superseded.update(parents)
            vouching = [source for source in sources if source in could_vouch]
            matches = [source for source in vouching if entries[source][path] == tip[path]]
            if matches:
                source = next((source for source in matches if source in base_chain), matches[0])
                decisions.append({"path": path, "outcome": "kept", "source": source})
                continue
            alternatives = {tuple(entries[source][path]) if entries[source][path] else None
                            for source in vouching}
            source = input_commit
            if len(alternatives) == 1:
                source = vouching[0]
            decisions.append({"path": path, "outcome": "reverted", "source": source})
        return {"base_commit": base_commit, "producer_policies": producer_policies, "paths": decisions}

    def restored_tree(self, commit, decisions):
        groups = {}
        for decision in decisions:
            if decision["outcome"] == "reverted":
                groups.setdefault(decision["source"], []).append(decision["path"])
        with tempfile.TemporaryDirectory(dir=self.paths.scheduler) as tmp:
            env = {"GIT_INDEX_FILE": str(Path(tmp) / "index")}
            self.git("read-tree", commit, env=env, check=True)
            for source, paths in groups.items():
                # An already-absent path needs no index operation (and would
                # otherwise fail git restore's pathspec check on replay).
                target, current = self.tree_entries(source, paths), self.tree_entries(commit, paths)
                changed = [path for path in paths if target[path] != current[path]]
                if changed:
                    self.git("--literal-pathspecs", "restore", "--source=" + source, "--staged", "--",
                             *changed, env=env, check=True)
            return self.git("write-tree", env=env, check=True).out

    def capture(self, run, attempt, authority):
        record = checkout_record(run, authority)
        isolated = (attempt.get("launch_evidence") or {}).get("executor", {}).get("kind") == "isolated"
        branch = record["branch"]
        with pinned_checkout(record, authority) as pinned:
            metadata = pinned / ".git"
            if not metadata.is_dir() or metadata.is_symlink():
                raise gitops.GitError(("import_malformed: " if isolated else "")
                                     + "result git directory was replaced")
            # Read the recorded branch, never the agent's HEAD or .git pointer.
            try:
                commit = recorded_tip(pinned, branch)
            except (gitops.GitError, OSError, UnicodeError) as exc:
                if isolated:
                    raise gitops.GitError("import_malformed: " + str(exc)) from exc
                raise
            checkout_commit = commit
            # Quarantine and verify before adding immutable objects to the
            # project. An agent cannot overwrite an existing host object.
            with tempfile.TemporaryDirectory(dir=self.paths.scheduler) as tmp:
                if isolated:
                    from .import_validation import import_isolated
                    import_isolated(pinned, Path(tmp), self.root, commit, attempt["input_commit"], self.config)
                else:
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
                           attempt["input_commit"], commit, strip=False, check=True).out.split("\0")
        protected = [path for path in changed if path and matches_any(patterns, path)]
        decision = attempt.get("readonly_capture")
        if decision is None:
            decision = (self.readonly_decision(attempt, commit, protected) if protected
                        else {"base_commit": None, "paths": []})
        reverted = [item["path"] for item in decision["paths"] if item["outcome"] == "reverted"]
        kept = [{"path": item["path"], "source": item["source"]}
                for item in decision["paths"] if item["outcome"] == "kept"]
        # Replay uses only frozen authority. Additional protected mutations
        # still undergo the original restored-tree check, without new trust.
        known = {item["path"] for item in decision["paths"]}
        restorations = decision["paths"] + [
            {"path": path, "outcome": "reverted", "source": attempt["input_commit"]}
            for path in protected if path not in known]
        if any(item["outcome"] == "reverted" for item in restorations):
            tree = self.restored_tree(commit, restorations)
            frozen = decision.get("commit") or retained
            if frozen and self.git("rev-parse", frozen + "^{tree}", check=True).out != tree:
                raise gitops.GitError("captured result changed after run completion")
            commit = frozen or self.commit(tree, [commit], "multiagents: restore readonly paths")
        if (decision.get("commit") or retained) and (decision.get("commit") or retained) != commit:
            raise gitops.GitError("captured result changed after run completion")
        if "commit" not in decision:
            decision["commit"] = commit
            if self.persist_capture is None:
                raise gitops.GitError("result capture journal is unavailable")
            self.persist_capture(decision)
        # A successful non-writing isolated attempt that returned its input
        # has no new result to pin. Keep unexpected commits and failed-run
        # recovery refs, along with legacy and pre-upgrade retention.
        if not (isolated and attempt.get("writes") is False and run.status == "done"
                and commit == attempt["input_commit"]):
            self.move(retention_ref, commit, "")
        return {"status": run.status, "session_id": run.session_id, "branch": branch,
                "run_dir": str(self.paths.run_dir(run.id)), "commit": commit, "checkout_commit": checkout_commit,
                "readonly_reverted": reverted, **({"readonly_kept": kept} if kept else {})}

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
                   if attempt["result"].get("readonly_reverted") else {}),
                **({"readonly_kept": attempt["result"]["readonly_kept"]}
                   if attempt["result"].get("readonly_kept") else {})}, before

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
