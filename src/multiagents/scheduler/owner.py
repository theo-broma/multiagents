"""Opt-in lifetime ownership for test schedulers and their detached runs.

An environment marker is authority only when both it and the process identity
can be read. Ordinary schedulers have no marker and keep their detached-worker
semantics. All deadlines here use real time, independently of scheduler clocks.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import errno
import json
import os
from pathlib import Path
import signal
import threading
import time

from .. import procs


__all__ = ["mark_env", "unmarked_env", "marked_processes", "sweep_dead_owners", "reap"]


_KEY = "MULTIAGENTS_SCHEDULER_OWNER"
_RUN_KEY = "MULTIAGENTS_SCHEDULER_RUN_OWNER"
_POLL = 0.2
_GRACE = 1.0


@dataclass(frozen=True)
class _Owner:
    pid: int
    start: str
    project: str


def mark_env(owner_pid, owner_start, project, *, malformed=False) -> dict:
    """The marker variables, to merge over a child's environment."""
    value = json.dumps([1, owner_pid, str(owner_start), str(Path(project).resolve())])
    return {_KEY: "malformed" if malformed else value}


def unmarked_env(env=None) -> dict:
    copied = dict(os.environ if env is None else env)
    copied.pop(_KEY, None)
    copied.pop(_RUN_KEY, None)
    return copied


def _parse(value) -> _Owner | None:
    try:
        version, pid, start, project = json.loads(value)
        if (type(version) is not int or version != 1 or type(pid) is not int or pid <= 0
                or not isinstance(start, str) or not start.isascii() or not start.isdecimal()
                or int(start) <= 0 or not isinstance(project, str) or not project.startswith("/")
                or "\0" in project):
            return None
        return _Owner(pid, start, project)
    except (TypeError, ValueError, RecursionError):
        return None


def _marker(pid: int) -> tuple[bool, _Owner | None]:
    prefix = (_KEY + "=").encode()
    try:
        values = [item[len(prefix):] for item in Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
                  if item.startswith(prefix)]
    except OSError:
        return False, None
    if not values:
        return True, None
    owner = _parse(values[0]) if len(values) == 1 else None
    return owner is not None, owner


def _identity(pid: int) -> str:
    """A readable, same-uid, living process's start time; otherwise no authority."""
    try:
        if Path(f"/proc/{pid}").stat().st_uid != os.getuid():
            return ""
        fields = procs._stat(pid)
        if (len(fields) <= 19 or fields[0] in {"Z", "X"}
                or not fields[19].isascii() or not fields[19].isdecimal()
                or int(fields[19]) <= 0):
            return ""
        return fields[19]
    except (OSError, ValueError, procs._Gone):
        return ""


def _dead(owner: _Owner) -> bool:
    try:
        path = Path(f"/proc/{owner.pid}")
        if path.stat().st_uid != os.getuid():
            return False
        fields = procs._stat(owner.pid)
        if (len(fields) <= 19 or not fields[19].isascii() or not fields[19].isdecimal()
                or int(fields[19]) <= 0):
            return False
        return fields[19] != owner.start or fields[0] in {"Z", "X"}
    except (FileNotFoundError, ProcessLookupError, procs._Gone):
        return True
    except (OSError, ValueError):
        return False                    # unknown is not authority to kill


def _marked() -> dict[int, tuple[str, _Owner]]:
    out = {}
    try:
        names = os.listdir("/proc")
    except OSError:
        return out
    for name in names:
        if not name.isdigit():
            continue
        pid = int(name)
        start = _identity(pid)
        if not start:
            continue
        readable, owner = _marker(pid)
        if readable and owner is not None and _identity(pid) == start:
            out[pid] = (start, owner)
    return out


def marked_processes(owner_pid, project=None) -> set[int]:
    project = str(Path(project).resolve()) if project is not None else None
    return {pid for pid, (_, owner) in _marked().items()
            if owner.pid == owner_pid and (project is None or owner.project == project)}


