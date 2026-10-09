"""Host account state and transcript transport for launched roles (OA).

Identities are deliberately ephemeral: only the driver terminal receives them.
The durable record contains provider names, session ids and switch phases.
"""
from __future__ import annotations

import ctypes
import fcntl
import json
import math
import os
import re
import shutil
import stat
import time
from dataclasses import replace
from pathlib import Path

from . import budget, gitops, scripts
from .executor import get_executor
from .executor.local import LocalExecutor
from .paths import global_config_dir
from .providers import resolved_profile
from .transcripts import session_transcript


def host_budget(name, provider, config, *, fetch=True):
    _, reader = budget._builtin_for(name, provider, None)
    if (getattr(provider, "budget_profile_env", "") and reader is not None
            and budget.reader_takes_profile(reader)):
        profile = resolved_profile(provider)
        reading = budget._call_reader(reader, config_dir=Path(profile) if profile else None,
                                      limits=config.limits, fetch=fetch)
        reading = budget._apply_reading_age(
            reading, budget.reading_age_bound(getattr(config, "project", {})), time.time())
        return replace(reading, provider=name)
    layers = getattr(config, "layers", [])
    return budget.read_all({name: provider}, lambda _: LocalExecutor(),
                           global_config_dir(), layers[-1] if layers else None,
                           {}, {}, use_cache=False, limits=getattr(config, "limits", None),
                           max_reading_age=budget.reading_age_bound(
                               getattr(config, "project", {}))).get(name)


def identity(name, provider, paths, executor=None):
    try:
        code, out, _ = scripts.run_action(
            name, provider, executor if executor is not None else LocalExecutor(),
            "identity", global_config_dir(),
            paths.config, timeout=5, cwd=paths.root)
        data = json.loads(out) if code == 0 else None
        value = data.get("identity") if isinstance(data, dict) else None
        if isinstance(value, str) and value.strip() and len(value) <= 254 and not any(
                ord(c) < 32 or ord(c) == 127 for c in value):
            return value
    except (OSError, ValueError, TypeError):
        pass
    return None


def agent_executor(paths, config, providers):
    """The project route, rather than the root role's host-only executor."""
    return get_executor(config.executor, config.project.get("executor", {}).get("docker", {}),
                        paths=paths, providers=providers, config_dir=global_config_dir())


def same_account(root_identity, agent_identity, *, clearing=False):
    """Unknown routes may be paused, but can never prove recovery (OA-R8/QF-R6)."""
    if root_identity is None or agent_identity is None:
        return not clearing
    return root_identity == agent_identity


def limit_records(paths, role):
    try:
        raw = gitops._read_beneath(paths.data, ("launch",), f"{role}.limit.json", 65536)
        value = json.loads(raw.decode("utf-8", errors="replace") if raw else "null")
        records = value.get("limits", [value]) if isinstance(value, dict) else []
        if isinstance(records, list):
            return [r for r in records if isinstance(r, dict)
                    and isinstance(r.get("provider"), str)
                    and isinstance(r.get("until"), (float, int))
                    and not isinstance(r["until"], bool) and math.isfinite(r["until"])
                    and r["until"] > time.time()]
    except (ValueError, TypeError):
        pass
    return []


def write_limit(paths, role, provider, detail, until, cause):
    from .driver import _launch_write
    records = [r for r in limit_records(paths, role) if r["provider"] != provider]
    records.append({"provider": provider, "detail": detail, "until": until, "cause": cause})
    _launch_write(paths, f"{role}.limit.json", json.dumps({"limits": records}))


def clear_limit(paths, role, provider, *, cause=None):
    from .driver import _launch_write
    records = limit_records(paths, role)
    kept = [r for r in records if r["provider"] != provider
            or (cause is not None and r.get("cause") != cause)]
    if kept != records:
        _launch_write(paths, f"{role}.limit.json", json.dumps({"limits": kept}))


def _route_cleared(record, name, root_identity, agent_identity):
    cleared = record.get("cleared_for")
    mode = cleared.get(name) if isinstance(cleared, dict) else None
    return mode == "agent" or (mode == "host" and same_account(
        root_identity, agent_identity, clearing=True))


def _agent_reading_usable(config, provider, reading):
    if reading is None:
        return False
    if config.executor == "docker" and reading.account_readings:
        label = (getattr(provider, "container_account", "") or
                 getattr(getattr(provider, "auth_owner", None), "container_account", "") or
                 "default")
        reading = reading.account_readings.get(label)
        if reading is None:
            return False
    reading = budget._apply_reading_age(
        reading, budget.reading_age_bound(config.project), time.time())
    return reading.known and not reading.stale and reading.usable


