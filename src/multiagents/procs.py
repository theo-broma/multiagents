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