def _owned_branch(child: int, root: int, owner: _Owner | None) -> bool:
    seen = set()
    while child != root and child not in seen:
        seen.add(child)
        readable, child_owner = _marker(child)
        if not readable or child_owner not in (None, owner):
            return False
        child = procs.parent(child)
        if child is None or child <= 1:
            return False
    return child == root


def _targets(roots: dict[int, str]) -> dict[int, str]:
    targets = dict(roots)
    for pid, start in roots.items():
        if _identity(pid) != start:
            continue
        readable, owner = _marker(pid)
        if not readable:
            continue
        for child in procs.descendants(pid) or []:
            identity = _identity(child)
            if (identity and _owned_branch(child, pid, owner) and _identity(pid) == start):
                targets[child] = identity
    targets.pop(os.getpid(), None)
    return targets


def _wait_children(targets):
    # Only wait for identities we selected. waitpid(-1) could consume a child
    # belonging to an unrelated fixture or to an asyncio subprocess transport.
    for pid, start in targets.items():
        if procs.start_time(pid) != start:
            continue
        try:
            os.waitpid(pid, os.WNOHANG)
        except (ChildProcessError, OSError):
            pass


def _signal(targets, sig):
    for pid, start in targets.items():
        try:
            try:
                fd = os.pidfd_open(pid)
            except AttributeError:
                fd = None
            except OSError as exc:
                if exc.errno != errno.ENOSYS:
                    raise
                fd = None
            if fd is not None:
                try:
                    # Pin the process before checking: a reused numeric pid
                    # after this check cannot redirect the signal.
                    if _identity(pid) != start:
                        continue
                    signal.pidfd_send_signal(fd, sig)
                    continue
                except AttributeError:
                    pass
                except OSError as exc:
                    if exc.errno != errno.ENOSYS:
                        raise
                finally:
                    os.close(fd)
            # Older kernels/Python retain the identity-checked fallback.
            if _identity(pid) == start:
                os.kill(pid, sig)
        except (ProcessLookupError, PermissionError):
            pass


def _terminate(roots: dict[int, str], *, refresh=None) -> None:
    targets = _targets(roots)
    if not targets and refresh is not None:
        targets = _targets(refresh())
    if not targets:
        return
    # Capture descendants before their parents exit and reparent them. A later
    # signal is still authorised by that captured identity, never a pid alone.
    _signal(targets, signal.SIGTERM)
    began = time.monotonic()
    terminated = {(pid, start): began for pid, start in targets.items()}
    killed = set()
    deadline = began + 3 * _GRACE
    while time.monotonic() < deadline:
        # A TERM handler may fork and then exit. Refresh the marked roots too,
        # so its newly reparented children cannot escape the captured tree.
        discovered = _targets(refresh() if refresh is not None else roots)
        fresh = {pid: start for pid, start in discovered.items()
                 if (pid, start) not in terminated}
        targets.update(fresh)
        _signal(fresh, signal.SIGTERM)
        instant = time.monotonic()
        terminated.update({(pid, start): instant for pid, start in fresh.items()})
        _wait_children(targets)
        if not any(_identity(pid) == start for pid, start in targets.items()):
            # The last parent could have forked between discovery and exiting.
            # Its death makes this final scan safe from that same race.
            if refresh is not None and _targets(refresh()):
                continue
            _wait_children(targets)
            return
        expired = {pid: start for pid, start in targets.items()
                   if instant - terminated[pid, start] >= _GRACE
                   and (pid, start) not in killed}
        _signal(expired, signal.SIGKILL)
        killed.update(expired.items())
        time.sleep(0.05)
    _signal(targets, signal.SIGKILL)
    deadline = time.monotonic() + _GRACE
    while time.monotonic() < deadline:
        _wait_children(targets)
        if not any(_identity(pid) == start for pid, start in targets.items()):
            _wait_children(targets)
            return
        time.sleep(0.05)
    _wait_children(targets)