def clear_agent_limits(paths, config, providers, usable, *, root_identity=None, readings=None):
    """Lift quota projections, keeping other and unknown routes paused.

    A host reading needs account equivalence. An explicit agent-account
    refresh proves its own route healthy, including when identity is unknown.
    The record keeps only provider names and the kind of proof, never identity.
    """
    from .driver import _launch_write
    if not usable:
        return set()
    mode = "host" if root_identity is not None else "agent"
    agents = {}
    if mode == "agent":
        usable = {n for n in usable if n in providers and _agent_reading_usable(
            config, providers[n], (readings or {}).get(n))}
        if not usable:
            return set()
        executor = agent_executor(paths, config, providers)
        agents = {n: identity(n, providers[n], paths, executor)
                  for n in usable if n in providers}
    lifted = set()
    for role in ("orchestrator", "initializer"):
        records = limit_records(paths, role)
        changed = False
        for record in records:
            if record.get("cause") != "quota":
                continue
            source = record["provider"]
            who = identity(source, providers[source], paths) if source in providers else None
            if mode == "host" and not same_account(root_identity, who, clearing=True):
                continue
            for name in usable:
                agent = root_identity if mode == "host" else agents.get(name)
                if (same_account(who, agent) and not _route_cleared(record, name, who, agent)):
                    cleared = record.get("cleared_for")
                    record["cleared_for"] = {
                        **(cleared if isinstance(cleared, dict) else {}), name: mode}
                    lifted.add(name)
                    changed = True
        if changed:
            _launch_write(paths, f"{role}.limit.json", json.dumps({"limits": records}))
    return lifted


def agent_limit_pauses(paths, config, providers, options):
    """Project-account projections of root pauses, with no durable identity.

    Recheck the routes at admission: a vault default or a host login may have
    changed since the root hit its limit. Independent role records also leave
    unrelated agent pauses and their expiry intact.
    """
    records = [r for role in ("orchestrator", "initializer")
               for r in limit_records(paths, role)]
    if not records:
        return []
    executor = agent_executor(paths, config, providers)
    agents = {name: identity(name, providers[name], paths, executor)
              for name in options if name in providers}
    pauses = []
    for record in records:
        name = record["provider"]
        who = identity(name, providers[name], paths) if name in providers else None
        affected = [n for n in options if same_account(who, agents.get(n))
                    and not _route_cleared(record, n, who, agents.get(n))]
        if affected:
            pauses.append({"until": record["until"], "providers": affected,
                           "reason": f"{name}: {record.get('detail') or 'usage limit'}"})
    return pauses


def clear_agent_quota(paths, config, providers, name):
    """Project fresh host headroom onto only proven matching agent accounts."""
    from .tree import Tree
    tree = Tree(paths.tree_file, paths.events_file)
    state = tree.read()
    records = [(role, r) for role in ("orchestrator", "initializer")
               for r in limit_records(paths, role) if r.get("cause") == "quota"]
    candidates = (set((state.get("pause") or {}).get("providers") or [])
                  | set(state.get("cooldowns") or {}) | (set(providers) if records else set()))
    if not candidates:
        return
    who = identity(name, providers[name], paths)
    if who is None:
        return
    executor = agent_executor(paths, config, providers)
    usable = {n for n in candidates if n in providers
              and same_account(who, identity(n, providers[n], paths, executor), clearing=True)}
    tree.clear_quota(usable)
    clear_agent_limits(paths, config, providers, usable, root_identity=who)


def launch_line(paths, config, spec, provider, *, supervised=True):
    import sys
    if provider is None or not hasattr(config, "limits"):
        return
    who = identity(spec.provider, provider, paths) or "identity unknown"
    reading = host_budget(spec.provider, provider, config, fetch=False)
    room = (f"{reading.headroom:.0%}" if reading and reading.known
            and reading.headroom is not None else "unknown")
    notice = "" if supervised else "; automatic switching unavailable"
    print(f"root account {spec.provider}: {who}; headroom {room}{notice}",
          file=sys.stdout if sys.stdout.isatty() else sys.stderr, flush=True)


