"""Is this pid still the process we recorded, or merely *a* process?

``os.kill(pid, 0)`` answers the second question, and everything here exists
because the first is the one that matters. Pids are recycled: Linux hands them
out in order and wraps at ``kernel.pid_max``, and a reboot restarts the count
from the low numbers. Crash recovery runs in exactly that situation — the pids
in ``tree.json`` and ``launch/*.pid`` were all recorded before a machine that
is no longer the machine that is running now — so every one of them is stale,
and some of them are alive again as something else entirely.

Both directions of that mistake are silent and neither is cheap:

* A recycled agent pid reads as alive, and the reaper answers that by sending
  SIGTERM to its whole *process group* — someone else's editor, build, or
  shell session, killed by a tool that was trying to tidy up after itself.
* A recycled ``orchestrator.pid`` makes ``multiagents run`` conclude another
  session owns the project's agents, so it skips reconciliation altogether and
  leaves nodes stuck ``running`` with uncommitted worktrees, forever.

The fix is to record what the kernel already tracks: field 22 of
``/proc/<pid>/stat``, the process's start time in clock ticks since boot. It is
assigned once and never changes, a recycled pid cannot reproduce it, and it
needs no dependency and no privilege to read.

Where there is no ``/proc`` the start time reads as ``""``, and every check
here falls back to the bare ``os.kill`` it replaced. That is the old behaviour,
which is wrong in the ways described above — but it is wrong no more often than
before, and a recovery that refuses to run at all would be worse.
"""

from __future__ import annotations

import os
from pathlib import Path


def start_time(pid: int) -> str:
    """Field 22 of ``/proc/<pid>/stat``, or ``""`` if it cannot be read.

    Kept as the kernel's own string rather than converted to a wall clock. The
    only thing ever done with it is comparing it to another reading of the same
    field, and a conversion needs the boot time and the tick rate — two more
    things to get wrong in service of a number nobody reads.
    """
    if pid is None or pid <= 0:
        return ""
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return ""
    # Field 2 is the executable name, in parentheses, and it may itself contain
    # both spaces and parentheses — `(Web Content)`, or a process that renamed
    # itself to something worse. Splitting on whitespace from the left puts
    # every later field at an offset that depends on the name. The last ')' in
    # the line is unambiguous, so the split happens there.
    _, _, rest = raw.rpartition(")")
    fields = rest.split()
    # `rest` starts at field 3 (state), so field 22 is index 19.
    return fields[19] if len(fields) > 19 else ""


def alive(pid: int | None, start: str = "") -> bool:
    """Is the process we recorded as `pid` still running?

    `start` is its recorded start time, from `start_time` at the moment the pid
    was written down. Given one, a pid whose current start time disagrees is a
    different process wearing a recycled number, and the answer is False even
    though something by that name exists.
    """
    if not pid or pid <= 0:
        return False
    if start:
        current = start_time(pid)
        # Only a reading that disagrees is evidence. An empty one means the
        # process is gone, or that this is not Linux; `os.kill` below settles
        # the first and is all we have for the second.
        if current and current != start:
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True                      # exists, owned by someone else
    except (OSError, ValueError):
        return False
    return True


def parent(pid: int) -> int | None:
    """Field 4 of ``/proc/<pid>/stat``, the parent's pid, or None if unknown."""
    if pid is None or pid <= 0:
        return None
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    # Split after the last ')' for the reason `start_time` gives: `rest`
    # starts at field 3, so field 4 is index 1.
    fields = raw.rpartition(")")[2].split()
    try:
        return int(fields[1])
    except (IndexError, ValueError):
        return None


class _Gone(Exception):
    """The process does not exist (any more)."""


def _stat(pid: int) -> list[str]:
    """``/proc/<pid>/stat`` from field 3 on. Raises `_Gone` when there is no
    such process, `OSError` or `ValueError` when it exists and cannot be
    read: unknown is not gone."""
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
    except (FileNotFoundError, ProcessLookupError):
        raise _Gone(pid) from None
    fields = raw.rpartition(")")[2].split()
    if len(fields) < 2:
        raise ValueError(f"unreadable /proc/{pid}/stat")
    int(fields[1])
    return fields


def living(pid: int | None, start: str = "") -> bool:
    """`alive`, and not a zombie: a process that has exited but not been
    reaped still answers `kill(pid, 0)`, and is dead for every purpose a
    caller of this has."""
    if not alive(pid, start):
        return False
    try:
        return _stat(int(pid))[0] not in ("Z", "X")
    except (_Gone, OSError, ValueError):
        # Gone since `alive` looked, or no /proc to ask: `alive` has decided.
        return True


def descends_from(pid: int, ancestor: int) -> bool | None:
    """Is `ancestor` among `pid`'s ancestors? None where that cannot be read —
    no `/proc`, an unreadable entry, or a chain broken while it was walked."""
    seen: set[int] = set()
    cursor = pid
    while cursor and cursor not in seen:
        seen.add(cursor)
        try:
            up = int(_stat(cursor)[1])
        except _Gone:
            return False if cursor == pid else None
        except (OSError, ValueError):
            return None
        if up == ancestor:
            return True
        if up <= 1:
            return False
        cursor = up
    return False


def descendants(pid: int) -> list[int] | None:
    """Every live process below `pid`, from ``/proc``; None where that cannot
    be established — no `/proc`, or any process whose parent cannot be read
    (it could be below `pid`, so an answer without it would be a guess)."""
    proc = Path("/proc")
    try:
        names = [n for n in os.listdir(proc) if n.isdigit()]
    except OSError:
        return None
    children: dict[int, list[int]] = {}
    for name in names:
        try:
            up = int(_stat(int(name))[1])
        except _Gone:
            continue                    # exited since the listing
        except (OSError, ValueError):
            return None
        children.setdefault(up, []).append(int(name))
    out, todo = [], list(children.get(pid, []))
    while todo:
        child = todo.pop()
        if child in out:
            continue
        out.append(child)
        todo.extend(children.get(child, []))
    return out