def reap(owner_pid, project) -> None:
    project = str(Path(project).resolve())

    def roots():
        return {pid: start for pid, (start, owner) in _marked().items()
                if owner.pid == owner_pid and owner.project == project}

    _terminate(roots(), refresh=roots)


def sweep_dead_owners() -> None:
    marked = _marked()
    dead = {owner for _, owner in marked.values() if _dead(owner)}

    def roots():
        return {pid: start for pid, (start, owner) in _marked().items()
                if owner in dead and _dead(owner)}

    _terminate({pid: start for pid, (start, owner) in marked.items() if owner in dead},
               refresh=roots)


def _check() -> bool:
    """Immediately before a launch: absent or invalid markers remain inert."""
    owner = _parse(os.environ.get(_KEY))
    return owner is None or not _dead(owner)


def _forward_env(env: dict) -> None:
    """Keep the lifetime marker through the agent environment's clean base."""
    value = os.environ.get(_KEY)
    if _parse(value) is not None:
        env[_KEY] = value
        if _RUN_KEY in os.environ:
            env[_RUN_KEY] = os.environ[_RUN_KEY]


def _bind_run(run_id: str) -> None:
    # A replacement supervisor adopts the same run, including its already
    # reparented wrappers. The token must survive supervisor pid changes.
    if _parse(os.environ.get(_KEY)) is not None:
        os.environ[_RUN_KEY] = json.dumps(["run", run_id])


def _worker_env(run_id: str) -> dict | None:
    """Bind the run before a worker can block in startup or store access."""
    if (_parse(os.environ.get(_KEY)) is None
            or not isinstance(run_id, str) or not run_id):
        return None
    return dict(os.environ, **{_RUN_KEY: json.dumps(["run", run_id])})


@contextmanager
def _watch(*, run=False):
    """Monitor even when the main thread blocks in startup or admission.

    Each worker owns its cleanup; it does not need its daemon to survive. A
    normal daemon stop only cancels this monitor and never ends its workers.
    """
    owner = _parse(os.environ.get(_KEY))
    stopped = threading.Event()
    pid = os.getpid()
    token = json.dumps([pid, procs.start_time(pid)])
    previous = os.environ.get(_RUN_KEY)
    if owner is not None and run:
        try:
            previous_run = json.loads(previous)
        except (TypeError, ValueError, RecursionError):
            previous_run = None
        if (isinstance(previous_run, list) and len(previous_run) == 2
                and previous_run[0] == "run" and isinstance(previous_run[1], str)
                and previous_run[1]):
            token = previous
        os.environ[_RUN_KEY] = token

    def own_run(child):
        if not run:
            return True
        try:
            value = os.environ.get(_RUN_KEY, token)
            return (_RUN_KEY + "=" + value).encode() in Path(f"/proc/{child}/environ").read_bytes().split(b"\0")
        except OSError:
            return False

    def cleanup():
        # The run token also finds marked grandchildren whose wrapper
        # exited and reparented them before owner death.
        def roots():
            selected = {child: start for child, (start, marked_owner) in _marked().items()
                        if marked_owner == owner and own_run(child)}
            selected[pid] = procs.start_time(pid)
            return selected

        _terminate(roots(), refresh=roots)
        os._exit(0)                     # bounded even if startup holds a lock

    def monitor():
        while not stopped.is_set():
            if _dead(owner):
                cleanup()
            stopped.wait(_POLL)

    thread = None
    if owner is not None:
        thread = threading.Thread(target=monitor, name="scheduler-owner", daemon=True)
        thread.start()
    try:
        yield
    finally:
        if owner is not None and _dead(owner):
            cleanup()
        stopped.set()
        if thread is not None:
            thread.join(timeout=4 * _GRACE + _POLL)
        if owner is not None and run:
            if previous is None:
                os.environ.pop(_RUN_KEY, None)
            else:
                os.environ[_RUN_KEY] = previous