def record(paths, role):
    from .driver import _launch_read
    try:
        value = json.loads(_launch_read(paths, f"{role}.switch.json", 65536) or "null")
        if (isinstance(value, dict) and value.get("role") == role
                and all(isinstance(value.get(key), str) and value[key]
                        for key in ("source", "target", "session"))
                and value.get("phase") in ("installing", "launched", "resumed")
                and re.fullmatch(r"[A-Za-z0-9_-]+", value.get("session", ""))):
            return value
    except (ValueError, TypeError):
        pass
    return None


def write_record(paths, role, source, target, session, phase):
    from .driver import _launch_write
    _launch_write(paths, f"{role}.switch.json", json.dumps({
        "role": role, "source": source, "target": target,
        "session": session, "phase": phase}) + "\n")


def reported_root(paths, config):
    from .tree import Tree
    nodes = Tree(paths.tree_file, paths.events_file).drivers()
    if nodes:
        node = max(nodes, key=lambda n: n.started_at or 0)
        return {"role": node.role, "provider": reported_provider(paths, node.role, node.session, node.provider)}
    from .driver import _launched_spec
    spec = _launched_spec(config, "orchestrator")
    return {"role": "orchestrator", "provider": spec.provider} if spec else None



def reported_provider(paths, role, session, fallback):
    state = record(paths, role)
    if state and state["session"] == session:
        return state["target"] if state["phase"] == "resumed" else state["source"]
    return fallback


def transcript_size(provider, paths, session):
    path = session_transcript(provider, paths.root, session, LocalExecutor())
    try:
        return path.stat().st_size if path else 0
    except OSError:
        return 0


def _directory(path, *, create=False):
    """Open each component without following a link, including ancestors."""
    path = Path(path).absolute()
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            if create:
                try:
                    os.mkdir(part, dir_fd=fd)
                except FileExistsError:
                    pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def _copy_entry(src, name, dst, *, replace_prefix=False):
    """Copy only regular files/directories, through pinned directory fds."""
    info = os.stat(name, dir_fd=src, follow_symlinks=False)
    if stat.S_ISDIR(info.st_mode):
        os.mkdir(name, dir_fd=dst)
        s = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=src)
        d = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dst)
        try:
            for entry in os.listdir(s):
                _copy_entry(s, entry, d)
            os.fsync(d)
        finally:
            os.close(s)
            os.close(d)
    elif stat.S_ISREG(info.st_mode):
        s = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=src)
        try:
            if not stat.S_ISREG(os.fstat(s).st_mode):
                raise OSError("not a regular transcript file")
            d = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                        0o600, dir_fd=dst)
            try:
                with os.fdopen(os.dup(s), "rb") as source, os.fdopen(os.dup(d), "wb") as target:
                    shutil.copyfileobj(source, target)
                    target.flush()
                os.fsync(d)
            finally:
                os.close(d)
        finally:
            os.close(s)
    else:
        raise OSError("symlink or special file in session")


def _equal(src, dst, name, *, older=False):
    a = os.stat(name, dir_fd=src, follow_symlinks=False)
    b = os.stat(name, dir_fd=dst, follow_symlinks=False)
    if stat.S_ISDIR(a.st_mode) and stat.S_ISDIR(b.st_mode):
        s = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=src)
        d = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dst)
        try:
            sn, dn = set(os.listdir(s)), set(os.listdir(d))
            return (dn <= sn if older else dn == sn) and all(
                _equal(s, d, entry, older=older) for entry in dn)
        finally:
            os.close(s)
            os.close(d)
    if not stat.S_ISREG(a.st_mode) or not stat.S_ISREG(b.st_mode):
        return False
    s = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=src)
    d = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dst)
    try:
        with os.fdopen(os.dup(s), "rb") as sh, os.fdopen(os.dup(d), "rb") as dh:
            while chunk := dh.read(65536):
                if sh.read(len(chunk)) != chunk:
                    return False
            return older or not sh.read(1)
    finally:
        os.close(s)
        os.close(d)


