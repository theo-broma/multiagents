"""THE SEAM for SL-R6..SL-R9 (`context/specs/test-process-leak.md`, part 2).

Re-exports the five names of the production module `multiagents.scheduler.owner`
(the Decision "who writes what" of the spec): `mark_env`, `unmarked_env`,
`marked_processes`, `sweep_dead_owners`, `reap`. `tests/test_sl_owner_dies.py`
is written against exactly these names. The marker's concrete form (env var
names, encoding) is the implementer's choice and is invisible to the tests:
they only go through this module.

While that module does not exist (`ImportError`, and only that), the names are
stubs raising `NotImplementedError`, so the SL tests fail on their own
assertions, and `AVAILABLE` is False. The harness wiring (`harness_env`,
`harness_reap`, `session_sweep`, used by `World`/`ClockWorld`/`NtWorld`,
`nc_harness.Sched` and `tests/conftest.py`) then falls back to today's
behaviour: no marker, the harness's existing teardown, no sweep.

Conventions the tests rely on:
- an *owner* is a process, identified by `(pid, start)`; `start` is field 22 of
  `/proc/<pid>/stat` as an int (clock ticks since boot).
- a process is *marked* for an owner when the marker it carries (inherited
  through the environment, or any other channel the implementer picks) names
  that owner and a project path.
- a harness (`World`, `ClockWorld`, `Sched`, `NtWorld`) that starts a daemon
  marks it with the CALLING process as owner (`os.getpid()` and its own start
  time), never with a marker it inherited from its environment: a pytest
  session started from inside another marked process owns its own daemons.
"""
from __future__ import annotations

import os
from pathlib import Path

try:
    from multiagents.scheduler.owner import (  # noqa: F401
        mark_env, marked_processes, reap, sweep_dead_owners, unmarked_env)
    AVAILABLE = True
except ImportError:
    AVAILABLE = False

    def mark_env(owner_pid: int, owner_start: int, project: Path, *,
                 malformed: bool = False) -> dict[str, str]:
        """Environment variables that make a process launched with them (merged
        over a copy of `os.environ`) carry the marker for owner `(owner_pid,
        owner_start)` and `project`. With `malformed=True` the marker is present but
        unparseable / internally inconsistent (it must never give authority to kill)."""
        raise NotImplementedError("SL-R6 seam: mark_env")

    def unmarked_env(env: dict[str, str] | None = None) -> dict[str, str]:
        """A copy of `env` (default `os.environ`) with every marker variable removed."""
        raise NotImplementedError("SL-R6 seam: unmarked_env")

    def marked_processes(owner_pid: int, project: Path | None = None) -> set[int]:
        """Pids of live (not zombie) processes carrying a marker whose owner pid is
        `owner_pid`, whether or not that owner is alive; restricted to those whose
        marker names `project` when given. Reads the marker the way the sweep does."""
        raise NotImplementedError("SL-R6 seam: marked_processes")

    def sweep_dead_owners() -> None:
        """The session-start sweep (SL-R9), callable directly: kill marked scheduler
        and worker processes whose owner is dead, plus their verified descendants.
        Idempotent; safe to run concurrently with itself."""
        raise NotImplementedError("SL-R9 seam: sweep_dead_owners")

    def reap(owner_pid: int, project: Path) -> None:
        """The shared teardown helper (SL-R8): kill the live processes marked for
        owner `owner_pid` and `project` (whatever their argv), plus their
        verified descendants. Never touches an unmarked process or one marked
        for another owner or another project."""
        raise NotImplementedError("SL-R8 seam: reap")


# ------------------------------------------------------- the harness wiring

def own_start(pid: int | None = None) -> int:
    """Field 22 of `/proc/<pid>/stat` (default: this process) as an int."""
    stat = Path(f"/proc/{pid or 'self'}/stat").read_text()
    return int(stat.rsplit(")", 1)[1].split()[19])


def harness_env(project: Path, base: dict[str, str] | None = None) -> dict[str, str]:
    """The environment a harness launches its daemon with: `base` (default
    `os.environ`) with any inherited marker removed and this process marked as
    owner for `project`. Without the production module: a plain copy of `base`."""
    env = dict(os.environ if base is None else base)
    if not AVAILABLE:
        return env
    return dict(unmarked_env(env), **mark_env(os.getpid(), own_start(), Path(project)))


def harness_reap(project: Path) -> bool:
    """Teardown: `reap(os.getpid(), project)`. False (and nothing done) without
    the production module, so the caller keeps its own cleanup."""
    if not AVAILABLE:
        return False
    reap(os.getpid(), Path(project))
    return True


def session_sweep() -> None:
    """The session-start sweep (SL-R9); nothing without the production module."""
    if AVAILABLE:
        sweep_dead_owners()
