"""Persistent alias bindings and stable, host-reseated checkouts (NC-R30/R62)."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import tempfile

from .. import gitops
from .model import Refused, invalid
from .store import encode


def instance_node(node, nodes):
    while node:
        if node.get("template"):
            return node
        node = nodes.get(node["parent"])
    return None


def alias_id(node, nodes):
    instance = instance_node(node, nodes)
    if not node.get("session") or not instance:
        return None
    return hashlib.sha256(encode([instance["template"]["instance"], node["session"]]).encode()).hexdigest()


def aliases(db):
    return {id: json.loads(raw) for id, raw in db.execute("SELECT id, record FROM aliases")}


def save_alias(db, binding):
    db.execute("INSERT OR REPLACE INTO aliases VALUES (?, ?)", (binding["id"], encode(binding)))


def validate_attachments(nodes, original, journal, bindings):
    launched = {alias_id(node, original) for node in original.values() if node["runs"]}
    launched.update(key for key, binding in bindings.items() if binding.get("last_run"))
    launched.update(a.get("alias_id") for a in journal.values()
                    if a["state"] in {"launched", "captured", "recorded"}
                    or a.get("launch_in_progress") or a.get("launch_evidence"))
    launched.discard(None)
    for node in nodes.values():
        key = alias_id(node, nodes)
        previous = original.get(node["id"])
        previous_key = alias_id(previous, original) if previous else None
        if key != previous_key and (key in launched or previous_key in launched):
            invalid("session: alias already launched")


def blockers(node, nodes, journal, bindings, runner):
    key = alias_id(node, nodes)
    if not key:
        return []
    if any(a.get("alias_id") == key and a["state"] in {"claimed", "launched", "captured", "suspended"}
           for a in journal.values()):
        return [{"code": "session_busy", "detail": node["session"]}]
    binding = bindings.get(key)
    if binding and not binding.get("renew"):
        if unavailable(binding, runner):
            return [{"code": "session_unavailable", "detail": binding["provider"]}]
    return []


def unavailable(binding, runner):
    provider = runner.providers.get(binding["provider"])
    if provider is None or not provider.enabled:
        return True
    account = provider.container_account or provider.name
    return (account != binding["account"]
            or bool(runner._cap_refusal(binding["provider"], binding["model"]))
            or bool(runner.tree.cooldown(binding["provider"])))


def freeze(db, node, nodes, result, paths, config):
    key = alias_id(node, nodes)
    if not key:
        return {}
    bound = aliases(db).get(key)
    provider = result["provider"]
    family = config.providers.get(provider, {}).get("family") or config.providers.get(provider, {}).get("extends") or provider
    # Recheck at activation: an accepted assignment may have outlived its
    # provider declaration. Its actual route must still match the alias family.
    for other in nodes.values():
        if alias_id(other, nodes) != key:
            continue
        spec = config.agents.get(other.get("agent"))
        declared = other["pins"].get("provider") or (spec.provider if spec else None)
        if declared:
            other_family = config.providers.get(declared, {}).get("family") or config.providers.get(declared, {}).get("extends") or declared
            if other_family != family:
                invalid("session: incompatible provider families at activation")
    if bound and bound.get("renew"):
        bound.update(provider=provider, model=result["model"], session_id=None,
                     account=config.providers.get(provider, {}).get("container_account") or provider)
        bound.pop("renew")
        save_alias(db, bound)
    if bound:
        if (provider, result["model"]) != (bound["provider"], bound["model"]):
            raise Refused("session_unavailable")
    else:
        bound = {"id": key, "alias": node["session"], "instance": instance_node(node, nodes)["template"]["instance"],
                 "provider": provider, "account": config.providers.get(provider, {}).get("container_account") or provider,
                 "model": result["model"], "session_id": None, "worktree": str(paths.worktree("alias-" + key[:24])),
                 "home_id": "alias-" + key[:24]}
        save_alias(db, bound)
    return {"alias_id": key, "alias_worktree": bound["worktree"], "alias_home": bound["home_id"],
            "session_id": bound["session_id"], "binding": {k: bound[k] for k in ("provider", "account", "model")}}


@contextmanager
def checkout_git(paths, binding, authority):
    """Use host objects/config and a private gitdir, never checkout config."""
    path = Path(binding["worktree"])
    with authority.pinned_worktree(path) as pinned, tempfile.TemporaryDirectory(dir=paths.scheduler) as tmp:
        metadata = pinned / ".git"
        if metadata.is_symlink() or not metadata.is_dir():
            raise gitops.GitError("alias git directory was replaced")
        gitops._read_beneath(pinned, (".git",), "index", 128 * 1024 * 1024)
        private = Path(tmp) / "gitdir"
        gitops.run(paths.root, "init", "--bare", str(private), env=gitops._host_env(None), check=True)
        (private / "objects" / "info" / "alternates").write_text(str(paths.root / ".git" / "objects") + "\n")
        (private / "HEAD").write_text(binding["commit"] + "\n")
        def git(*args, index=None, **kwargs):
            with gitops._host_scope(paths.root):
                return gitops.run(paths.root, *args, env={"GIT_DIR": str(private), "GIT_COMMON_DIR": str(private),
                    "GIT_WORK_TREE": str(pinned), "GIT_INDEX_FILE": str(index or metadata / "index")}, **kwargs)
        yield pinned, git


def check_clean(paths, binding, authority):
    if not binding.get("commit"):
        return
    with checkout_git(paths, binding, authority) as (_, git):
        staged = git("diff", "--cached", "--name-status", "HEAD", "--", check=True).out
        # Agent index flags (assume-unchanged/skip-worktree) cannot hide dirty
        # tracked files. Compare the filesystem with a fresh host index, while
        # also retaining changes staged only in the checkout's own index.
        with tempfile.TemporaryDirectory(dir=paths.scheduler) as tmp:
            index = Path(tmp) / "index"
            git("read-tree", "HEAD", index=index, check=True)
            status = git("status", "--porcelain", "--untracked-files=all", "--ignored=matching",
                         "--ignore-submodules=all", index=index, check=True).out
        if status or staged:
            diff = paths.scheduler / ("dirty-" + binding["id"] + ".diff")
            diff.write_text(status + "\n" + staged + "\n" + git("diff", "HEAD", "--", check=True).out)
            raise Refused("dirty_worktree", detail=str(diff))


def create_checkout(results, authority, path, branch, attempt):
    if not attempt.get("alias_id") or not path.exists():
        results.create_checkout(path, branch, attempt["input_commit"])
        return
    binding = {"worktree": str(path), "commit": attempt["previous_commit"]}
    with checkout_git(results.paths, binding, authority) as (pinned, git):
        git("reset", "--hard", attempt["input_commit"], check=True)
        gitops._write_beneath(pinned, (".git", "refs", "heads", *branch.split("/")[:-1]),
                             branch.split("/")[-1], (attempt["input_commit"] + "\n").encode(), create=True)
        gitops._write_beneath(pinned, (".git",), "HEAD", ("ref: refs/heads/" + branch + "\n").encode())


def retains_checkout(paths, run):
    if not run or not run.node_id:
        return False
    from .store import Store
    with Store(paths.root).transaction(write=False) as db:
        return bool(Store.nodes(db)[run.node_id]["session"])