def install(paths, source, target, session, *, older=False):
    """Publish the transcript and companion together, under a profile lock.

    Updating an existing project directory uses Linux renameat2 exchange: both
    generations remain complete even if the driver is killed during publication.
    A recorded return home may replace a byte-prefix of the newer session;
    unrelated or diverged files are never overwritten.
    """
    if not re.fullmatch(r"[A-Za-z0-9_-]+", session):
        raise ValueError("invalid session id")
    src_path = session_transcript(source, paths.root, session, LocalExecutor())
    dst_path = session_transcript(target, paths.root, session, LocalExecutor())
    if src_path is None or dst_path is None:
        raise OSError("provider does not declare session transcripts")
    if src_path == dst_path:
        return
    # Lock the profile directory itself: no extra account data file is written.
    profile = dst_path.parent.parent.parent
    lock = _directory(profile)
    src = projects = stage_fd = old = None
    stage = None
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        src = _directory(src_path.parent)
        projects = _directory(dst_path.parent.parent, create=True)
        if not os.fstat(projects).st_mode & 0o222:
            raise PermissionError("target projects directory is not writable")
        slug = dst_path.parent.name
        try:
            old = os.open(slug, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                          dir_fd=projects)
        except FileNotFoundError:
            pass
        names = [src_path.name]
        if session in os.listdir(src):
            names.append(session)
        for name in names:
            if old is not None and name in os.listdir(old) and not _equal(
                    src, old, name, older=older):
                raise FileExistsError("target session differs")
        import uuid
        stage = f".oa-install-{uuid.uuid4().hex}"
        os.mkdir(stage, mode=0o700, dir_fd=projects)
        stage_fd = os.open(stage, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=projects)
        if old is not None:
            for name in os.listdir(old):
                if name not in names:
                    _copy_entry(old, name, stage_fd)
        for name in names:
            _copy_entry(src, name, stage_fd)
        os.fsync(stage_fd)
        gate = os.environ.get("MULTIAGENTS_OA_INSTALL_PAUSE")
        while gate and not Path(gate).exists():
            time.sleep(0.05)
        if old is None:
            os.rename(stage, slug, src_dir_fd=projects, dst_dir_fd=projects)
            stage = None
        else:
            libc = ctypes.CDLL(None, use_errno=True)
            exchange = getattr(libc, "renameat2", None)
            if exchange is None:
                raise OSError("atomic directory exchange is unavailable")
            if exchange(projects, os.fsencode(stage), projects,
                        os.fsencode(slug), 2) != 0:  # RENAME_EXCHANGE
                raise OSError(ctypes.get_errno(), "atomic session installation failed")
        os.fsync(projects)
    finally:
        if stage and projects is not None:
            shutil.rmtree(stage, dir_fd=projects)
        for fd in (stage_fd, old, projects, src):
            if fd is not None:
                os.close(fd)
        os.close(lock)


def recover(paths, providers, role, configured, session):
    state = record(paths, role)
    if not state or state["session"] != session:
        return
    latest = state["target"] if state["phase"] == "resumed" else state["source"]
    # The target may have replied before the driver could confirm it. An
    # interrupted confirmation must not make that exchange disappear.
    if state["source"] in providers and state["target"] in providers:
        source = session_transcript(providers[state["source"]], paths.root, session, LocalExecutor())
        target = session_transcript(providers[state["target"]], paths.root, session, LocalExecutor())
        if source and target and source != target and target.exists():
            s = t = None
            try:
                t = _directory(target.parent)
                try:
                    s = _directory(source.parent)
                    present = source.name in os.listdir(s)
                except FileNotFoundError:
                    present = False
                if not present or _equal(t, s, source.name, older=True):
                    latest = state["target"]
                elif not _equal(s, t, source.name, older=True):
                    raise OSError("interrupted session copies diverged")
            finally:
                for fd in (s, t):
                    if fd is not None:
                        os.close(fd)
    if latest == configured or latest not in providers:
        return
    install(paths, providers[latest], providers[configured], session, older=True)
    print(f"{role}: brought the newer quota session back to {configured}; resuming.")


def safe_fresh_directory(paths, provider):
    """A rejected project-directory link must not reach a fresh CLI either."""
    path = session_transcript(provider, paths.root, "fresh", LocalExecutor())
    if path is None:
        return
    projects = None
    lock = _directory(path.parent.parent.parent)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        projects = _directory(path.parent.parent)
        name = path.parent.name
        info = os.stat(name, dir_fd=projects, follow_symlinks=False)
        if stat.S_ISLNK(info.st_mode):
            import uuid
            os.rename(name, f".oa-conflict-{uuid.uuid4().hex}",
                      src_dir_fd=projects, dst_dir_fd=projects)
            os.mkdir(name, dir_fd=projects)
    except FileNotFoundError:
        pass
    finally:
        if projects is not None:
            os.close(projects)
        os.close(lock)
