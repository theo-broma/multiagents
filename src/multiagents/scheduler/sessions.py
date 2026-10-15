"""Persistent alias bindings and stable, host-reseated checkouts (NC-R30/R62)."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tarfile
import tempfile
import uuid

from .. import gitops
from ..tree import now, new_id
from .model import Refused, invalid
from .store import matching_records, select_records, encode


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


def provider_family(provider, config):
    declaration = config.providers.get(provider, {})
    return declaration.get("family") or declaration.get("extends") or provider


def pin_changed(node, binding, config):
    """A current pin may replace a conversation, never silently resume it elsewhere."""
    pins = node["pins"]
    if not binding or binding.get("renew") or not (pins.get("provider") or pins.get("model")):
        return False
    provider = pins.get("provider") or binding["provider"]
    account = config.providers.get(provider, {}).get("container_account") or provider
    return (provider != binding["provider"]
            or pins.get("model", binding["model"]) != binding["model"]
            or account != binding["account"]
            or provider_family(provider, config) != binding.get(
                "family", provider_family(binding["provider"], config)))


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
    binding = bindings.get(key)
    changed = pin_changed(node, binding, runner.config) if binding else False
    if any(a.get("alias_id") == key and a["state"] in {"claimed", "launched", "captured", "suspended"}
           for a in select_records(journal, "claimed", "launched", "captured", "suspended")):
        if changed:
            return [{"code": "session_binding", "detail": binding["provider"]}]
        return [{"code": "session_busy", "detail": node["session"]}]
    if binding and not binding.get("renew") and not changed:
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
    family = provider_family(provider, config)
    changed = pin_changed(node, bound, config)
    # Recheck at activation: an accepted assignment may have outlived its
    # provider declaration. Its actual route must still match the alias family.
    for other in nodes.values():
        if alias_id(other, nodes) != key:
            continue
        spec = config.agents.get(other.get("agent"))
        declared = other["pins"].get("provider") or (spec.provider if spec else None)
        if other["id"] == node["id"] and other["pins"].get("model"):
            # A model-only pin can select a configured fallback family.
            declared = provider
        if declared:
            other_family = provider_family(declared, config)
            if other_family != family:
                invalid("session: incompatible provider families at activation")
    if changed:
        # Preserve the old store and its route. A fresh HOME also prevents a
        # different provider/account from adopting provider-local state.
        bound.setdefault("archives", []).append({k: bound[k] for k in
            ("provider", "family", "account", "model", "session_id", "home_id", "last_run") if k in bound})
        bound.update(renew=True, reason="provider_changed",
                     home_id="alias-" + key[:24] + "-" + uuid.uuid4().hex[:12])
    if bound and bound.get("renew"):
        if not changed:
            bound.pop("reason", None)
        bound.update(provider=provider, model=result["model"], session_id=None,
                     account=config.providers.get(provider, {}).get("container_account") or provider,
                     family=family)
        bound.pop("renew")
        save_alias(db, bound)
    if bound:
        if (provider, result["model"]) != (bound["provider"], bound["model"]):
            raise Refused("session_unavailable")
    else:
        bound = {"id": key, "alias": node["session"], "instance": instance_node(node, nodes)["template"]["instance"],
                 "provider": provider, "account": config.providers.get(provider, {}).get("container_account") or provider,
                 "family": family, "model": result["model"], "session_id": None,
                 "worktree": str(paths.worktree("alias-" + key[:24])),
                 "home_id": "alias-" + key[:24]}
        save_alias(db, bound)
    bound.setdefault("family", family)
    kind = (result.get("executor_selection") or {}).get("kind") or config.executor
    # Old bindings predate isolated resources. Their HOME belongs to the
    # legacy activation, whichever of the two legacy executors it uses.
    previous_kind = bound.get("executor_kind", kind if kind in {"local", "docker"} else "local")
    if previous_kind != kind:
        bound["home_id"] = "alias-" + key[:24] + "-" + kind + "-" + uuid.uuid4().hex[:12]
    bound["executor_kind"] = kind
    save_alias(db, bound)
    return {"alias_id": key, "alias_worktree": bound["worktree"], "alias_home": bound["home_id"],
            "previous_executor_kind": previous_kind,
            **({"reason": "provider_changed"} if not bound["session_id"] and bound.get("reason") == "provider_changed" else {}),
            "session_id": bound["session_id"], "binding": {k: bound[k] for k in ("provider", "account", "model")}}


@contextmanager
def checkout_git(paths, binding, authority, *, include_objects=False):
    """Use host objects/config and a private gitdir, never checkout config."""
    path = Path(binding["worktree"])
    with authority.pinned_worktree(path) as pinned, tempfile.TemporaryDirectory(dir=paths.scheduler) as tmp:
        metadata = pinned / ".git"
        if metadata.is_symlink() or not metadata.is_dir():
            raise gitops.GitError("alias git directory was replaced")
        gitops._read_beneath(pinned, (".git",), "index", 128 * 1024 * 1024)
        private = Path(tmp) / "gitdir"
        gitops.run(paths.root, "init", "--bare", str(private), env=gitops._host_env(None), check=True)
        alternates = str(paths.root / ".git" / "objects") + "\n"
        if include_objects:
            # Staging creates blobs only in the independent alias checkout.
            # Snapshot them without its config or alternates, and keep host
            # objects first so checkout objects cannot replace trusted ones.
            copied = Path(tmp) / "checkout-objects"
            copied.mkdir()
            fd = gitops._open_beneath(pinned, (".git", "objects"))
            try:
                directories = os.listdir(fd)
            finally:
                os.close(fd)
            for directory in directories:
                if not re.fullmatch(r"[0-9a-f]{2}|pack", directory):
                    continue
                fd = gitops._open_beneath(pinned, (".git", "objects", directory))
                try:
                    names = os.listdir(fd)
                finally:
                    os.close(fd)
                pattern = r"[0-9a-f]{38}" if directory != "pack" else r"pack-[0-9a-f]{40}\.(pack|idx)"
                for name in names:
                    if not re.fullmatch(pattern, name):
                        continue
                    data = gitops._read_beneath(pinned, (".git", "objects", directory), name, 512 * 1024 * 1024)
                    if data is None:
                        raise gitops.GitError("unreadable alias object")
                    gitops._write_beneath(copied, (directory,), name, data, create=True)
            alternates += str(copied) + "\n"
        (private / "objects" / "info" / "alternates").write_text(alternates)
        (private / "HEAD").write_text(binding["commit"] + "\n")
        def git(*args, index=None, **kwargs):
            with gitops._host_scope(paths.root):
                return gitops.run(paths.root, *args, env={"GIT_DIR": str(private), "GIT_COMMON_DIR": str(private),
                    "GIT_WORK_TREE": str(pinned), "GIT_INDEX_FILE": str(index or metadata / "index")}, **kwargs)
        yield pinned, git


def archive_untracked(pinned, names, destination, *, metadata=(), prefix=""):
    # Open every parent without following symlinks: checkout content cannot
    # make the host archive files outside this scheduler-owned checkout.
    def add(archive, parent, name, relative):
        info = os.stat(name, dir_fd=parent, follow_symlinks=False)
        member = tarfile.TarInfo(relative)
        member.mode, member.mtime = stat.S_IMODE(info.st_mode), info.st_mtime
        if stat.S_ISLNK(info.st_mode):
            member.type = tarfile.SYMTYPE
            member.linkname = os.readlink(name, dir_fd=parent)
            archive.addfile(member)
        elif stat.S_ISDIR(info.st_mode):
            member.type = tarfile.DIRTYPE
            archive.addfile(member)
            fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            try:
                for child in os.listdir(fd):
                    add(archive, fd, child, relative + "/" + child)
            finally:
                os.close(fd)
        elif stat.S_ISREG(info.st_mode):
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            with os.fdopen(fd, "rb") as source:
                opened = os.fstat(source.fileno())
                if not stat.S_ISREG(opened.st_mode):
                    raise gitops.GitError(f"untracked file was replaced: {relative}")
                member.size = opened.st_size
                archive.addfile(member, source)
        elif stat.S_ISFIFO(info.st_mode):
            member.type = tarfile.FIFOTYPE
            archive.addfile(member)
        else:
            raise gitops.GitError(f"cannot archive untracked path: {relative}")

    with tarfile.open(destination, "w") as archive:
        for path in metadata:
            archive.add(path, arcname=path.name)
        for name in names:
            parts = Path(name).parts
            if not parts or Path(name).is_absolute() or ".." in parts:
                raise gitops.GitError("invalid untracked path")
            parent = gitops._open_beneath(pinned, parts[:-1])
            try:
                add(archive, parent, parts[-1], prefix + name.rstrip("/"))
            finally:
                os.close(parent)


def check_clean(paths, binding, authority):
    if not binding.get("commit"):
        return
    with checkout_git(paths, binding, authority, include_objects=True) as (pinned, git):
        staged = git("diff", "--cached", "--name-status", "HEAD", "--", check=True).out
        # Agent index flags (assume-unchanged/skip-worktree) cannot hide dirty
        # tracked files. Compare the filesystem with a fresh host index, while
        # also retaining changes staged only in the checkout's own index.
        with tempfile.TemporaryDirectory(dir=paths.scheduler) as tmp:
            index = Path(tmp) / "index"
            git("read-tree", "HEAD", index=index, check=True)
            # Protect both HEAD's files (including staged deletions) and new
            # staged files while removing ignored leftovers. The agent's
            # index alone would let a staged deletion discard ignored data.
            protected = set(git("ls-files", "-z", index=index, check=True, strip=False).out.split("\0"))
            for entry in git("ls-files", "--stage", "-z", check=True, strip=False).out.split("\0"):
                if not entry:
                    continue
                metadata, name = entry.split("\t", 1)
                if name not in protected:
                    mode, oid, _ = metadata.split()
                    git("update-index", "--add", "--cacheinfo", f"{mode},{oid},{name}", index=index, check=True)
                    protected.add(name)
            git("clean", "-ffdX", index=index, check=True)
            git("read-tree", "HEAD", index=index, check=True)
            status = git("status", "--porcelain", "--untracked-files=all",
                         "--ignore-submodules=all", index=index, check=True, strip=False).out
            if status or staged:
                tracked = git("diff", "--binary", "HEAD", "--", index=index, check=True, strip=False).out
                checkout_diff = git("diff", "--binary", "HEAD", "--", check=True, strip=False).out
                if checkout_diff != tracked:
                    tracked += checkout_diff
                staged_diff = git("diff", "--binary", "--cached", "HEAD", "--", check=True, strip=False).out
                names = git("ls-files", "--others", "--exclude-standard", "-z", index=index,
                            check=True, strip=False).out.split("\0")
                archive = Path(tempfile.mkdtemp(prefix="dirty-" + binding["id"] + "-", dir=paths.scheduler))
                (archive / "status.txt").write_text(status + "\n" + staged)
                (archive / "tracked.diff").write_text(tracked)
                (archive / "staged.diff").write_text(staged_diff)
                recovery = archive / "archive.tar"
                archive_untracked(pinned, [name for name in names if name], recovery,
                                  metadata=[archive / name for name in ("status.txt", "tracked.diff", "staged.diff")],
                                  prefix="untracked/")
                (paths.scheduler / ("dirty-" + binding["id"] + ".diff")).write_text(status + "\n" + staged + "\n" + tracked + staged_diff)
                raise Refused("dirty_worktree", detail=str(recovery))


def cleanup_message(detail):
    archive = Path(detail)
    excerpts = []
    try:
        with tarfile.open(archive) as bundle:
            for name in ("status.txt", "tracked.diff", "staged.diff"):
                with bundle.extractfile(name) as source:
                    excerpts.append(source.read(20_000).decode("utf-8", "replace"))
    except (OSError, tarfile.TarError, KeyError):
        # An unreadable archive cannot support a recoverable cleanup. Hold
        # with the existing refusal detail instead of launching a callback.
        return None
    inline = "\n".join(excerpts)
    return ("The scheduler refused the next run: dirty_worktree. Clean this alias worktree. "
            "Commit nothing; remove or revert each listed item, and leave the worktree clean. "
            "Say in your reply anything you believe should have been kept. "
            "This cleanup gives no verdict.\n\n" + inline)


def request_cleanup(store, db, node, binding, runner, detail, task, *, replacing=None):
    """Persist TB2-R6's single cleanup reservation before its supervisor runs."""
    from .engine import attempts, save_attempt
    if task is None:
        return None
    activation = node.setdefault("activation_id", uuid.uuid4().hex)
    journal = attempts(db)
    if any(a.get("cleanup") and a["node_id"] == node["id"] and a["activation_id"] == activation
           for a in matching_records(journal, "node_id", node["id"])):
        return None
    previous = runner.tree.get(binding.get("last_run", ""))
    if not previous or not previous.session_id or not binding.get("commit"):
        return None
    prior = next((a for a in matching_records(journal, "run_id", previous.id) if a["run_id"] == previous.id), {})
    route = {"provider": previous.provider, "model": previous.model,
             "account": prior.get("binding", {}).get("account", binding["account"])}
    if unavailable(route, runner):
        return None
    if any(a.get("alias_id") == binding["id"] and a["state"] in {"claimed", "launched", "captured", "suspended"}
           and a["attempt_id"] != replacing for a in select_records(journal, "claimed", "launched", "captured", "suspended")):
        return None
    attempt = {"attempt_id": uuid.uuid4().hex, "activation_id": activation,
               "node_id": node["id"], "run_id": new_id(store.paths, db),
               "state": "claimed", "at": now(), "cleanup": True,
               "cleanup_archive": detail, "cleanup_task": task, "cleanup_agent": previous.agent,
               "cleanup_previous_run": previous.id,
               "cleanup_branch": previous.branch, "cleanup_effort": previous.effort,
               "input_commit": binding["commit"], "previous_commit": binding["commit"],
               "alias_id": binding["id"], "alias_worktree": binding["worktree"],
               "alias_home": binding["home_id"], "session_id": previous.session_id,
               "binding": route}
    # Cleanup acts on the old checkout with its original executor selection.
    if prior.get("launch_evidence", {}).get("executor"):
        selection = prior["launch_evidence"]["executor"]
        attempt["launch_evidence"] = {"executor": {k: selection[k] for k in
            ("kind", "backend", "backend_version", "spec_version") if k in selection}}
    if replacing:
        claimed = journal[replacing]
        attempt.update(locks=claimed.get("locks", []), lock_owners=claimed.get("lock_owners", {}))
        claimed.update(state="abandoned")
        save_attempt(db, claimed)
        db.execute("UPDATE capabilities SET revoked=1 WHERE subject=?", (claimed["run_id"],))
    store.save_node(db, node)
    save_attempt(db, attempt)
    store.transition(db, "cleanup_requested", node["id"],
                     {"alias": node["session"], "alias_id": binding["id"], "run_id": attempt["run_id"], "archive": detail})
    return attempt


def finish_cleanup(store, runner, attempt, run):
    """Settle the cleanup independently of the scheduler's lifetime."""
    from .engine import attempts, save_attempt
    with store.transaction(write=False) as db:
        binding = aliases(db)[attempt["alias_id"]]
    failure = run.status != "done"
    try:
        check_clean(store.paths, binding, runner.authority)
    except (Refused, gitops.GitError, OSError, ValueError):
        failure = True
    with store.transaction() as db:
        current = attempts(db)[attempt["attempt_id"]]
        if current["state"] == "recorded":
            return
        failure |= bool(current.get("cancel_requested"))
        current.update(state="recorded", ended_at=now(), cleanup_succeeded=not failure)
        save_attempt(db, current)
        db.execute("UPDATE capabilities SET revoked=1 WHERE subject=?", (current["run_id"],))
        node = store.nodes(db)[current["node_id"]]
        if failure and node["state"] not in {"held", "cancelled", "done"}:
            node.update(state="held", hold={"reason": "dirty_worktree", "detail": current["cleanup_archive"]},
                        revision=node["revision"] + 1)
            store.save_node(db, node)
            store.transition(db, "dirty_worktree", node["id"], node["hold"])
            store.transition(db, "held", node["id"], node["hold"])
    store.mirror()


def create_checkout(results, authority, path, branch, attempt):
    if attempt.get("cleanup"):
        return
    if attempt.get("launch_evidence", {}).get("executor", {}).get("kind") == "isolated":
        # Provisioning stages the backend's verified clone; never create a
        # shared repository even temporarily for an isolated activation.
        return
    if attempt.get("previous_executor_kind") == "isolated" and path.exists():
        staged = path.with_name(".legacy-" + attempt["attempt_id"])
        results.create_checkout(staged, branch, attempt["input_commit"])
        install_checkout(results.paths, authority, path, staged, attempt)
        return
    if not attempt.get("alias_id") or not path.exists():
        results.create_checkout(path, branch, attempt["input_commit"])
        return
    binding = {"worktree": str(path), "commit": attempt["previous_commit"]}
    with checkout_git(results.paths, binding, authority) as (pinned, git):
        git("reset", "--hard", attempt["input_commit"], check=True)
        gitops._write_beneath(pinned, (".git", "refs", "heads", *branch.split("/")[:-1]),
                             branch.split("/")[-1], (attempt["input_commit"] + "\n").encode(), create=True)
        gitops._write_beneath(pinned, (".git",), "HEAD", ("ref: refs/heads/" + branch + "\n").encode())


def swap_intent(paths, attempt, path, staged, *, complete=False):
    """Persist host-issued swap operands before staging or moving a clone."""
    if not attempt.get("alias_id"):
        return
    from .store import Store
    with Store(paths.root).transaction() as db:
        binding = aliases(db)[attempt["alias_id"]]
        prior = binding.get("swap") or {}
        if prior.get("attempt_id") == attempt["attempt_id"]:
            if complete:
                binding["swap"] = dict(prior, complete=True)
                save_alias(db, binding)
            return
        binding["swap"] = {"state": "staging", "old": str(path), "new": str(staged),
                           "attempt_id": attempt["attempt_id"]}
        save_alias(db, binding)


def checkout_swap(paths, attempt):
    if not attempt.get("alias_id"):
        return {}
    from .store import Store
    with Store(paths.root).transaction(write=False) as db:
        intent = aliases(db)[attempt["alias_id"]].get("swap") or {}
    return intent if intent.get("attempt_id") == attempt["attempt_id"] else {}


def install_checkout(paths, authority, path, staged, attempt):
    """Swap forward with a durable intent and retain the prior gitdir host-side."""
    from .store import Store
    old = path.with_name(".previous-" + attempt["attempt_id"])
    recovery = paths.scheduler / "alias-recovery" / attempt["attempt_id"]

    def record(state):
        if attempt.get("alias_id"):
            with Store(paths.root).transaction() as db:
                binding = aliases(db)[attempt["alias_id"]]
                binding["swap"] = {"state": state, "old": str(old), "new": str(staged),
                                   "attempt_id": attempt["attempt_id"]}
                save_alias(db, binding)

    if checkout_swap(paths, attempt).get("state") != "done":
        record("swapping")
        path.parent.mkdir(parents=True, exist_ok=True)
        # Both names have scheduler-generated basenames and anchored parents.
        with authority.pinned_parent(path) as target, authority.pinned_parent(staged) as source:
            if source.exists():
                if target.exists() and recovery.exists() and attempt.get("alias_id"):
                    # An archive alone does not prove that this staged clone
                    # was installed. Preserve both checkouts on a collision.
                    raise gitops.GitError("checkout recovery archive already exists")
                # A non-alias restart may stage again after archival. The
                # installed checkout is already new; never archive it over
                # the predecessor or leave a second .previous directory.
                if target.exists() and (old.exists() or recovery.exists()):
                    shutil.rmtree(source)
                else:
                    if target.exists():
                        os.replace(target, old)
                    try:
                        os.replace(source, target)
                    except BaseException:
                        if not target.exists() and old.exists():
                            os.replace(old, target)
                        raise
            elif not target.exists():
                raise gitops.GitError("staged clone is missing")
        record("done")
    if old.exists():
        recovery.parent.mkdir(parents=True, exist_ok=True)
        if not recovery.exists():
            shutil.move(str(old), str(recovery))


def settle_checkout(paths, authority, path, attempt):
    """Settle P7 before abandoning an uninvoked resource or its storage."""
    intent = checkout_swap(paths, attempt)
    staged = path.with_name(".seed-" + attempt["attempt_id"])
    old = path.with_name(".previous-" + attempt["attempt_id"])
    if intent.get("rolled_back"):
        if old.exists():
            shutil.rmtree(old)
        return
    if intent.get("state") in {"swapping", "done"} or intent.get("complete"):
        install_checkout(paths, authority, path, staged, attempt)
        if attempt.get("alias_id") and attempt.get("input_commit"):
            from .store import Store
            with Store(paths.root).transaction() as db:
                binding = aliases(db)[attempt["alias_id"]]
                binding["commit"] = attempt["input_commit"]
                save_alias(db, binding)
        return
    # Staging has not recorded a complete swap. Discard a partial copy and
    # keep the clean checkout, retaining its recovery data before releasing it.
    with authority.pinned_parent(path) as target, authority.pinned_parent(staged) as source:
        if source.exists():
            shutil.rmtree(source)
        if not target.exists() and old.exists():
            os.replace(old, target)
        recovery = paths.scheduler / "alias-recovery" / attempt["attempt_id"]
        prior = old if old.exists() else target
        if prior.exists() and not recovery.exists():
            recovery.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix=".archive-", dir=recovery.parent) as tmp:
                archive = Path(tmp) / "checkout"
                shutil.copytree(prior, archive, symlinks=True)
                os.replace(archive, recovery)
    if attempt.get("alias_id") and intent:
        from .store import Store
        with Store(paths.root).transaction() as db:
            binding = aliases(db)[attempt["alias_id"]]
            binding["swap"] = dict(intent, state="done", rolled_back=True)
            save_alias(db, binding)
    if old.exists():
        shutil.rmtree(old)


def retains_checkout(paths, run):
    if not run or not run.node_id:
        return False
    from .store import Store
    with Store(paths.root).transaction(write=False) as db:
        return bool(Store.nodes(db)[run.node_id]["session"])


def cleanup_run(paths, run):
    if not run or not run.node_id:
        return False
    from .store import Store
    with Store(paths.root).transaction(write=False) as db:
        row = db.execute("SELECT record FROM attempts WHERE id=?", (run.attempt_id,)).fetchone()
        return bool(row and json.loads(row[0]).get("cleanup"))
