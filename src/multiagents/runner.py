"""Spawning, streaming, supervising and collecting agent runs.

This is where the pieces meet. A run is: allocate a tree node, cut a worktree and
branch, build a private HOME and a deny-by-default environment, compose the
prompt, start the process through an executor, then consume its event stream —
writing a per-run log, rolling usage up the tree, and letting the supervisor
watch for the four stuck conditions.

Two rules shape the interface:

* **Context isolation.** A run's full transcript never comes back through a tool
  result. Callers get a bounded summary plus a path, and read more only if they
  ask. Returning everything would make delegating pointless.
* **Nothing merges itself into your work.** Agents squash-merge their own
  children, because that work is still quarantined on their branch. Landing on
  the base branch is always an explicit call.
"""

from __future__ import annotations

import asyncio
import contextlib
from contextvars import ContextVar
import copy
import fcntl
import functools
import signal
import hashlib
import json
import os
import random
import re
import shutil
import stat
import tempfile
import textwrap
import time
import traceback
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

from . import budget as budget_mod
from . import gitops
from . import config as config_mod
from .config import AgentSpec, Config, budget_number, limit_number, matches_any
from .executor import build_env, get_executor, prepare_home, private_file
from .executor.base import (BASE_ENV_KEYS, FollowHandle, Handle, read_exit_status,
                            running, session_alive, stop_wrapped)
from . import providers as providers_mod
from . import notices
from . import spendcap
from . import procs
from . import scripts
from . import paths as paths_mod
from .paths import ProjectPaths, global_config_dir, state_root
from .providers import (Event, Provider, expand_env_value, get_path,
                        load_providers, resolved_profile)
from .redact import scrub
from .safepoint import Gate, strip_key
from .auth import looks_like_auth_failure
from .startup import StartupHealth, StartupUnavailable
from .authority import HostAuthority
from .launch_limits import LaunchLimits
from .occupancy import ContainerOccupancy
from .executor.docker import DockerExecutor
from .supervisor import Supervisor, looks_like_quota_failure
from .tree import (ACTIVE, PAUSED, DRIVER_ROLES, PC_CAUSE, TERMINAL, Node, Tree,
                   deferred_malformed, find_deferred, new_id, node_from_raw,
                   now, pc_waiting)
from .transcripts import session_transcript
from .quota_handover import QuotaHandover, promotion_merge_guard

MAX_SUMMARY_CHARS = 6000
_HOST_HOOK_NOTIFIED: set[Path] = set()

# How often stream progress is flushed to the shared tree. The watchdogs read
# the in-process Supervisor, not the tree, so this bounds disk writes without
# affecting supervision; it only delays what another process sees in
# check_agent by at most this long.
TREE_FLUSH_SECONDS = 2.0
# SV-R5: the flock on this file in a run dir is "this server supervises it".
SUPERVISOR_LOCK = "supervisor.lock"
# SV-R6: how often a root server looks for runs nobody is supervising.
ADOPT_SECONDS = 5.0
# How long a steer waits for the resumed run to show it is alive — the
# first stream event, or its death, whichever arrives. Only the ceiling;
# a healthy agent usually settles it in well under a second.
STEER_CONFIRM_SECONDS = 5.0
WRAP_UP = (
    "STOP AND HAND OVER. {provider} is about to run out of quota — roughly "
    "{minutes} minute(s) of it left, shared with every other agent running "
    "right now. You are being interrupted deliberately, before it cuts you off "
    "mid-thought, because what you leave behind decides what the next run "
    "costs.\n\n"
    "Do exactly this, and nothing else:\n"
    "1. Commit whatever currently works, even if incomplete. An uncommitted "
    "worktree is the one thing that cannot be recovered.\n"
    "2. Write a short handoff — what is done, what is left, which file you were "
    "in the middle of, and anything you worked out that is not obvious from the "
    "diff.\n"
    "3. Stop. Do not start anything new.\n\n"
    "The work resumes from your branch and this handoff, not from your memory "
    "of this conversation: after the window resets, that memory costs more to "
    "reload than it is worth."
)
# CI-R5: what a fix turn is told. The hook's output is the tail, and at least
# the last 4000 characters of it — a formatter's complaint is at the end.
COMMIT_FIX_OUTPUT_CHARS = 8000
COMMIT_FIX = (
    "Your end-of-run commit was refused by the repository's `{hook}` git hook "
    "(fix attempt {attempt} of {allowed}). Your work is still in the worktree, "
    "uncommitted. Fix what the hook reports below, then end your turn: the "
    "runner commits again afterwards, and committing it yourself is fine too. "
    "Do not bypass the hook — never use --no-verify.\n\n"
    "The hook's output (its last {chars} characters at most):\n\n{output}"
).replace("{chars}", str(COMMIT_FIX_OUTPUT_CHARS))


@dataclass(frozen=True)
class LaunchContext:
    """Host-supplied identity for a planned activation (NC-R12)."""
    caller: str | None
    run_parent: str | None
    depth: int
    node_id: str
    attempt_id: str
    run_id: str = ""
    provider: str = ""
    effort: str = ""
    admission_only: bool = False


_launch_context: ContextVar[LaunchContext | None] = ContextVar("launch_context", default=None)


def encode_admission(admission):
    return json.dumps(admission, sort_keys=True)


def admission_block(reason, retry_after=None):
    text = str(reason)
    code = next((code for needle, code in (
        ("provider_concurrency", "provider_concurrency"),
        ("max_concurrent", "max_concurrent"), ("Depth limit", "max_depth"),
        ("active children", "max_children"), ("token budget", "budget_tokens"),
        ("Budget for", "budget_tokens"), ("spend_cap", "spend_cap"),
        ("disabled", "provider_disabled"), ("not granted permission", "permission"),
    ) if needle in text), "refused")
    return {"blocked": [{"code": "admission:" + code, "detail": text}],
            **({"retry_after": retry_after} if retry_after else {})}


def _both_ends(text: str, keep: int = 80, tail: int = 200) -> str:
    """The start and the end of some output, which is where reasons live.

    A CLI announces why it stopped at the END; an agent announces what it is
    about to do at the start. Recording only one of them recorded, on the day
    this was written, "I'll start by reading the spec" as the reason a provider
    was taken out of service.
    """
    text = (text or "").strip()
    if len(text) <= keep + tail:
        return text
    # More from the end than the start: a reason is usually the last thing
    # written and is rarely one line — a stack trace's meat sits above its
    # final line, and eighty characters of tail is often just the exit call.
    return f"{text[:keep]} … {text[-tail:]}"


# How often the worktree is sampled for the doom-loop check. Debounced by time
# rather than by tool count so a chatty agent cannot turn this into a `git`
# call per event on a large repository.
PROGRESS_SAMPLE_SECONDS = 3.0


def _worktree_state(worktree: Path, root: Path) -> str:
    """A cheap hash of what the agent has actually changed on disk.

    This is the ground truth the tool stream cannot give: a CLI that reports a
    write as {"TargetFile": "..."} with no content makes three different edits
    indistinguishable, while the tree itself is never ambiguous about whether
    anything happened.

    Pinned to project `root`'s trusted paths (SG-R4): polled while the agent
    runs, it executes nothing the agent wrote. A tree that cannot be read
    that way has no state, as a tree with no repository has none.
    """
    try:
        result = gitops.status(worktree, root=root)
    except gitops.GitError:
        return ""
    if not result.ok:
        return ""
    return hashlib.sha1(result.out.encode()).hexdigest()[:12]


def _size(path: Path) -> int:
    # A run-dir file: whatever an agent put in its place is not followed.
    try:
        st = os.lstat(path)
    except OSError:
        return 0
    return st.st_size if stat.S_ISREG(st.st_mode) else 0


# SG-R7: a run dir is under `.multiagents`, which a docker agent can write.
# What the host writes or reads in it goes through these: no link followed
# below `.multiagents`, no FIFO blocked on, reads bounded.
RUN_FILE_MAX_BYTES = 64 * 1024 * 1024
STREAM_LOG_MAX_BYTES = 1024 * 1024 * 1024


def _run_dir_fd(run_dir: Path) -> int:
    """A directory fd for `run_dir`, made if missing (SG-R7)."""
    return gitops._open_beneath(*gitops.beneath(run_dir), create=True)


def _run_write(run_dir: Path, name: str, text: str, mode: int = 0o644) -> None:
    """Replace `name` in `run_dir` with `text` (SG-R7)."""
    base, parts = gitops.beneath(run_dir)
    sub = Path(name)
    gitops._write_beneath(base, parts + sub.parent.parts, sub.name, text.encode(),
                          mode=mode, create=True)


def _run_read(run_dir: Path, name: str, limit: int = RUN_FILE_MAX_BYTES) -> str:
    """`name` in `run_dir`, if it is a regular file of a sane size (SG-R7);
    `OSError` otherwise, a missing file included."""
    raw = gitops._read_beneath(*gitops.beneath(run_dir), name, limit)
    if raw is None:
        raise OSError(f"{run_dir / name} is not a readable regular file")
    return raw.decode("utf-8", errors="replace")


def _run_open(run_dir: Path, name: str, flags: int, mode: str) -> Any:
    """`name` in `run_dir` as a file object, for a file the host owns: a link
    or FIFO in its place is replaced, never opened (SG-R7)."""
    fd = gitops._open_file_beneath(*gitops.beneath(run_dir), name, flags,
                                   create=True, replace=True)
    return os.fdopen(fd, mode)


def _holds_a_record(path: Path) -> bool:
    """Does this session file hold at least one complete line of JSON (SP-R3)?

    A file that is empty, or holds only an unterminated line, has no
    conversation a CLI could resume. A truncated LAST line after complete
    ones is a write in progress and does not count against it.
    """
    try:
        with path.open("rb") as handle:
            for line in handle:
                if not line.endswith(b"\n"):
                    return False
                try:
                    json.loads(line)
                except ValueError:
                    continue
                return True
    except OSError:
        return False
    return False


def _declares_turn(provider: Provider) -> bool:
    """Does this provider's stream tag events with a model-turn id?

    Read from the shape of its own rules, never from the provider's name —
    the Supervisor's turn-based step counting is opt-in per config, not
    per binary.
    """
    return any((rule.get("fields") or {}).get("turn")
               for rule in provider.stream.get("rules", []) or [])


def _occupies_slot(node: Node) -> bool:
    """SL-R4: does this node hold a concurrency slot (or belong in a wait)
    right now?

    A `pending` node has no pid yet and always counts. A `running` node
    without a recorded pid also counts — that is the ordinary shape of a
    just-launched or lightly-constructed node, and the historical behaviour
    kept for it. A `stuck` node is different: a trip can only fire on a
    process that was actually running, so a `stuck` node without a pid is
    not a live agent that has yet to record one — it is a leftover or
    malformed record, and per SL-R4 must not hold a slot forever. Both
    `running` and `stuck` stop counting the moment a recorded pid is
    checked and found dead, by pid identity (`procs.alive`, immune to pid
    reuse) rather than by the status label alone.

    RM-R1d: a node carrying the durable `cleanup_hold` — a failed-launch
    cleanup that could not confirm its process's death — counts whatever
    its pid liveness, in every Runner: the hold is tree state, not one
    process's memory.
    """
    if node.cleanup_hold:
        return True
    if node.status == "pending":
        return True
    if node.status == "running":
        return node.pid is None or procs.alive(node.pid, node.pid_start)
    if node.status == "stuck":
        return node.pid is not None and procs.alive(node.pid, node.pid_start)
    return False


# RM-R1c: how long the failed-launch cleanup keeps confirming death before
# it reports failure and holds ownership. A stop whose process will not die
# must not hang the caller for ever, but nothing is released unconfirmed.
LAUNCH_CONFIRM_SECONDS = 30.0


# RM-R1e: the gap between the two empty session scans that confirm a run's
# end. A cheap narrowing of the fork race, not a proof.
SESSION_RESCAN_SECONDS = 0.05


def _unknown_probe() -> None:
    """The probe of a run whose execution identity is not known: it can
    never be confirmed dead (RM-R1c)."""
    return None


def _positively_ended(pid: int | None, pid_start: str, probe_raw: Any) -> bool:
    """Whether the launched run has POSITIVELY ended (RM-R1c).

    Positive evidence from the actual process or container only:

    - the pid the host holds — the wrapper locally, the `docker exec`
      client for a container run — gone (zombie-aware, immune to pid
      reuse), AND no live process left in its session: the wrapper leads
      one, and the agent it starts stays in it, so a dead wrapper alone is
      not a dead run (review ag-43f57f). A session that cannot be read is
      unknown;
    - for a container run, the executor's raw probe DEFINITELY reporting the
      run dead, asked of the container; a missing pid file there, or an
      unanswerable container, is None — unknown.

    An exit-status file is never proof, and unknown is never death: the
    check answers False, so the cleanup keeps its hold (reviews ag-f7ced5,
    ag-b859f1). A local run with no pid known at all is unknown too.
    Blocking.
    """
    if pid:
        if running(pid, pid_start or ""):
            return False                    # positively alive
        if session_alive(pid) is not False:
            return False                    # its session lives, or can't be read
        # RM-R1e: one /proc snapshot cannot see a member that forks while
        # the scan runs. A second empty scan after a short gap narrows that
        # window; it is not a proof, and the residue is an accepted limit.
        time.sleep(SESSION_RESCAN_SECONDS)
        if session_alive(pid) is not False:
            return False
    elif probe_raw is None:
        return False                        # no identity at all: unknown
    if probe_raw is None:
        return True                         # local: the session is the whole run
    try:
        answer = probe_raw()
    except Exception:
        return False                        # an unanswerable probe is unknown
    if answer is None:
        return False                        # unknown: never death
    return not answer                       # the probe's positive "dead"


def _owner_alive(owner: dict) -> bool:
    """Is the server named by a `slot_owner` still running? By pid and its
    start time, so a reused pid is not mistaken for it."""
    pid = owner.get("owner_pid")
    if isinstance(pid, bool) or not isinstance(pid, int):
        return False
    return procs.alive(pid, owner.get("owner_start") or "")


def _slot_process_state(pid: int | None, start: str) -> tuple[bool, bool]:
    """Running and existing, releasing only on positive death evidence.

    A zombie has exited but still exists (the historical tree-wide count
    includes it). Unreadable process state and unexpected probe errors are
    unknown, so both counts conservatively keep the slot. Outside locks.
    """
    if pid is None or pid == 0:
        return False, False
    if isinstance(pid, bool) or not isinstance(pid, int) or pid < 0:
        return True, True
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rpartition(")")[2].split()
    except OSError:
        fields = []
    if len(fields) > 19:
        if start and fields[19] != start:
            return False, False
        return fields[0] not in ("Z", "X"), True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False, False
    except (OSError, ValueError):
        return True, True
    return True, True


def _claim_alive(claim: dict) -> bool:
    """A consult waiter's process, by pid AND start time: a reused pid is
    not the waiter (review round 2, finding 6)."""
    return procs.alive(claim.get("pid"), claim.get("start") or "")


def _held_refusal(node_id: str) -> str:
    return (f"{node_id}'s previous launch failed and its process is not yet "
            f"confirmed dead, so it cannot be relaunched; its slot is held "
            f"until it is (RM-R1d). Stop it, or try again later.")


def _same_hold(current: Any, record: dict) -> bool:
    """Is the durable hold still the one `record` describes?"""
    return isinstance(current, dict) and all(
        current.get(k) == record.get(k) for k in ("since", "owner"))


def _recorded_wrapper(run_dir: Path) -> int | None:
    """The pid in a local run's `wrapper.pid`, or None."""
    try:
        text = (run_dir / "wrapper.pid").read_text().split()
        return int(text[0]) if text else None
    except (OSError, ValueError):
        return None


@dataclass
class _Predecessor:
    """SF-R3 (review r2): the identity of the run a steer is about to stop,
    captured BEFORE the stop so its death can be confirmed after it. The
    in-process Run first — the only predecessor whose supervision lock this
    Runner holds — then the node's recorded pid, for a run this process
    never launched. An absent one (no pid, no probe) is nothing to keep a
    lock for."""
    # False until `_steer_predecessor` has looked: the default a caller holds
    # when the capture itself failed is NOT "there was no predecessor"
    # (review r5 finding 1) — that is unknown, never death.
    captured: bool = False
    pid: int | None = None
    pid_start: str = ""
    probe_raw: Any = None
    provider: str = ""
    token: str = ""
    executor: Any = None
    handle: Any = None
    run: Any = None

    @property
    def absent(self) -> bool:
        return self.pid is None and self.probe_raw is None


@dataclass
class _Hold:
    """RM-R1c/R1d: the owning Runner's side of one launch hold.

    The durable half is the node's `cleanup_hold` field in `tree.json`,
    which every Runner's admission honours. It is written BEFORE the
    process is started — a launch whose reservation cannot be written does
    not happen (review ag-43f57f) — and records what recovery needs: the
    process identity once there is one, and the execution identity it was
    launched under (executor kind and container), never re-read from a
    config that may have changed since (review ag-43f57f).

    This half keeps what only the owner has: the handle's identity to
    re-check, and the lock, claim and occupancy released once death is
    confirmed. Phases:

    - `launching`: the reservation, until supervision is established; the
      launch path itself owns every release;
    - `cleanup`: the launch failed after the process started. Nothing is
      released until death is confirmed (`_end_hold`);
    - `lifting`: nothing left to release; only the durable field is still
      to be cleared, because a tree write failed.
    """
    record: dict
    pid: int | None
    pid_start: str
    probe_raw: Any
    provider: str
    token: str
    phase: str = "launching"
    container: str = ""
    run: Any = None
    done: Any = None
    durable: bool = False
    # Death confirmed, and which of its releases have gone through.
    confirmed: bool = False
    done_releases: set = field(default_factory=set)
    # The status a caller asked for while the hold was up, applied when it
    # ends: a held node keeps its status, which is also its durable slot
    # reservation should a later hold write not reach the tree.
    then: tuple[str, str] | None = None
    task: Any = None
    # SF-R3: the steps a refused steer still owes, mirrored in record.
    steer: dict | None = None


# Matched against an agent's TEXT only, never tool arguments — an agent reading
# a file that mentions the marker must not park itself. Require a line start,
# optionally with a list bullet, so prose and inline-code examples do not count.
NEED_DECISION = re.compile(
    r"(?m)^[ \t]*(?:(?:[-*+]|[0-9]+[.)])[ \t]+)?NEED_DECISION\(([^)]{0,80})\)\s*:\s*(.+)")
PROPOSED_DEFAULT = re.compile(r"(?im)^\s*DEFAULT\s*:\s*(.+)$")
# A bug in multiagents itself, written up for publication. Parsed from the
# finished message rather than mid-stream like NEED_DECISION: a ticket is the
# agent's product, so there is nothing to interrupt.
TICKET = re.compile(r"(?im)^[ \t]*TICKET\((blocking|minor)\)[ \t]*:[ \t]*(.+)$")
# A verifier's own verdict on the work it was asked to check. Structured
# because the alternative is reading its prose, and this project does not
# classify on agent text. Declared by the agent hired to make exactly this
# judgement, which is the judgement the whole arrangement already relies on.
VERDICT = re.compile(
    r"(?im)^[ \t]*VERDICT\((approved|rejected)(?:[ \t]*,[ \t]*(\d+))?\)[ \t]*:[ \t]*(.*)$")
# A line that IS the marker: bare, with a colon, or as a Markdown heading.
PROPOSED_FIX = re.compile(r"(?im)^[ \t]*(?:#{1,6}[ \t]+)?PROPOSED_FIX[ \t]*:?[ \t]*$")
FENCE_LINE = re.compile(r"^[ \t]*(`{3,}|~{3,})")
INLINE_CODE = re.compile(r"(?<!`)(`+)(?!`)(.+?)(?<!`)\1(?!`)", re.S)


def _wrap_globs(patterns: list[str], limit: int = 12) -> str:
    """Indented, wrapped list of globs for the generated preamble."""
    shown = ", ".join(f"`{p}`" for p in patterns[:limit])
    if len(patterns) > limit:
        shown += f", and {len(patterns) - limit} more"
    return textwrap.fill(shown, width=76, initial_indent="    ",
                         subsequent_indent="    ") + "\n"


PREAMBLE = """\
You are an autonomous subagent in a delegated agent tree. This block is
generated — it tells you where you stand.

- Your id: {agent_id} ({agent_name}), running on {provider}/{model}
- Parent: {parent}
- Depth: {depth} of a maximum {max_depth}
- Working directory: {workdir}
{branch_line}{spawn_line}{readonly_line}
How this works:

- Nobody is watching you interactively and you cannot ask a question mid-run.
  Two markers are available, and choosing the right one matters:

  `NEED_INFO(<topic>): <question>` — for something another agent or your parent
  could tell you. Non-blocking: state your assumption and carry on. Prefer this.

  `NEED_DECISION(<topic>): <question>` — for a choice that changes what
  "correct" means, where guessing wrong wastes everything built on it. This
  STOPS you immediately, so use it sparingly and only when you genuinely
  cannot proceed sensibly either way. You must follow it with a line
  `DEFAULT: <what you would have chosen>` — if writing that line makes the
  answer obvious, you did not need to ask.
- Your parent sees only your final message, never your intermediate steps. Put
  everything that matters in it.
- If `BRIEF.md` exists at the top of your working directory, read it first: it
  is the agreed statement of what this project is and what done looks like.
  `context/` holds the reference material it points at. Both are reference —
  read them, and do not edit them unless your task explicitly says to.
- If the multiagents tooling itself misbehaves — a tool contradicting its own
  description, state that disagrees with itself — say so plainly in your final
  message rather than working around it silently. Your parent decides whether it
  gets written up.
- Work only inside your working directory.
- Do not merge, rebase, push, or switch branches. Your parent owns that.

---
"""


class TransportRefused(RuntimeError):
    """TG-R1: a transport mismatch discovered at the final launch gate."""


class ProviderFull(RuntimeError):
    """PC-R3: an admission refused because its provider has no free slot —
    its runs fill `max_concurrent`, or eligible work is queued ahead of it
    (PC-R3a). Not a failure: the caller queues the work, or waits.

    `gone` is the one other answer for an admission that was draining a
    queued entry: the entry is no longer there (cancelled), so nothing may
    launch for it.
    """

    def __init__(self, provider: str, limit: int | None, holders: list[str],
                 ahead: int = 0, gone: bool = False):
        self.provider, self.limit, self.holders = provider, limit, list(holders)
        self.ahead, self.gone = ahead, gone
        if gone:
            text = f"the queued entry on {provider} is gone"
        else:
            held = ", ".join(self.holders) or "none"
            text = (f"{PC_CAUSE}: provider {provider!r} is full "
                    f"(max_concurrent={limit if limit is not None else 'none'}, "
                    f"{len(self.holders)} slot(s) held by {held}")
            text += f", {ahead} queued ahead)" if ahead else ")"
        super().__init__(text)


# PC-R3a: how often a consult waiting for a slot re-reads the shared count —
# the reconciliation that sees a release in ANOTHER process. A release in
# this one wakes it at once.
PC_RECONCILE_SECONDS = 1.0
# PC-R2a: the least time between two container reconciliations of one server
# (each may ask `docker exec` about every unconfirmed container run).
PC_RECONCILE_CONTAINERS_SECONDS = 3.0
# The bound on one container probe: twice the `docker exec` subprocess
# timeout `wrapper_verdict` uses. Past it the answer is unknown.
PC_PROBE_SECONDS = 60.0
# Round 7: what a launch refused because the server is shutting down says.
SHUT_TEXT = "the server is shutting down; nothing was launched"
# PC-R3a: how long a head refused for another reason (with no retry time of
# its own) is skipped before it is eligible again and re-evaluated first.
PC_BLOCKED_RECHECK_SECONDS = 15.0


# SC-R2a (#2): the tree key holding charges the ledger could not take yet.
SPEND_PENDING = "spend_pending"


class SpendCapRefused(RuntimeError):
    """SC-R3b: a launch refused at spawn because a spend cap binds (or the
    ledger a cap needs is unusable). `refusal` is `Runner._cap_refusal`'s."""

    def __init__(self, refusal: dict):
        self.refusal = refusal
        super().__init__(refusal["reason"])


class _ConsultLockError(RuntimeError):
    """A conversation's turn lock failed for a reason other than contention."""


class _FlushGate:
    """Decides when accumulated stream progress is written to the shared tree.

    Every write flocks, reads and rewrites the whole tree, which every nested
    server shares — so writing per stream line is both disk churn and lock
    contention. Batching is safe because the watchdogs read the in-process
    Supervisor, not the tree; the only cost is that another process's
    check_agent lags by at most `interval`.
    """

    def __init__(self, interval: float = TREE_FLUSH_SECONDS,
                 now: float | None = None):
        self.interval = interval
        self.pending = 0
        self.last = time.monotonic() if now is None else now

    def add(self, urgent: bool = False, now: float | None = None) -> int:
        """Record one event. Returns the batch size to flush, or 0 to hold.

        `urgent` forces a flush — used the moment a session id is first seen,
        because steer() and answer_question() cannot resume an agent without it.
        """
        self.pending += 1
        moment = time.monotonic() if now is None else now
        if urgent or moment - self.last >= self.interval:
            batch, self.pending, self.last = self.pending, 0, moment
            return batch
        return 0

    def drain(self) -> int:
        batch, self.pending = self.pending, 0
        return batch


@dataclass
class _SlotSnapshot:
    """Process observations taken before admission takes the tree lock."""

    nodes: dict
    claims: dict
    running: dict
    alive: dict
    confirmed_dead: set

    def matches(self, key: str, raw: dict) -> bool:
        return self.nodes.get(key) == raw


@dataclass
class Run:
    """In-process state for a run this server started."""

    node_id: str
    provider: Provider
    spec: AgentSpec
    handle: Handle | None = None
    capability_hash: str = ""
    supervisor: Supervisor | None = None
    task: asyncio.Task | None = None
    events: list[dict] = field(default_factory=list)
    text_parts: list[str] = field(default_factory=list)
    final_assistant_message: str = ""
    refusal_signal: str = ""
    # RC-R2 (review ag-997df9 finding 3): the verdict's evidence as structure —
    # {"source", "pattern", "excerpt"} — so result.json can carry it verbatim.
    refusal: dict | None = None
    final_status: str = ""
    requested_session: str = ""     # adapter explicitly reported a resume mismatch
    startup_token: str = ""
    startup_progress: bool = False
    active_tools: set[str] = field(default_factory=set)
    stop_requested: bool = False      # distinguishes an explicit stop from teardown
    internal_stop: bool = False       # steer() ending this turn to respawn it, not a real cancel
    awaiting: dict | None = None      # a NEED_DECISION seen mid-stream
    ticket: dict | None = None        # the last TICKET filed from the final message
    tickets: list = field(default_factory=list)   # every TICKET filed from it
    # bug-c050b0: "asked to wrap up" lives on the Node (tree.py), not here —
    # steer()/_launch() replace the Run, and a flag kept there resets on every
    # replacement, same trap the retry counter hit first.
    server_reported: bool = False     # SM-R5: an unavailable MCP server, recorded once
    # SL-R3: the live trip this run is currently marked `stuck` for, and the
    # supervisor state at the moment it fired — compared against the current
    # state on each later event to tell "moved on" from "still repeating".
    trip_kind: str = ""
    trip_signature: str = ""
    trip_progress: str = ""
    trip_opaque_calls: int = 0
    # SV-R6/R7: where this turn starts in `output.ndjson`, and — for a run
    # adopted from a server that is gone — how far that server had already
    # accounted for. Lines up to `replay_to` rebuild this run's state without
    # being counted, logged or acted on a second time.
    turn_start: int = 0
    replay_to: int = -1
    adopted: bool = False
    final_result: bool = False        # the stream held the provider's result event
    detaching: bool = False           # SV-R3: the server is leaving it running
    # CI-R5: this run is a fix turn — the same session resumed to satisfy a
    # git hook that refused the end-of-run commit. Its `_finalize` only
    # records `fix_verdict` for the loop in the original run's `_finalize`,
    # which owns the result.
    fix_turn: bool = False
    # PC-R3f: the token of the launch marker this run set, activated when
    # its post-mortem starts ("" when it set none: unlimited provider, or
    # nested in an outer one).
    slot_token: str = ""
    fix_verdict: dict | None = None
    fix_timed_out: bool = False       # CI-R5: ended at `commit_fix_timeout`
    # LN-C1/LN-C2: the `{value, source, source_detail}` limits this turn was
    # launched under, so a trip names the value in force — and the file and
    # line it came from — at the launch, not ones resolved again later.
    limits: dict = field(default_factory=dict)
    # LN-C5: the container's `oom_kill` count at launch (None: unreadable,
    # or the run began under another server that never told us). Occupancy
    # is judged from the shared host-side record, not kept here; `oom_since`
    # is when this run entered it, so "was any sibling alive during this
    # run" can be answered over the run's whole life (adversary finding 1).
    oom_baseline: int | None = None
    oom_reader: Any = None
    oom_container: str = ""
    oom_since: float | None = None
    # SC-R4: set when a spend cap stops this run — `{cause, reason, until,
    # caps}` — and read by `_finalize`, which ends it `limited`.
    cap_stop: dict | None = None
    # SC-R4a (#1): when this turn passed its spawn-time cap check; a crossing
    # recorded after it stops the run, one recorded before does not.
    launched_at: float = 0.0
    prompt_file: str = ""
    # SC-R2a (#3): charges not yet committed, retried first with the next
    # one, and the stream offset the replay checkpoint may not pass meanwhile.
    pending_charges: list = field(default_factory=list)
    charge_hold: int | None = None

    done: asyncio.Event = field(default_factory=asyncio.Event)


def server_env(env: dict[str, str], node_id: str) -> dict[str, str]:
    """The environment the multiagents MCP server of agent `node_id` runs with.

    SM-R3: the server acts as this agent, so it carries the agent's identity
    itself rather than trusting the CLI to pass it through — and its id is
    pinned here, last, whatever `env` says. It RUNS as multiagents does,
    though: in the user's HOME and the machine's state and config
    directories, not the agent's private ones — otherwise it would look for
    worktrees, and link the next agent's credentials, inside this agent's
    HOME. And with the agent's PATH and locale, which carry no credentials,
    so the tools it starts (git, docker) resolve.
    """
    out = {k: v for k, v in env.items() if k in BASE_ENV_KEYS}
    out.update({k: v for k, v in env.items() if k.startswith("MULTIAGENTS_")})
    out.update({
        "HOME": str(Path.home()),
        "MULTIAGENTS_STATE_DIR": str(state_root()),
        "MULTIAGENTS_CONFIG_DIR": str(global_config_dir()),
        "MULTIAGENTS_AGENT_ID": node_id,
    })
    out.setdefault("PATH", os.environ.get("PATH") or os.defpath)
    return strip_key(out)                # CW-R2b: never a nested server's


def _resolves(command: str, path: str) -> bool:
    """Would `command` start, looked up the way an exec looks it up?"""
    return shutil.which(command, path=path or None) is not None


def _write_own(target: Path, text: str) -> None:
    """Write `text` to `target`, mode 0600, replacing whatever is there.

    Written beside it and renamed over it, so a link planted at `target` is
    replaced rather than followed into what it points at (SM-R4).
    """
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        os.replace(tmp, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _admitted(what: str):
    """CW-R2: a Runner entry point that launches, admitted through its
    safe-point gate — refused while the server is stopping for a compaction,
    and counted until it returns, so the stop waits for it. Nested entry
    points (an answer resumes through `steer`) cannot be split: the gate only
    closes with nothing counted."""
    def decorate(fn):
        @functools.wraps(fn)
        async def admitted(self, *args, **kwargs):
            await self.gate.wait_granted()
            with self.gate.enter(what):
                return await fn(self, *args, **kwargs)
        return admitted
    return decorate


def _scheduled_start(fn):
    """NC-R56: route starts through node admission with task-local authority."""
    @functools.wraps(fn)
    async def scheduled(self, agent_name, task, *, launch_context=None, **kwargs):
        context = launch_context or _launch_context.get()
        token = _launch_context.set(context)
        try:
            if self.scheduler_enabled() and context is None:
                from .scheduler import submit
                internal = {key: kwargs.pop(key, None) for key in
                            ("deferred_id", "recorded_provider", "queued", "_cap_raced", "_qh_floor_grant")}
                if any(internal.get(key) for key in ("deferred_id", "recorded_provider", "queued", "_qh_floor_grant")):
                    return admission_block("legacy queue start requires scheduler migration")
                return await asyncio.to_thread(submit, self.paths.root, agent_name, task,
                                               caller=self.self_id(), **kwargs)
            try:
                result = await fn(self, agent_name, task, **kwargs)
            except (RuntimeError, PermissionError, ValueError, KeyError, FileNotFoundError) as exc:
                if context:
                    return admission_block(exc)
                raise
            if context and not result.get("agent_id") and not result.get("admitted"):
                if not isinstance(result.get("blocked"), list):
                    return admission_block(result.get("error") or result.get("reason") or result,
                                           result.get("retry_after"))
            return result
        finally:
            _launch_context.reset(token)
    return scheduled


class Runner(QuotaHandover):
    # How often `_watch_timers` looks at a run's silence and wall clock, and
    # how much longer than the other turn's own limit a consult waits for that
    # turn to end (CF-R7). Class attributes so a test can shorten them for one
    # Runner; nothing in production sets them.
    WATCH_POLL_SECONDS = 5.0
    # SC-R4a: how often an active run looks for another process's crossing;
    # well inside the 15 s within which every run on the scope is stopped.
    SPEND_CAP_POLL_SECONDS = 2.0
    # SC-R4a (#13): how long a crossing's claimer has to record its event
    # before another process records it instead.
    ANNOUNCE_GRACE_SECONDS = 10.0
    # #2: how often, with no held charge known, the tree is asked for one.
    PENDING_CHARGE_CHECK_SECONDS = 5.0
    CONSULT_LOCK_SLACK_SECONDS = 60.0

    def __init__(self, paths: ProjectPaths, config: Config):
        self.paths = paths
        self.config = config
        self.config_error = ""
        self._scheduler_version = config_mod.source_version(paths)
        self._scheduler_gate = config.project.get("scheduler", {}).get("enabled", False)
        self.providers = load_providers(config.providers, config.provider_sources)
        self.tree = Tree(paths.tree_file, paths.events_file)
        self.startup = StartupHealth(paths)
        self.runs: dict[str, Run] = {}
        # LN-C5: which runs are alive in each container, decided from the
        # host-side record every server of this project shares — never only
        # this process's own runs, or a sibling started by a second Runner
        # would not stop a `killed` attribution (review finding 1).
        self.occupancy = ContainerOccupancy(paths)
        # LN-C2, adversary findings 3/8: the launch-time limits of each run,
        # kept where the container cannot write them, because `tree.json`'s
        # node records can be forged by the very run they describe.
        self.launch_limits = LaunchLimits(paths)
        self.authority = (None if DockerExecutor({}, paths, {}, state_root()).inside()
                          else HostAuthority(paths, self.tree))
        reap_pending_branches(paths.root, self.tree, self.authority)
        # SC-R2: the project's spend ledger, and the ledger errors already
        # reported (each once: with no cap they never block anything).
        self.ledger = spendcap.Ledger(paths.data / spendcap.LEDGER_NAME)
        self._ledger_errors: set[str] = set()
        # SC-R4: stops of runs another run's crossing ended, kept referenced.
        self._cap_tasks: set[asyncio.Task] = set()
        # SC-R4c: (crossing id, node) stops whose record has not landed yet.
        self._unrecorded_stops: set[tuple[str, str]] = set()

    def scheduler_enabled(self):
        if self.config.project.get("scheduler", {}).get("enabled"):
            return True
        version = config_mod.source_version(self.paths)
        if version != self._scheduler_version:
            from .scheduler_config import settings
            try:
                self._scheduler_gate = settings(self.paths.root).get("enabled", False)
                self._scheduler_version = version
            except Exception:
                pass  # Retain the last valid gate during a malformed reload.
        return self._scheduler_gate

    def authoritative(self, node: Node, action: str) -> Node | None:
        """Use recorded operands, reporting container-written disagreements once.

        None when the entry's inner id is not its key (HA-R9): the host then
        performs no mutation on the node at all.
        """
        if self.authority is None:
            return node
        if not self.identity_ok(node.id, action):
            return None
        record = self.authority.get(node.id)
        if record is None:
            return node
        fields = [key for key in ("branch", "worktree", "parent")
                  if getattr(node, key) != record.get(key)]
        if fields:
            self.tree.emit(node.id, "host_authority_mismatch", action=action,
                           node=node.id, fields=fields)
        return replace(node, **{key: record.get(key) or "" if key != "parent"
                                else record.get(key)
                                for key in ("branch", "worktree", "parent")})

    def identity_ok(self, node_id: str, action: str) -> bool:
        """HA-R9: a node is the key it is stored under. An entry whose inner
        `id` names anything else is reported and left alone by the host."""
        if self.authority is None or not self.tree.id_mismatch(node_id):
            return True
        self.mismatch(node_id, action, ["id"], "entry id differs from its key")
        return False

    def mismatch(self, node_id: str, action: str, fields: list[str], reason: str = "") -> None:
        self.tree.emit(node_id, "host_authority_mismatch", action=action,
                       node=node_id, fields=fields, reason=reason)

    def unrecorded_branch_ok(self, node: Node, action: str) -> bool:
        if (self.authority and not self.authority.get(node.id) and node.branch
                and not self.authority.safe_unrecorded_branch(node.branch)):
            self.mismatch(node.id, action, ["branch"], "branch is outside the container domain")
            return False
        return True

    def reload(self, config: Config) -> None:
        """Swap in a freshly loaded config and everything derived from it.

        Only what is built FROM config is replaced. The tree, in-flight runs
        and the deferred queue are state, and survive untouched; a run already
        going keeps the provider, spec and supervisor it was started with.
        Providers are built before anything is assigned, so a config that
        fails validation here leaves the previous one wholly in force.
        """
        providers = load_providers(config.providers, config.provider_sources)
        if config.providers != self.config.providers:
            # A cached reading was taken through the old provider definition.
            # Dropped only when providers changed: a re-read costs a script run
            # per provider, on every spawn's critical path.
            budget_mod.invalidate_cache()
        raised = [name for name, here in providers.items()
                  if name in self.providers
                  and self.providers[name].max_concurrent is not None
                  and (here.max_concurrent is None
                       or here.max_concurrent > self.providers[name].max_concurrent)]
        self.config = config
        self.providers = providers
        self.config_error = ""
        if raised:
            # PC-R2a: a raised or removed limit wakes the queue.
            self._pc_kick()

    def executor(self, spec: AgentSpec | None = None):
        """The execution backend for an agent, with the context docker needs.

        An agent may pin its own executor. That is not a stylistic choice: a CLI
        whose credentials do not survive containerisation has to run on the
        host, and forcing the whole project back to `local` for its sake would
        give up isolation for every other agent.
        """
        return get_executor(
            (spec.executor if spec and spec.executor else self.config.executor),
            self.config.project.get("executor", {}).get("docker", {}),
            paths=self.paths,
            providers=self.providers,
            config_dir=global_config_dir(),
        )

    def agent_git(self, node: Node) -> gitops.Git:
        """Where this agent's own commits run, and their hooks (SG-R3): its
        executor's sandbox. The end-of-run commit, the CI-R5 fix-loop commits
        and the merge gate's revert all commit on its branch."""
        return self.executor(self.config.agents.get(node.agent)).git(node.id)

    def git_unreadable(self, node_id: str, path: Path, exc: Exception) -> None:
        """Record that a pinned read (SG-R4) could not read an agent's tree.
        The caller then takes its conservative branch: it neither merges nor
        takes the tree for clean."""
        self.tree.emit(node_id, "git_unreadable", path=str(path),
                       detail=str(exc)[:400])

    # ------------------------------------------------------------- identity --

    def self_id(self) -> str | None:
        """Which node *this* server is running as, if it was spawned by us."""
        context = _launch_context.get()
        return context.caller if context else os.environ.get("MULTIAGENTS_AGENT_ID") or None

    def session(self) -> str:
        """Which launched session this server belongs to, if any.

        `driver.py` puts it in the CLI's environment before exec'ing, and the
        CLI starts this server as a child, so it arrives by inheritance. Empty
        for a server nobody launched — a bare `python -m multiagents.server`.
        """
        return os.environ.get("MULTIAGENTS_SESSION_ID", "")

    def self_depth(self) -> int:
        context = _launch_context.get()
        if context:
            return context.depth - 1
        try:
            return int(os.environ.get("MULTIAGENTS_DEPTH", "0"))
        except ValueError:
            return 0

    def _credential_group(self, name: str) -> list[str]:
        """PS-R4: the credential group of `name` — its owner (or itself) plus
        every provider that borrows from it. Auth failures and recoveries
        move the whole group together; quota and provider_down never do."""
        provider = self.providers.get(name)
        owner = str(getattr(provider, "auth_from", "") or "") if provider else ""
        key = owner or name
        return [member for member, entry in sorted(self.providers.items())
                if (str(getattr(entry, "auth_from", "") or "") or member) == key]

    @staticmethod
    def _auth_context(executor: Any) -> str:
        """PS-R4b: the execution context a check speaks for — whose login it
        reads. Under docker the agents' login is the CONTAINER's; anywhere
        else it is the host's own."""
        return "container" if getattr(executor, "kind", "local") == "docker" \
            else "host"

    def _context_executor(self, context: str) -> Any:
        """PS-R4b: an executor whose check speaks for `context`'s login —
        the kind that observed a block made under an agent's executor
        override (`executor: local` in a docker project, or the reverse).
        Probing such a block through the project executor asked the wrong
        login, so it could only time out (review ag-3644ef, finding 4)."""
        return self.executor(AgentSpec(
            "-", "", "", executor="docker" if context == "container" else "local"))

    def _block_auth(self, name: str, seconds: float, context: str) -> str:
        """PS-R4: mark `name`'s whole credential group unauthenticated in
        `context`, in one transaction (`Tree.block_auth`). Returns the reason
        recorded on `name` itself.

        The login hint names the credential OWNER (PS-R10): logging in as a
        dependent would fix nothing, since it has no login of its own."""
        owner, _ = self._auth_target(name)
        reason = (f"{name} is not authenticated — run "
                  f"`multiagents auth login {owner}`")
        reasons = {member: reason if member == name
                   else f"{reason} (shared credentials with {name})"
                   for member in self._credential_group(name)}
        # A block written before contexts were recorded is the default
        # context's: a failure observed there replaces it.
        legacy = {""} if context == self._auth_context(self.executor()) else set()
        self.tree.block_auth(reasons, now() + seconds, context, supersedes=legacy)
        return reason

    def _auth_target(self, name: str) -> tuple[str, Any]:
        """Whose `check` answers for `name`'s credentials (PS-R2/R3): the
        credential owner's, when it borrows one; its own otherwise."""
        provider = self.providers.get(name)
        owner = str(getattr(provider, "auth_from", "") or "")
        if owner in self.providers:
            return owner, self.providers[owner]
        return name, provider

    def _auth_ok(self, name: str, executor: Any = None) -> bool | None:
        """Ask the provider's own `check` action. None = it would not say.

        Structured, not prose: `check` is part of the script contract and
        answers with an exit code (0 authenticated, 10 not). Reading it is the
        opposite of the thing this project refuses to do — it is asking the CLI
        rather than guessing from what a model wrote.

        PS-R2: for a credential-dependent the OWNER's check answers — that is
        whose login a failure (or a recovery) is about. PS-R4b: the check runs
        through the EXECUTOR the question is about (the failed run's own, when
        recovering one), because a container login and the host's are
        different credentials and a pass in one says nothing about the other.
        """
        from . import auth, scripts as scripts_mod

        check_name, provider = self._auth_target(name)
        if provider is None:
            return None
        code, out, err = scripts_mod.run_action(
            check_name, provider, executor if executor is not None else self.executor(),
            "check", global_config_dir(), self.paths.config, timeout=20)
        if code == auth.AUTHENTICATED:
            return True
        if code == auth.NOT_AUTHENTICATED:
            return False
        return None

    def _sample_headroom(self, provider) -> None:
        """One cached budget reading, recorded for the burn rate. Blocking."""
        try:
            from .budget import read_provider

            budget = read_provider(provider.name, provider, self.executor(),
                                   global_config_dir(), self.paths.config,
                                   limits=self.config.limits)
            if budget.stale:
                # RM-R4c: a reading that routes as unknown feeds no
                # prediction — its raw headroom must not enter the burn
                # series the wind-down projects a wall from.
                return
            self.tree.note_headroom(provider.name, budget.headroom,
                                    self.tree.rollup_usage().get("cost_usd", 0))
        except Exception:
            pass                              # a reading must never break a run

    def _wind_down(self, budgets: dict) -> None:
        """Stop sending work to a provider that is minutes from its wall.

        Not a cooldown — nothing is broken — but starting an agent into the
        last minutes of a window buys a run that will be cut off mid-thought,
        and its context has to be paid for again afterwards. Measured: two opus
        agents refilled a fresh five-hour window in thirteen minutes, and the
        second pair was doomed the moment it launched.

        It also gives the agents already running the room to wrap up, which an
        advisor pointed out is otherwise self-defeating: interrupting them to
        write a handoff while new work keeps draining the same window means the
        handoff is cut off too.
        """
        lead = float(self.config.limits.get("wind_down_seconds", 300))
        budget_cfg = self.config.project.get("budget") or {}
        min_span = budget_number(budget_cfg, "burn_min_span_seconds", zero_ok=True)
        min_samples = budget_number(budget_cfg, "burn_min_samples", zero_ok=True)
        for name, budget in budgets.items():
            # RM-R4c: a stale reading routes as unknown (RM-R4b), so it is
            # not evidence that a wall is minutes away — skipping only
            # `unusable` let the stale raw number wind down the very
            # provider the demotion had just made usable again.
            if budget.stale or not budget.usable or budget.cooldown_until:
                continue
            burn = self.tree.burn(name, min_span_seconds=min_span,
                                  min_samples=min_samples)
            left = burn.get("seconds_to_wall")
            if left is not None and left < lead:
                budget.cooldown_until = now() + max(60.0, left)
                budget.note = (f"winding down: about {left / 60:.0f} min of this "
                               f"window left at the current rate")

    def _deferral_notices(self, agent_name: str, choose, budgets: dict,
                          before_wind_down: dict, reserve: float) -> None:
        """LN-C1/C6: name the limit that deferred this start, only when it is
        what did — the same choice without it would have found a provider."""
        wind_down = float(self.config.limits.get("wind_down_seconds", 300))
        if reserve > 0 and choose(budgets, 0.0)[0] is not None:
            key = "budget.reserve_headroom"
            source = notices.provenance(self.config, key, reserve)
            self._notice(key, reserve, "deferred", "tree", source,
                         f"start of {agent_name!r} deferred to keep the headroom reserve",
                         "lower it there to spend closer to the wall")
        if choose(before_wind_down, reserve)[0] is not None:
            key = "limits.wind_down_seconds"
            value = int(wind_down) if wind_down == int(wind_down) else wind_down
            source = notices.provenance(self.config, key, value)
            self._notice(key, value, "deferred", "tree", source,
                         f"start of {agent_name!r} deferred: its provider is winding "
                         f"down before its window ends",
                         "lower it there to keep starting work closer to the wall")

    def _half_open(self, budgets: dict, cooldowns: dict) -> None:
        """When a tripped provider's cooldown lapses, allow exactly one trial.

        Two faults are fixed here. The first is a barrage: every task deferred
        behind a cooldown wakes the moment it lapses, and without a claim they
        all try the same broken provider at once and all fail before any of them
        can set a new cooldown.

        The second is that some faults do not heal. A revoked token will fail
        the trial every time, forever, and each trial costs a real agent run. So
        where the trip was recorded as an authentication failure, the trial is
        the provider's `check` — one subprocess, no tokens — and a pass clears
        the cooldown immediately, so logging back in takes effect at once
        instead of at the end of a timer.
        """
        health = self.tree.provider_health()
        auth_window = float(self.config.limits.get(
            "provider_auth_cooldown_seconds", 6 * 3600))
        probe_every = float(self.config.limits.get("provider_probe_seconds", 120))
        for name, budget in budgets.items():
            state = health.get(name) or {}
            entry = cooldowns.get(name) or {}
            tripped = bool(state.get("tripped"))
            # PS-R4: a provider can carry an authentication block its own runs
            # never earned — a group member's failure marked it too. Such a
            # block is probed like a tripped one, or a shared login could stay
            # locked out for the whole window after the owner recovered.
            auth_block = bool(entry.get("needs_login")) and \
                entry.get("cause") == "auth"
            if not tripped and not auth_block:
                continue                                  # healthy
            cooling = entry.get("until", 0) > now()
            # A cooling provider is already routed around and needs no trial —
            # unless the trial is free. For an authentication failure it is, and
            # a long cooldown must not mean a long wait AFTER somebody logs in:
            # the probe runs on its own short interval and the cooldown only
            # keeps the provider out of routing between probes.
            if cooling and not entry.get("needs_login"):
                continue
            if not self.tree.claim_trial(name, window=probe_every):
                if not cooling:
                    budget.cooldown_until = now() + 60     # somebody else is trying
                continue
            if not cooling:
                # This run IS the trial, so the tally of what went wrong before
                # it starts again. Left standing, it is read as "this provider
                # is unsafe" by everything that looks — including the
                # orchestrator, which then never routes the run that would have
                # cleared it.
                self.tree.begin_trial(name)
            if not entry.get("needs_login"):
                continue                      # a real run is the trial; let it
            # PS-R4b: the probe asks the login the block was observed in, and
            # its answer clears or re-asserts only blocks from that context —
            # a host pass never clears a container failure, or the reverse.
            default = self._auth_context(self.executor())
            context = entry.get("context") or default
            contexts = {context, ""} if context == default else {context}
            if context == default:
                ok = self._auth_ok(name)      # the project executor's login
            else:
                ok = self._auth_ok(name, self._context_executor(context))
            if ok:
                # PS-R4/R4a: every member's AUTHENTICATION block lifts
                # together, and nothing else.
                lifted = self.tree.clear_auth(self._credential_group(name),
                                              contexts)
                for member in lifted:
                    if member in budgets:
                        live = self.tree.cooldown(member)
                        budgets[member].cooldown_until = \
                            live.get("until") if live else None
                self.tree.clear_provider_health(name)
            else:
                budget.note = self._block_auth(name, auth_window, context)
                budget.cooldown_until = now() + auth_window

    def _maybe_cool_family(self, tripped: str, _seconds: float) -> None:
        """Stop the router walking every account of a broken integration.

        If the CLI itself breaks — a parse rule, an update, a vendor outage —
        each account fails in turn and each needs its own three failed runs to
        trip. With four profiles that is twelve wasted runs before anything
        stops.

        But an instance-only fault must not take the family down with it: a
        corrupt profile, a permission error, one account's own trouble. So the
        family is only cooled on CORRELATED failure — a second member already
        cooling — which is evidence about the vendor rather than a guess about
        the cause. An advisor's rule, and the right one.
        """
        provider = self.providers.get(tripped)
        family = getattr(provider, "family", "") or tripped
        members = [name for name, entry in self.providers.items()
                   if (getattr(entry, "family", "") or name) == family
                   and name != tripped]
        if not members:
            return
        cooling = [name for name in members if self.tree.cooldown(name)]
        if not cooling:
            return
        # SHORT, and deliberately not the tripped instance's own window. That
        # one is an observed penalty — this account hit a wall that lasts fifty
        # hours. A family cooldown is an inference from two members failing at
        # once, and inheriting the observed duration would turn one account's
        # quota wall plus another's transient error into a multi-day lockout of
        # a vendor that was never actually down. An advisor's point, and right.
        seconds = float(self.config.limits.get(
            "provider_family_cooldown_seconds", 300))
        for name in members:
            if not self.tree.cooldown(name):
                self.tree.set_cooldown(
                    name, now() + seconds,
                    f"{family}: {tripped} and {cooling[0]} both failed — pausing "
                    f"the family briefly, which looks like the integration "
                    f"rather than either account",
                    cause="family")
        self.tree.emit("system", "family_down", family=family,
                       members=sorted([tripped, *members]))

    def _instance_load(self) -> tuple[dict[str, int], dict[str, float]]:
        """How busy each provider is, and when it was last given work.

        Read from the tree rather than kept in memory: every agent runs its own
        MCP server process, so an in-memory count would have each of them
        believing it was the only one choosing.
        """
        load: dict[str, int] = dict(self.tree.recent_claims())
        last: dict[str, float] = {}
        for node in self.tree.read().get("nodes", {}).values():
            name = node.get("provider") or ""
            if not name:
                continue
            started = float(node.get("started_at") or node.get("created_at") or 0)
            last[name] = max(last.get(name, 0.0), started)
            if node.get("status") in ("running", "starting"):
                load[name] = load.get(name, 0) + 1
        return load, last

    def _orchestrator_provider(self) -> str:
        """Whose quota the orchestrator itself is spending."""
        for spec in self.config.agents.values():
            if spec.launch and spec.role == "orchestrator":
                return spec.provider
        return ""

    def can_spawn(self) -> bool:
        context = _launch_context.get()
        if context:
            parent = self.tree.get(context.caller) if context.caller else None
            return not context.caller or bool(parent and self.config.agents.get(parent.agent)
                                              and self.config.agents[parent.agent].can_spawn)
        if self.self_id() is None:
            return True                       # the root orchestrator always may
        return os.environ.get("MULTIAGENTS_CAN_SPAWN", "0") == "1"

    # ------------------------------------------------------------- guardrails --

    def _slot_snapshot(self, data: dict | None = None) -> _SlotSnapshot:
        """PC-R2a: observe candidate processes outside the global lock.

        Admission compares the copied node records with the durable ones
        under the lock. New or changed records hold capacity until another
        admission can observe them; stale evidence never releases a slot.
        Owner and consult-waiter checks use the same snapshot.
        """
        if data is None:
            data = self.tree.read()
        nodes = copy.deepcopy(data["nodes"])
        claims = {entry.get("id"): copy.deepcopy(entry["claim"])
                  for entry in data.get("deferred", [])
                  if isinstance(entry, dict) and isinstance(entry.get("claim"), dict)}
        identities = set()
        for raw in nodes.values():
            if not isinstance(raw, dict):
                continue
            identities.add((raw.get("pid"), raw.get("pid_start") or ""))
            owner = raw.get("slot_owner")
            if isinstance(owner, dict):
                identities.add((owner.get("owner_pid"), owner.get("owner_start") or ""))
        for claim in claims.values():
            identities.add((claim.get("pid"), claim.get("start") or ""))
        live, alive = {}, {}
        for pid, start in identities:
            live[pid, start], alive[pid, start] = _slot_process_state(pid, start)
        return _SlotSnapshot(nodes, claims, live, alive,
                             set(self.__dict__.get("_pc_confirmed_dead", set())))

    @staticmethod
    def _slot_owner_alive(owner: dict, snapshot: _SlotSnapshot | None) -> bool:
        if snapshot is None:
            return _owner_alive(owner)
        pid = owner.get("owner_pid")
        if isinstance(pid, bool) or not isinstance(pid, int):
            return False
        return snapshot.alive.get((pid, owner.get("owner_start") or ""), True)

    @staticmethod
    def _tree_slot_holds(node: Node, snapshot: _SlotSnapshot) -> bool:
        """The unlimited-provider tree rule, without process reads in-lock."""
        if node.cleanup_hold or node.status == "pending":
            return True
        if node.status == "running":
            return node.pid is None or snapshot.alive.get((node.pid, node.pid_start or ""), True)
        if node.status == "stuck":
            return node.pid is not None and snapshot.alive.get((node.pid, node.pid_start or ""), True)
        return False

    def _occupying(self, nodes: dict, exclude: str = "",
                   snapshot: _SlotSnapshot | None = None):
        """The (key, raw) entries holding a slot — the rule `_occupants`
        documents, shared with the per-provider count (PC-R2).

        PC-R2a adds two cases. A node's `slot_owner` (a reservation before
        launch, or an active post-mortem after exit) holds its slot while that
        server lives; a `pending` reservation whose server died holds
        nothing (reconciliation by owner identity). And a `detached` node
        — a live agent its server left behind — holds its slot until its
        process is confirmed gone."""
        if snapshot is None:
            snapshot = self._slot_snapshot({"nodes": nodes})
        for key, raw in nodes.items():
            if (key == exclude or not isinstance(raw, dict)
                    or raw.get("role", "") in DRIVER_ROLES):
                continue
            if raw.get("cleanup_hold") or key in self._holds:
                yield key, raw
                continue
            if not snapshot.matches(key, raw):
                yield key, raw
                continue
            status = raw.get("status")
            if self._pc_limit(str(raw.get("provider") or "")) is None:
                # PC-R5: with no provider limit, the count is exactly the
                # tree-wide rule as it was.
                if status not in ACTIVE:
                    continue
                try:
                    node = node_from_raw(raw, key)
                except (TypeError, ValueError):
                    continue
                if self._tree_slot_holds(node, snapshot):
                    yield key, raw
                continue
            try:
                node = node_from_raw(raw, key)
            except (TypeError, ValueError):
                continue
            if self._pc_holds(node, status, raw.get("slot_owner"), snapshot):
                yield key, raw

    def _pc_process_live(self, node: Node,
                         snapshot: _SlotSnapshot | None = None) -> bool:
        """PC-R2a: is the node's recorded run still running — released only
        on CONFIRMED exit? The local pid first (zombie-aware). A container
        run whose local pid (the host's `docker exec` client) is gone is NOT
        asked here. Admission supplies its pre-lock snapshot. It counts as held
        unless reconciliation (`_pc_reconcile_containers`, off the loop and
        outside the lock) has recorded its death for this process identity."""
        live = (snapshot.running.get((node.pid, node.pid_start or ""), True)
                if snapshot is not None else _slot_process_state(node.pid, node.pid_start)[0])
        if node.pid and live:
            return True
        key = self._pc_container_key(node)
        if key is None:
            return False
        dead = (snapshot.confirmed_dead if snapshot is not None
                else self.__dict__.get("_pc_confirmed_dead", set()))
        return key not in dead

    @staticmethod
    def _pc_container_key(node: Node) -> tuple | None:
        """The process identity of a container run, or None for a local one."""
        identity = node.exec_identity if isinstance(node.exec_identity, dict) else {}
        if identity.get("kind") != "docker" or not identity.get("container"):
            return None
        return (node.id, node.pid, node.pid_start, str(identity["container"]))

    def _pc_reconcile_soon(self) -> None:
        """Ask for a run of `_pc_reconcile_containers` in the background: at
        most one task per server, a pass no more often than every
        PC_RECONCILE_CONTAINERS_SECONDS, and trailing-edge — a request made
        inside that window, or while a pass is in flight, sets `rerun`, and
        one more pass runs at the window's end, so no request is lost.
        Nothing ever awaits it (a confirmed death kicks the drain itself).
        Nothing at all when no provider has a limit (PC-R5), during
        shutdown, or outside a loop."""
        if self.__dict__.get("_pc_shutting_down"):
            return
        if not any(p.max_concurrent is not None for p in self.providers.values()):
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        current = self.__dict__.get("_pc_reconcile_task")
        if current is not None and not current.done() and current.get_loop() is loop:
            self.__dict__["_pc_reconcile_rerun"] = True
            return
        last = self.__dict__.get("_pc_reconciled_at", float("-inf"))
        delay = max(0.0, last + PC_RECONCILE_CONTAINERS_SECONDS - time.monotonic())
        self.__dict__["_pc_reconcile_task"] = loop.create_task(
            self._pc_reconcile_loop(delay))

    async def _pc_reconcile_loop(self, delay: float) -> None:
        """The background task: a pass after `delay`, then another at the
        end of each window for as long as requests arrived meanwhile."""
        while True:
            if delay:
                await asyncio.sleep(delay)
            if self.__dict__.get("_pc_shutting_down"):
                return
            self.__dict__["_pc_reconcile_rerun"] = False
            self.__dict__["_pc_reconciled_at"] = time.monotonic()
            with contextlib.suppress(Exception):
                await self._pc_reconcile_containers()
            if (not self.__dict__.get("_pc_reconcile_rerun")
                    or self.__dict__.get("_pc_shutting_down")):
                return
            delay = max(0.0, self.__dict__["_pc_reconciled_at"]
                        + PC_RECONCILE_CONTAINERS_SECONDS - time.monotonic())

    async def _pc_reconcile_containers(self) -> int:
        """PC-R2a reconciliation: ask the container about each container run
        on a limited provider whose local pid is gone and whose death is not
        yet recorded — in a thread, outside the tree lock — and record the
        explicit deaths (`wrapper_verdict`: transport errors and timeouts are
        unknown, never recorded). A newly confirmed death kicks the drain.
        Returns how many were newly confirmed."""
        dead = self.__dict__.setdefault("_pc_confirmed_dead", set())
        candidates = []
        for key, raw in self.tree.read()["nodes"].items():
            if not isinstance(raw, dict) or self._pc_limit(str(raw.get("provider") or "")) is None:
                continue
            try:
                node = node_from_raw(raw, key)
            except (TypeError, ValueError):
                continue
            ident = self._pc_container_key(node)
            if ident is None or ident in dead:
                continue
            if node.pid and running(node.pid, node.pid_start):
                continue
            candidates.append((ident, node))
        if not candidates:
            return 0

        def ask(node: Node) -> bool | None:
            executor = self._docker_for(str(node.exec_identity["container"]))
            verdict = getattr(executor, "wrapper_verdict", None)
            if verdict is None:
                return None
            try:
                return verdict(node.id)
            except Exception:
                return None

        confirmed = 0
        for ident, node in candidates:
            # Each probe is bounded: a hung one is unknown, and the pass —
            # the one reconciliation task — always completes, whatever the
            # thread it left behind is still doing.
            try:
                verdict = await asyncio.wait_for(asyncio.to_thread(ask, node),
                                                 PC_PROBE_SECONDS)
            except (asyncio.TimeoutError, TimeoutError):
                verdict = None
            if verdict is False:
                dead.add(ident)
                confirmed += 1
        if confirmed and not self.__dict__.get("_pc_shutting_down"):
            self._pc_kick()
        return confirmed

    def _pc_holds(self, node: Node, status: Any, owner: Any,
                  snapshot: _SlotSnapshot | None = None) -> bool:
        """PC-R2a: does a node on a LIMITED provider hold its slot?

        A slot is released only on confirmed exit, so a live process holds
        one whatever its status says — `cancelled` written before a detached
        kill lands, `pending` under a dead reservation owner, `detached`
        with no server. A pre-launch reservation or an active post-mortem
        (PC-R3f) holds it while its server lives. A launch marker alone does
        not keep a confirmed-dead process's slot; a `pending`
        reservation whose server died, with no live process of its own,
        holds nothing."""
        def pid_live() -> bool:
            return self._pc_process_live(node, snapshot)
        finalizing = isinstance(owner, dict) and owner.get("kind") == "finalizing"
        if finalizing and owner.get("active") and self._slot_owner_alive(owner, snapshot):
            # PC-R3f (round 3, finding 4): a live post-mortem holds the slot
            # whatever status it has already written.
            return True
        if status not in ACTIVE:
            # A live bare pid is not evidence of death, even after a stop
            # has moved the node out of ACTIVE. Identity mismatch releases
            # only when a recorded start time positively proves it.
            return pid_live()
        if (not finalizing and isinstance(owner, dict)
                and self._slot_owner_alive(owner, snapshot)):
            return True
        if status == "detached":
            return node.pid is None or pid_live()
        if status == "pending":
            return pid_live() if isinstance(owner, dict) else True
        if status in ("running", "stuck") and node.pid is not None:
            return pid_live()
        return (self._tree_slot_holds(node, snapshot) if snapshot is not None
                else _occupies_slot(node))

    def _slot_claim(self, kind: str) -> dict:
        """A `slot_owner` naming this server (PC-R2a)."""
        return {"kind": kind, **self._owner_fields()}

    def _launch_slot_owner(self, node_id: str, provider: str) -> str:
        """PC-R2a/R3f: replace the reservation with a launch marker. It can
        become an active post-mortem claim, unless admission has reclaimed
        it after confirmed death. The claim nests: a commit-fix turn
        launched inside a post-mortem of this server keeps the outer claim
        and takes none, so only the outer one ends it. Returns the token
        that ends it ("" for none). With no provider limit, only the
        reservation is dropped (PC-R5)."""
        token = ""
        claim = dict(self._slot_claim("finalizing"),
                     token=os.urandom(6).hex(), active=False)
        with self.tree.transaction() as data:
            entry = data["nodes"].get(node_id)
            if entry is None:
                return ""
            owner = entry.get("slot_owner")
            if (isinstance(owner, dict) and owner.get("kind") == "finalizing"
                    and owner.get("active")
                    and owner.get("owner") == self._hold_owner):
                return ""
            if self._pc_limit(provider) is not None:
                token = claim["token"]
                entry["slot_owner"] = claim
            elif isinstance(owner, dict):
                entry["slot_owner"] = None
        return token

    def _pc_reclaim_launch_claims(self, nodes: dict, snapshot: _SlotSnapshot) -> None:
        """PC-R2a: revoke dead launch markers in the admission transaction.

        Counting alone is not revocation: a delayed finalizer could restore
        its marker after a replacement took the slot. Active post-mortems,
        cleanup holds and processes whose death is unknown are kept.
        """
        for key, raw in nodes.items():
            if not isinstance(raw, dict):
                continue
            owner = raw.get("slot_owner")
            if (not isinstance(owner, dict) or owner.get("kind") != "finalizing"
                    or owner.get("active") or raw.get("cleanup_hold")
                    or key in self._holds or not snapshot.matches(key, raw)
                    or self._pc_limit(str(raw.get("provider") or "")) is None):
                continue
            try:
                node = node_from_raw(raw, key)
            except (TypeError, ValueError):
                continue
            if not self._pc_holds(node, raw.get("status"), owner, snapshot):
                raw["slot_owner"] = None

    async def _begin_slot_claim(self, run: Run) -> bool:
        """PC-R3f: activate an intact marker, or reacquire a revoked slot.

        An intact token already owns capacity, even after a limit was lowered.
        Reclaimed or adopted dead runs reserve both limits again before any
        post-mortem work, never restoring their claim over a replacement.
        False means another turn already owns this node, or it was removed.
        """
        provider = run.provider.name
        if not run.slot_token and self._pc_limit(provider) is None:
            return True                     # PC-R5: no claim on an unlimited provider
        claim = dict(self._slot_claim("finalizing"),
                     token=run.slot_token or os.urandom(6).hex(), active=True)
        while True:
            snapshot = self._slot_snapshot()
            with self.tree.transaction() as data:
                entry = data["nodes"].get(run.node_id)
                if entry is None:
                    return False
                unchanged = snapshot.matches(run.node_id, entry)
                owner = entry.get("slot_owner")
                if isinstance(owner, dict):
                    if (owner.get("kind") == "finalizing"
                            and owner.get("owner") == self._hold_owner
                            and ((run.slot_token and owner.get("token") == run.slot_token)
                                 or (run.fix_turn and not run.slot_token
                                     and owner.get("active")))):
                        owner["active"] = True
                        return True
                    if unchanged and self._slot_owner_alive(owner, snapshot):
                        return False
                if self._pc_limit(provider) is None:
                    return True
                if unchanged:
                    active = self._occupants(data["nodes"], exclude=run.node_id,
                                             snapshot=snapshot)
                    if (active < int(self.config.limits.get("max_concurrent", 4))
                            and self._pc_admit(data, provider, run.node_id,
                                               snapshot=snapshot) is None):
                        entry["slot_owner"] = claim
                        run.slot_token = claim["token"]
                        return True
            # The dead run may have freed capacity for queued work ahead
            # of this finalizer, including when a limit was added mid-run.
            self._pc_kick(provider)
            await self._pc_wait(PC_RECONCILE_SECONDS)

    def _end_slot_claim(self, node_id: str, token: str) -> bool:
        """End the post-mortem claim `token` set, and no other."""
        if not token:
            return False
        with self.tree.transaction() as data:
            entry = data["nodes"].get(node_id)
            owner = (entry or {}).get("slot_owner")
            if isinstance(owner, dict) and owner.get("token") == token:
                entry["slot_owner"] = None
                return True
        return False

    def _slot_holders(self, nodes: dict, exclude: str = "",
                      snapshot: _SlotSnapshot | None = None) -> dict[str, list[str]]:
        """PC-R2: who holds a slot on each provider — the provider the run
        actually launched on, as recorded on its node — by exactly the rule
        the tree-wide count uses, so a dead process (`_occupies_slot`'s pid
        check) frees its slot in every process's view at once."""
        out: dict[str, list[str]] = {}
        for key, raw in self._occupying(nodes, exclude, snapshot):
            out.setdefault(str(raw.get("provider") or ""), []).append(key)
        return out

    def _pc_limit(self, provider: str) -> int | None:
        """PC-R1: the provider's `max_concurrent` as loaded now (re-read on
        every config change, so a new value applies at the next admission)."""
        here = self.providers.get(provider)
        return here.max_concurrent if here is not None else None

    @staticmethod
    def _pc_eligible(entry: dict, snapshot: _SlotSnapshot | None = None) -> bool:
        """PC-R3a: does a queued entry stand ahead of later arrivals? Not a
        head skipped for another reason (`blocked`), and not a consult's
        waiter whose waiting process has died."""
        if entry.get("blocked") and float(entry.get("blocked_until") or 0) > now():
            # PC-R3a: a head skipped for another reason stays skipped only
            # until its re-check time; then it is eligible again, so a new
            # arrival cannot overtake it on a stale verdict.
            return False
        claim = entry.get("claim")
        if isinstance(claim, dict):
            if snapshot is None:
                alive = _claim_alive(claim)
            elif snapshot.claims.get(entry.get("id")) != claim:
                alive = True
            else:
                alive = snapshot.alive.get((claim.get("pid"), claim.get("start") or ""), True)
            if not alive:
                return False
        return True

    def _pc_admit(self, data: dict, provider: str, exclude: str = "",
                  queued_id: str = "",
                  snapshot: _SlotSnapshot | None = None) -> ProviderFull | None:
        """PC-R2a/R3a: the provider half of an admission, INSIDE the caller's
        tree transaction — the same one that takes the tree-wide slot, so
        the two are reserved together or not at all.

        Refused (returned, the caller raises outside the transaction) when
        the provider's slots are all held, or when an eligible entry is
        queued ahead: for a new arrival that is any, for a queued entry
        being drained (`queued_id`) any with a lower sequence number. On
        success a drained entry is consumed in this same transaction, so
        claiming it and reserving its slot are one step.

        Transactional callers must supply a snapshot taken before locking.
        Read-only routing callers can observe their copied data here.
        """
        if snapshot is None:
            snapshot = self._slot_snapshot(data)
        limit = self._pc_limit(provider)
        queue = pc_waiting(data["deferred"], provider)
        if queued_id:
            mine = find_deferred(queue, queued_id)
            if mine is None:
                return ProviderFull(provider, limit, [], gone=True)
            ahead = [d for d in queue
                     if float(d.get("seq") or 0) < float(mine.get("seq") or 0)]
        else:
            ahead = queue
        ahead = [d for d in ahead if self._pc_eligible(d, snapshot)]
        holders: list[str] = []
        if limit is not None:
            holders = self._slot_holders(data["nodes"], exclude, snapshot).get(provider, [])
        if ahead or (limit is not None and len(holders) >= limit):
            return ProviderFull(provider, limit, holders, ahead=len(ahead))
        self._pc_reclaim_launch_claims(data["nodes"], snapshot)
        if queued_id:
            data["deferred"] = [d for d in data["deferred"]
                                if not (isinstance(d, dict) and d.get("id") == queued_id)]
        return None

    def _pc_full(self, provider: str, queued_id: str = "") -> ProviderFull | None:
        """PC-R3: would `provider` refuse an admission now? A read, for
        routing; the admission itself decides again, atomically."""
        data = self.tree.read()           # a copy: the consumption is not written
        if self._pc_limit(provider) is None and not pc_waiting(data["deferred"], provider):
            return None
        return self._pc_admit(data, provider, queued_id=queued_id)

    def _pc_dequeued(self, entry_id: str, provider: str, agent_id: str,
                     op: str = "") -> None:
        """PC-R4: a queued entry left the queue for a slot."""
        self.tree.emit(agent_id, PC_CAUSE, action="released", provider=provider,
                       deferred_id=entry_id, op=op)
        self.tree.emit("system", "deferred_exit", deferred_id=entry_id,
                       outcome="restarted", agent_id=agent_id)

    # ------------------------------------------------------------ spend caps --

    def _cap_providers(self) -> dict[str, Provider]:
        """SC-R3c: the providers as the config files say NOW, for their caps.

        Every admission point asks through here, and the copy kept is
        re-validated at each check by the mtime and size of every layer's
        `providers.yaml`, so a cap another process lowered (or raised) binds
        at this process's next launch, whatever its last config reload. An
        edit that fails to load never weakens the caps in force: the previous
        ones stay, and the error is reported once."""
        files = [Path(layer) / "providers.yaml" for layer in self.config.layers]
        stamp = []
        for path in files:
            try:
                st = path.stat()
                stamp.append((str(path), st.st_mtime_ns, st.st_size))
            except OSError:
                stamp.append((str(path), None, None))
        cached = self.__dict__.get("_cap_cache")
        if cached is not None and cached[0] == stamp:
            return cached[1]
        try:
            merged: dict[str, Any] = {}
            for path in files:
                merged = config_mod.deep_merge(
                    merged, config_mod.read_yaml_cached(path, strict=True))
            current = load_providers(merged.get("providers", {}) or {})
        except Exception as exc:
            self._ledger_failed(RuntimeError(f"spend caps not re-read, the caps in "
                                             f"force stay: {type(exc).__name__}: {exc}"))
            current = cached[1] if cached is not None else self.providers
        self.__dict__["_cap_cache"] = (stamp, current)
        return current

    def _caps(self, provider_name: str, model: str) -> list[spendcap.Cap]:
        """SC-R1/R3c: the caps in force for `model` on `provider_name`, read
        from the config files as they are now. A plan provider is never
        capped (SC-R2)."""
        provider = self._cap_providers().get(provider_name)
        if provider is None or provider.billing == "plan" or provider.spend_cap is None:
            return []
        return provider.spend_cap.caps_for(provider_name, model or "")

    def _ledger_failed(self, exc: Exception) -> None:
        """SC-R2a: a ledger error, reported once per distinct error."""
        text = f"{type(exc).__name__}: {exc}"[:400]
        if text in self._ledger_errors:
            return
        self._ledger_errors.add(text)
        self.tree.emit(self.self_id() or "system", "spend_ledger_error", error=text)

    @staticmethod
    def _cap_text(state: dict) -> str:
        label = (f"{state['provider']} model {state['model']}" if state["model"]
                 else state["provider"])
        return (f"{label} reached its spend cap of ${state['usd']:g} per "
                f"{state['period']} (${state['spend']:g} spent this {state['period']}; "
                f"resets {spendcap.iso(state['resets_at'])})")

    def _cap_verdict(self, binding: list[dict]) -> dict:
        """The refusal or stop for caps that bind: every one of them named,
        and `until` the latest of their resets (SC-R3a/SC-R4a)."""
        until = max(state["resets_at"] for state in binding)
        # SC-R4c: the crossings this verdict answers, kept on the node when it
        # stops (`_cap_stopped_event`) so recovery can name it from the tree.
        crossing_ids = [spendcap.crossing_id(
            f"model:{s['provider']}:{s['model']}" if s["model"] else f"provider:{s['provider']}",
            s.get("period_start", spendcap.period_start(s["resets_at"] - 1, s["period"])),
            s["usd"]) for s in binding]
        caps = [{"provider": s["provider"], "model": s["model"] or None,
                 "cap": s["usd"], "period": s["period"], "spend": s["spend"],
                 "resets_at": spendcap.iso(s["resets_at"])} for s in binding]
        return {"cause": spendcap.CAUSE, "until": until, "caps": caps,
                "crossing_ids": crossing_ids,
                "reason": f"{spendcap.CAUSE}: "
                          + "; ".join(self._cap_text(s) for s in binding)}

    def _ledger_unusable(self, provider_name: str, exc: Exception | None = None) -> dict:
        """SC-R2a: the verdict for a cap whose ledger cannot be used."""
        retry = now() + float(self.config.project.get("budget", {})
                              .get("blind_cooldown_seconds", 900))
        detail = f" ({exc})" if exc is not None else ""
        return {"cause": spendcap.UNREADABLE, "until": retry, "caps": [],
                "reason": f"{spendcap.UNREADABLE}: {provider_name} has a spend cap "
                          f"and the spend ledger cannot be read or written"
                          f"{detail}; launches under the cap are refused until it can"}

    def _cap_refusal(self, provider_name: str, model: str) -> dict | None:
        """SC-R3/R3a/R3b: why the spend caps refuse a launch of `model` on
        `provider_name` now, or None. With no cap, never anything — the
        ledger is not even read (SC-R6). With one, a ledger that cannot be
        read and written refuses (`spend_cap_unreadable`, SC-R2a)."""
        caps = self._caps(provider_name, model)
        if not caps:
            return None
        try:
            self.ledger.probe()
        except OSError as exc:
            self._ledger_failed(exc)
            return self._ledger_unusable(provider_name, exc)
        # Charges on this scope that could not be committed earlier count
        # before anything is admitted under it — read from the tree now, never
        # from the cheap cache the uncapped path uses (r3 #2). One that still
        # cannot be committed leaves the spend unknown: refused (r3 #1).
        if not self._flush_pending(provider=provider_name, fresh=True):
            return self._ledger_unusable(provider_name, RuntimeError(
                "a held charge on this provider could not be committed"))
        at = now()
        binding = [state for state in (self.ledger.describe(cap, at) for cap in caps)
                   if state["reached"]]
        return self._cap_verdict(binding) if binding else None

    def _charge(self, run: Run, event: Event, session_id: str, position: int,
                replayed: bool, line_start: int | None = None) -> bool:
        """SC-R2/R2a/R4: one cost event of a metered run, into the ledger,
        and what its caps say about it. False when it could not be committed.

        A replayed line (adoption) is never charged: the server before this
        one committed every charge before its checkpoint moved past it, so a
        line inside that checkpoint is either charged already or was seen by
        a server from before the ledger existed — and SC-R2 has no backfill
        (#10).

        A charge that cannot be committed is never dropped (#2). It is kept
        in the tree (`spend_pending`), which outlives the run, and retried by
        any later charge on its provider, by the node's next steer or
        adoption, by every cap check on the provider and by every drain; on
        the run as well when even the tree cannot take it. Meanwhile
        `run.charge_hold` keeps the replay checkpoint from moving past it,
        and under a cap the caller fails closed and stops the run."""
        if replayed:
            return True
        provider, model = run.provider.name, run.spec.model or ""
        # R-3: an unambiguous encoding — a separator could appear in an id.
        if event.step_id:
            key = json.dumps(["step", provider, session_id or run.node_id, event.step_id])
        else:
            key = json.dumps(["pos", run.node_id, run.turn_start, position])
        entry = {"key": key, "usd": event.cost, "at": now(), "provider": provider,
                 "model": model, "agent": run.spec.name, "node": run.node_id}
        account = self._qh_account(run.provider, run.spec)
        if account is not None:
            entry["account"] = account
        queue = [*run.pending_charges, entry]
        run.pending_charges = []
        flushed = self._flush_pending(provider=provider)
        crossings, binding, done = [], [], 0
        if flushed:
            for pending in queue:
                try:
                    new, binding = self._commit_charge(pending)
                except OSError as exc:
                    self._ledger_failed(exc)
                    break
                crossings += new
                done += 1
        left = queue[done:]
        if left and not self._hold_charges(left):
            run.pending_charges = left            # the tree refused them too
        for crossing in crossings:
            self._crossed(run.node_id, crossing)
        if left or not flushed:
            if run.charge_hold is None:
                run.charge_hold = position if line_start is None else line_start
            return False
        if not self._has_pending(node=run.node_id):
            run.charge_hold = None
        if binding and run.cap_stop is None:
            run.cap_stop = self._cap_verdict(binding)
        return True

    def _commit_charge(self, entry: dict) -> tuple[list[dict], list[dict]]:
        return self.ledger.charge(
            key=entry["key"], provider=entry["provider"], model=entry["model"],
            agent=entry["agent"], node=entry["node"], usd=entry["usd"],
            at=entry["at"], caps=self._caps(entry["provider"], entry["model"]),
            account=entry.get("account"))

    def _hold_charges(self, entries: list[dict]) -> bool:
        """#2: keep charges the ledger refused in the tree, which survives
        the run and the server, until a retry commits them."""
        try:
            with self.tree.transaction() as data:
                held = data.setdefault(SPEND_PENDING, [])
                keys = {e.get("key") for e in held if isinstance(e, dict)}
                held.extend(e for e in entries if e["key"] not in keys)
        except Exception as exc:                            # noqa: BLE001
            self._ledger_failed(exc)
            return False
        self.__dict__["_pending_seen"] = True
        return True

    def _has_pending(self, *, node: str | None = None,
                     provider: str | None = None) -> bool:
        return bool(self._pending(node=node, provider=provider))

    def _pending(self, *, node: str | None = None, provider: str | None = None,
                 fresh: bool = False) -> list[dict]:
        # One tree read per PENDING_CHARGE_CHECK_SECONDS while nothing is
        # known to be held, so a cost event costs no tree read of its own.
        # `fresh` (a cap check) always reads.
        if (not fresh and not self.__dict__.get("_pending_seen")
                and now() - self.__dict__.get("_pending_checked", 0.0)
                < self.PENDING_CHARGE_CHECK_SECONDS):
            return []
        try:
            held = self.tree.read().get(SPEND_PENDING) or []
        except Exception:                                   # noqa: BLE001
            return []
        self.__dict__["_pending_checked"] = now()
        self.__dict__["_pending_seen"] = bool(held)
        return [e for e in held if isinstance(e, dict) and isinstance(e.get("key"), str)
                and (node is None or e.get("node") == node)
                and (provider is None or e.get("provider") == provider)]

    def _flush_pending(self, *, node: str | None = None, provider: str | None = None,
                       fresh: bool = False) -> bool:
        """#2: retry the held charges (of one node or provider, or all).
        True when none of them is left. A crossing one of them causes is
        handled as any other: announced, and — only while its period is
        current (SC-R4b) — its scope's runs stopped."""
        held = self._pending(node=node, provider=provider, fresh=fresh)
        if not held:
            return True
        done: list[str] = []
        ok = True
        for entry in held:
            try:
                new, _ = self._commit_charge(entry)
            except OSError as exc:
                self._ledger_failed(exc)
                ok = False
                break
            done.append(entry["key"])
            for crossing in new:
                self._crossed(entry["node"], crossing)
        if done:
            with contextlib.suppress(Exception):
                with self.tree.transaction() as data:
                    data[SPEND_PENDING] = [
                        e for e in data.get(SPEND_PENDING) or []
                        if not (isinstance(e, dict) and e.get("key") in done)]
        return ok

    def _on_scope(self, provider: str, model: str, node_provider: str,
                  node_model: str) -> bool:
        return node_provider == provider and (not model or node_model == model)

    def _scope_agents(self, provider: str, model: str) -> list[str]:
        return sorted(
            node_id for node_id, raw in self.tree.read()["nodes"].items()
            if isinstance(raw, dict) and raw.get("status") in (*ACTIVE, "steered")
            and self._on_scope(provider, model, raw.get("provider") or "",
                               raw.get("model") or ""))

    def _announce(self, crossing: dict, agents: list[str], recovering: bool) -> None:
        """The crossing's one `spend_cap` event (SC-R4a), recorded once across
        processes through the ledger's announcement. A recovery first looks
        for the event itself, in case its claimer died between the two."""
        ident = crossing.get("id") or spendcap.crossing_id(
            crossing["scope"], crossing["period_start"], crossing["cap"])
        provider, model = crossing["provider"], crossing.get("model") or ""

        def emit() -> None:
            if recovering and self._event_recorded(ident):
                return
            # SC-R4c: a write that fails raises, so the announcement is not
            # recorded and a later watcher retries it.
            self.tree.emit_checked(crossing.get("by") or self.self_id() or "system",
                           spendcap.CAUSE, provider=provider,
                           **({"model": model} if model else {}),
                           cap=crossing["cap"], spend=crossing["spend"],
                           period=crossing["period"], until=crossing["until"],
                           agents=agents, crossing_id=ident,
                           **({"recovered": True} if recovering else {}))
        try:
            self.ledger.announce(ident, emit)
        except OSError as exc:
            self._ledger_failed(exc)

    def _event_recorded(self, ident: str) -> bool:
        """A `spend_cap` event for crossing `ident` is in the log: a whole,
        parseable line naming it — a torn line is not a delivery (r3 #4)."""
        try:
            with self.paths.events_file.open("r", errors="replace") as fh:
                for line in fh:
                    # A whole JSON object is a delivery with or without its
                    # newline (r4 #3); a torn one does not parse.
                    if ident not in line:
                        continue
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if (isinstance(event, dict) and event.get("kind") == spendcap.CAUSE
                            and event.get("crossing_id") == ident):
                        return True
        except OSError:
            return False
        return False

    def _announce_pending(self) -> None:
        """#13: a crossing claimed by a process that died before recording
        its event is announced by the first process to see it, once its
        claimer has had ANNOUNCE_GRACE_SECONDS to do it itself."""
        self._record_stops()
        self._retry_startup_releases()
        try:
            self.ledger.poll()
            pending = self.ledger.unannounced(now() - self.ANNOUNCE_GRACE_SECONDS)
        except OSError as exc:
            self._ledger_failed(exc)
            return
        for crossing in pending:
            # SC-R4c: the recovered event names the runs recorded as stopped
            # by this crossing — in the ledger, or durably on their node (a
            # stop whose ledger record never landed, r4 #2) — never whoever
            # happens to be active now. SF-R2: that evidence is historical:
            # a node the crossing stopped is named whatever its status now,
            # since it may have been cancelled, steered, resumed or finished
            # in between.
            agents = sorted(set(self.ledger.stops.get(crossing["id"], [])) | {
                node_id for node_id, raw in self.tree.read()["nodes"].items()
                if isinstance(raw, dict)
                and crossing["id"] in (raw.get("spend_cap_crossings") or [])})
            self._announce(crossing, agents, recovering=True)

    def _crossed(self, node_id: str, crossing: dict) -> None:
        """SC-R4/R4a: a crossing this process claimed for `node_id`'s charge.
        Its one `spend_cap` event names the runs it stops — every active run
        drawing on the scope, all launched before it: this process's own are
        stopped now, other processes' at their next poll of the ledger, where
        the crossing is the durable stop request."""
        provider, model = crossing["provider"], crossing["model"]
        if spendcap.period_start(now(), crossing["period"]) != crossing["period_start"]:
            # SC-R4b (r3 #3): a held charge from a period that has ended
            # crossed that period's cap. It is recorded, and stops nothing now.
            self._announce(crossing, [], recovering=False)
            return
        agents = self._scope_agents(provider, model)
        if node_id not in agents:
            agents.append(node_id)
        for other in list(self.runs.values()):
            if (other.node_id != node_id and other.cap_stop is None
                    and other.handle is not None and not other.done.is_set()
                    and self._on_scope(provider, model, other.provider.name,
                                       other.spec.model or "")):
                self._stop_for_cap(other, crossing)
        self._announce(crossing, agents, recovering=False)

    def _stop_for_cap(self, run: Run, crossing: dict) -> None:
        """Stop `run` for a cap: its verdict from every cap of its own that
        binds, else from the crossing that stopped it."""
        at = now()
        binding = [state for state in (self.ledger.describe(cap, at) for cap in
                                       self._caps(run.provider.name, run.spec.model or ""))
                   if state["reached"]]
        if not binding:
            binding = [{"provider": crossing["provider"], "model": crossing["model"],
                        "usd": crossing["cap"], "period": crossing["period"],
                        "spend": crossing["spend"], "resets_at": crossing["until"],
                        "period_start": crossing["period_start"]}]
        run.cap_stop = self._cap_verdict(binding)
        ident = crossing.get("id") or spendcap.crossing_id(
            crossing["scope"], crossing["period_start"], crossing["cap"])
        # SF-R1: the crossing that actually stopped the run is evidence even
        # when the caps configured now (`binding`) name a different crossing,
        # e.g. crossed at $1 and lowered to $0.50 before this poll.
        if ident not in run.cap_stop["crossing_ids"]:
            run.cap_stop["crossing_ids"].append(ident)
        # SC-R4c: the stop is recorded against the crossing that caused it —
        # retried until it lands (r3 #5).
        self._unrecorded_stops.add((ident, run.node_id))
        self._record_stops()
        task = asyncio.ensure_future(run.handle.stop())
        self._cap_tasks.add(task)
        task.add_done_callback(self._cap_tasks.discard)

    def _record_stops(self) -> None:
        """r3 #5: write the stop records not yet in the ledger. Retried at
        every watcher poll, at every finalization and before a recovery.
        SF-R1/R2 (revision): the node's cumulative crossing evidence is
        written under the same retry — so a stop whose ledger record never
        landed still recovers from the tree. Review r1 finding 5: the pair is
        kept until BOTH writes have succeeded, so a failed evidence write is
        not discarded by a ledger write that landed."""
        for ident, node_id in sorted(self._unrecorded_stops):
            remembered = self._remember_stop(node_id, ident)
            try:
                self.ledger.record_stop(ident, node_id)
            except OSError as exc:
                self._ledger_failed(exc)
                return
            if remembered:
                self._unrecorded_stops.discard((ident, node_id))

    def _remember_stop(self, node_id: str, ident: str) -> bool:
        """SF-R1/R2: add `ident` to the node's crossing evidence, never
        replacing what is there — the union survives later stops, steers and
        resumes. True when the node holds it (or no longer exists); False
        when the write failed, reported like a failed ledger write so the
        pair is retried rather than silently dropped (review r1 finding 5)."""
        try:
            node = self.tree.get(node_id)
            if node is None or ident in node.spend_cap_crossings:
                return True
            self.tree.update(node_id,
                             spend_cap_crossings=[*node.spend_cap_crossings, ident])
            return True
        except Exception as exc:                            # noqa: BLE001
            self._ledger_failed(exc)
            return False

    async def _watch_spend_caps(self, run: Run) -> None:
        """SC-R4a: the stop requests other processes' crossings wrote. Every
        active metered run polls the ledger — a stat when nothing changed —
        for a crossing on its scope, this period, recorded after it launched
        (`Ledger.stop_request`), whatever cap this process has configured
        (#1). It also announces crossings whose claimer died (#13)."""
        while run.cap_stop is None or self._unrecorded_stops:
            await asyncio.sleep(self.SPEND_CAP_POLL_SECONDS)
            if self._unrecorded_stops:
                self._record_stops()
            if run.cap_stop is not None or run.handle is None:
                continue
            provider = self.providers.get(run.provider.name) or run.provider
            if provider.billing == "plan":
                continue
            try:
                self.ledger.poll()
            except OSError as exc:
                self._ledger_failed(exc)
                continue
            if self.ledger.crossings:
                self._announce_pending()
            crossing = self.ledger.stop_request(run.provider.name, run.spec.model or "",
                                                run.launched_at, now())
            if crossing is not None and run.cap_stop is None:
                self._stop_for_cap(run, crossing)

    def _with_concurrency(self, reason: str, providers: Any) -> str:
        """PC/SC: a cap refusal's text, with the concurrency refusal of any
        of `providers` that is also full, so both causes are reported."""
        full = []
        for name in providers:
            with contextlib.suppress(Exception):
                refused = self._pc_full(name)
                if refused is not None and not refused.gone:
                    full.append(str(refused))
        return "; ".join([reason, *full])

    def _cap_raced_start(self, node_id: str, refusal: dict, agent_name: str,
                         task: str, *, spec: AgentSpec, provider: str,
                         model: str | None, timeout: int | None,
                         workdir: str | None, queued: dict | None) -> dict[str, Any] | None:
        """SC-R3 (#7): a fresh start a cap refused at spawn. Its node, which
        never ran, is `refused`. A queued entry goes back to its place and a
        pin is refused, both causes named; otherwise None: the caller routes
        the task again, as admission does (#6)."""
        reason = self._with_concurrency(refusal["reason"], [provider])
        # r3 #6: the node never ran, so it gives up the deferred entry's
        # identity FIRST — only the node that launches carries it, and a
        # crash from here on leaves no carrier: the entry is requeued.
        self.tree.update(node_id, deferred_id="")
        if not self._defer_while_held(node_id, "refused", reason):
            self.tree.set_status(node_id, "refused",
                                 f"{reason}; refused at spawn, nothing ran")
        if queued is not None:
            self.tree.restore_deferred(queued)
            return {"blocked": True, "reason": reason, "retry_after": refusal["until"]}
        if model:
            return {**self._pin_refusal(provider, reason, refusal["until"]),
                    "refused_node": node_id}
        return None

    def _cap_defer_route(self, node_id: str, refusal: dict, agent_name: str,
                         task: str, *, spec: AgentSpec, provider: str,
                         timeout: int | None, workdir: str | None) -> dict[str, Any]:
        """A fresh start deferred on the one route a cap refused at spawn,
        released early when that cap is raised (SC-R3b)."""
        reason = self._with_concurrency(refusal["reason"], [provider])
        entry = self.tree.defer(
            {"agent": agent_name, "task": task, "timeout": timeout, "model": None,
             "workdir": workdir, "provider": provider},
            refusal["until"], reason, deferred_by=self.self_id(),
            cause=spendcap.CAUSE,
            extra={"spend_cap_routes": [[provider, spec.model or ""]],
                   "refused_node": node_id})
        return {"deferred": True, "reason": reason, "retry_after": refusal["until"],
                "paused": False, "cause": spendcap.CAUSE, "deferred_id": entry["id"],
                "refused_node": node_id}

    def _cap_released(self) -> list[dict]:
        """SC-R3b: waiting `spend_cap` deferrals not yet due whose cap was
        raised or removed — one of the routes they were refused on admits
        now — restarted at the next drain, before their stored time."""
        current = now()
        out = []
        for entry in self.tree.read()["deferred"]:
            if (not isinstance(entry, dict) or deferred_malformed(entry)
                    or entry.get("cause") != spendcap.CAUSE
                    or entry.get("status", "waiting") != "waiting"
                    or entry["retry_after"] <= current):
                continue
            routes = [r for r in entry.get("spend_cap_routes") or []
                      if isinstance(r, list) and len(r) == 2]
            if any(self._cap_refusal(str(p), str(m or "")) is None for p, m in routes):
                out.append(entry)
        return out

    def _occupants(self, nodes: dict, exclude: str = "",
                   snapshot: _SlotSnapshot | None = None) -> int:
        """How many nodes hold a `max_concurrent` slot — the ONE count every
        admission and `capacity()` uses.

        A node carrying a launch-cleanup hold (RM-R1d: the durable
        `cleanup_hold`, or this Runner's own hold whose durable write has
        not landed) occupies in ANY status: a `stop()` can mark it
        `cancelled` while the process it could not confirm dead may still
        run (review ag-f27608). Every other node counts by the SL-R4 rule,
        and only while active. Drivers never count; a malformed entry is
        skipped (HA-R12).
        """
        return sum(1 for _ in self._occupying(nodes, exclude, snapshot))

    def _refuse_full(self, spec: AgentSpec, active: int,
                     max_concurrent: int) -> RuntimeError:
        return RuntimeError(self._refused(
            f"{active} agents already running (max_concurrent={max_concurrent}). "
            f"Wait for one to finish or stop it.",
            spec, "limits.max_concurrent", max_concurrent, "tree",
            notices.provenance(self.config, "limits.max_concurrent", max_concurrent),
            f"{active} already running"))

    def _admission(self, spec: AgentSpec) -> None:
        """The `max_concurrent` occupancy rule (RM-R1, shared with start()).

        A node counts against the tree's slot limit exactly as `_preflight`
        counts it for `start()` — same count, same refusal text and shape.
        The idle node of a standing conversation holds no slot
        (`_occupies_slot`), so only live agents are measured against the cap.
        """
        self._settle_holds()
        max_concurrent = int(self.config.limits.get("max_concurrent", 4))
        active = self._occupants(self.tree.read()["nodes"])
        if active >= max_concurrent:
            raise self._refuse_full(spec, active, max_concurrent)

    def _admission_reserved(self, spec: AgentSpec, node: Node,
                            queued_id: str = "") -> None:
        """RM-R1a: the resumed consult's admission, with the slot reserved.

        `_admission` alone checks and returns, and the resuming node only
        holds a slot once its process is up — so two resumes competing for
        the last slot could both pass while the other's node still read
        `idle`, and both would launch. Here the check and the reservation
        are one tree transaction: the node is written `pending`, the status
        that always holds a slot (`_occupies_slot`), only when the count
        taken in that same transaction says there is room. A refusal raises
        before any write, so the conversation stays idle, exactly as
        RM-R1 requires; `_release_reserved_slot` gives the slot back when
        the turn fails after reserving.

        RM-R1d: a conversation whose previous launch still carries a cleanup
        hold is refused outright — resuming it would put a second process
        on the session while the first is not confirmed dead.
        """
        self._settle_holds()
        max_concurrent = int(self.config.limits.get("max_concurrent", 4))
        full = None
        claim = self._slot_claim("reservation")
        snapshot = self._slot_snapshot()
        with self.tree.transaction() as data:
            entry = data["nodes"].get(node.id)
            held = bool((entry or {}).get("cleanup_hold")) or node.id in self._holds
            active = self._occupants(data["nodes"], exclude=node.id, snapshot=snapshot)
            if not held and active < max_concurrent:
                # PC-R2a: the provider slot in the same transaction.
                full = self._pc_admit(data, node.provider, node.id, queued_id, snapshot)
                if full is None:
                    if entry is not None:
                        entry["status"] = "pending"
                        entry["slot_owner"] = claim
        if full is not None:
            raise full
        if not held and active < max_concurrent:
            if queued_id:
                self._pc_dequeued(queued_id, node.provider, node.id, "consult")
            return
        # The refusal is recorded OUTSIDE the transaction: `_refused` writes
        # a notice, and holding the tree's flock while it does would ask the
        # same lock of a second file descriptor.
        if held:
            raise RuntimeError(_held_refusal(node.id))
        raise self._refuse_full(spec, active, max_concurrent)

    def _admission_add(self, spec: AgentSpec, node: Node, queued_id: str = "") -> None:
        """RM-R1a: start()'s admission, one transaction with the node insert.

        `_preflight` checked occupancy long before this point, and the
        telemetry await in between is a window: a resumed consult reserves
        the last slot there (its own RM-R1a reservation), and a start that
        trusted its earlier check would launch into a full tree — occupancy
        2 under `max_concurrent=1`. So the count and the write that takes
        the slot are one transaction, exactly as `_admission_reserved` does
        for a resumed consult: the node enters the tree `pending`, the
        status that always holds a slot (`_occupies_slot`), only when the
        count taken in that same transaction says there is room. A refusal
        raises before any write; the refusal itself is recorded OUTSIDE the
        transaction, as above.
        """
        self._settle_holds()
        max_concurrent = int(self.config.limits.get("max_concurrent", 4))
        admitted = False
        full = None
        claim = self._slot_claim("reservation")
        snapshot = self._slot_snapshot()
        with self.tree.transaction() as data:
            active = self._occupants(data["nodes"], snapshot=snapshot)
            # PC-R2a: the provider slot is reserved in this same transaction,
            # so a tree-wide refusal leaves no provider slot held and the
            # reverse.
            if active < max_concurrent:
                full = self._pc_admit(data, node.provider, queued_id=queued_id,
                                      snapshot=snapshot)
            if active < max_concurrent and full is None:
                data["nodes"][node.id] = dict(asdict(node),
                                              slot_owner=claim)
                if node.parent and node.parent in data["nodes"]:
                    kids = data["nodes"][node.parent].setdefault("children", [])
                    if node.id not in kids:
                        kids.append(node.id)
                admitted = True
        if full is not None:
            raise full
        if not admitted:
            raise self._refuse_full(spec, active, max_concurrent)
        if queued_id:
            self._pc_dequeued(queued_id, node.provider, node.id, "start")
        self.tree.emit(
            node.id, "created",
            agent=node.agent, provider=node.provider, model=node.model,
            parent=node.parent, depth=node.depth, branch=node.branch,
        )

    def _release_reserved_slot(self, node_id: str) -> None:
        """RM-R1a: give back the slot a reserved resumed turn did not use.

        Only a `pending` node is touched — once the turn is `running` the
        slot belongs to the run, and its own finalization releases it.
        RM-R1c: while a launch cleanup holds the node (death not confirmed)
        the give-back is deferred to the hold's end: the `pending` status is
        the durable reservation should the hold itself not have reached the
        tree.
        """
        if self._defer_while_held(node_id, "idle", ""):
            return
        with self.tree.transaction() as data:
            entry = data["nodes"].get(node_id)
            if entry is not None and entry.get("status") == "pending":
                entry["status"] = "idle"
                entry["slot_owner"] = None

    def _preflight(self, spec: AgentSpec, workdir: str | None = None,
                   budget_tag: str = "", *, pinned: bool = False) -> None:
        limits = self.config.limits
        if spec.launch:
            raise PermissionError(
                f"Agent {spec.name!r} is the orchestrator: it is launched by "
                f"`multiagents run`, not spawned as a subagent. Spawning it "
                f"would give you an orchestrator inside an orchestrator."
            )
        # Isolation is the whole design: every agent gets its own branch in its
        # own worktree, which a project with no repository cannot provide. This
        # used to fall through to running each agent in the project directory
        # itself — concurrently, sharing one working tree, with no branch to
        # merge and nothing to discard when one went wrong. Failing here is the
        # only honest answer; an explicit workdir override is the caller saying
        # they meant it.
        paused = self.tree.pause_state()
        if paused and not pinned:
            # A pause names the providers that were exhausted. Refusing an agent
            # that still has a usable provider would be over-applying it: the
            # protection against unreviewed work is the orchestrator's own rule
            # about merging, not freezing agents that can still run.
            out = set(paused.get("providers") or [])
            options = {spec.provider, *(spec.models or {})}
            if out and not options - out:
                waiting = max(0, int(paused.get("until", 0) - now()))
                raise RuntimeError(
                    f"Paused: {paused.get('reason', 'no provider available')}. "
                    f"Every provider {spec.name!r} can use ({', '.join(sorted(options))}) "
                    f"is exhausted. It clears in about {waiting // 60}m{waiting % 60:02d}s "
                    f"and the tasks deferred behind it restart by themselves. Do not "
                    f"work around this — there is nothing left to run it on."
                )
        if workdir and not limits.get("allow_workdir_override", False):
            raise PermissionError(
                "workdir= is not permitted in this project. It would run the "
                "agent outside its worktree: no branch, no isolation from the "
                "other agents, and nothing to discard if the run goes wrong. "
                "A human can allow it with `limits.allow_workdir_override: "
                "true` in project.yaml."
            )
        if not workdir and not gitops.is_repo(self.paths.root):
            raise RuntimeError(
                f"{self.paths.root} is not a git repository, so no agent can be "
                f"given a branch and a worktree of its own. Run `multiagents "
                f"init` there and accept the offer to create one, or "
                f"`git init && git add -A && git commit -m 'initial commit'`."
            )
        if not self.can_spawn():
            raise PermissionError(
                "This agent was not granted permission to spawn subagents "
                "(can_spawn is false in its config)."
            )
        # Refused, not quietly allowed. An orchestrator denied an agent it
        # genuinely needs will say so, and that is a finding about the team's
        # roster — the alternative is a `teams` concept that describes nothing,
        # because anyone can step outside it. The expensive failure this guards
        # against is the orchestrator doing the work in its own context instead.
        team = self.config.team
        if team and not self.config.in_team(spec.name):
            raise PermissionError(
                f"Agent {spec.name!r} is not in the {team!r} team's roster "
                f"({', '.join(self.config.team_roster()) or 'empty'}). Either this "
                f"work belongs to a different team, or the roster is missing "
                f"someone — say which in your reply rather than doing it "
                f"yourself. Changing the roster is the user's call."
            )

        # LN-C1/C6: each refusal below is also a limit notice, and its text
        # carries the notice's line (LN-C3).
        caller = self.self_id() or "tree"
        depth = self.self_depth() + 1
        max_depth = int(limits.get("max_depth", 3))
        if depth > max_depth:
            raise PermissionError(self._refused(
                f"Depth limit reached: {depth} > max_depth={max_depth}",
                spec, "limits.max_depth", max_depth, caller,
                notices.provenance(self.config, "limits.max_depth", max_depth),
                "it would be deeper than the tree allows"))

        # RM-R1: the occupancy rule `start()` is admitted by, extracted so a
        # resumed consult turn is held to literally the same check and the
        # same refusal text rather than a copy that can drift.
        self._admission(spec)

        # LM-R1a: the cap is the SPAWNING parent's, as recorded when it
        # started; the requested child's own `max_children` governs its
        # children, not its siblings. The root orchestrator is not capped
        # here: max_concurrent bounds it, and `limits.max_children` would
        # halve the tree's parallelism (LM-R3).
        context = _launch_context.get()
        parent = context.run_parent if context else self.self_id()
        if parent:
            siblings = [c for c in self.tree.children_of(parent) if _occupies_slot(c)]
            cap = self._child_cap(parent)
            if len(siblings) >= cap["value"]:
                where = {"agent": "its agent config",
                         "project": "the project's limits.max_children",
                         "builtin": "the built-in default"}.get(cap["source"], cap["source"])
                owner = self.tree.get(parent)
                key = (f"agents.{owner.agent}.max_children"
                       if cap["source"] == "agent" and owner else "limits.max_children")
                raise RuntimeError(self._refused(
                    f"This agent already has {len(siblings)} active children "
                    f"(max {cap['value']}, source: {cap['source']} — {where}).",
                    spec, key, cap["value"], parent,
                    notices.provenance(self.config, key, cap["value"], cap["source"]),
                    f"{parent} already has {len(siblings)} active children"))

        ceiling = int(limits.get("budget_tokens", 0) or 0)
        if ceiling:
            used = self.tree.rollup_usage().get("total", 0)
            if used >= ceiling:
                raise RuntimeError(self._refused(
                    f"Tree token budget exhausted: {used:,} >= {ceiling:,}",
                    spec, "limits.budget_tokens", ceiling, "tree",
                    notices.provenance(self.config, "limits.budget_tokens", ceiling),
                    f"the tree has spent {used:,} tokens"))

        if budget_tag:
            cap = self.tree.budget_for_tag(budget_tag)
            if cap:
                spent = int(self.tree.usage_for_tag(budget_tag).get("total", 0) or 0)
                if spent >= cap:
                    # LN-C2: the FIRST ceiling set wins, so the source is the
                    # call that set it, not this one.
                    record = self.tree.budget_record(budget_tag)
                    raise RuntimeError(self._refused(
                        f"Budget for {budget_tag!r} is spent: {spent:,} of {cap:,} "
                        f"tokens. This is the limit doing its job, not an "
                        f"obstacle — decide what this slice of work does NOT get, "
                        f"report what you covered and what you did not, and move "
                        f"on. Raising it is the user's call, not yours.",
                        spec, f"budget_tag.{budget_tag}", cap, budget_tag,
                        notices.call_source("budget_tokens", budget_tag=budget_tag,
                                            node=record.get("set_by"),
                                            set_at=record.get("set_at")),
                        f"{budget_tag!r} has spent {spent:,} tokens"))

        provider = self.providers.get(spec.provider)
        if provider is None:
            raise KeyError(f"Agent {spec.name!r} names unknown provider {spec.provider!r}")
        if not provider.enabled:
            raise PermissionError(
                f"Provider {provider.name!r} is disabled in providers.yaml "
                f"(needed by agent {spec.name!r}). Set `enabled: true` to use it."
            )
        if self._host_binary_needed(spec) and not provider.available():
            raise FileNotFoundError(provider.bin_error())
        # An agent whose purpose failed to load should not run and guess at it.
        # The file is resolved across three config layers, so this is a typo or
        # a deleted brief, and the symptom without it — a capable agent doing
        # something adjacent to the task — is expensive to diagnose.
        missing = self.config.missing_instructions(spec)
        if missing:
            raise FileNotFoundError(
                f"Agent {spec.name!r} names instructions {', '.join(missing)}, "
                f"which are not in any config layer's agents/ directory. Fix the "
                f"path in agents.yaml or restore the file; `multiagents doctor` "
                f"lists every agent whose brief is missing, and `multiagents "
                f"prompt {spec.name}` shows what it would actually be sent."
            )
        if not (provider.spawn or {}).get("args"):
            raise PermissionError(
                f"Provider {provider.name!r} declares no spawn args, so it cannot run "
                f"delegates (agent {spec.name!r}). Without this check it would exec the "
                f"bare binary with no stdin and hang or fail obscurely."
            )

    def _refused(self, text: str, spec: AgentSpec, key: str, value: Any,
                 scope: str, source: dict[str, Any] | None, why: str) -> str:
        """LN-C1/C3: record a refusal as a limit notice and return the error
        text carrying its line. A repeat while the notice is active only counts
        (LN-C4), and says how many times."""
        notice = self._notice(key, value, "refused", scope, source,
                              f"start of {spec.name!r} refused ({why})",
                              "raise it there to allow more")
        count = int(notice.get("count", 1))
        again = f" (refused {count} times while this limit holds)" if count > 1 else ""
        return f"{text}\n{notice['message']}{again}"

    def _notice(self, key: str, value: Any, effect: str, scope: str,
                source: dict[str, Any] | None, what: str, advice: str = "",
                node: str | None = None) -> dict[str, Any]:
        """LN-C1: one `limit_hit`, deduplicated across processes (LN-C4)."""
        return notices.hit(self.tree, key=key, value=value, effect=effect,
                           scope=scope, source=source, node=node,
                           message=notices.message(key, value, source, what, advice))

    def _limits_detail(self, agent_name: str,
                       limits: dict[str, dict[str, Any]],
                       route: str = "") -> dict[str, dict[str, Any]]:
        """LN-C2: `effective_limits` as reported, each entry `{value, source,
        source_detail}`. `source` stays LM-R2's layer name; `source_detail`
        says which file and line (or which call argument) it came from."""
        out: dict[str, dict[str, Any]] = {}
        for name, entry in limits.items():
            limit_key = "limits." + ("max_steps" if name == "max_steps"
                                      else config_mod.LIMIT_FIELDS[name][0])
            layer = entry.get("source")
            if layer == "call":
                detail = notices.call_source(name, limit_key)
            else:
                key = f"agents.{agent_name}.{name}" if layer == "agent" else limit_key
                configured = self.config.agents.get(agent_name)
                entry_on_route = (configured.models or {}).get(route) if configured else None
                if (layer == "agent" and isinstance(entry_on_route, dict)
                        and config_mod._positive(entry_on_route.get(name)) == entry.get("value")):
                    key = f"agents.{agent_name}.models.{route}.{name}"
                detail = notices.provenance(self.config, key, entry.get("value"), layer)
            # SR-R3: the global project's defaults are the default layer;
            # a project's own limits remain the project layer.
            source = layer
            if layer == "project" and detail.get("file"):
                if Path(detail["file"]).parent == global_config_dir().resolve():
                    source = "default"
            out[name] = {**entry, "source": source, "source_detail": detail}
        return out

    def _node_cap(self, node: Node, spec: AgentSpec) -> Any:
        recorded = (node.limits or {}).get("max_children") or {}
        if "value" in recorded:
            return recorded["value"]
        return self.config.effective_limits(spec)["max_children"]["value"]

    def _child_cap(self, parent: str) -> dict[str, Any]:
        """LM-R1a: `{value, source}` of how many children `parent` may run at
        once. Recorded on its node at start; a node from before that was
        recorded is resolved from its agent's current config."""
        node = self.tree.get(parent)
        recorded = ((node.limits if node else None) or {}).get("max_children")
        if isinstance(recorded, dict) and "value" in recorded:
            return recorded
        spec = self.config.agents.get(node.agent) if node else None
        return self.config.effective_limits(spec)["max_children"]

    # ---------------------------------------------------------------- prompt --

    def compose_prompt(self, spec: AgentSpec, task: str, node: Node, workdir: Path) -> str:
        limits = self.config.limits
        branch_line = f"- Branch: {node.branch} (yours alone; commit freely)\n" if node.branch else ""
        spawn_line = (
            f"- You may spawn subagents (up to {self._node_cap(node, spec)}).\n"
            if spec.can_spawn else "- You may not spawn subagents.\n"
        )
        # Naming the protected paths HERE, rather than leaving it to the brief,
        # is what lets the rule follow the project's own layout. The brief can
        # only say "the tests"; this says which files, in this repository.
        readonly = self.config.readonly_paths_for(spec)
        readonly_line = ""
        if readonly and node.branch:
            protects_everything = any(
                p.strip() in ("**", "*") for p in readonly
            ) and not any(str(p).strip().startswith("!") for p in readonly)
            if protects_everything:
                head = ("- Read-only to you: EVERY file that already exists. You may ADD\n"
                        "  new files — that is how your work reaches anyone — but you\n")
            else:
                keep = [p for p in readonly if not str(p).strip().startswith("!")]
                drop = [p[1:].strip() for p in (str(x).strip() for x in readonly)
                        if p.startswith("!")]
                head = "- Read-only to you:\n" + _wrap_globs(keep)
                if drop:
                    head += "  except, which you may change freely:\n" + _wrap_globs(drop)
                head += "  You may READ these, and you may ADD new files among them, but you\n"
            readonly_line = (
                head
                + "  may not modify, delete or rename an existing one — if you do, the\n"
                "  change is reverted before your branch merges and your parent is told.\n"
                "  When one of them looks wrong, say so with NEED_INFO and let your\n"
                "  parent settle it; editing it is the one thing that cannot work.\n"
            )
        preamble = PREAMBLE.format(
            agent_id=node.id,
            agent_name=spec.name,
            provider=spec.provider,
            model=spec.model,
            parent=node.parent or "you (the orchestrator)",
            depth=node.depth,
            max_depth=limits.get("max_depth", 3),
            workdir=workdir,
            branch_line=branch_line,
            spawn_line=spawn_line,
            readonly_line=readonly_line,
        )
        # The caller half of the brief is stripped here. An agent that reads
        # "your caller should give you the harness API" may behave as though it
        # had been given, or spend its run complaining it was not — and either
        # way it is paying context for instructions addressed to somebody else.
        instructions, _ = config_mod._split_calling(
            self.config.instructions_for(spec))
        parts = [preamble]
        if spec.role == "bug-reporter":
            parts.append(self._bug_context())
        if instructions.strip():
            parts.append(instructions.strip() + "\n\n---\n")
        # Keyed on the provider the run actually launched on (`node.provider`),
        # not `spec.provider` — a run that fell back keeps its pinned spec but
        # `node.provider` is updated to whatever it landed on (see `start`'s
        # `chosen != spec.provider` branch), and that is whose tools and quirks
        # this prompt needs to describe. `notes:` is deliberately not read here:
        # it is for whoever edits providers.yaml, never for a model.
        launched_on = self.providers.get(node.provider)
        guidance = (launched_on.agent_guidance if launched_on else "").strip()
        if guidance:
            parts.append(guidance + "\n\n---\n")
        parts.append(f"## Task\n\n{task.strip()}\n")
        return "\n".join(parts)

    def _bug_context(self) -> str:
        """Facts a ticket needs, gathered here rather than asked of the agent.

        Two reasons. The agent cannot see most of this — it runs in a worktree
        of *your* project, not of multiagents. And a ticket is published, so
        what goes in it should be chosen by code that can be reviewed, not by a
        model improvising about its own environment.
        """
        import platform

        source = Path(__file__).resolve().parent   # for the commit only
        commit = ""
        if gitops.is_repo(source):
            commit = gitops.head_sha(source)[:12]
            if gitops.is_dirty(source):
                commit += " (modified)"
        providers = ", ".join(sorted(
            n for n, p in self.providers.items() if p.enabled and p.available()
        ))
        # The source path is NOT given. It used to be, with "read it to locate
        # the defect" — and the agent cannot: its file tools are confined to its
        # worktree, and under docker the source is not mounted in the container
        # at all. Two blocking tickets ended with a paragraph apologising for
        # that instead of describing the bug, and the path itself was a home
        # directory in a prompt whose product is meant to be publishable.
        #
        # The commit hash does the job the path was there for: it lets whoever
        # reads the ticket open the exact code the agent could not.
        # ...unless the project being orchestrated IS multiagents. Then the
        # agent's worktree is a checkout of the very source the ticket is
        # about, its file tools reach all of it, and telling it otherwise
        # would throw away the best evidence available to any reporter this
        # project has: a defect cited at file and line by something that just
        # read the code and ran the suite over it.
        return (
            "## Environment (generated — include it verbatim, add nothing to it)\n\n"
            f"- multiagents commit: {commit or 'unknown (not a checkout)'}\n"
            f"- python: {platform.python_version()} on {platform.system()} "
            f"{platform.release().split('-')[0]}\n"
            f"- executor: {self.config.project.get('executor', {}).get('kind', 'local')}\n"
            f"- providers available: {providers or 'none'}\n\n"
            + (self._reading_its_own_source()
               if self._is_multiagents_checkout()
               else "You cannot read the multiagents source from here and are "
                    "not expected to: report what you observed, and label any "
                    "cause you infer as a hypothesis. The commit above is what "
                    "locates the code.\n")
            + "\n---\n"
        )

    def _is_multiagents_checkout(self) -> bool:
        """Is the project being orchestrated multiagents' own source?

        By the shape of the tree rather than its name or its remote: a fork, a
        rename and a local clone are all still the source, and a directory that
        merely happens to be called multiagents is not.
        """
        root = getattr(self.paths, "root", None)
        if root is None:
            return False
        return (Path(root) / "src" / "multiagents" / "runner.py").is_file()

    @staticmethod
    def _reading_its_own_source() -> str:
        return (
            "**You can read the multiagents source from here.** This project "
            "IS multiagents: your worktree is a checkout of it, so the code "
            "the ticket is about is under `src/multiagents/` beside you, and "
            "the suite that covers it is under `tests/`.\n\n"
            "So do not stop at what you observed. Find the code, cite it as "
            "`path:line`, and say which test would have caught it and why it "
            "did not. Run the suite if it settles the question — "
            "`uv run --frozen pytest tests/ -q -k <pattern>`.\n\n"
            "The publishing rules above do not relax. A path under `src/` is "
            "the tool's own layout and belongs in the ticket; a path under a "
            "home directory is still the user's business and does not.\n"
        )

    @staticmethod
    def _fenced_ranges(text: str) -> list[tuple[int, int]]:
        """Character ranges of fenced code blocks, fences included. An
        unclosed fence runs to the end, as in Markdown."""
        ranges, opened, pos = [], None, 0
        for line in text.splitlines(keepends=True):
            fence = FENCE_LINE.match(line)
            if opened is None:
                if fence:
                    opened = (pos, fence.group(1))
            elif fence and fence.group(1)[0] == opened[1][0] \
                    and len(fence.group(1)) >= len(opened[1]) \
                    and not line.strip().strip(opened[1][0]):
                ranges.append((opened[0], pos + len(line)))
                opened = None
            pos += len(line)
        if opened is not None:
            ranges.append((opened[0], len(text)))
        return ranges

    def _file_tickets(self, node_id: str, text: str) -> list[dict]:
        """Turn a finished bug-reporter message into queued tickets.

        Every real marker files one ticket. The agent is reasoning about a
        system whose own documentation contains the literal string
        `TICKET(blocking):` — its brief does, and so does the orchestrator's —
        so a model that quotes the rule while thinking must not turn the rest
        of its monologue into a ticket. Quoting is therefore recognised, not
        guessed from position: a marker inside a fenced block, on a `>` line
        or inside inline backticks is not real.

        With two or more real markers an empty section is dropped and a
        repeated title files once, the later section winning. A lone marker
        always files, as it always did.
        """
        text = text or ""
        fenced = self._fenced_ranges(text)

        def in_fence(pos: int) -> bool:
            return any(a <= pos < b for a, b in fenced)

        # Inline code may wrap across lines but not across a blank one; fenced
        # text is masked so a stray backtick inside it pairs with nothing.
        masked = list(text)
        for a, b in fenced:
            masked[a:b] = ["." if c != "\n" else c for c in text[a:b]]
        spans = []
        for para in re.finditer(r"(?:[^\n]+\n?)+", "".join(masked)):
            base = para.start()
            spans += [(base + m.start(), base + m.end())
                      for m in INLINE_CODE.finditer(para.group())]

        matches = []
        for m in TICKET.finditer(text):
            line_start = text.rfind("\n", 0, m.start()) + 1
            if in_fence(m.start()) or re.match(r"[ \t]*>", text[line_start:]):
                continue
            if any(a <= m.start() < b for a, b in spans):
                continue
            matches.append(m)

        sections = []
        for i, match in enumerate(matches):
            stop = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            rest, offset = text[match.end():stop], match.end()
            fix = ""
            split = next((s for s in PROPOSED_FIX.finditer(rest)
                          if not in_fence(offset + s.start())), None)
            if split:
                fix = rest[split.end():].strip()
                rest = rest[:split.start()]
            sections.append((match.group(1).lower(), match.group(2).strip(),
                             rest.strip(), fix))
        if len(sections) > 1:
            sections = [s for s in sections if s[2] or s[3]]
            latest = {}
            for n, (_, title, _, _) in enumerate(sections):
                latest[title] = n
            sections = [s for n, s in enumerate(sections) if latest[s[1]] == n]
        return [
            self.tree.add_ticket(node_id, title, body, severity, fix,
                                 project_root=self.paths.root)
            for severity, title, body, fix in sections
        ]

    def _file_ticket(self, node_id: str, text: str) -> dict | None:
        """File the tickets in a message; return the last one, or None."""
        tickets = self._file_tickets(node_id, text)
        return tickets[-1] if tickets else None

    # ------------------------------------------------------------ ownership --

    @property
    def _locks(self) -> dict[str, Any]:
        return self.__dict__.setdefault("_supervision_locks", {})

    def _claim(self, node_id: str) -> bool:
        """SV-R5: take the exclusive lock on supervising this node.

        An advisory flock on a file in its run dir, held for as long as this
        server follows the node. The kernel drops it when the holder dies, so a
        crashed server's nodes become adoptable by themselves, while a live —
        or merely suspended — one keeps them. Re-entrant within this process.
        """
        if node_id in self._locks:
            return True
        run_dir = self.paths.run_dir(node_id)
        handle = _run_open(run_dir, SUPERVISOR_LOCK, os.O_RDWR | os.O_CREAT, "a+")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        self._locks[node_id] = handle
        return True

    def _release(self, node_id: str) -> None:
        handle = self._locks.pop(node_id, None)
        if handle is not None:
            with contextlib.suppress(OSError):
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()

    def _limits_for(self, node_id: str, spec: AgentSpec,
                    timeout: float | None = None) -> dict[str, dict[str, Any]]:
        """LM-R1b: the limits a (re)launch of this node runs under. A timeout
        the caller gave when the run started (source `call`) survives a retry
        or a steer; everything else is resolved again from the current config.
        The recorded value comes from the host-owned launch record, never from
        the node's `limits` in `tree.json` — that block is container-writable,
        and a run relaunching under its own forged wall clock is not a limit
        at all (adversary finding 8)."""
        if not timeout:
            recorded = self.launch_limits.lookup(node_id).get("timeout")
            if isinstance(recorded, dict):
                src = recorded.get("source")
                if src == "call" or (isinstance(src, dict) and src.get("layer") == "call"):
                    timeout = recorded.get("value")
        return self.config.effective_limits(spec, timeout)

    def _max_steps_limit(self, spec: AgentSpec, route: str = "") -> dict[str, Any]:
        """C7-R1: capture the step cap and its launch-time provenance."""
        value = int(spec.max_steps or self.config.limits.get("max_steps", 250))
        layer = "agent" if spec.max_steps else "project"
        return self._limits_detail(spec.name, {
            "max_steps": {"value": value, "source": layer}}, route)["max_steps"]

    def _supervisor(self, spec: AgentSpec, provider: Provider,
                    wall_timeout: float, silence_timeout: float | None = None,
                    max_steps: int | None = None
                    ) -> Supervisor:
        loop_repeats = int(self.config.limits.get("doom_loop_repeats", 5))
        if silence_timeout is None:
            silence_timeout = self.config.effective_limits(spec)["silence_timeout"]["value"]
        return Supervisor(
            silence_timeout=silence_timeout,
            wall_timeout=wall_timeout,
            max_steps=(self._max_steps_limit(spec, provider.name)["value"]
                       if max_steps is None else max_steps),
            loop_repeats=loop_repeats,
            loop_rearm=int(self.config.limits.get("doom_loop_rearm", loop_repeats)),
            declares_turn=_declares_turn(provider),
            opaque_tools=frozenset(provider.opaque_tools),
            opaque_tool_args=tuple(provider.opaque_tool_args),
        )

    def _effort_conflict(self, spec: AgentSpec, provider: Provider) -> None:
        """Refuse an effort EXPLICITLY configured against the model's own (RM-R5a).

        The pre-claim half of `_settle_effort`. A refusal must happen before
        the startup claim (RM-R7) — a contradiction is a configuration error,
        and taking the half-open provider's only probe or writing a
        startup.json run record for a start that then refuses would outlive
        it — so the routing loop runs this for EVERY candidate, while the
        normalisation below only ever runs for the candidate whose claim
        succeeded.
        """
        implied = provider.implied_effort(spec.model)
        if implied is None or not spec.effort or spec.effort == implied:
            return
        # FO-R1b generalised: an effort is explicit whenever it came from a
        # `models.<X>` entry, whatever X is and however the destination was
        # reached. The destination's own entry is merged last, so it is the
        # first place to look; when the destination is a family sibling of a
        # listed route, that route's entry supplied the effort that travelled.
        # Only a top-level effort is inherited, and stays normalised.
        explicit = bool(spec.fallback_for(provider.name)[1].get("effort"))
        if not explicit:
            _, route = self._routed_spec(spec, provider.name)
            if route and route != provider.name:
                explicit = bool(spec.fallback_for(route)[1].get("effort"))
        if explicit:
            raise ValueError(
                f"refusing to start {spec.name!r} on {provider.name}: model "
                f"{spec.model!r} implies effort {implied!r}, but the route "
                f"under `models:` explicitly configures effort "
                f"{spec.effort!r}. The CLI would reject the pair at launch. "
                f"Fix the route's effort (or remove it to inherit the "
                f"model's), then start again."
            )

    def _settle_effort(self, spec: AgentSpec, provider: Provider,
                       node_id: str) -> AgentSpec:
        """Reconcile the effort with a model id that declares its own (RM-R5a).

        A provider may map anchored model-id suffixes to efforts in
        providers.yaml (`effort_suffixes`). After routing has resolved the
        provider and model, and BEFORE any worktree or process side effect:

        - an effort INHERITED onto this destination — the agent's top-level
          `effort`, or a bare-string `models:` route — is normalised to the
          model's implied effort, with an event recording the old value, the
          effective pair and the reason; the normalised spec is what is
          persisted (Node.effort), so steer and consult reuse it;
        - an effort EXPLICITLY configured on this destination route is a
          configuration contradiction: the start is refused, naming the
          model, the effort and the route, rather than launching into the
          CLI's own rejection eight seconds later (the pre-claim check is
          `_effort_conflict`, which the routing loop runs for every
          candidate; this method re-checks only as a belt for callers that
          settle without a claim, as the consult path does);
        - and a provider with no `effort_suffixes` is unchanged, as is any
          pair that agrees or carries no effort at all (an empty route
          `effort: ""` still just drops the option).

        Called once for the provider the run actually launches on. In
        start()'s loop that is after `startup.claim` succeeded — settling
        inside the loop let a failed claim hand the next candidate the
        previous one's normalised effort and leave it an event naming a
        provider the run never launched on.
        """
        implied = provider.implied_effort(spec.model)
        if implied is None or not spec.effort or spec.effort == implied:
            return spec
        self._effort_conflict(spec, provider)
        self.tree.emit(node_id, "effort_normalised",
                       provider=provider.name, model=spec.model,
                       effort=implied, was=spec.effort,
                       reason=f"model id {spec.model!r} ends in a suffix "
                              f"declaring effort {implied!r}; the inherited "
                              f"effort was normalised to it")
        return spec.replace(effort=implied)

    # ------------------------------------------------- launch-cleanup hold --

    @property
    def gate(self) -> Gate:
        """CW-R2: whether this Runner admits launches while its server stops
        for a compaction, and how many admitted transitions are in flight."""
        return self.__dict__.setdefault("_safe_point_gate", Gate())

    @property
    def _holds(self) -> dict[str, _Hold]:
        return self.__dict__.setdefault("_launch_holds", {})

    @property
    def _hold_owner(self) -> str:
        """Which Runner owns a hold: two Runners can share one process."""
        return self.__dict__.setdefault("_launch_hold_owner", os.urandom(8).hex())

    def _owner_fields(self) -> dict:
        owner = os.getpid()
        return {"owner_pid": owner, "owner_start": procs.start_time(owner) or "",
                "owner": self._hold_owner}

    def _new_hold(self, node_id: str, provider_name: str,
                  startup_token: str, executor: Any) -> _Hold:
        """The in-memory half of a launch hold, before anything is written.
        `_reserve_launch` requires the durable write before spawning."""
        kind = str(getattr(executor, "kind", "local") or "local")
        identity = {"kind": kind,
                    "container": str(getattr(executor, "container", "") or "")
                    if kind == "docker" else ""}
        return _Hold(
            record={"since": now(), **self._owner_fields(),
                    "pid": None, "pid_start": "", "executor": identity,
                    "occupancy": "", "then": None},
            pid=None, pid_start="",
            probe_raw=self._raw_alive_probe(executor, node_id),
            provider=provider_name, token=startup_token)

    def _reserve_launch(self, node_id: str, provider_name: str,
                        startup_token: str, executor: Any) -> _Hold:
        """RM-R1d: the durable reservation, taken BEFORE the process starts.

        From here until supervision is established the node occupies its
        slot through `cleanup_hold`, whatever its status and whatever pid it
        still records — a retry's `running` node names its dead predecessor
        until the new pid is written, and that write can fail. The write is
        required: a launch whose reservation cannot be made durable does not
        happen (review ag-43f57f). It records the execution identity, which
        recovery uses instead of whatever the config says later.
        """
        hold = self._new_hold(node_id, provider_name, startup_token, executor)
        self._record_node_launch_evidence(node_id, hold.record)
        node = self.tree.get(node_id)
        attempt = node.handover_attempt if node else None
        fields = {"cleanup_hold": dict(hold.record)}
        if attempt and attempt.get("state") == "launching":
            # Write the actual destination execution identity BEFORE spawn;
            # recovery cannot infer it from the predecessor or current config.
            fields["handover_attempt"] = dict(attempt, target_exec_identity=hold.record["executor"])
        self.tree.update(node_id, **fields)
        hold.durable = True
        self._holds[node_id] = hold
        return hold

    def _record_launched(self, node_id: str, hold: _Hold, handle: Any) -> None:
        """RM-R1d: the NEW process identity, on the node and on its hold, in
        one transaction — before any other step that can fail, so admission
        never judges the node by the retry's dead predecessor. The in-memory
        hold learns it first, so a failed write still leaves the owner able
        to confirm this process's death."""
        hold.pid = handle.pid
        hold.pid_start = (getattr(handle, "pid_start", "")
                          or (procs.start_time(handle.pid) if handle.pid else "")
                          or "")
        hold.record = dict(hold.record, pid=hold.pid, pid_start=hold.pid_start)
        self._record_node_launch_evidence(node_id, hold.record,
                                          turn_started_at=getattr(handle, "launched_at", 0) or None)
        with self.tree.transaction() as data:
            entry = data["nodes"].get(node_id)
            if entry is not None:
                entry["pid"] = hold.pid
                entry["pid_start"] = hold.pid_start
                entry["cleanup_hold"] = dict(hold.record)
                entry["exec_identity"] = dict(hold.record.get("executor") or {})

    def _retire_hold(self, node_id: str) -> None:
        """The reservation's end when nothing is left to release: supervision
        is established (the node now occupies as a live `running` one), or
        nothing was started. If the tree cannot be written, the hold stays
        and `_settle_holds` lifts it later — counted a little longer, never
        released early."""
        hold = self._holds.get(node_id)
        if hold is None:
            return
        hold.phase = "lifting"
        try:
            self._lift_hold(node_id, hold.then, record=hold.record)
        except Exception:
            return
        self._holds.pop(node_id, None)

    def _launch_cleanup_task(self, handle: Any, node_id: str, *,
                             run: Any = None, done: Any = None,
                             oom_container: str = "") -> asyncio.Task:
        """RM-R1c: THE failed-launch cleanup, as one task.

        It stops the launched process, CONFIRMS that the actual run has
        ended — `handle.wait()` alone is not proof, since it may return on
        an exit-status file, and `stop()` has only bounded settling — then
        releases the supervision lock, the container occupancy, the startup
        claim and the concurrency slot, and only then removes the tracking
        and signals `run.done` (`_end_hold`). Nothing is released merely
        because stop returned, raised or timed out: if death cannot be
        confirmed within the bound, the failure is reported and the hold
        stays up; `_settle_holds` re-checks it and ends it once death is
        confirmed, here or — after this server is gone — in any Runner.

        The launch's durable hold already exists (`_reserve_launch`), so
        nothing here has to be written before the stop: termination is
        always attempted (review ag-f27608).
        """
        hold = self._holds[node_id]
        hold.phase = "cleanup"
        hold.run, hold.done, hold.container = run, done, oom_container
        # Best effort, never before the stop's chance: the pid (if its own
        # write failed) and the occupancy reach the durable hold now, or on
        # a later `_settle_holds` pass.
        hold.record = dict(hold.record, occupancy=oom_container)
        self._persist_hold(node_id, hold)

        async def cleanup() -> bool:
            try:
                await handle.stop()
            except BaseException:
                # RM-R1c: stop returning, raising or timing out decides
                # nothing — not even a cancellation raised from inside it.
                # Death is judged by the confirmation below.
                pass
            if await self._confirm_ended(hold.pid, hold.pid_start,
                                         hold.probe_raw):
                self._end_hold(node_id)
                return True
            # RM-R1c: termination failed. Ownership and occupancy are kept
            # until death is confirmed; report and hold.
            with contextlib.suppress(Exception):
                self.tree.emit(node_id, "launch_cleanup_failed",
                               detail="the launched process did not confirm "
                                      "its end after stopping; ownership and "
                                      "occupancy are held until it does")
            return False

        hold.task = asyncio.ensure_future(cleanup())
        return hold.task

    def _persist_hold(self, node_id: str, hold: _Hold) -> None:
        """Rewrite the durable hold with what changed (its deferred status,
        its occupancy), best effort — retried by `_settle_holds`. The hold
        itself is durable already: this only keeps it current."""
        try:
            if hold.steer is not None and "inspect" in hold.steer["steps"]:
                # A failed get must not overwrite an unreadable predecessor
                # hold. If transactional storage still works, mirror our
                # obligations alongside that hold without deciding liveness.
                with self.tree.transaction() as data:
                    raw = data["nodes"].get(node_id)
                    if raw is None:
                        return
                    current = raw.get("cleanup_hold")
                    if current and not _same_hold(current, hold.record):
                        hold.record = dict(current)
                        hold.steer["foreign"] = True
                        hold.steer["steps"] = [s for s in hold.steer["steps"]
                                               if s != "confirm"]
                    hold.record = dict(hold.record, steer_cleanup=dict(hold.steer))
                    raw["cleanup_hold"] = dict(hold.record)
            else:
                hold.record = dict(
                    hold.record, then=list(hold.then) if hold.then else None,
                    **({"steer_cleanup": dict(hold.steer)} if hold.steer else {}))
                self.tree.update(node_id, cleanup_hold=dict(hold.record))
            hold.durable = True
        except Exception:
            hold.durable = False

    def _end_hold(self, node_id: str) -> None:
        """RM-R1c, steps 3 and 4: death is confirmed. Release the supervision
        lock, the container occupancy and the startup claim, apply the
        status a caller deferred, lift the durable hold — the slot — and
        only then drop the tracking and wake the waiters. The same sequence
        whether this Runner launched the run or took the hold over from an
        owner that crashed (review ag-43f57f).

        Each release is confirmed before the next step, and the hold — the
        slot — is lifted only once all of them are (review ag-598c45): a
        startup claim whose release write failed would otherwise keep a
        half-open provider `startup_down` with nothing left to retry it. Any
        release or the lift that does not go through leaves the entry,
        marked `confirmed`, and `_settle_holds` retries: the slot is held a
        little longer, never released early.
        """
        hold = self._holds.get(node_id)
        if hold is None:
            return
        hold.confirmed = True
        if hold.steer is not None:
            self._settle_steer_cleanup(node_id, hold)
            if hold.steer is not None or self._holds.get(node_id) is not hold:
                return
        if "lock" not in hold.done_releases:
            self._release(node_id)
            hold.done_releases.add("lock")
        if "occupancy" not in hold.done_releases:
            if hold.container:
                try:
                    self.occupancy.forget(hold.container, node_id)
                    self.occupancy.ensure_ended(hold.container, node_id)
                except Exception:
                    return                # not durably ended: retried later
            hold.done_releases.add("occupancy")
        if "claim" not in hold.done_releases:
            if hold.token:
                with contextlib.suppress(Exception):
                    self._startup_finish(hold.provider, node_id, hold.token)
                if self.startup.holds(hold.provider, node_id, hold.token) is not False:
                    return                # not confirmed gone: retried later
            hold.done_releases.add("claim")
        try:
            self._lift_hold(node_id, hold.then, record=hold.record)
        except Exception:
            return
        del self._holds[node_id]
        if hold.run is not None and self.runs.get(node_id) is hold.run:
            self.runs.pop(node_id, None)
        done = hold.run.done if hold.run is not None else hold.done
        if done is not None:
            done.set()
        self._pc_kick(hold.provider)          # the hold was a slot (finding 8)

    def _lift_hold(self, node_id: str, then: Any, record: dict | None = None
                   ) -> None:
        """Apply a deferred status, then clear the durable hold — only the
        hold that was confirmed (`record`), never a newer one. The status
        goes first, so the node is never seen with neither its hold nor its
        final status. A status the node left meanwhile (`stop()` marking it
        `cancelled`) is not overwritten."""
        if then:
            status, reason = then
            current = self.tree.get(node_id)
            if current is not None and current.status in ACTIVE:
                self.tree.set_status(node_id, status, reason)
        with self.tree.transaction() as data:
            entry = data["nodes"].get(node_id)
            if entry is None or not entry.get("cleanup_hold"):
                return
            if record is not None and not _same_hold(entry["cleanup_hold"], record):
                return
            held = entry["cleanup_hold"]
            cleanup = held.get("steer_cleanup")
            if (isinstance(cleanup, dict) and cleanup.get("steps")
                    and cleanup.get("owner") != held.get("owner")):
                # A different steer still owes releases. The predecessor
                # owner is finished; transfer the mirror instead of erasing
                # that steer's only durable retry owner.
                cleanup = dict(cleanup, foreign=False, launch_owned=False)
                entry["cleanup_hold"] = dict(
                    held, **{k: cleanup[k] for k in
                             ("owner", "owner_pid", "owner_start")},
                    occupancy="", then=None, steer_cleanup=cleanup)
                return
            entry["cleanup_hold"] = None

    def _defer_while_held(self, node_id: str, status: str, reason: str) -> bool:
        """While a launch hold is up, a caller's status change is recorded on
        the hold and applied when it ends (RM-R1c): the node's present status
        stays its durable slot reservation. True when deferred."""
        hold = self._holds.get(node_id)
        if hold is not None:
            hold.then = (status, reason)
            self._persist_hold(node_id, hold)
            return True
        node = self.tree.get(node_id)
        if node is None or not node.cleanup_hold:
            return False
        with contextlib.suppress(Exception):
            with self.tree.transaction() as data:
                entry = data["nodes"].get(node_id) or {}
                if isinstance(entry.get("cleanup_hold"), dict):
                    entry["cleanup_hold"]["then"] = [status, reason]
        return True

    def _settle_holds(self) -> None:
        """RM-R1c/R1d: the recovery path of a launch hold (review ag-f27608).
        Run before every admission and capacity count, and on every adoption
        pass (server startup and its periodic pass).

        - A hold another owner left, once that owner is gone (its pid
          identity dead, as startup.json and the occupancy records judge a
          crashed owner): taken over (`_take_over_hold`) — this Runner then
          owns every release the dead owner would have run.
        - This Runner's own holds, once their cleanup task has returned:
          death is re-checked from the same positive evidence the cleanup
          uses, against the execution identity the hold recorded; on
          confirmation the hold ends with every release it owns. A hold
          whose durable half could not be lifted or rewritten is retried.

        A live owner's hold is that owner's to end.
        """
        self._retry_startup_releases()
        try:
            nodes = self.tree.read()["nodes"]
        except Exception:
            nodes = {}
        for node_id, raw in nodes.items():
            held = raw.get("cleanup_hold") if isinstance(raw, dict) else None
            if not isinstance(held, dict) or node_id in self._holds:
                continue
            if held.get("owner") != self._hold_owner and procs.alive(
                    held.get("owner_pid"), held.get("owner_start") or ""):
                cleanup = held.get("steer_cleanup")
                if (isinstance(cleanup, dict) and cleanup.get("foreign")
                        and not procs.alive(cleanup.get("owner_pid"),
                                            cleanup.get("owner_start") or "")):
                    # The predecessor's owner still lives, but the refused
                    # steer's owner died. Recover its independent releases
                    # without taking the predecessor's supervision lock.
                    with contextlib.suppress(Exception):
                        self._take_over_steer_cleanup(node_id, held, cleanup)
                continue                  # a live owner ends its own
            with contextlib.suppress(Exception):
                self._take_over_hold(node_id, raw, held)
        for node_id, hold in list(self._holds.items()):
            if hold.steer is not None:
                self._settle_steer_cleanup(node_id, hold)
                continue
            if hold.phase == "launching":
                continue                  # its launch is still deciding
            if hold.phase == "lifting":
                self._retire_hold(node_id)
                continue
            if hold.task is not None and not hold.task.done():
                continue                  # its own cleanup is still deciding
            if not hold.durable:
                self._persist_hold(node_id, hold)
            if hold.confirmed or _positively_ended(hold.pid, hold.pid_start,
                                                  hold.probe_raw):
                self._end_hold(node_id)

    def _take_over_steer_cleanup(self, node_id: str, held: dict,
                                 cleanup: dict) -> None:
        taken = dict(cleanup, **self._owner_fields(), launch_owned=False)
        with self.tree.transaction() as data:
            raw = data["nodes"].get(node_id) or {}
            current = raw.get("cleanup_hold")
            if (not _same_hold(current, held)
                    or current.get("steer_cleanup") != cleanup):
                return
            current["steer_cleanup"] = taken
        self._holds[node_id] = _Hold(
            record=dict(held, steer_cleanup=taken), pid=None, pid_start="",
            probe_raw=None, provider=taken["provider"], token="",
            phase="cleanup", durable=True, steer=taken)
        self._recover_steer_predecessor(node_id, self._holds[node_id])

    def _recover_steer_predecessor(self, node_id: str, hold: _Hold) -> None:
        """SF-R3: recover what an adopted confirm step needs, once. When no
        process or probe identity can be recovered, record a terminal held
        decision; absence of identity is never evidence of death."""
        cleanup = hold.steer
        if cleanup.get("blocked_reason"):
            instruction = f"run stop_agent {node_id} to release this hold"
            if instruction not in cleanup["blocked_reason"]:
                cleanup["blocked_reason"] += f"; {instruction}"
                hold.durable = False
            return
        if "confirm" not in cleanup["steps"] or cleanup.get("absent"):
            return
        saved = cleanup.get("predecessor") or hold.record
        pid, start = saved.get("pid"), saved.get("pid_start") or ""
        identity = saved.get("executor")
        try:
            probe = self._identity_probe(identity, node_id)
            if not pid and isinstance(identity, dict) and identity.get("kind") == "local":
                pid = _recorded_wrapper(self.paths.run_dir(node_id))
            if not pid or not start or probe is _unknown_probe:
                node = self.tree.get(node_id)
                if node is not None and (not pid or (
                        node.pid == pid and (not start or node.pid_start == start))):
                    pid, start = pid or node.pid, start or node.pid_start
                    if (not isinstance(identity, dict)
                            or identity.get("kind") not in {"local", "docker"}):
                        # Historical launch identity, never today's executor
                        # config: a dead docker client is not a dead agent.
                        identity = node.exec_identity
                    probe = self._identity_probe(identity, node_id)
            if (pid and start and probe is not _unknown_probe) or probe not in (None, _unknown_probe):
                if not start:
                    # A container probe can identify the run independently;
                    # a bare host pid cannot identify its historical client.
                    pid = None
                hold.pid, hold.pid_start, hold.probe_raw = pid, start, probe
                cleanup["captured"] = True
                cleanup["predecessor"] = {"pid": pid, "pid_start": start,
                                           "executor": identity}
                return
        except Exception:
            pass
        self._block_steer_predecessor(node_id, hold)

    def _block_steer_predecessor(self, node_id: str, hold: _Hold) -> None:
        hold.steer["blocked_reason"] = (
            "predecessor liveness is unknown: no process or probe identity "
            "with a recorded start time could be recovered; the node remains "
            f"held and refuses steers; run stop_agent {node_id} to release this hold")
        hold.durable = False

    def _take_over_hold(self, node_id: str, raw: dict, held: dict) -> None:
        """RM-R1d (review ag-43f57f): a crashed owner's hold becomes this
        Runner's own, so its end runs the owner's release sequence — the
        supervision flock, the startup claim, the container occupancy, the
        slot — and nothing is released before death is confirmed.

        The flock first (a live server that holds it is never robbed); then,
        in one transaction, the exact hold is re-validated and rewritten to
        name this Runner. Any failure on the way leaves the hold as it was,
        the flock given back — including a startup claim or an occupancy
        record whose durable read or write cannot be confirmed (RM-R1e).
        The run is not stopped again: its process is
        judged by positive evidence alone, against the execution identity
        the hold recorded, so the hold stays for as long as that cannot be
        had.
        """
        if not self._claim(node_id):
            return
        provider = str(raw.get("provider") or "")
        try:
            # RM-R1e: a claim read that failed is unknown, never "no claim".
            token = (self.startup.token_for(provider, node_id, strict=True)
                     if provider and (not held.get("steer_cleanup")
                                      or held["steer_cleanup"].get("foreign")) else "")
        except Exception:
            self._release(node_id)
            return
        container = str(held.get("occupancy") or "")
        if container:
            # The occupancy record speaks for the held run until its death
            # is confirmed: rebound to this Runner first, or reconciliation
            # prunes it with its dead owner while the run may live, and a
            # sibling's OOM kill is attributed as if it were alone (review
            # ag-598c45). Failing that, nothing is taken over this pass.
            try:
                pid = held.get("pid") or 0
                pid_start = str(held.get("pid_start") or "")
                self.occupancy.rebind(container, node_id, pid, pid_start)
                self.occupancy.ensure_live(container, node_id, pid, pid_start)
            except Exception:
                self._release(node_id)
                return
        taken = dict(held, **self._owner_fields())
        if taken.get("steer_cleanup"):
            taken["steer_cleanup"] = dict(taken["steer_cleanup"], **self._owner_fields())
            # Taking over acquired a NEW flock, even if the old owner had
            # released its own and was only retrying the durable lift.
            cleanup = taken["steer_cleanup"]
            cleanup["steps"] = sorted(set(cleanup["steps"]) | {"lock"})
        try:
            with self.tree.transaction() as data:
                entry = data["nodes"].get(node_id)
                if entry is None or not _same_hold(entry.get("cleanup_hold"), held):
                    taken = None
                else:
                    entry["cleanup_hold"] = taken
        except Exception:
            taken = None
        if taken is None:
            self._release(node_id)
            return
        pid = taken.get("pid")
        pid_start = str(taken.get("pid_start") or "")
        identity = taken.get("executor")
        if not pid and isinstance(identity, dict) and identity.get("kind") == "local":
            # The owner died between the start and the pid's write: the
            # wrapper's own record, removed before every launch, is this
            # run's or nothing.
            recorded = _recorded_wrapper(self.paths.run_dir(node_id))
            pid, pid_start = (recorded, "") if recorded else (None, "")
        then = taken.get("then")
        self._holds[node_id] = _Hold(
            record=taken, pid=pid, pid_start=pid_start,
            probe_raw=self._identity_probe(identity, node_id),
            provider=provider,
            token=token,
            phase="cleanup", container=str(taken.get("occupancy") or ""),
            confirmed=bool(taken.get("operator_release")),
            durable=True, steer=dict(taken["steer_cleanup"])
            if taken.get("steer_cleanup") else None,
            then=tuple(then) if then else (
                "failed", "its launch failed and its server exited before the "
                          "process was confirmed dead"))
        if taken.get("steer_cleanup"):
            cleanup = self._holds[node_id].steer
            if cleanup["foreign"]:
                cleanup["launch_owned"] = True
            elif not then:
                self._holds[node_id].then = None
            self._recover_steer_predecessor(node_id, self._holds[node_id])
        self.tree.emit(node_id, "launch_hold_taken_over",
                       previous_owner=held.get("owner_pid"))

    def _identity_probe(self, identity: Any, node_id: str) -> Any:
        """The raw liveness probe for the execution identity a hold recorded
        — never the executor the current config names (review ag-43f57f).
        A local run needs none; a container run asks THAT container; a hold
        with no readable identity can never be confirmed dead."""
        if not isinstance(identity, dict):
            return _unknown_probe
        kind = identity.get("kind")
        if kind == "local":
            return None
        if kind == "docker" and identity.get("container"):
            executor = self._docker_for(str(identity["container"]))
            if executor is None:
                return _unknown_probe
            return self._raw_alive_probe(executor, node_id) or _unknown_probe
        return _unknown_probe

    def _docker_for(self, container: str) -> Any:
        """The docker executor for a RECORDED container, or None."""
        docker = dict(self.config.project.get("executor", {}).get("docker", {}))
        docker["container_name"] = container
        try:
            return get_executor("docker", docker, paths=self.paths,
                                providers=self.providers,
                                config_dir=global_config_dir())
        except Exception:
            return None

    def _raw_alive_probe(self, executor: Any, node_id: str) -> Any:
        """The executor's own three-valued liveness answer, or None when it
        has none — a local run's pid identity is the whole story.

        RM-R1c (review ag-f7ced5): death is confirmed from THIS raw answer,
        never from the FollowHandle's `_alive`, whose container probe counts
        an unanswerable container as alive for `UNKNOWN_ALIVE_SECONDS` and
        then reports dead — an expired Docker liveness grace period is
        "unknown", and unknown is never death.
        Docker's explicit verdict also distinguishes a transport exit 1
        from an answer that the container-side session has ended.
        """
        verdict = getattr(executor, "wrapper_verdict", None)
        if verdict is None:
            verdict = getattr(executor, "wrapper_alive", None)
        if verdict is None:
            return None
        return lambda: verdict(node_id)

    async def _confirm_ended(self, pid: int | None, pid_start: str,
                             probe_raw: Any = None, bound: float = None) -> bool:
        """Whether the launched run has POSITIVELY ended: the pid identity
        gone (a local run — immune to pid reuse), or the executor's raw
        container probe definitely saying the wrapper is dead. "Unknown" is
        never death: a Docker probe past its liveness grace reports None,
        and treating that as death released ownership over a process that
        may well be alive (review ag-f7ced5). Blocking, so it runs off the
        loop. The bound only stops a process that refuses to die from
        hanging the caller for ever: past it the cleanup reports failure
        and the hold stays up (RM-R1c)."""
        if bound is None:
            bound = LAUNCH_CONFIRM_SECONDS
        deadline = time.monotonic() + bound
        while True:
            if await asyncio.to_thread(_positively_ended, pid, pid_start,
                                       probe_raw):
                return True
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(0.2)

    async def _await_cleanup(self, task: asyncio.Task) -> None:
        """RM-R1c: await THE one cleanup task through any number of our own
        cancellations — there is no retry limit and no replacement task, and
        nothing is detached. A cancellation of the task itself is not ours,
        and is not looped on. The task's outcome is retrieved here so nothing
        goes unobserved; the launch's original failure is the one that
        propagates to the caller."""
        while True:
            try:
                await asyncio.shield(task)
                break
            except asyncio.CancelledError:
                if task.cancelled():
                    break
                continue
        if not task.cancelled():
            task.exception()

    def _check_node_launch(self, node_id, *, window_resume=False):
        context = _launch_context.get()
        run = self.tree.get(node_id)
        managed = context.node_id if context else run.node_id if run else ""
        if not managed:
            return
        from .scheduler import enabled
        if not enabled(self.paths.root):
            raise RuntimeError("scheduler_disabled: node runs require scheduler admission")
        from .scheduler.engine import attempts
        from .scheduler.store import Store
        store = Store(self.paths.root)
        with store.transaction(write=False) as db:
            node = store.nodes(db)[managed]
            attempt = next((a for a in attempts(db).values() if a["run_id"] == node_id), None)
            if not attempt or attempt["state"] not in {"claimed", "launched"} or attempt.get("cancel_requested"):
                raise RuntimeError(f"node_state: {managed} has no live launch permission; nothing launched")
            expected = ("suspended" if window_resume and attempt.get("window_resume")
                        else "open" if attempt["state"] == "claimed" else "running")
            if node["state"] != expected:
                raise RuntimeError(f"node_state: {managed} is {node['state']}; nothing launched")
            nodes = store.nodes(db)
            clock_file = store.meta(db, "clock_file")
        from .scheduler import suspension, windows
        zone = self.config.project["scheduler"].get("timezone", "Europe/Paris")
        windows.prepare(zone, nodes)
        window = windows.effective(node, nodes, zone, suspension.instant(clock_file))
        if not window["open"]:
            raise RuntimeError("node_suspended: the window closed before launch")

    def _record_node_launch_evidence(self, node_id, record, *, turn_started_at=None):
        context = _launch_context.get()
        run = self.tree.get(node_id) if context is None else None
        managed = context.node_id if context else run.node_id if run else ""
        attempt_id = context.attempt_id if context else run.attempt_id if run else ""
        if not managed or not attempt_id:
            return
        from .scheduler.engine import attempts, save_attempt
        from .scheduler.store import Store
        store = Store(self.paths.root)
        with store.transaction() as db:
            attempt = attempts(db)[attempt_id]
            attempt["launch_evidence"] = {k: record[k] for k in ("pid", "pid_start", "executor")}
            if turn_started_at is not None:
                attempt["turn_started_at"] = turn_started_at
            save_attempt(db, attempt)

    async def _launch(
        self,
        *,
        node_id: str,
        spec: AgentSpec,
        provider: Provider,
        prompt: str,
        workdir: Path,
        branch: str,
        parent: str | None,
        depth: int,
        session_id: str | None = None,
        timeout: int | None = None,
        done: asyncio.Event | None = None,
        startup_token: str = "",
        release_lock: bool = True,
        preserved_limits: dict | None = None,
        handover_attempt: int | None = None,
        window_resume: bool = False,
    ) -> Run:
        """Build the environment and command for one turn and start the process.

        Shared by every path that runs an agent — a fresh task, a steer, and a
        turn of a standing conversation — so identity injection and credential
        handling cannot drift between them.

        `done` lets a caller hand this launch an event a waiter already holds
        (the free retry in `_finalize`) so the new Run is born already sharing
        it — `self.runs[node_id]` is replaced with this Run before `_launch`
        returns, so building it with the right event from the start closes the
        window a post-hoc `retried.done = run.done` would leave open: anyone
        reading `self.runs[node_id]` during that window would otherwise get a
        fresh event nobody will ever set.
        """
        if self.scheduler_enabled() and (
                _launch_context.get() is None or self.launch_limits.launch_time(node_id) is not None):
            from .scheduler import resume_admission, window_resume_admission
            check = window_resume_admission if window_resume else resume_admission
            admission = await asyncio.to_thread(check, self.paths.root,
                                               node_id, None if _launch_context.get() else self.self_id())
            if admission.get("error") or admission.get("blocked"):
                self._startup_release(provider.name, node_id, startup_token)
                raise RuntimeError(encode_admission(admission))
        try:
            self._check_node_launch(node_id, window_resume=True) if window_resume else self._check_node_launch(node_id)
        except BaseException:
            self._startup_release(provider.name, node_id, startup_token)
            raise
        transport_error = self._transport_refusal(provider)
        if transport_error:
            self._startup_release(provider.name, node_id, startup_token)
            raise TransportRefused(transport_error)
        # PS-R6: no launch — first, free retry, commit-fix turn, steer's
        # respawn, a conversation's next turn — runs a model the destination
        # provider does not allow. Admission refuses earlier and more kindly;
        # this is the one place every path passes through, so it is the one
        # that cannot be bypassed by a spec rebuilt the wrong way. Refused
        # before anything is taken, like the hold check below.
        refusal = self._model_refusal(provider.name, spec.model or "")
        if refusal:
            # SF-R3a: no launch, so the caller's startup claim goes back
            # neutrally, on every path that refuses before the spawn.
            self._startup_release(provider.name, node_id, startup_token)
            raise RuntimeError(refusal)
        # SC-R3b: and none spawns a metered CLI under a cap that binds — the
        # free retry, a wrap-up or commit-fix turn, a fallback, a deferred
        # restart — read from the ledger as it stands now, so a launch
        # admitted just before another run's crossing is still refused.
        if self.__dict__.get("_pc_shutting_down"):
            # Round 7: once shutdown has begun nothing launches — start,
            # steer, consult, free retry, queued retry or commit-fix turn —
            # since shutdown has already captured the runs it ends.
            self._startup_release(provider.name, node_id, startup_token)
            raise RuntimeError(SHUT_TEXT)
        if self._held(node_id):
            # RM-R1d: a second process on a node whose previous launch is
            # not confirmed dead. Refused before anything is taken, so the
            # hold keeps everything it owns — only the caller's own startup
            # claim goes back (SF-R3a).
            self._startup_release(provider.name, node_id, startup_token)
            raise RuntimeError(_held_refusal(node_id))
        # SR-R1/R2/R2b: all relaunches drain the previous wrapper AND its
        # session before touching their shared files. This includes consult
        # and the free retry, whose stream can end before the wrapper exits.
        previous = self.tree.get(node_id)
        if (self.runs.get(node_id) is not None
                or (previous and (previous.pid or previous.turn_started_at))):
            predecessor = _Predecessor()
            try:
                predecessor = self._steer_predecessor(node_id)
                if not await self._steer_predecessor_dead(predecessor):
                    reason = "the predecessor's wrapper or agent is not confirmed dead"
                    raise RuntimeError(f"refusing to relaunch {node_id}: {reason}")
            except BaseException:
                if release_lock:
                    await self._steer_release(
                        node_id, provider.name, startup_token, None, None, "",
                        False, predecessor)
                raise
        try:
            home = None
            if self.config.home_policy == "per-agent":
                # PS-R2: a provider that borrows its credentials gets the
                # owner's credential files in its private HOME too — the
                # owner's links and copies first, then its own.
                owner = getattr(provider, "auth_owner", None)
                links = list(dict.fromkeys(
                    [*(owner.home_links if owner else []), *provider.home_links]))
                copies = list(dict.fromkeys(
                    [*(owner.home_copy if owner else []), *provider.home_copy]))
                home_id = node_id
                context = _launch_context.get()
                if context and context.node_id:
                    from .scheduler.store import Store
                    from .scheduler.engine import attempts
                    with Store(self.paths.root).transaction(write=False) as db:
                        home_id = attempts(db)[context.attempt_id].get("alias_home", node_id)
                home = prepare_home(self.paths.home(home_id), links,
                                    "per-agent", agent=spec.name, copies=copies)
            identity = {
                "MULTIAGENTS_AGENT_ID": node_id,
                "MULTIAGENTS_PARENT_ID": parent or "",
                "MULTIAGENTS_DEPTH": str(depth),
                "MULTIAGENTS_BRANCH": branch,
                "MULTIAGENTS_CAN_SPAWN": "1" if spec.can_spawn else "0",
                "MULTIAGENTS_ROOT": str(self.paths.data),
                "MULTIAGENTS_PROJECT": str(self.paths.root),
            }
            env = build_env(
                passthrough=self.config.env_passthrough,
                blocked=self.config.env_block,
                home=home,
                identity=identity,
            )
            # SM-R2: the variables a provider is handed the server through are
            # not inherited. Passed through, one would give an agent without spawn
            # rights whatever server it names; a spawner gets ours below.
            for key in (provider.mcp or {}).get("env") or {}:
                env.pop(str(key), None)
            # The provider instance's own environment — the thing that makes a
            # second subscription a second account rather than the same one twice.
            # After build_env, because build_env starts from a clean slate and this
            # is not passthrough: it is configuration, not inheritance.
            # PS-R2: a provider that borrows its credentials launches with its
            # own env with the OWNER's laid over it (`credential_env`,
            # attached at config load); without it, its own env alone.
            for key, value in (getattr(provider, "credential_env", None)
                               or provider.env or {}).items():
                env[key] = expand_env_value(value)
            # The account profile, resolved by the same helper the budget
            # reader uses, so the CLI and the reader can never be pointed at
            # different directories — including a relative value, which the
            # helper makes absolute rather than leaving to each cwd.
            profile_field = getattr(provider, "budget_profile_env", "") or ""
            profile = resolved_profile(provider)
            if profile_field and profile:
                env[profile_field] = profile
            # Identity last: it is what the server's gates trust (SM-R3), so no
            # configuration may restate it.
            env.update(identity)
            executor = self.executor(spec)

            # LN-C2, finding 8: the provenance captured with the values at THIS
            # launch — the file and line as they are now, not as they will be when
            # a trip fires.
            limits = self._limits_detail(spec.name, self._limits_for(node_id, spec,
                                                                     timeout), provider.name)
            limits["max_steps"] = self._max_steps_limit(spec, provider.name)
            if preserved_limits:
                # QH-R8: a switch carries every original limit and its
                # provenance, rather than resolving against a new route.
                limits = copy.deepcopy(preserved_limits)
            # LN-C2, adversary findings 3/8: written where the container cannot
            # reach, so a later adoption or relaunch reads what THIS launch ran
            # under, not what the node's forgeable record in `tree.json` claims.
            wall = limits["timeout"]["value"]
            # FO-R1: every scalar option reaches the command line, floats
            # included (`max_budget_usd: 0.5` is a real budget, and the old
            # `isinstance(v, (str, int))` filter dropped it silently). Bools
            # are scalars too and `build_command` renders them deliberately.
            # A dict or list is not an option value: it is reported, never
            # dropped without a trace.
            options = {"effort": spec.effort}
            for key, value in spec.extra.items():
                if value is None or value == "":
                    continue
                if isinstance(value, (str, int, float)):
                    options[key] = value
                else:
                    self.tree.emit(node_id, "option_not_renderable",
                                   provider=provider.name, option=str(key),
                                   value_type=type(value).__name__)
            # PF-R1/R3/R5/R7: allocate input by launch identity before argv
            # exists. Retain every attempt, including a failed launch; never
            # infer identity from the run directory's agent-writable listing.
            run_dir = self.paths.run_dir(node_id)
            prompt_limit = int(config_mod.limit_number(
                self.config.limits, "prompt_file_max_bytes"))
            if len(prompt.encode("utf-8")) > prompt_limit:
                raise ValueError(f"prompt exceeds prompt_file_max_bytes ({prompt_limit} bytes)")
            prompt_identity = f"{now():.9f}-{os.urandom(8).hex()}"
            prompt_name = f"prompt.{prompt_identity}.md"
            _run_write(run_dir, prompt_name, prompt, mode=0o600)
            transport = provider.prompt_transport
            if transport != "argv":
                # Consumer safety is checked again when the wrapper/adapter
                # opens the input; this catches errors before spawning too.
                from .agentwrap import read_prompt
                read_prompt(str(run_dir / prompt_name), prompt_limit, str(run_dir))
            if not (run_dir / "prompt.md").exists():
                _run_write(run_dir, "prompt.md", prompt, mode=0o600)
            argv = provider.build_command(
                prompt=prompt, prompt_file=str(run_dir / prompt_name),
                model=spec.model, workdir=str(workdir),
                permission=spec.permission, session_id=session_id, options=options,
                timeout=int(wall),
            )
            if provider.adapter:
                # CX-C1: the adapter runs at its absolute path. The executor is
                # told whose run it is by name (CX-C16), not by this path.
                adapter = scripts.resolve_adapter(provider, global_config_dir(),
                                                  self.paths.config)
                if adapter is None:
                    raise RuntimeError(
                        f"provider {provider.name!r} names adapter "
                        f"{provider.adapter!r}, which is in none of the provider "
                        f"script directories (project, global, shipped)")
                argv[0] = str(adapter)

            managed = self.tree.get(node_id)
            if managed and managed.node_id:
                from .scheduler import issue_run_capability, transport_directory
                permissions = {"read"} | ({"delegate"} if spec.can_spawn else set())
                from .scheduler.store import Store
                plan = Store(self.paths.root)
                with plan.transaction(write=False) as db:
                    records = plan.nodes(db)
                own = records[managed.node_id]
                composite = records.get(own["parent"])
                if composite and composite["kind"] == "loop" and composite["loop"]["verdict_child"] == managed.node_id:
                    permissions.add("verdict")
                env["MULTIAGENTS_RPC_TOKEN"] = issue_run_capability(
                    self.paths.root, node_id, managed.node_id, permissions)
                env["MULTIAGENTS_RPC_SOCKET"] = str(transport_directory(self.paths.root) / "rpc.sock")
                env["MULTIAGENTS_NODE_PERMISSIONS"] = ",".join(sorted(permissions))
            # Managed runs always have their scoped node server (NC-R58).
            if spec.can_spawn or managed and managed.node_id:
                server_argv, server_env = self._hand_server(node_id, provider, env, home, run_dir)
                argv += server_argv
                env.update(server_env)
            else:
                self._withdraw_server(provider, home)
            # CW-R2b: the safe-point key, after the last overlay — passthrough,
            # the provider's env, its `mcp.env` — so none can hand it to an agent.
            strip_key(env)
            # H8: refused before anything starts — the kernel's per-argument
            # limit (checked over the fully assembled argv, adapter and server
            # arguments included).
            providers_mod.check_argv_limit(provider.name, argv)
            env.update({
                "MULTIAGENTS_PROMPT_FILE": str(run_dir / prompt_name),
                "MULTIAGENTS_PROMPT_RUN_DIR": str(run_dir),
                "MULTIAGENTS_PROMPT_TRANSPORT": transport,
                "MULTIAGENTS_PROMPT_MAX_BYTES": str(prompt_limit),
                "MULTIAGENTS_PROMPT_STDIN_FORMAT": str(provider.spawn.get("stdin_format", "text")),
            })
            # Environment KEYS only — values may be secret and this file is on disk.
            # Diagnostic info only: adoption's limits and launch clock come
            # exclusively from the protected host records, never this file.
            command_record = {
                "argv": argv, "cwd": str(workdir), "env_keys": sorted(env),
                "provider": provider.name, "model": spec.model,
                "permission": spec.permission, "resumed": bool(session_id),
                "prompt_file": prompt_name, "launch_identity": prompt_identity,
            }

            problems = executor.preflight()
            if problems:
                raise RuntimeError("; ".join(problems))
            # RM-R1d (review ag-7c0716): everything that can fail comes
            # BEFORE the flock. Supervisor construction parses the limits
            # and can raise on a malformed one; building it here means a
            # failure never has to walk back a held `_claim`.
            supervisor = self._supervisor(spec, provider, wall,
                                          limits["silence_timeout"]["value"],
                                          limits["max_steps"]["value"])
            # SV-R5: owned before it exists, so no other server's adoption pass
            # can find it running and unowned in between.
            if not self._claim(node_id):
                raise RuntimeError(f"{node_id} is supervised by another server")
        except BaseException:
            # Nothing was started, so there is nothing to stop or confirm:
            # everything this turn owns goes back here, on every path. The
            # startup claim — steer takes its own before calling in (review
            # ag-cef33c); token-guarded, so a caller's own release is a
            # no-op. And the supervision flock, which a relaunch still holds
            # from the turn it replaced (steer's internal stop keeps it for
            # the relaunch): with no process started, nothing is supervised
            # any more, and a lock kept would make the node unadoptable for
            # this server's whole lifetime (review ag-f27608). The caller's
            # own handler settles the node's status. SF-R3a: the startup
            # claim goes back neutrally — nothing launched, nothing learnt.
            # SF-R3 (review r2 finding 1): a steer passes `release_lock=False`
            # because it may still have a live predecessor under that lock;
            # `_steer_release` owns the decision then.
            if release_lock:
                self._release(node_id)
                self._startup_release(provider.name, node_id, startup_token)
            raise

        # SV-R1/R4: under the launch wrapper, which writes the output and the
        # exit status to the run dir and ends the run at its wall clock even
        # when no server is left to.
        handle = hold = None
        try:
            startup_token = startup_token or self.startup.claim(provider.name, node_id)
            # RM-R1d: the durable reservation exists before the process does;
            # a reservation that cannot be written means no launch.
            hold = self._reserve_launch(node_id, provider.name, startup_token,
                                        executor)
            # SC-R3b: no launch — first, free retry, commit-fix or wrap-up
            # turn, steer's respawn, a fallback, a deferred restart — spawns a
            # metered CLI under a cap that binds, read from the ledger right
            # before the spawn (#2). Inside this block, so a refusal gives
            # back the hold, the supervision lock and the startup claim (#6).
            launched_at = now()
            capped = self._cap_refusal(provider.name, spec.model or "")
            if capped:
                raise SpendCapRefused(capped)
            # SR-R3/R4: admission and cleanup do not spend the turn's clock.
            launched_at = now()
            supervisor.started = time.monotonic()
            supervisor.last_event = supervisor.started
            self.launch_limits.record(node_id, limits, launched_at)
            frozen = asdict(spec)
            frozen["set_fields"] = sorted(spec.set_fields or ())
            self.launch_limits.record_spec(node_id, frozen, launched_at, prompt_name)
            _run_write(run_dir, "command.json",
                       json.dumps(scrub(command_record), indent=2))
            env["MULTIAGENTS_TURN_STARTED_AT"] = str(launched_at)
            self._check_node_launch(node_id, window_resume=True) if window_resume else self._check_node_launch(node_id)
            if handover_attempt is not None:
                self._qh_check_switch(node_id, handover_attempt)
            handle = await executor.start(argv, workdir, env, run_dir=run_dir,
                                          deadline=launched_at + wall if wall else 0,
                                          provider=provider.name)
            launched_at = getattr(handle, "launched_at", 0) or launched_at
            supervisor.started = time.monotonic() - max(0.0, now() - launched_at)
            supervisor.last_event = supervisor.started
            self.launch_limits.record(node_id, limits, launched_at)
            self.launch_limits.record_spec(node_id, frozen, launched_at, prompt_name)
            self._record_launched(node_id, hold, handle)
            if handover_attempt is not None:
                self._qh_check_switch(node_id, handover_attempt)
            self.startup.bind(provider.name, node_id, startup_token, handle.pid,
                              getattr(handle, "pid_start", "") or "")
        except BaseException:
            if handle is None:
                # Nothing started, so nothing is followed: a lock kept here
                # would make the node unadoptable for this server's whole
                # lifetime, and the reservation has nothing left to hold.
                # SF-R3a: the startup claim goes back neutrally too. SF-R3
                # (review r2 finding 1): a steer's inherited lock is
                # `_steer_release`'s to keep or release — a live predecessor
                # may still be under it.
                self._retire_hold(node_id)
                if release_lock:
                    self._release(node_id)
                    self._startup_release(provider.name, node_id, startup_token)
                raise
            # RM-R1c: the process did start, so the one cleanup task stops
            # it and confirms its death before anything goes, including a
            # handover stop detected after executor.start; nothing is
            # released merely because stop returned.
            await self._await_cleanup(
                self._launch_cleanup_task(handle, node_id, done=done))
            raise
        # RM-R1c (review ag-cef33c): the Run is assembled from ready parts
        # only — the supervisor was built in the prologue, and the handle is
        # attached here — so nothing between executor.start succeeding and
        # the guard below can raise outside the cleanup's ownership.
        run = Run(
            node_id=node_id, provider=provider, spec=spec, handle=handle,
            supervisor=supervisor,
            turn_start=getattr(handle, "offset", 0), limits=limits,
            startup_token=startup_token, launched_at=launched_at,
            prompt_file=prompt_name,
            # #2: charges an earlier turn of this node could not commit
            # anywhere stay with the node's next turn, never dropped.
            pending_charges=list(getattr(self.runs.get(node_id), "pending_charges", None) or []),
            **({"done": done} if done is not None else {}),
        )
        if env.get("MULTIAGENTS_RPC_TOKEN"):
            run.capability_hash = hashlib.sha256(env["MULTIAGENTS_RPC_TOKEN"].encode()).hexdigest()
        self.runs[node_id] = run
        try:
            self._qh_launched(node_id, spec, provider, session_id)
            await self._track_container_run(run, executor)
            if handover_attempt is not None:
                self._qh_check_switch(node_id, handover_attempt)
                if run.stop_requested:
                    raise RuntimeError("handover was stopped")
            self.tree.update(node_id,
                             follow={"turn": run.turn_start, "offset": run.turn_start,
                                     "log": _size(run_dir / "stream.jsonl")},
                             adopted_at=None, turn_started_at=launched_at,
                             turn_ended_at=None)
            self.tree.set_status(node_id, "running")
            run.slot_token = self._launch_slot_owner(node_id, provider.name)
            if window_resume:
                from .scheduler.suspension import resumed
                from .scheduler.store import Store
                resumed(Store(self.paths.root), node_id)
        except BaseException:
            # RM-R1c (review ag-467011): the process is ALIVE here and no
            # consumer follows it yet. ONE cleanup task stops it, confirms
            # its death, and only then releases the supervision lock, the
            # container occupancy, the startup claim and the concurrency
            # slot, and signals `run.done`. The caller awaits that same task
            # through any number of its own cancellations — nothing is
            # released merely because stop returned, and if death cannot be
            # confirmed, ownership and occupancy are held.
            await self._await_cleanup(self._launch_cleanup_task(
                handle, node_id, run=run, oom_container=run.oom_container))
            raise
        # Supervised: the node occupies as a live `running` one from here,
        # so the reservation is lifted.
        self._retire_hold(node_id)
        run.task = asyncio.create_task(self._supervise(run))
        # Owned by the Runner, not by the run: asking an agent to wrap up means
        # stopping and relaunching it, which a task belonging to that same run
        # cannot safely do to itself.
        asyncio.create_task(self._wrap_up_watch(node_id))
        self._start_credential_watch()
        return run

    async def _track_container_run(self, run: Run, executor) -> None:
        """LN-C5: note the container's `oom_kill` count as this run starts,
        and register it in the occupancy record every Runner of this project
        shares — a sibling started by another server is as much a sibling as
        one of ours (finding 1).

        RM-R1e (review ag-2792f3) supersedes adversary finding 2 here: the
        registration is a write the launch hold depends on, so it must be
        confirmed durable. One that cannot be — the hold naming the
        container, or the occupancy entry itself — fails the launch into
        the one cleanup task, which stops the just-started process and
        releases everything once its death is confirmed. A run left going
        unregistered would let another server's store attribute a SIGKILL
        as an OOM kill of a lone occupant. A reading of the `oom_kill`
        counter that fails still costs only the attribution its baseline."""
        reader = getattr(executor, "oom_kill_count", None)
        if not callable(reader):
            return                                  # not a container
        run.oom_reader = reader
        run.oom_container = str(getattr(executor, "container", "") or "")
        with contextlib.suppress(Exception):
            run.oom_baseline = await asyncio.to_thread(reader)
        handle_pid = getattr(run.handle, "pid", 0) or 0
        hold = self._holds.get(run.node_id)
        if hold is not None:
            # RM-R1e: the launch hold names the container BEFORE the run is
            # registered there, durably, so a crash before any cleanup still
            # leaves recovery the occupancy record to rebind and end. If the
            # hold cannot say so, the run is not registered at all.
            hold.container = run.oom_container
            hold.record = dict(hold.record, occupancy=run.oom_container)
            try:
                self.tree.update(run.node_id, cleanup_hold=dict(
                    hold.record, then=list(hold.then) if hold.then else None))
            except Exception as exc:
                self._unrecorded_occupancy(run.node_id, "the launch hold "
                                           "could not record the container")
                raise RuntimeError(
                    f"the launch hold could not record container "
                    f"{run.oom_container!r}: {type(exc).__name__}: {exc}") from exc
        try:
            pid_start = getattr(run.handle, "pid_start", "") or ""
            entry = self.occupancy.register(run.oom_container, run.node_id,
                                            handle_pid, pid_start)
            run.oom_since = entry.get("since")
            entry = self.occupancy.ensure_live(run.oom_container, run.node_id,
                                               handle_pid, pid_start)
            run.oom_since = entry.get("since")
        except Exception as exc:
            # RM-R1e: never taken for a durable registration. Reported, and
            # the launch fails into cleanup rather than run unregistered.
            detail = f"{type(exc).__name__}: {exc}"
            self._unrecorded_occupancy(run.node_id, detail)
            raise RuntimeError(
                f"the run could not be registered in container "
                f"{run.oom_container!r}'s occupancy record: {detail}") from exc

    def _unrecorded_occupancy(self, node_id: str, detail: str) -> None:
        with contextlib.suppress(Exception):
            self.tree.emit(node_id, "occupancy_unrecorded", detail=detail[:300])

    async def _sigkill_notice(self, run: Run, code: int) -> None:
        """LN-C5: a run in the container died by SIGKILL. Attributed to
        `executor.docker.memory` only when the container's `oom_kill` count
        rose during it AND no other run was alive in there at any point of
        its life, judged across every Runner of this project; every other
        case is a SIGKILL of unknown cause — including a run whose baseline
        is unknown because it began under another server (finding 2), and a
        sibling that ended before the kill but overlapped the run (adversary
        finding 1). `docker inspect … OOMKilled` describes the container,
        not the exec'd process, and is not used."""
        node_id = run.node_id
        after = None
        with contextlib.suppress(Exception):
            after = await asyncio.to_thread(run.oom_reader)
        rose = (run.oom_baseline is not None and after is not None
                and after > run.oom_baseline)
        crowded = self.occupancy.others(run.oom_container, node_id,
                                        run.oom_since)
        memory = (self.config.project.get("executor", {}).get("docker", {}) or {}).get("memory")
        if rose and not crowded and memory:
            key = "executor.docker.memory"
            self._notice(key, memory, "killed", node_id,
                          notices.provenance(self.config, key, memory),
                          f"{node_id} ({run.spec.name}) was SIGKILLed when the "
                          f"container ran out of memory",
                          "raise it there if the work needs more", node=node_id)
            return
        if rose:
            why = (f"the container hit its memory limit at the time "
                   f"(executor.docker.memory = {memory}), a possible cause, but "
                   f"another run shared the container" if memory else
                   "the container's OOM killer fired at the time, but another "
                   "run shared the container")
        elif run.oom_baseline is None:
            why = ("its starting OOM counter is unknown — the run began under "
                   "another server, or the counter could not be read at launch")
        elif after is None:
            why = "the container's OOM counter could not be read"
        else:
            why = "the container's OOM counter did not change"
        self._notice("process.sigkill", code, "kill_uncertain", node_id, None,
                     f"{node_id} ({run.spec.name}) ended by SIGKILL of unknown "
                     f"cause; {why}", node=node_id)

    def _hand_server(self, node_id: str, provider: Provider, env: dict[str, str],
                     home: Path | None, run_dir: Path) -> tuple[list[str], dict[str, str]]:
        """What gives this spawn the multiagents MCP server: (argv, env) to add.

        How is the provider's to declare (`mcp:` in providers.yaml); this only
        fills it in and writes the config where the run owns it — `runs/<id>/`
        or the agent's private HOME, never the user's own (SM-R4). A provider
        that cannot be given the server, here, is recorded rather than failed:
        the agent still runs its task without it (SM-R5).
        """
        def unavailable(reason: str) -> tuple[list[str], dict[str, str]]:
            self.tree.emit(node_id, "mcp_unavailable", server="multiagents",
                           detail=f"the multiagents MCP server is unavailable to "
                                  f"this agent: {reason}")
            return [], {}

        if not provider.mcp:
            return unavailable(f"provider {provider.name} declares no `mcp:` block")
        command, *args = paths_mod.server_command()
        environment = server_env(env, node_id)
        # SM-R5, for every provider alike: a CLI that cannot start the server
        # may not say so in a way anything here reads. Checked on this side,
        # where the same paths are mounted at the same place in a container.
        if not _resolves(command, environment.get("PATH", "")):
            return unavailable(f"its command {command!r} does not resolve")
        block = provider.mcp
        try:
            if block.get("home_file"):
                if home is None:
                    return unavailable(f"{provider.name} reads its MCP servers only from "
                                       f"HOME, and home_policy is not per-agent")
                target = private_file(home, str(block["home_file"]))
            else:
                target = run_dir / str(block.get("file") or "mcp.json")
            launch = provider.mcp_launch({
                "mcp_command": command, "mcp_args": args, "mcp_argv": [command, *args],
                "mcp_env": environment, "mcp_config": str(target),
            })
            config = launch["config"]
            if launch["merge"]:
                config = config_mod.deep_merge(
                    self._user_mcp_config(str(block["home_file"])), config)
            if block.get("home_file"):
                _write_own(target, json.dumps(config, indent=2) + "\n")
            else:
                _run_write(run_dir, str(target.relative_to(run_dir)),
                           json.dumps(config, indent=2) + "\n", mode=0o600)
        except OSError as exc:
            return unavailable(f"its config could not be written: {exc}")
        return launch["args"], launch["env"]

    def _withdraw_server(self, provider: Provider, home: Path | None) -> None:
        """Take back a server config an earlier spawner turn left in `home` (SM-R2).

        Only a file this run wrote: a real file reached through real
        directories, never a link into the user's own configuration. Where the
        user has a copy of their own, the link `prepare_home` would have made
        to it is put back, so the agent has exactly what it had before.
        """
        relative = str((provider.mcp or {}).get("home_file") or "")
        if home is None or not relative:
            return
        target = home / relative
        with contextlib.suppress(OSError):
            if (target.is_symlink() or not target.is_file()
                    or home.resolve() not in target.resolve().parents):
                return
            target.unlink()
            user_copy = Path.home() / relative
            if user_copy.exists():
                target.symlink_to(user_copy)

    @staticmethod
    def _user_mcp_config(relative: str) -> dict:
        """The user's own copy of a CLI's MCP config, read and never written."""
        try:
            data = json.loads((Path.home() / relative).read_text())
        except (OSError, ValueError):
            return {}

        # The user's own `multiagents` entry — the orchestrator's global
        # registration, if any — is replaced whole, never merged key by key.
        def without_ours(obj):
            if isinstance(obj, dict):
                return {k: without_ours(v) for k, v in obj.items() if k != "multiagents"}
            return obj
        return without_ours(data) if isinstance(data, dict) else {}

    def _start_credential_watch(self) -> None:
        """Keep the container's access token fresh while runs are in flight.

        The token is renewed before each spawn, which is enough for an agent
        that finishes inside eight hours and no use at all to one that does
        not. A long run started with seven hours left dies mid-turn — and a run
        that dies mid-turn costs its worktree, its session, and the whole
        conversation that would have to be re-derived.

        One task for the Runner, not one per run: the work is per-machine, the
        renewal is behind a host-side lock anyway, and N agents each polling
        would contend on that lock for no gain.
        """
        if getattr(self, "_credential_task", None) is not None:
            return
        self._credential_task = asyncio.create_task(self._credential_watch())

    async def _credential_watch(self) -> None:
        interval = float(self.config.limits.get("credential_poll_seconds", 300))
        try:
            while True:
                await asyncio.sleep(interval)
                if not any(not r.done.is_set() for r in self.runs.values()):
                    return                # nothing running; the next spawn renews
                executor = self.executor()
                renew = getattr(executor, "refresh_private_credentials", None)
                if renew is None:
                    return                # local executor: no projection to keep
                # Cheap unless something is actually near expiry: the check is
                # a file read, and the renewal is skipped entirely outside the
                # margin. In a thread because both are blocking.
                for note in await asyncio.to_thread(renew):
                    self.tree.emit("-", "credential", detail=note[:200])
        except asyncio.CancelledError:
            raise
        except Exception:
            return                        # never take the event loop down with it
        finally:
            self._credential_task = None

    async def _wrap_up_watch(self, node_id: str) -> None:
        """Ask an agent to land what it has, once, before the window closes.

        The alternative is not "keep working" — it is being cut off mid-thought
        with an uncommitted worktree and a 173,000-token conversation that costs
        more to reload than it saved. Measured on the day this was written: four
        agents cut off that way, four branches discarded, the work re-derived
        from scratch by other agents.

        Once per NODE, not once per run: bug-c050b0 found the same drain send
        the wrap-up 6 times to one agent in about a second, because the flag
        lived on the Run and steer()/_launch() replace the Run on every resend
        — each fresh Run started its own watcher with the flag clear. The flag
        now lives on the node (`tree.py`, same fix as the free-retry counter),
        so it survives every steer, relaunch and free retry that follows. It is
        cleared only when a later reading shows more headroom than at the
        moment of asking — a window reset — which lets a genuinely new drain
        ask again.
        """
        interval = float(self.config.limits.get("wind_down_poll_seconds", 60))
        budget_cfg = self.config.project.get("budget") or {}
        min_span = budget_number(budget_cfg, "burn_min_span_seconds", zero_ok=True)
        min_samples = budget_number(budget_cfg, "burn_min_samples", zero_ok=True)
        # Staggered, so N agents do not all decide to write their handoffs in
        # the same second — the spike would be what finally hits the wall.
        await asyncio.sleep(interval * (0.5 + random.random()))
        while True:
            run = self.runs.get(node_id)
            if run is None or run.done.is_set() or run.awaiting:
                return
            node = self.tree.get(node_id)
            if node is None:
                return
            # Take a reading rather than trusting the last one. An advisor's
            # point, and a good one: budgets are sampled where they are already
            # read, which is at spawn — so a tree full of agents running local
            # test suites for ten minutes reads a burn rate from before any of
            # them started, and walks into the wall without ever crossing a
            # threshold. The read is cached (60s in process, 5 min per machine),
            # so polling it is nearly free.
            await asyncio.to_thread(self._sample_headroom, run.provider)
            burn = self.tree.burn(run.provider.name, min_span_seconds=min_span,
                                  min_samples=min_samples)
            headroom = burn.get("headroom")
            if node.wrap_up_asked:
                recovered = (headroom is not None and node.wrap_up_headroom is not None
                            and headroom > node.wrap_up_headroom)
                if not recovered:
                    return
                self.tree.update(node_id, wrap_up_asked=False, wrap_up_headroom=None)
            left = burn.get("seconds_to_wall")
            lead = float(self.config.limits.get("wrap_up_seconds", 420))
            if left is None or left > lead:
                await asyncio.sleep(interval)
                continue
            self.tree.update(node_id, wrap_up_asked=True, wrap_up_headroom=headroom)
            self.tree.emit(node_id, "wrap_up", provider=run.provider.name,
                           seconds_left=round(left))
            try:
                # Started by the Runner itself, so not refused at the
                # safe point (CW-R2): it waits for the gate to reopen.
                with await self.gate.enter_when_open():
                    await self.steer(node_id, WRAP_UP.format(
                        provider=run.provider.name, minutes=max(1, round(left / 60))))
            except Exception as exc:            # never let this kill the run
                self.tree.emit(node_id, "wrap_up_failed", detail=str(exc)[:200])
            return

    def _host_binary_needed(self, spec: AgentSpec) -> bool:
        executor = self.executor(spec)
        return not (getattr(executor, "kind", "local") == "docker"
                    and not executor.config.get("mount_cli_from_host", True))

    @staticmethod
    def _pin_refusal(provider: str, reason: str, retry_after=None) -> dict:
        message = (f"{provider}: {reason}. Omitting model lets the router choose "
                   "another provider.")
        result = {"reason": reason, "error": message, "message": message}
        if retry_after:
            result["retry_after"] = retry_after
        return result

    def _transport_refusal(self, provider: Provider | str) -> str | None:
        """TG-R1: validate admission before lifecycle changes, including reloads.

        A resumed run keeps its original provider definition. Validate today's
        config as well, so a stale override cannot kill its healthy predecessor
        or claim a queued task before the mismatch is reported.
        """
        name = provider if isinstance(provider, str) else provider.name
        current = self.providers.get(name)
        candidates = [current]
        if not isinstance(provider, str) and provider is not current:
            candidates.append(provider)
        for candidate in candidates:
            if candidate is not None:
                try:
                    error = candidate.transport_error()
                except ValueError as exc:
                    error = str(exc)
                if error:
                    return error
        return None

    def _model_refusal(self, provider_name: str, model: str) -> str | None:
        """PS-R6: why `provider_name` may not run `model`, or None when it may.

        The one allowlist check every admission and every launch asks, always
        of the CURRENT declaration (`self.providers`) and of the model
        actually being run — never of a Provider object a run retained from
        before a reload, nor of a spec rebuilt from the roster (reviews
        ag-2f0d3e 4/5, ag-3644ef 1). An unknown provider or one without an
        allowlist allows everything, as always. The text names a provider
        whose allowlist would take the model, when one exists; nothing is
        substituted."""
        provider = self.providers.get(provider_name)
        if provider is None or provider.allows_model(model or ""):
            return None
        acceptors = sorted(name for name, entry in self.providers.items()
                           if name != provider_name and entry.allows_model(model))
        way_out = (f" {acceptors[0]} allows it." if len(acceptors) == 1
                   else f" These allow it: {', '.join(acceptors)}."
                   if acceptors else "")
        return (f"{provider_name} does not allow model {model!r} (its "
                f"models_include exclude it).{way_out} No model or provider "
                f"is substituted; name a model that provider allows, or route "
                f"the agent to a provider that does.")

    def _runs_allowed_model(self, spec: AgentSpec, name: str,
                            pinned_model: str = "") -> bool:
        """PS-R6 (review ag-2f0d3e, finding 3): would the model this agent
        actually runs on `name` be allowed by `name`'s allowlist? Routing
        asks this of every candidate — a same-family sibling that shares the
        model namespace does not share the allowlist, and must never be
        chosen to run a model it excludes."""
        routed = self._usable_spec(spec, name, pinned_model=pinned_model)
        return routed is not None \
            and self.providers[name].allows_model(routed.model or "")

    def _family_of(self, name: str) -> str:
        """PS-R7a: a provider's family; an unknown name is its own family of
        one, so a destination that has since left the map is a change."""
        provider = self.providers.get(name)
        return (getattr(provider, "family", "") or name) if provider else name

    def _pin_problem(self, spec: AgentSpec) -> dict | None:
        provider = self.providers.get(spec.provider)
        if provider is None or not provider.enabled:
            return self._pin_refusal(spec.provider, "provider is disabled or unavailable")
        if self._host_binary_needed(spec) and not provider.available():
            return self._pin_refusal(spec.provider, provider.bin_error())
        problem = self.startup.availability(spec.provider)
        if problem:
            return self._pin_refusal(spec.provider, problem["reason"],
                                     problem.get("retry_after"))
        return None

    def _budget_providers(self, spec):
        if _launch_context.get() is None:
            return self.providers
        # Planned admission only waits for routes this agent can use and the
        # accounts that supply their quota readings.
        names = {spec.provider, *(spec.models or {})}
        pending = list(names)
        while pending:
            candidate = self.providers.get(pending.pop())
            for owner in (getattr(candidate, "budget_from", ""),
                          getattr(candidate, "auth_from", "")):
                if owner and owner not in names:
                    names.add(owner)
                    pending.append(owner)
        return {name: p for name, p in self.providers.items() if name in names}

    async def _pin_health(self, spec: AgentSpec) -> dict | None:
        problem = self._pin_problem(spec)
        if problem:
            return problem
        if await asyncio.to_thread(self._auth_ok, spec.provider) is False:
            return self._pin_refusal(spec.provider, "provider is not authenticated")
        cooldowns = self.tree.read().get("cooldowns", {})
        budgets = await asyncio.to_thread(
            budget_mod.read_all, self._budget_providers(spec), lambda _name: self.executor(spec),
            global_config_dir(), self.paths.config, None, cooldowns,
            limits=self.config.limits)
        self._half_open(budgets, cooldowns)
        self._wind_down(budgets)
        cfg = self.config.project.get("budget", {})
        chosen, why = budget_mod.choose_provider(
            spec.provider, budgets, [], float(cfg.get("reserve_headroom", 0.15)),
            reserved=budget_mod.reserved_providers(
                self.config.project, self.providers, self._orchestrator_provider()),
            allowed={spec.provider}, **self._instance_strategy(spec.provider))
        if chosen is None:
            entry = budgets.get(spec.provider)
            return self._pin_refusal(spec.provider, why,
                                     entry.cooldown_until if entry else None)
        return None

    def _instance_strategy(self, preferred: str) -> dict[str, Any]:
        """IS-R2a: the preferred instance's effective strategy for the pool."""
        cfg = self.config.project.get("budget", {})
        provider = self.providers.get(preferred)
        return {
            "strategy": (getattr(provider, "instance_strategy", None)
                         or cfg.get("instance_strategy", "soonest_reset")),
            "tolerance_minutes": budget_number(cfg, "instance_tolerance_minutes", zero_ok=True),
            "tolerance_points": budget_number(cfg, "instance_tolerance_points", zero_ok=True),
        }

    def _held(self, node_id: str) -> bool:
        """RM-R1d: whether a launch hold stands between this node and a new
        launch or a release: this Runner's own launch in progress or cleanup
        not yet confirmed, or the durable `cleanup_hold` another Runner set.
        This Runner's hold with nothing left to release (`lifting`) is not."""
        hold = self._holds.get(node_id)
        if hold is not None:
            return hold.phase != "lifting"
        node = self.tree.get(node_id)
        return node is not None and bool(node.cleanup_hold)

    def _mark_cap_refused(self, node_id: str, session_id: str | None,
                          refusal: dict) -> None:
        """SC-R3a: a launch a spend cap refused at spawn. A node with a
        session ends `limited` (resumable once the cap permits); one without
        has nothing to resume and fails like any refused launch."""
        status = "limited" if session_id else "failed"
        if not self._defer_while_held(node_id, status, refusal["reason"]):
            self.tree.set_status(node_id, status, refusal["reason"])

    def _mark_launch_failed(self, node_id: str, reason: str) -> None:
        """Mark a node `failed` — once its launch cleanup, if one holds it,
        has confirmed the process dead (RM-R1c). Until then the status stays
        what it is: `pending` is also the slot's durable reservation should
        the hold itself not have reached the tree."""
        if not self._defer_while_held(node_id, "failed", reason):
            self.tree.set_status(node_id, "failed", reason)

    def _startup_finish(self, provider: str, node_id: str, token: str,
                        failed: bool = False, error: str = "",
                        resolved: bool = False) -> None:
        if not token:
            return
        hold = self._holds.get(node_id)
        if (hold is not None and hold.phase == "cleanup" and not hold.confirmed
                and hold.token == token):
            # RM-R1c: this claim belongs to a launch cleanup that has not
            # confirmed death; it goes with everything else the hold owns.
            return
        event = self.startup.finish(
            provider, node_id, token, failed=failed, error=error,
            resolved=resolved,
            threshold=int(self.config.limits.get("startup_failure_threshold", 2)),
            cooldown=float(self.config.limits.get("provider_down_cooldown_seconds", 1800)))
        if event:
            self.tree.emit(node_id, "startup_down", **event)

    def _startup_release(self, provider: str, node_id: str, token: str) -> None:
        """SF-R3a: give back the startup claim of a launch that never
        happened, neutrally — a half-open probe free, no cooldown re-armed,
        because nothing was launched and nothing was learnt about the
        provider. A claim a launch cleanup still owns is left to it, exactly
        as `_startup_finish` leaves it (RM-R1c)."""
        if not token:
            return
        hold = self._holds.get(node_id)
        if (hold is not None and hold.phase == "cleanup" and not hold.confirmed
                and hold.token == token):
            return
        self._retry_startup_release(provider, node_id, token)

    @property
    def _startup_release_pending(self) -> set[tuple[str, str, str]]:
        """Review r1 finding 4: neutral releases whose write failed, kept
        until the reconciliation retries them. In memory on purpose: a
        restart drops the owner too, and `StartupHealth._reconcile` then
        clears the dead owner's claim by itself."""
        return self.__dict__.setdefault("_startup_release_pending", set())

    def _retry_startup_release(self, provider: str, node_id: str,
                               token: str) -> bool:
        """One attempt at a neutral release. True when the claim is gone;
        False leaves it on the pending set for `_retry_startup_releases`."""
        try:
            if self.startup.release(provider, node_id, token):
                self._startup_release_pending.discard((provider, node_id, token))
                return True
        except Exception as exc:                            # noqa: BLE001
            self._ledger_failed(exc)
        self._startup_release_pending.add((provider, node_id, token))
        return False

    def _retry_startup_releases(self) -> None:
        """Review r1 finding 4: retry the neutral releases whose write
        failed, on the reconciliation path (`_settle_holds`) and before a
        recovery (`_announce_pending`). Neutral throughout (SF-R3a): a probe
        goes back free and half-open, never re-armed or cleared, so once
        storage recovers the claim cannot stay stuck while this server
        lives."""
        for provider, node_id, token in sorted(self._startup_release_pending):
            self._retry_startup_release(provider, node_id, token)

    # ----------------------------------------------------------------- start --

    @_admitted("a new agent")
    @_scheduled_start
    async def start(
        self,
        agent_name: str,
        task: str,
        *,
        workdir: str | None = None,
        timeout: int | None = None,
        model: str | None = None,
        verifies: str = "",
        budget_tag: str = "",
        budget_tokens: int = 0,
        deferred_id: str = "",
        recorded_provider: str = "",
        queued: dict | None = None,
        _cap_raced: bool = False,
        _qh_floor_grant: str = "",
    ) -> dict[str, Any]:
        """`queued` is a provider-concurrency entry being drained (PC-R3a):
        it runs on the provider it queued on and nowhere else, claims its
        entry in the admission transaction, and is never re-queued — a
        refusal answers `pc_full` (still no slot: the entry keeps its place)
        or `blocked` (refused for another reason, reported by the drain)."""
        floor_id, floor_request = (self._qh_take_floor_grant(_qh_floor_grant)
                                   if _qh_floor_grant else ("", ""))
        context = _launch_context.get()
        if context and context.node_id and floor_request:
            return admission_block("scheduler-managed runs cannot use legacy floor admission")
        if context and context.node_id:
            # Managed activations always work on their recorded input checkout.
            workdir = None
        spec = self.config.agent(agent_name)
        if context and context.provider:
            spec = self._usable_spec(spec, context.provider, resume=True) or spec
            spec = spec.replace(provider=context.provider)
        if context and context.effort:
            spec = spec.replace(effort=context.effort)
        queued_model, queued_pinned = "", False
        if queued:
            # PC-R3d: a queued start runs the provider and model recorded
            # when it was routed and queued, validated as that pair alone
            # (`_pc_recorded_model_problem`, by the drain) — never against
            # today's roster for the preferred provider (round 3, finding 3).
            # A pin stays a pin on the node.
            queued_pinned = bool((queued.get("spec") or {}).get("pinned"))
            queued_model, model = model or (queued.get("spec") or {}).get("model") or "", None
        # FO-R1: a model chosen by a per-run pin (or recorded on a queued start)
        # outranks the `models.P` entry's model, but not its options.
        pinned = bool(model) or queued_pinned or bool(queued_model)
        if model:
            # A model id belongs to one provider's namespace. In a real session
            # the orchestrator sent `claude --model opencode-go/kimi-k2.7-code`
            # and `claude --model deep`, both of which the CLI rejected after a
            # spawn had already been paid for. Refusing here costs nothing and
            # says what the choices are.
            #
            # But an agent has TWO namespaces, and only checking the first made
            # the refusal wrong and its own advice circular: `flutter-tester`
            # names `models: {agy: gemini-3.1-pro-high}`, and asking for exactly
            # that string was refused with "claude does not serve a model called
            # 'gemini-3.1-pro-high' — name a model under `models:` instead",
            # which is what the agent already did. Filed as bug-583360 by an
            # agent that then had no way to run the fallback it could see.
            known = {m.get("id") for m in (self.config.models.get(spec.provider) or [])
                     if isinstance(m, dict)}
            elsewhere = next((name for name in (spec.models or {})
                              if spec.fallback_for(name)[0] == model), None)
            if elsewhere is not None and (not known or model not in known):
                # Naming a fallback's model is a request to run THERE. Carrying
                # the provider across matters as much as the model: the id is
                # meaningless in the old one's namespace, which is the whole
                # reason this check exists.
                alternative, overrides = spec.fallback_for(elsewhere)
                spec = spec.replace(provider=elsewhere, model=alternative, **overrides)
            elif known and model not in known:
                offers = ", ".join(
                    f"{name}:{spec.fallback_for(name)[0]}" for name in (spec.models or {})
                    if spec.fallback_for(name)[0]) or "none"
                # PS-R6: and, when the allowlist is why, who would take it.
                refusal = self._model_refusal(spec.provider, model)
                raise ValueError(
                    f"{spec.provider} does not serve a model called {model!r}. "
                    f"A model id belongs to its provider's namespace. "
                    f"{agent_name!r} can also run on: {offers} — naming one of "
                    f"those models here runs it on that provider."
                    + (f" {refusal}" if refusal else "")
                )
            else:
                spec = spec.replace(model=model)
            # PS-R6: a pin runs only on a provider whose allowlist accepts the
            # model — the same rule the roster is validated against at load,
            # here at admission, before any side effect. A provider without an
            # allowlist allows everything, as always.
            refusal = self._model_refusal(spec.provider, spec.model)
            if refusal:
                message = f"refusing to start {agent_name!r}: {refusal}"
                return {"reason": message, "error": message, "message": message}
        destination = ((queued.get("spec") or {}).get("provider") if queued else None) \
            or spec.provider
        transport_error = self._transport_refusal(destination)
        if transport_error:
            return {"error": transport_error}
        if budget_tag and budget_tokens:
            # First value wins, so a re-declaration cannot lift a spent ceiling.
            self.tree.set_budget(budget_tag, budget_tokens,
                                 set_by=self.self_id() or self.session() or "root")
        if model:
            problem = await self._pin_health(spec)
            if problem:
                return problem
        queued_id = (queued or {}).get("id") or ""
        if queued:
            # PC-R3a: a queued start launches on the provider it queued on.
            target = (queued.get("spec") or {}).get("provider") or spec.provider
            # FS-R2: a queued entry is a recorded destination, so an unlisted
            # sibling it was queued on still resolves for this restart.
            routed = self._usable_spec(spec, target, resume=True) or spec
            # Review finding 1: the spec runs AS the queue's provider, so
            # routing looks for it there and nowhere else, whichever
            # provider the agent prefers today.
            spec = routed.replace(provider=target)
            if queued_model:
                spec = spec.replace(model=queued_model)
        self._preflight(spec, workdir, budget_tag, pinned=bool(model) or queued_pinned)
        # LN-C4: every refusal limit this start was checked against let it
        # through, so the notices for them in its scopes have stopped.
        passed = {"tree", self.self_id() or "tree", *([budget_tag] if budget_tag else [])}
        notices.clear(self.tree, lambda e: e.get("effect") == "refused"
                      and e.get("scope") in passed)
        provider = self.providers[spec.provider]
        transport_error = self._transport_refusal(provider)
        if transport_error:
            return {"error": transport_error}

        parent = self.self_id()
        depth = self.self_depth() + 1
        if queued and "parent" in (queued.get("spec") or {}):
            # PC-R3a: whichever process drains it, the run is the one that
            # was asked for — the queuing agent's child, at its depth.
            parent = queued["spec"].get("parent")
            depth = int(queued["spec"].get("depth") or depth)
        if context:
            parent, depth = context.run_parent, context.depth
        node_id = floor_id or (context.run_id if context and context.run_id else new_id())

        # --- budget routing -------------------------------------------------
        # Budget now shells out to provider scripts, so it must not run on the
        # event loop: a slow provider would freeze every concurrent _consume,
        # wait_for_agents and check_agent. Cached for 60s and offloaded.
        cooldowns = self.tree.read().get("cooldowns", {})
        budgets = await asyncio.to_thread(
            budget_mod.read_all, self._budget_providers(spec),
            # read_all hands the PROVIDER name; Runner.executor takes an
            # AgentSpec. The budget scripts only need the backend kind and any
            # container context, so the project default is the right answer.
            lambda _provider_name: self.executor(),
            global_config_dir(), self.paths.config, None, cooldowns,
            limits=self.config.limits,
            # RM-R4b: a reading older than this routes as unknown.
            max_reading_age=budget_mod.reading_age_bound(self.config.project),
        )
        self._half_open(budgets, cooldowns)
        spend_now = self.tree.rollup_usage().get("cost_usd", 0)
        for name, entry in budgets.items():
            # RM-R4c: a reading that routes as unknown is not a sample — its
            # raw headroom must not enter the burn series.
            if entry.stale:
                continue
            self.tree.note_headroom(name, entry.headroom, spend_now)
        before_wind_down = copy.deepcopy(budgets)
        self._wind_down(budgets)
        routed_from, routed_why = "", ""
        budget_cfg = self.config.project.get("budget", {})
        # PS-R5b: routing and the startup claim are two steps, and another
        # server can take a half-open provider's only probe between them.
        # Losing that race is not a refusal — the call never asked for this
        # provider by name — so routing runs again with the provider that
        # could not be claimed excluded, exactly as `provider_down` is
        # handled: siblings and fallbacks are tried, and deferral applies
        # when none is left. A pinned start still gets the PS-R6 refusal.
        unclaimable: set[str] = set()
        # PC-R3: providers routing found full, in the order it chose them —
        # the first is where the start queues when no route has a slot
        # (PC-R3a), with the model it would have run there.
        pc_full: dict[str, tuple[ProviderFull, str]] = {}
        configured_spec = spec
        # FO-R1: the model routing must keep when it is a per-run pin, "" when
        # the `models.P` entry is free to name one.
        pinned_spec = spec.model if pinned else ""
        while True:
            spec = configured_spec
            provider = self.providers[spec.provider]
            routed_from, routed_why = "", ""
            # FS-R1/FS-R2: candidates are exactly the agent's own provider
            # plus the keys of its `models:` map. A family sibling that is
            # not listed there is not a candidate, so the family pool below
            # is narrowed to the names the agent actually wrote; the family
            # still decides which of THOSE shares a model namespace and may
            # take the work.
            named = {spec.provider, *(spec.models or spec.extra.get("models") or {})}
            # Other accounts on the same CLI, among the named ones.
            # Interchangeable without a `models:` entry, because a model id
            # means the same thing on both.
            family = providers_mod.families(self.providers).get(
                self.providers[spec.provider].family
                if spec.provider in self.providers else "", [])
            family = [name for name in family if name in named]
            # A disabled sibling is never a candidate (CX-C6): `read_all` omits
            # it, and a provider with no budget would otherwise read as one
            # with room. Neither is a sibling whose allowlist rejects the
            # model this agent would run there (PS-R6, review ag-2f0d3e
            # finding 3): sharing a model namespace is not sharing an
            # allowlist.
            family = [name for name in family
                      if name == spec.provider or self.providers[name].enabled]
            family = [name for name in family
                      if self._runs_allowed_model(spec, name, pinned_spec)]
            # FS-R1: the project `fallback_chain` adds no provider. Its only
            # surviving role was the terminal `defer`, and deferral after the
            # agent's own candidates is now unconditional — so it is not
            # walked at all. Ordering is unchanged: preferred first, then the
            # agent's `models:` order.
            chain: list[str] = []
            # RT-R1: a candidate is a provider this agent has a model on — its
            # own, or a `models:` entry naming one. A key with an empty model
            # is not one: routing there ran `--model ""`.
            # RM-R2a: the agent's own `models:` routes are their own tier, in
            # the order they are written. They are candidates always, not only
            # when the preferred provider is startup-blocked — a
            # budget-exhausted preferred provider is exactly when its own
            # fallbacks should speak up. A pinned start has no tiers: it asked
            # for one provider.
            # RM-R2b: a disabled route is never a candidate. `families` lists
            # every provider, enabled or not, and a disabled one has no budget
            # reading to answer the room question with.
            routes: list[str] = []
            if not model and not queued:
                for name in (spec.models or spec.extra.get("models") or {}):
                    here = self.providers.get(name)
                    if here is not None and not here.enabled:
                        continue
                    if name not in routes:
                        routes.append(name)
            if model or queued:
                family, chain = [], []
            startup_blocked = {}
            for name, candidate in self.providers.items():
                problem = self.startup.availability(name)
                if problem or name in unclaimable or name in pc_full:
                    if problem:
                        startup_blocked[name] = problem
                    entry = budgets.setdefault(name, budget_mod.Budget(name, known=False))
                    entry.cooldown_until = max(
                        entry.cooldown_until or 0, now() + 1,
                        (problem or {}).get("retry_after") or 0)
                    entry.note = (problem["reason"] if problem else
                                  PC_CAUSE if name in pc_full else "startup_down")
            # SC-R3: a provider whose cap binds — or the model this agent
            # would run on it, whose own cap binds — is routed around like an
            # exhausted provider, for THIS start only: it is a copy of the
            # reading that is marked, so a model cap never cools the provider
            # for anyone else. Asked of every candidate's own resolved model.
            capped: dict[str, dict] = {}
            current_caps = self._cap_providers()
            for name, candidate in self.providers.items():
                if getattr(current_caps.get(name), "spend_cap", None) is None:
                    continue
                routed = self._usable_spec(spec, name, pinned_model=pinned_spec)
                if routed is None:
                    continue
                refusal = self._cap_refusal(name, routed.model or "")
                if refusal is None:
                    continue
                capped[name] = refusal | {"model": routed.model or ""}
                entry = budgets.get(name) or budget_mod.Budget(name, known=False)
                budgets[name] = replace(
                    entry, cooldown_until=max(entry.cooldown_until or 0, refusal["until"]),
                    note=refusal["reason"])
            usable = {name for name in self.providers
                      if self._runs_allowed_model(spec, name, pinned_spec)}
            unmodelled = [name for name in dict.fromkeys([*routes, *chain, *family])
                          if name in self.providers and name not in usable
                          and self.providers[name].enabled]
            load, last_used = self._instance_load()
            reserve = float(budget_cfg.get("reserve_headroom", 0.15))

            def choose(budgets: dict, reserve: float) -> tuple[str | None, str]:
                def legacy(readings):
                    return budget_mod.choose_provider(
                        spec.provider, readings, chain, reserve,
                        reserved=budget_mod.reserved_providers(
                            self.config.project, self.providers, self._orchestrator_provider()),
                        # Only the providers this agent has a model to run on. A
                        # candidate it cannot use is not a candidate, and discovering
                        # that afterwards is how a run ended up back on the provider
                        # just ruled out. (The `models:` keys are named explicitly,
                        # even though `routes` supersedes them, so the guard below
                        # — the chooser is never offered a provider the agent did
                        # not name a model for — reads on its own.)
                        allowed={spec.provider, *(spec.models or spec.extra.get("models") or {}),
                                 *routes, *family, *chain} & usable,
                        family=family,
                        routes=routes,
                        load=dict(load), last_used=dict(last_used),
                        wait_for_reset_within=float(budget_cfg.get("wait_for_reset_seconds", 1800)),
                        **self._instance_strategy(spec.provider),
                    )
                return self._qh_choose_new(spec, budgets, legacy, managed=bool(context and context.node_id),
                                           floor=bool(floor_request))

            chosen, why = choose(budgets, reserve)
            if chosen is not None:
                # PC-R3: a full provider is passed over exactly like an
                # exhausted one — the next route with a free slot (and an
                # empty queue, PC-R3a) takes the start.
                full = self._pc_full(chosen, queued_id)
                if (full is not None and not queued and full.ahead
                        and (full.limit is None or len(full.holders) < full.limit)):
                    # Slots are free and only queued work stands ahead: it
                    # goes first (PC-R3a), then this start looks again.
                    await self._drain_queues()
                    full = self._pc_full(chosen)
                if full is not None:
                    if queued:
                        return {"pc_full": True, "gone": full.gone, "reason": str(full)}
                    routed = self._usable_spec(configured_spec, chosen,
                                               pinned_model=pinned_spec)
                    pc_full[chosen] = (full, routed.model if routed else spec.model)
                    continue
            if chosen is None and pc_full and context:
                return admission_block(next(iter(pc_full.values()))[0])
            if chosen is None and pc_full:
                return self._pc_queue_start(next(iter(pc_full.values())), agent_name, task,
                                            model=model, timeout=timeout, workdir=workdir,
                                            verifies=verifies, budget_tag=budget_tag)
            if chosen is None and queued:
                # PC-R3a: a head refused for another reason keeps its place.
                # SC-R3: a cap is such a reason, named with its reset.
                target = spec.provider
                if target in capped:
                    return {"blocked": True,
                            "reason": self._with_concurrency(capped[target]["reason"],
                                                             [target]),
                            "retry_after": capped[target]["until"]}
                return {"blocked": True, "reason": why}
            if chosen is None and model:
                entry = budgets.get(spec.provider)
                problem = startup_blocked.get(spec.provider) or capped.get(spec.provider) or {}
                if spec.provider in capped and spec.provider not in startup_blocked:
                    # #9: a pin refused by a cap names a full provider too.
                    problem = dict(problem, reason=self._with_concurrency(
                        problem["reason"], [spec.provider]))
                return self._pin_refusal(spec.provider, problem.get("reason") or why,
                                         problem.get("retry_after") or
                                         (entry.cooldown_until if entry else None))
            if chosen is None and context:
                return admission_block(why, min((b.cooldown_until for b in budgets.values()
                                                if b.cooldown_until), default=None))
            if chosen is None:
                self._deferral_notices(agent_name, choose, budgets, before_wind_down, reserve)
            else:
                # LN-C4: work routed, so a deferral limit is no longer deferring it.
                notices.clear(self.tree, lambda e: e.get("effect") == "deferred"
                              and e.get("scope") == "tree")
            if chosen is not None and len(family) > 1:
                self.tree.claim_instance(chosen)
            if chosen != spec.provider:
                # Only when routing looked past the agent's own provider: that is
                # when a provider it has no model on was passed over.
                for name in unmodelled:
                    self.tree.emit(node_id, "route_skipped", provider=name,
                                   reason=f"no model configured for this agent on {name}")
            if chosen is None:
                reserved = self._qh_reserved()
                reserve_spec = self._usable_spec(spec, reserved) if reserved else None
                if (not context and not queued and reserve_spec
                        and not self._qh_above_floor(budgets.get(reserved))
                        and await self._qh_usable(reserved, reserve_spec, budgets, floor=True)):
                    return self._qh_request(agent_name, task, kwargs={
                        "workdir": workdir, "timeout": timeout, "model": model,
                        "verifies": verifies, "budget_tag": budget_tag})
                # Prefer a real reset time over the blind cooldown: a provider that
                # told us when it comes back should not be waited on for longer.
                # Only from providers THIS agent could use — waking for one it
                # cannot run on finds nothing changed and defers again, forever.
                options = {spec.provider, *(spec.models or spec.extra.get("models") or {})}
                resets = [b.cooldown_until for name, b in budgets.items()
                          if b.cooldown_until and name in options]
                retry_at = min(resets) if resets else now() + float(
                    self.config.project.get("budget", {}).get("blind_cooldown_seconds", 900)
                )
                # SC-R3: refused by a spend cap on any route this agent has —
                # the cause is `spend_cap` (or `spend_cap_unreadable`), each
                # binding cap is named, and the entry records the routes the
                # caps refused, so a raised or removed cap releases it early.
                cap_routes = {name: refusal for name, refusal in capped.items()
                              if name in options or name in routes or name in family
                              or name in chain}
                cause = None
                if cap_routes:
                    cause = spendcap.CAUSE
                    # PC/SC: where a route is both capped and full, both
                    # causes are reported (#12).
                    why = self._with_concurrency("; ".join(dict.fromkeys(
                        r["reason"] for r in cap_routes.values())), cap_routes) + f" ({why})"
                queued = self.tree.defer(
                    {"agent": agent_name, "task": task, "timeout": timeout,
                     "model": model, "workdir": workdir,
                     # PS-R7a/R7b: where this entry was headed — the preferred
                     # provider that was unavailable. A restart refuses the
                     # pin if that provider no longer allows the model, and
                     # never silently re-routes it to another family.
                     "provider": spec.provider},
                    retry_at, why,
                    deferred_by=self.self_id(), cause=cause,
                    extra={"spend_cap_routes": [[name, r["model"]]
                                                for name, r in cap_routes.items()]}
                    if cap_routes else None)
                # Nothing can run, so nothing should keep being started. Pausing is
                # the difference between a system that stops and one that carries on
                # writing code while the agents that check it are unreachable.
                #
                # Named by what is actually UNAVAILABLE, not by everything this
                # agent could have used: a pause listing a healthy provider would
                # refuse other agents that only need that one, turning one agent's
                # problem into everybody's.
                unavailable = sorted(name for name in options
                                     if name in budgets and not budgets[name].usable)
                # SC-R3: a cap pauses nothing. It binds one route of one agent
                # (a model cap leaves its siblings admitted), and its entry is
                # released by the cap's own check, not by a pause lifting.
                if cap_routes:
                    unavailable = [n for n in unavailable if n not in cap_routes]
                paused = not cap_routes or bool(unavailable)
                if paused:
                    self.tree.pause(retry_at, why, providers=unavailable or sorted(options),
                                    deferral=True)
                if not paused:
                    return {"deferred": True, "reason": why, "retry_after": retry_at,
                            "paused": False, "cause": cause,
                            "deferred_id": queued["id"],
                            "note": "it restarts by itself when the period ends, or "
                                    "at the next wait_for_agents once the cap is "
                                    "raised or removed"}
                return {"deferred": True, "reason": why, "retry_after": retry_at,
                        "paused": True,
                        # DQ-R11: the caller is told which entry this deferral
                        # created, so it never has to identify it by diffing
                        # the queue — another agent may defer in the same
                        # window, and diffing takes that one instead.
                        "deferred_id": queued["id"],
                        "note": "the tree is paused until this clears; deferred tasks "
                                "restart by themselves when it does"}
            # FO-R1: the chosen provider's `models.P` entry is merged in even
            # when it IS the agent's own provider — the primary is a route too.
            routed = self._usable_spec(spec, chosen, pinned_model=pinned_spec)
            if routed is None:              # choose_provider offers only `allowed`
                raise RuntimeError(f"routing chose {chosen!r}, where agent "
                                   f"{agent_name!r} has no model")
            if chosen != spec.provider:
                # The model id belongs to the original provider's namespace, so it
                # is meaningless to the new one — failing over without remapping
                # would run `agy --model opencode-go/glm-5.3-flash`. choose_provider
                # is told which providers this agent named a model for and offers no
                # other, so there is always one to use here.
                #
                # It used to discover the missing model at this point and respond by
                # reverting to the provider it had just ruled out. Measured cost of
                # that: an agent whose configured fallback sat one place further
                # down the chain ran five times into a revoked token instead.
                _, overrides = spec.fallback_for(chosen)
                provider = self.providers[chosen]
                routed_from, routed_why = spec.provider, why
                if overrides:
                    routed_why += f" ({', '.join(f'{k}={v!r}' for k, v in overrides.items())})"
            spec = routed
            # PS-R7a (review ag-2f0d3e, finding 6): a pinned restart that
            # records its destination may not be carried, by a roster change
            # since, onto a DIFFERENT family. The recorded provider allowed
            # the model (checked by the drain); "or on its normal routing"
            # means this family's routing, never another vendor's.
            # PS-R6: routing never picks a provider whose allowlist excludes
            # the model (roster routes are checked at load), so this refuses
            # only a spec that reached here some other way — before the node
            # exists, rather than as a failed run.
            transport_error = self._transport_refusal(provider)
            if transport_error:
                return {"error": transport_error}
            refusal = self._model_refusal(provider.name, spec.model)
            if refusal:
                message = f"refusing to start {agent_name!r}: {refusal}"
                return {"reason": message, "error": message, "message": message}
            if recorded_provider and model and self._family_of(provider.name) \
                    != self._family_of(recorded_provider):
                return self._pin_refusal(
                    provider.name,
                    f"the task was deferred on {recorded_provider}, and this "
                    f"pin would run it on {provider.name}, a different "
                    f"family; nothing is substituted. Route the agent back "
                    f"to {recorded_provider} (or its family) and re-issue it")
            # RM-R7: an explicitly contradicted effort refuses BEFORE the
            # startup claim, so the refusal takes neither the half-open
            # provider's only probe nor a startup.json run record — both
            # would outlive the refused start, because the release in the
            # try/finally below only runs for a claim that was actually
            # taken.
            self._effort_conflict(spec, provider)
            if context and context.admission_only:
                return {"admitted": True, "provider": provider.name, "model": spec.model}
            try:
                startup_token = self.startup.claim(provider.name, node_id)
            except StartupUnavailable as exc:
                if model:
                    return self._pin_refusal(provider.name, exc.reason, exc.retry_after)
                unclaimable.add(provider.name)
                continue
            break
        launched = False
        try:
            # RM-R5a/RM-R7: the pair is settled HERE, inside the block whose
            # finally releases the startup claim — after the claim, so the
            # normalisation and its event belong only to the provider the
            # run launches on (ag-37f81c), and inside the protection, so an
            # exception during normalisation releases the claim instead of
            # leaking it (review ag-f21a0c). `spec` was reset from
            # `configured_spec` at the loop top, so this settles the
            # un-normalised candidate.
            spec = self._settle_effort(spec, provider, node_id)
            # LM-R1/R2: resolved once, recorded on the node, reported to the
            # caller. LN-C2, finding 8: the provenance is resolved HERE, at
            # launch, and travels with the run — a later edit of the yaml must
            # not rewrite where a trip says the value came from.
            limits = self._limits_detail(
                agent_name, self.config.effective_limits(spec, timeout))

            # --- git isolation ---------------------------------------------------
            # EVERY agent gets a worktree, including read-only ones. `writes: false`
            # is a statement of intent, not an enforced permission — nothing stops a
            # model from calling an edit tool. Giving a "read-only" agent the real
            # project directory would mean trusting that intent with your working
            # tree. A worktree costs almost nothing and makes the flag irrelevant to
            # your safety: a non-writing agent that writes anyway is quarantined,
            # and its branch is dropped afterwards if it turns out to be empty.
            repo = self.paths.root
            branch = ""
            worktree_path = Path(workdir).expanduser() if workdir else self.paths.root
            if not workdir:
                # _preflight has already established that this is a repository.
                base = self.config.base_branch or gitops.current_branch(repo)
                desired = f"{self.config.branch_prefix}/{agent_name}/{node_id.removeprefix('ag-')}"
                worktree_path = self.paths.worktree(node_id)
                if context:
                    from .scheduler.store import Store
                    from .scheduler.engine import attempts
                    with Store(repo).transaction(write=False) as db:
                        activation = attempts(db)[context.attempt_id]
                    worktree_path = Path(activation.get("alias_worktree", worktree_path))
                branch = gitops.unique_branch(repo, desired)

            node = Node(
                id=node_id, agent=agent_name, provider=provider.name, model=spec.model,
                parent=parent, depth=depth, task=task[:500], branch=branch,
                worktree=str(worktree_path), status="pending",
                verifies=verifies if verifies in self.tree.read()["nodes"] else "",
                budget_tag=budget_tag,
                # DQ-R11: written in the transaction that creates the node, so
                # a drain that dies between start() returning and its own
                # bookkeeping leaves a node recovery can find — the restart
                # happened once, and nothing starts the task a second time.
                deferred_id=deferred_id,
                node_id=context.node_id if context else "",
                attempt_id=context.attempt_id if context else "",
                routed_from=routed_from, routed_why=routed_why,
                effort=spec.effort or "",
                limits=limits, session=self.session(),
                model_pinned=bool(model) or queued_pinned,
                on_reserve_floor=bool(floor_request),
                reserve_request=floor_request,
            )
            # RM-R1a: admission is one transaction with the insert, so the
            # slot is ours from this moment; every exit below that does not
            # launch gives it back.
            try:
                self._admission_add(spec, node, queued_id)
            except ProviderFull as full:
                # PC-R2: lost the race for the last slot since routing looked.
                # QH-R16: the reserve queue owns an approved floor retry;
                # recursive admission would lose its consumed grant.
                if queued or floor_request:
                    return {"pc_full": True, "gone": full.gone, "reason": str(full)}
                # Review finding 12: not queued yet — routing runs again,
                # with this provider now seen full, so a free fallback is
                # tried first and the queue is the last resort. The claim
                # taken for this attempt goes back first.
                self._startup_finish(provider.name, node_id, startup_token)
                startup_token = ""
                return await self.start(
                    agent_name, task, workdir=workdir, timeout=timeout, model=model,
                    verifies=verifies, budget_tag=budget_tag,
                    deferred_id=deferred_id, recorded_provider=recorded_provider)
            try:
                if self.authority:
                    self.authority.add(node)
                if not workdir:
                    if context:
                        from .scheduler.results import Results
                        from .scheduler.sessions import create_checkout
                        create_checkout(Results(self.paths, self.config), self.authority,
                                        worktree_path, branch, activation)
                    else:
                        gitops.create_worktree(repo, worktree_path, branch, base, unique=False)
                if routed_from:
                    # Loud enough to find later. This decision changes which model does
                    # the work, and until now it left no trace anywhere.
                    self.tree.emit(node_id, "routed", **{"from": routed_from,
                                                         "to": provider.name,
                                                         "model": spec.model,
                                                         "reason": routed_why})

                prompt = self.compose_prompt(spec, task, node, worktree_path)
                if context:
                    prompt += f"\nworking directory: {worktree_path}\nnode: {context.node_id}\n"
                    if activation.get("findings"):
                        prompt += "\nfindings:\n" + json.dumps(activation["findings"]) + "\n"
                    review = activation.get("review")
                    if review:
                        # State the reviewed identity separately from the
                        # activation's own node and working directory.
                        prompt = (f"review: node_id={review['node_id']} "
                                  f"generation_seq={review['generation_seq']} commit={review['commit']}\n" + prompt)
                try:
                    run = await self._launch(
                        node_id=node_id, spec=spec, provider=provider, prompt=prompt,
                        workdir=worktree_path, branch=branch, parent=parent, depth=depth,
                        timeout=timeout, startup_token=startup_token,
                        session_id=activation.get("session_id") if context else None,
                    )
                except SpendCapRefused as exc:
                    # SC-R3/R3b (#7): admitted, then a crossing landed before
                    # the spawn. Nothing ran; the node is `refused`, and the
                    # task goes through admission again (#6) — which now sees
                    # the cap, so it tries the remaining routes, then defers.
                    refused = self._cap_raced_start(
                        node_id, exc.refusal, agent_name, task, spec=spec,
                        provider=provider.name, model=model, timeout=timeout,
                        workdir=workdir, queued=queued if queued_id else None)
                    if refused is not None:
                        return refused
                    if floor_request:
                        # QH-R16: retain this approval in the reserve queue,
                        # which rechecks the cap before granting another run.
                        return {"blocked": True, "reason": exc.refusal["reason"],
                                "retry_after": exc.refusal["until"], "refused_node": node_id}
                    self._startup_finish(provider.name, node_id, startup_token)
                    startup_token = ""
                    if _cap_raced:
                        # Raced twice: admission and the spawn keep disagreeing
                        # (a cap flapping). Deferred on this route, no further
                        # re-routing.
                        return self._cap_defer_route(
                            node_id, exc.refusal, agent_name, task, spec=spec,
                            provider=provider.name, timeout=timeout, workdir=workdir)
                    again = await self.start(
                        agent_name, task, workdir=workdir, timeout=timeout,
                        model=model, verifies=verifies, budget_tag=budget_tag,
                        deferred_id=deferred_id, recorded_provider=recorded_provider,
                        _cap_raced=True)
                    return {**again, "refused_node": node_id}
                except RuntimeError as exc:
                    self._mark_launch_failed(node_id, str(exc))
                    return {"agent_id": node_id, "status": "failed", "error": str(exc)}
            except BaseException as exc:
                # RM-R1a: the slot this start took is given back visibly —
                # or, while a launch cleanup holds the node (RM-R1c), once
                # that cleanup confirms the process dead.
                self._mark_launch_failed(
                    node_id, f"start did not launch: {type(exc).__name__}: {exc}")
                raise

            launched = True
            return {
                "agent_id": node_id,
                "agent": agent_name,
                "provider": provider.name,
                "model": spec.model,
                "branch": branch or None,
                "workdir": str(worktree_path),
                "status": "running",
                "routing": why,
                "effective_limits": limits,
                "log": str(self.paths.run_dir(node_id)),
                "pid": run.handle.pid if run.handle else None,
            }
        finally:
            if not launched:
                self._startup_finish(provider.name, node_id, startup_token)

    def _pc_queue_start(self, refused: tuple[ProviderFull, str], agent_name: str,
                        task: str, *, model: str | None, timeout: int | None,
                        workdir: str | None, verifies: str,
                        budget_tag: str) -> dict[str, Any]:
        """PC-R3/R3a: queue a fresh start behind a full provider. The entry
        is everything the start needs to happen later exactly as asked — a
        model pin stays a pin (`pinned`), and `model` is the one it will run
        — and holds no slot of any kind while it waits (PC-R2a)."""
        full, routed_model = refused
        entry = self.tree.enqueue(
            full.provider,
            {"op": "start", "agent": agent_name, "task": task, "timeout": timeout,
             "model": model or routed_model, "pinned": bool(model),
             "workdir": workdir, "verifies": verifies, "budget_tag": budget_tag,
             "parent": self.self_id(), "depth": self.self_depth() + 1},
            str(full), deferred_by=self.self_id(),
            dispatcher=self._owner_fields())
        self._pc_kick(full.provider)
        return {"deferred": True, "reason": str(full), "deferred_id": entry["id"],
                "provider": full.provider, "queued": True,
                "note": f"queued on {full.provider} ({PC_CAUSE}); it starts by "
                        f"itself, in order, when a slot there frees — nothing "
                        f"to re-issue. cancel_deferred removes it."}

    # --------------------------------------------------------------- consume --

    async def _supervise(self, run: Run) -> None:
        """`_consume`, and — whichever way it exits, its setup failing
        included — the end of the post-mortem claim the launch set
        (PC-R3f, round 3 finding 2). The usual end is inside, before the
        release kick; this one only catches what that never reached."""
        try:
            await self._consume(run)
        except asyncio.CancelledError:
            # A stop or a server going away: the claim ends, but nothing is
            # launched from a turn being torn down.
            with contextlib.suppress(Exception):
                self._end_slot_claim(run.node_id, run.slot_token)
            raise
        finally:
            with contextlib.suppress(Exception):
                if self._end_slot_claim(run.node_id, run.slot_token):
                    self._pc_kick(run.provider.name)

    async def _consume(self, run: Run) -> None:
        """Read the event stream, log it, supervise it, record the outcome."""
        node_id, provider, handle = run.node_id, run.provider, run.handle
        assert handle is not None and run.supervisor is not None
        run_dir = self.paths.run_dir(node_id)
        stream_log = _run_open(run_dir, "stream.jsonl",
                               os.O_WRONLY | os.O_APPEND | os.O_CREAT, "a")
        stderr_task = asyncio.create_task(handle.drain_stderr())
        watchdog = asyncio.create_task(self._watch_timers(run))
        capwatch = asyncio.create_task(self._watch_spend_caps(run))
        usage: dict[str, Any] = {}
        cost_total = 0.0
        session_id = ""
        successful_result_response = False
        flush = _FlushGate()
        # Only worth sampling where the agent has a worktree of its own; with
        # no repository the state is always "" and the detector falls back to
        # signatures alone, which is what it did before.
        node = self.tree.get(node_id)
        progress_dir = (Path(node.worktree) if node and node.worktree
                        and gitops.is_repo(Path(node.worktree)) else None)
        last_progress = 0.0
        decision_text = ""
        text_block = ""
        pending_decision: dict[str, str] | None = None

        def decision_pending(*, ended: bool = False) -> bool:
            nonlocal decision_text, pending_decision
            # Event boundaries are not line boundaries. After a marker,
            # wait for the following line too: its default may still be
            # streaming, and an incomplete default must not be recorded.
            end = len(decision_text) if ended else decision_text.rfind("\n") + 1
            complete, decision_text = decision_text[:end], decision_text[end:]
            lines = complete.split("\n")
            if complete.endswith("\n") or not complete:
                lines.pop()
            for line in lines:
                if pending_decision is not None:
                    default = PROPOSED_DEFAULT.search(line)
                    if default:
                        pending_decision["proposed"] = default.group(1).strip()
                    run.awaiting = pending_decision
                    return True
                match = NEED_DECISION.search(line)
                if match:
                    pending_decision = {
                        "topic": match.group(1).strip(),
                        "question": match.group(2).strip(),
                        "proposed": "",
                    }
            if pending_decision is not None:
                prefix = decision_text.lstrip().lower()
                after_default = prefix[len("default"):].lstrip()
                possible_default = (
                    "default".startswith(prefix)
                    or (prefix.startswith("default")
                        and (not after_default or after_default.startswith(":")))
                )
                # Stop at the first incompatible character instead of waiting
                # for a paragraph's newline. Valid prefixes still need a full
                # line before their proposed default can be recorded.
                if ended or not possible_default:
                    run.awaiting = pending_decision
            return run.awaiting is not None

        def follow() -> dict[str, int]:
            # SV-R7: written in the same transaction as the counts it stands
            # for, so a server that dies between the two cannot exist.
            # SC-R2a (#3): never past a charge the ledger has not committed.
            offset = getattr(handle, "offset", 0)
            if run.charge_hold is not None:
                offset = min(offset, run.charge_hold)
            return {"turn": run.turn_start, "offset": offset,
                    "log": _size(run_dir / "stream.jsonl") if stream_log.closed
                    else stream_log.tell()}

        line_end = getattr(handle, "offset", 0)
        # #2: a steer or adoption of this node retries what its earlier turns
        # could not charge, before its own first cost.
        self.__dict__["_pending_seen"] = True
        self._flush_pending(node=node_id)
        try:
            async for line in handle.lines():
                line_start, line_end = line_end, getattr(handle, "offset", 0)
                event = provider.parse_line(line)
                if event is None:
                    continue
                # SV-R6/R7: a line the server before us already accounted for.
                # It rebuilds what this run knows — text, usage, session,
                # step count, loop signatures — and nothing else: it is not
                # logged, counted, or acted on again.
                replayed = (run.replay_to >= 0
                            and getattr(handle, "offset", 0) <= run.replay_to)
                if event.startup_progress and not run.startup_progress:
                    run.startup_progress = True
                    if self.startup.progress(provider.name, node_id, run.startup_token):
                        self.tree.emit(node_id, "provider_recovered", provider=provider.name)
                if event.kind == "result":
                    run.final_result = True

                record = scrub({
                    "t": now(), "kind": event.kind, "name": event.name,
                    "args": event.args, "state": event.state, "status": event.status,
                    "step": event.step, "text": event.text[:2000],
                })
                unhappy_result = (
                    event.kind == "result" and event.status
                    and event.status.upper() not in {"SUCCESS", "OK", "COMPLETED"})
                trailing_result_error = (
                    unhappy_result and not event.text
                    and event.status.upper() not in {"SESSION_LOST", "REFUSED", "TRUNCATED"}
                    and successful_result_response
                    and run.final_status.upper() in {"SUCCESS", "OK", "COMPLETED"})
                if trailing_result_error:
                    record["warning"] = (
                        f"{provider.name} reported {event.status} without a replacement "
                        "response; retaining the successful result")
                if (event.kind == "raw" or unhappy_result) and event.raw:
                    # The whole point of a `raw` event is to show what did not
                    # parse, and the payload was being dropped on the way to
                    # disk. A result event announcing an error is kept for the
                    # same reason: the rules extract a status and a response,
                    # and whatever detail the CLI put beside them is exactly
                    # what someone reading the failure needs — so a run that died right after an unrecognised
                    # line recorded `{"kind": "raw", "text": ""}` and threw the
                    # explanation away. Bounded and scrubbed: this file is not
                    # otherwise redacted, and an unparsed line is exactly where
                    # something unexpected would be.
                    record["raw"] = scrub(json.dumps(event.raw, default=str)[:4000])
                if not replayed:
                    stream_log.write(json.dumps(record) + "\n")
                    stream_log.flush()
                run.events.append(record)

                if (event.kind == "result" and event.text
                        and event.status.upper() in {"SUCCESS", "OK", "COMPLETED"}):
                    successful_result_response = True
                if event.text:
                    run.text_parts.append(event.text)
                    if event.kind == "text" or (event.kind == "result" and not run.final_assistant_message):
                        run.final_assistant_message = event.text
                reported_cost = event.cost
                if event.tokens:
                    usage = _merge_usage(usage, event.tokens, provider.usage_mode)
                    # QH-R20: a declared usage payload can carry its own cost.
                    # Explicit stream.cost remains per-event; an embedded
                    # cost follows the same delta/cumulative mode as usage.
                    embedded_cost = event.tokens.get("cost_usd")
                    if (not event.cost and isinstance(embedded_cost, (int, float))
                            and not isinstance(embedded_cost, bool) and embedded_cost >= 0):
                        previous_cost = cost_total
                        if provider.usage_mode == "delta":
                            cost_total += embedded_cost
                        else:
                            cost_total = max(cost_total, embedded_cost)
                        reported_cost = cost_total - previous_cost
                        usage["cost_usd"] = round(cost_total, 6)
                if event.cost:
                    # Cost is always a per-step amount, whichever way a provider
                    # reports its token counts. This is the same number the
                    # opencode web console shows on its usage page.
                    cost_total += event.cost
                    usage["cost_usd"] = round(cost_total, 6)
                if reported_cost:
                    if provider.billing != "plan" and not self._charge(
                            run, replace(event, cost=reported_cost), event.session_id or session_id,
                            getattr(handle, "offset", 0), replayed, line_start):
                        # SC-R2a: the charge is not in the ledger, so the
                        # checkpoint below holds before this line. Under a
                        # cap that is fail-closed: the run is stopped.
                        if run.cap_stop is None and self._caps(provider.name,
                                                                 run.spec.model or ""):
                            run.cap_stop = self._ledger_unusable(provider.name)
                captured_session = bool(event.session_id and not session_id)
                if captured_session:
                    session_id = event.session_id
                self._qh_observe(run, event)
                if event.session_id and event.status.upper() not in {"SESSION_LOST", "REFUSED", "TRUNCATED", "FAILED"}:
                    self._qh_confirm(run, event.session_id)
                if event.status:
                    if event.status.upper() == "SESSION_LOST":
                        run.requested_session = str(event.raw.get("requested_session") or "")
                    if (not trailing_result_error
                            and run.final_status.upper() not in {"REFUSED", "TRUNCATED"}):
                        run.final_status = event.status
                        if event.status.upper() in {"REFUSED", "TRUNCATED"}:
                            signal = next((f"{path}={get_path(event.raw, path)}"
                                           for rule in provider.stream.get("rules", [])
                                           if rule.get("as") == event.kind
                                           for path, mapping in (rule.get("status_map") or {}).items()
                                           if str(get_path(event.raw, path)) in mapping
                                           and mapping[str(get_path(event.raw, path))] == event.status),
                                          f"status={event.status}")
                            run.refusal_signal = signal
                if replayed:
                    run.supervisor.observe(event)
                    continue
                # SM-R5: the CLI carries on without a server that did not
                # start, and so does the run — but the orchestrator is told,
                # or a consult that never happened has no visible reason.
                failed = (provider.mcp_unavailable(event.raw)
                          if event.raw and not run.server_reported else "")
                if failed:
                    run.server_reported = True
                    self.tree.emit(node_id, "mcp_unavailable", server="multiagents",
                                   status=failed,
                                   detail=f"the multiagents MCP server did not start "
                                          f"(the CLI reports it {failed}); this agent "
                                          f"runs without consult or start_agent")

                # Batched: see TREE_FLUSH_SECONDS. A newly captured session id
                # is flushed immediately regardless, because steer() and
                # answer_question() cannot resume an agent without it.
                batch = flush.add(urgent=captured_session)
                if batch:
                    self.tree.note_event(
                        node_id, steps=run.supervisor.steps or None,
                        usage=usage or None, session_id=session_id or None,
                        events=batch, follow=follow(),
                    )

                # SC-R4: a cap this run's spend reached, or another run's
                # crossing on its scope: stop now; `_finalize` records it.
                if run.cap_stop is not None:
                    await handle.stop()
                    break

                # A decision only a human can make: stop once its following
                # default line is complete, or the message ends.
                if event.kind == "text":
                    # Parts of separate blocks (distinct provider block ids)
                    # are separate lines; parts of one block, or of a provider
                    # declaring no ids, join as-is. Any non-text event already
                    # ends the buffered line (`ended` below).
                    if decision_text and event.block and text_block and event.block != text_block:
                        decision_text += "\n"
                    decision_text += event.text or ""
                    text_block = event.block
                if decision_pending(ended=event.kind != "text"):
                    await handle.stop()
                    break

                # Sampled before observe(), so the signature this event adds is
                # paired with the state of the tree as it stands now. Threaded:
                # a blocking `git` call in this loop would stop draining the
                # process's pipe, which is a deadlock rather than a slowdown.
                if event.kind == "tool" and progress_dir is not None \
                        and time.monotonic() - last_progress >= PROGRESS_SAMPLE_SECONDS:
                    last_progress = time.monotonic()
                    run.supervisor.note_progress(
                        await asyncio.to_thread(_worktree_state, progress_dir,
                                                self.paths.root)
                    )

                trip = run.supervisor.observe(event)
                if trip:
                    self.tree.set_status(node_id, "stuck", f"{trip.reason}: {trip.detail}")
                    self.tree.emit(node_id, "stuck", reason=trip.reason, detail=trip.detail)
                    self._trip_notice(run, trip)
                    run.trip_kind = trip.reason
                    run.trip_signature = run.supervisor.last_digest
                    run.trip_progress = run.supervisor.current_progress
                    run.trip_opaque_calls = run.supervisor.opaque_calls
                elif run.trip_kind:
                    self._maybe_clear_stuck(run, node_id)

            if run.awaiting is None and decision_pending(ended=True):
                await handle.stop()
            code = await handle.wait()
        except asyncio.CancelledError:
            if run.detaching:
                # SV-R3: the server is going and the agent is not. What this
                # server counted is written down with where it got to, and the
                # process is left alone for the next server to follow.
                with contextlib.suppress(Exception):
                    remainder = flush.drain()
                    if remainder:
                        self.tree.note_event(
                            node_id, steps=run.supervisor.steps or None,
                            usage=usage or None, session_id=session_id or None,
                            events=remainder, follow=follow())
                    self.tree.update(node_id, follow=follow())
                raise
            if run.internal_stop:
                remainder = flush.drain()
                self.tree.note_event(node_id, steps=run.supervisor.steps or None,
                                     usage=usage or None, session_id=session_id or None,
                                     events=remainder, follow=follow())
            await handle.stop()
            # Both an explicit stop_agent() and the event loop shutting down
            # arrive here as a CancelledError, but they mean different things
            # and only one of them is anybody's decision. Recording both as
            # "cancelled by parent" makes a session ending look like a
            # deliberate kill, which is genuinely misleading when reading back
            # a log later.
            #
            # A third case arrives here too: steer() ends the current turn to
            # respawn the same run under the same id, and `run.internal_stop`
            # is how it is told apart from the other two. Writing "cancelled"
            # for that case — even briefly, before steer's own corrective
            # write lands — is bug-8195f2: a reader polling in the gap sees a
            # run that is being resumed reported as terminally ended, with a
            # reason blaming a parent that called nothing.
            if run.internal_stop:
                remainder = flush.drain()
                self.tree.note_event(node_id, steps=run.supervisor.steps or None,
                                     usage=usage or None, session_id=session_id or None,
                                     events=remainder, follow=follow())
            if not run.internal_stop:
                reason = ("stopped by parent" if run.stop_requested
                          else "interrupted: the server exited while this agent was running")
                # Deliberately NOT committing here. gitops shells out with a
                # two-minute timeout, and a git call in a teardown running on a
                # closing event loop can hang the shutdown it is part of. The work
                # is preserved instead by whoever cleans up afterwards — `run`
                # reconciles interrupted agents and `multiagents stop` commits
                # before it ends them — both with time, a live loop, and enough
                # information to label the commit as an interruption rather than a
                # result.
                if node_id in self._holds:
                    self._defer_while_held(node_id, "cancelled", reason)
                else:
                    self.tree.set_status(node_id, "cancelled", reason)
                    self._release(node_id)
                with contextlib.suppress(Exception):
                    notices.clear_node(self.tree, node_id)        # LN-C4
            raise
        except Exception as exc:
            self.tree.set_status(node_id, "failed", f"{type(exc).__name__}: {exc}")
            code = -1
        finally:
            watchdog.cancel()
            capwatch.cancel()
            # RC-R2 (bug-1213a0): stderr is drained to EOF before the run is
            # classified, so the verdict does not depend on how much of it the
            # async drain got to before the process was reaped. Bounded: a
            # grandchild still holding the pipe must not hold the post-mortem
            # — on timeout wait_for cancels the drain, as the bare cancel did.
            with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(stderr_task, 2.0)
            stream_log.close()
            if not run.detaching and (run.stop_requested
                    or asyncio.current_task().cancelling()):
                self._startup_finish(provider.name, node_id, run.startup_token)

        # Everything after the stream is guarded, because nothing else releases
        # this run. `consult` waits on `run.done` for the agent's whole timeout
        # and then reports "no reply within Ns" — an unresponsive-agent verdict
        # for a crash in our own post-mortem, accusing the agent of a fault that
        # was ours. Mistaking our own silence for the other side's is the exact
        # shape of error this project has already been caught by twice.
        relaunched = False
        # PC-R3f: the run keeps its slot through its post-mortem — commit-fix
        # turns and the free retry relaunch the same node from it — by the
        # claim activated here, before any post-mortem work (and before
        # calling `_finalize`, which may itself await), ended below.
        try:
            if not await self._begin_slot_claim(run):
                return
            remainder = flush.drain()
            if remainder:
                self.tree.note_event(node_id, steps=run.supervisor.steps or None,
                                     usage=usage or None, session_id=session_id or None,
                                     events=remainder, follow=follow())
            relaunched = await self._finalize(run, code, usage, session_id)
        except Exception as exc:
            # The traceback is the only thing that makes this debuggable, and
            # the node's reason can hold one line. Written where someone
            # reading a bad run already looks.
            with contextlib.suppress(OSError):
                _run_write(run_dir, "postmortem-crash.txt", traceback.format_exc())
            # Only a node still claiming to be in flight. A crash in the last
            # few lines — filing a ticket, reclaiming a worktree — must not
            # overwrite a verdict already recorded: `failed` over `done`
            # discards work that is merged and correct, over `awaiting_user`
            # loses the question, over `limited` loses a resumable session.
            # `detached` and `stuck` too (SV-R6): an adopted node can still
            # carry either when its post-mortem runs, and both are statuses
            # adoption picks up again — left as they are, a finalisation that
            # crashes would be retried every pass, forever.
            node = self.tree.get(node_id)
            if node and node.status in ("running", "pending", "steered",
                                        "detached", "stuck"):
                self.tree.set_status(
                    node_id, "failed",
                    f"the post-mortem crashed: {type(exc).__name__}: {exc}")
        finally:
            # This run's own claim: its process has ended, and a relaunch's
            # claim is a different token (a held one is kept by the hold).
            self._startup_finish(provider.name, node_id, run.startup_token)
            with contextlib.suppress(Exception):
                self._end_slot_claim(node_id, run.slot_token)
            if not relaunched and not self._held(node_id):
                self._release(node_id)
                # LN-C4: the node ended, so its own notices have stopped.
                with contextlib.suppress(Exception):
                    notices.clear_node(self.tree, node_id)
                run.done.set()
                # PC-R3a: the process that releases a slot drains the queue.
                self._pc_kick(provider.name)
            # RM-R1c (review ag-f7ced5): while a launch cleanup holds this
            # node — a relaunch (free retry, commit fix) whose process could
            # not be confirmed dead — the supervision lock and the waiters
            # stay: releasing either would hand the run over while its
            # process may still be alive. The hold's end releases both.

    def _maybe_clear_stuck(self, run: Run, node_id: str) -> None:
        """SL-R3: drop `stuck` the moment the agent visibly moves on.

        Never restarts, steers or otherwise touches the agent — only relabels
        a node the run itself is already changing. Three kinds of evidence
        count, matched to why each trip fired in the first place:

        * `silence` clears on ANY stream event at all — the trip was exactly
          "nothing arrived", so anything arriving answers it.
        * `doom_loop`/`runaway_steps` clear on a different tool-call signature
          (a genuinely new call, not the same one reported twice), an opaque
          tool call (SL-R6: its signature is unknowable, so it can never be
          confirmed identical to the call that tripped), or on the working
          tree moving — the same kinds of evidence the watchdog itself uses
          to tell "repeating" from "working".
        """
        node = self.tree.get(node_id)
        if node is None or node.status != "stuck":
            # Something else already moved this node off `stuck` — a steer, a
            # stop — so the trip state this run is carrying no longer
            # describes it. Drop it here rather than paying a tree.get() on
            # every remaining event of a run that can never be `stuck` again
            # under this trip.
            run.trip_kind = ""
            run.trip_signature = ""
            run.trip_progress = ""
            run.trip_opaque_calls = 0
            return
        supervisor = run.supervisor
        assert supervisor is not None
        cleared = (
            run.trip_kind == "silence"
            or supervisor.last_digest != run.trip_signature
            or supervisor.current_progress != run.trip_progress
            or supervisor.opaque_calls != run.trip_opaque_calls
        )
        if cleared:
            self.tree.set_status(node_id, "running")
            notices.clear_node(self.tree, node_id, "stuck")      # LN-C4
            run.trip_kind = ""
            run.trip_signature = ""
            run.trip_progress = ""
            run.trip_opaque_calls = 0

    def _trip_notice(self, run: Run, trip) -> None:
        """LN-C1/C6: a watchdog or supervisor trip is a limit notice, keyed by
        where the value in force came from. Reported, never enforced (SV-R4).

        A commit-fix turn's wall clock is `limits.commit_fix_timeout`, which
        its loop reports itself."""
        if run.fix_turn or run.supervisor is None:
            return
        node_id = run.node_id
        agent = run.spec.name
        # LN-C2, finding 8: prefer the limits (and their provenance) captured
        # at the launch still in flight — `run.limits` for a run this server
        # launched (adoption fills it from the same record), the host-owned
        # launch record for one it did not. The node's own `limits` block in
        # `tree.json` is container-writable and never consulted (adversary
        # finding 3); with no record at all the limits are resolved from the
        # config as it is NOW, and the provenance reported is that
        # resolution's — an honest "where it comes from today", not a
        # fabricated launch-time claim.
        limits = run.limits
        if not limits:
            limits = self.launch_limits.lookup(node_id)
        if not limits:
            limits = self._limits_detail(run.spec.name,
                                         self._limits_for(node_id, run.spec))
        if trip.reason in ("timeout", "silence"):
            name = "timeout" if trip.reason == "timeout" else "silence_timeout"
            limit_key = "limits." + ("default_timeout" if name == "timeout"
                                     else "silence_timeout")
            entry = limits.get(name) or {}
            value = entry.get("value")
            layer = entry.get("source")
            detail = entry.get("source_detail")
            if layer == "call":
                key, source = limit_key, detail or notices.call_source(name, limit_key)
            elif layer == "agent":
                key = f"agents.{agent}.{name}"
                source = detail or notices.provenance(self.config, key, value, "agent")
            else:
                key = limit_key
                source = detail or notices.provenance(self.config, key, value, layer)
        elif trip.reason == "runaway_steps":
            value = run.supervisor.max_steps
            entry = limits.get("max_steps") or self._max_steps_limit(
                run.spec, run.provider.name)
            key = (f"agents.{agent}.max_steps" if entry.get("source") == "agent"
                   else "limits.max_steps")
            source = entry["source_detail"]
        elif trip.reason == "doom_loop":
            value = run.supervisor.loop_repeats
            key = "limits.doom_loop_repeats"
            source = notices.provenance(self.config, key, value)
        else:
            return
        self._notice(key, value, "stuck", node_id, source,
                     f"{node_id} ({agent}) reported stuck: {trip.detail}",
                     "raise it there if runs like this need more", node=node_id)

    @staticmethod
    def _with_trip(prior_stuck: str, reason: str) -> str:
        """SL-R2: fold a run's last trip into its terminal reason.

        `stuck` itself does not survive past the run ending, so this is the
        only place the trip stays readable afterwards — inline with whatever
        the classification itself has to say, not replacing it.
        """
        if not prior_stuck:
            return reason
        if not reason:
            return f"was stuck: {prior_stuck}"
        return f"{reason} (was stuck: {prior_stuck})"

    async def _finalize(self, run: Run, code: int | None, usage: dict[str, Any],
                        session_id: str) -> bool:
        """Decide what a finished run meant, and record it.

        Split from `_consume`, which is the pump. Nothing here reads the
        stream: the process is already gone, and every line below runs once.
        The two were one 374-line method whose subject changed halfway, which
        is also why the outcome logic — the part carrying most of the hard-won
        reasoning in this file — could only be reached by starting a process.

        True means this run has been RELAUNCHED and is not over, so the caller
        must not release `run.done`. Said as a return value rather than left to
        the order of statements: the relaunch used to work by returning early
        past the line that set it, which made "nothing may follow this call" a
        rule a reader had to know.
        """
        node_id = run.node_id
        self.tree.update(node_id, turn_ended_at=now())
        if run.fix_turn:
            return await self._finalize_fix_turn(run, code, usage)
        # SV-R4: the wrapper ended it at its wall clock. SV-R6: the process is
        # gone and left no exit status — killed with its wrapper, or the file
        # lost — so the stream is the only evidence: a run that got as far as
        # the provider's own result event is judged by it as if it exited 0,
        # one that did not has nothing to be judged by.
        timed_out = bool(getattr(run.handle, "timed_out", False))
        unrecorded = code is None
        if unrecorded:
            code = 0 if run.final_result else -1
        # SL-R1/SL-R2: `stuck` is a label on a run still in flight, not a
        # verdict — once the run is over it must get the SAME terminal status
        # it would have gotten had it never tripped, with the trip folded into
        # the reason so it stays visible to whoever reads the node afterwards.
        stuck_before = self.tree.get(node_id)
        prior_stuck = (stuck_before.reason
                      if stuck_before and stuck_before.status == "stuck" else "")
        run_dir = self.paths.run_dir(node_id)
        text = "\n".join(run.text_parts).strip()
        stderr = run.handle.stderr_tail if run.handle else ""
        # Checked first and unconditionally. Stopping the process makes wait()
        # return a signal code, which _classify would read as "failed"; and a
        # silence trip in the window before exit would otherwise leave the node
        # `stuck` with the question invisible.
        status = "awaiting_user" if run.awaiting else self._classify(run, code, text, stderr)
        if (not run.awaiting and status not in {"refused", "truncated"}
                and (timed_out or (unrecorded and not run.final_result))):
            status = "failed"
        # SV-R10: `cancelled` here was written by someone else — this run's own
        # stop never reaches `_finalize` — so the exit being judged is that
        # stop's kill, and the stop is the verdict.
        stopped_elsewhere = bool(stuck_before and stuck_before.status == "cancelled")
        if stopped_elsewhere:
            status = "cancelled"
        if run.oom_reader is not None:
            if code in (137, -9) and not stopped_elsewhere and not timed_out:
                # Before `forget`: the notice asks whether any sibling was
                # alive during this run's life, and this run's own entry is
                # what keeps ended siblings in the record (adversary
                # finding 1). Forgetting first would empty the container and
                # answer "alone" for a run that shared it.
                with contextlib.suppress(Exception):
                    await self._sigkill_notice(run, code)
            self.occupancy.forget(run.oom_container, node_id)

        if not stopped_elsewhere and self._cap_verdict_of(run, status):
            # SC-R4a: the cap's verdict — resumable `limited`, never fed to the
            # failure breaker, never a provider cooldown, never retried or
            # moved to a fallback.
            status, limited = "limited", run.cap_stop
            self._cap_stopped_event(run)
        elif not stopped_elsewhere:
            status, limited = await self._provider_health_after(run, status, text, stderr)
        else:
            limited = None

        session_lost = (status == "failed" and bool(run.requested_session))
        if session_lost:
            session_id = ""

        # QH-R8/R9: transfer before the generic final commit so dirty work
        # reaches a continuation unchanged. The old wrapper is already dead.
        if not stopped_elsewhere and not run.awaiting:
            handover = await self._qh_after(run, status, usage, session_id, limited)
            if handover is True:
                return True
            if isinstance(handover, dict):
                # All targets rejected the resume. Keep the source's session,
                # quota verdict and worktree instead of filing session loss.
                status, limited = "limited", handover["limited"]
                session_id, usage = handover["session_id"], handover["usage"]
                session_lost = False

        # Commit anything the agent left uncommitted so no work is stranded on
        # an unreferenced worktree. Skipped while parked on a question: the
        # agent is mid-thought and will resume in the same worktree, and a
        # commit per question would both add noise and change what
        # _drop_if_empty decides for every later run.
        node = self.tree.get(node_id)
        from .scheduler.sessions import retains_checkout
        if (node and node.branch and Path(node.worktree).is_dir() and not run.awaiting
                and not retains_checkout(self.paths, node)):
            # CI-R7: off the event loop — a git hook is the agent's code, and
            # nothing it does may freeze every other run this server supervises.
            commit_result = await asyncio.to_thread(
                gitops.commit_all,
                Path(node.worktree), f"{node.agent}: work in progress ({node_id})",
                role=node.agent, agent_id=node_id, git=self.agent_git(node))
        else:
            commit_result = None

        # CI-R5: a commit a git hook refused goes back to the agent, in the
        # same session and worktree, before anything here is recorded — so
        # the node stays `running` and `result.json` is written once.
        # RC-R3 (review ag-2d1240): a refused turn is never resumed either —
        # the provider answered and declined the prompt. Its commit failure
        # is still reported below, as CI-R2 would.
        fix_attempts = 0
        fix_error = ""
        if (commit_result is not None and not commit_result.ok and commit_result.hook
                and not stopped_elsewhere and not session_lost
                and status not in ("limited", "quota", "unauthenticated", "refused")
                and session_id and run.provider.spawn.get("resume")):
            commit_result, fix_attempts, ended_by, fix_usage, fix_cut = \
                await self._commit_fix_loop(run, node, commit_result, session_id)
            if ended_by in ("stopped", "steered", "detached"):
                # The orchestrator's action is handled as for any live run: a
                # stop has already recorded `cancelled`, a steer has a new run
                # of its own (with its own loop), and a detach is adoption's.
                # Only a stop has released this run; the others still hold it.
                return ended_by != "stopped"
            usage = _merge_usage(usage, fix_usage, run.provider.usage_mode)
            if fix_cut is not None:
                # A status that tells the orchestrator to wait, to
                # re-authenticate — or, since RC-R3, one the provider refused —
                # wins over the commit failure, which is still reported: the
                # run ends as the cut would end any run, and a `limited` one
                # stays resumable.
                status, limited = fix_cut["status"], fix_cut["limited"]
                fix_error = fix_cut.get("error") or ""
                if fix_cut.get("requested_session"):
                    session_lost = True
                    run.requested_session = fix_cut["requested_session"]
                    session_id = ""
                if fix_cut.get("cap_stop") is not None:
                    # SC-R4a (#5): a fix turn a cap stopped ends the run as a
                    # cap stop, with the same verdict.
                    run.cap_stop = fix_cut["cap_stop"]
                    self._cap_stopped_event(run)

        # A run that ends with nothing to say still ended for a reason.
        said_nothing = not text.strip()
        if status not in ("done", "merged", "awaiting_user") and said_nothing:
            text = self._no_output_summary(run, code)

        # CI-R2: a failed end-of-run commit must never be a silent no-op —
        # the agent's work would otherwise be stranded, uncommitted, with
        # nothing telling anyone. Reported here rather than by changing
        # `status`: a commit failure doesn't by itself make the run failed.
        # BA-R3 (bug-ba55a9): the note names the run's own work-in-progress
        # commit and says it was refused — "commit failed" alone read as the
        # agent's own commit having failed, and said nothing about why.
        if commit_result is not None and not commit_result.ok:
            detail = commit_result.err or commit_result.out
            # CI-R4: bound the git output carried into the result text, so a
            # noisy hook can't bury the agent's own answer.
            detail_for_text = detail if len(detail) <= 500 else detail[:500] + " [truncated]"
            text = (f"{text}\n\nthe work-in-progress commit was refused: "
                    f"{detail_for_text}").strip()
            if fix_attempts:
                text += (f"\n(the agent was resumed {fix_attempts} time(s) to "
                         f"satisfy the git hook; it still refused the commit)")
            self.tree.emit(node_id, "commit_failed", detail=detail[:400])
        elif fix_attempts:
            text = (f"{text}\n\ncommit: a git hook refused the end-of-run commit; "
                    f"it succeeded after {fix_attempts} fix attempt(s).").strip()

        if fix_error:
            text = f"{text}\n\ncommit-fix resume refused: {fix_error}".strip()
        summary = text[-MAX_SUMMARY_CHARS:] if text else ""
        record = {
            "status": status, "exit_code": code, "session_id": session_id,
            "turn_started_at": run.launched_at, "usage": usage, "text": text, "stderr_tail": stderr,
        }
        warnings = [event["warning"] for event in run.events if event.get("warning")]
        if warnings:
            record["warnings"] = warnings
        if fix_error:
            record["reason"] = fix_error
        if session_lost:
            record.update(reason="session_lost", requested_session=run.requested_session)
        finished_node = self.tree.get(node_id)
        if finished_node:
            record.update(elapsed_seconds=round(finished_node.turn_elapsed()),
                          node_elapsed_seconds=round(finished_node.elapsed()))
        if run.refusal:
            # RC-R2 (review ag-997df9 finding 3): the verdict's evidence is in
            # the result artifact too, not only the reason and the events —
            # result.json is what survives the tree.
            record["refusal"] = run.refusal
        _run_write(run_dir, "result.json", json.dumps(scrub(record), indent=2))

        self.tree.update(node_id, usage=self._qh_total_usage(node_id, usage), session_id=session_id, summary=summary[:2000],
                         requested_session=run.requested_session if session_lost else "",
                         warnings=warnings)
        # Filed even when the run failed: a partial write-up of a real defect is
        # worth more than a lost one, and the orchestrator can see the status.
        # The last verdict wins, for the same reason the last TICKET does: a
        # verifier reasoning about the format may quote it before giving one.
        verdicts = list(VERDICT.finditer(text or ""))
        if verdicts and not run.awaiting:
            found = verdicts[-1]
            self.tree.update(
                node_id,
                verdict=found.group(1).lower(),
                defects=int(found.group(2)) if found.group(2) else 0,
            )

        filed = self._file_tickets(node_id, text) if not run.awaiting else []
        if filed:
            run.tickets = [{k: t[k] for k in ("id", "severity", "title", "status")}
                           for t in filed]
            run.ticket = run.tickets[-1]
        retry_error = ""
        retry_transport_refused = False
        if stopped_elsewhere:
            pass                          # its reason is the stopper's to give
        elif run.awaiting:
            question = self.tree.add_question(
                node_id, run.awaiting["topic"], run.awaiting["question"],
                run.awaiting["proposed"],
            )
            self.tree.update(node_id, summary=summary[:2000])
            self.tree.set_status(
                node_id, "awaiting_user",
                f"needs a decision on {run.awaiting['topic'] or 'something'} ({question['id']})",
            )
        elif status == "unauthenticated":
            self.tree.set_status(
                node_id, "failed",
                self._with_trip(prior_stuck,
                    f"{run.provider.name} is not authenticated — "
                    f"run: multiagents auth login {run.provider.name}"),
            )
            self.tree.emit(node_id, "unauthenticated", provider=run.provider.name)
        elif status == "quota":
            cooldown = now() + float(
                self.config.project.get("budget", {}).get("blind_cooldown_seconds", 900)
            )
            self.tree.set_cooldown(run.provider.name, cooldown, "quota failure during run",
                                   cause="quota")
            self.tree.set_status(node_id, "failed", self._with_trip(prior_stuck, "quota exhausted"))
        elif session_lost:
            self.tree.set_status(node_id, "failed", "session_lost")
        elif run.spec.conversational and status == "done":
            # A conversation is not finished just because a turn is. Park it as
            # idle so the session stays resumable for the next question.
            self.tree.set_status(node_id, "idle", self._with_trip(prior_stuck, ""))
        else:
            # One free retry for a cheap, unexplained death — a crash with
            # nothing to say, gone before it did any work. That shape is a
            # transient glitch far more often than a real fault, and making
            # the orchestrator handle it means a model reasoning about
            # infrastructure.
            #
            # Bounded by cost, which is where I part company with the advice to
            # retry any such failure: a run that died at 996 seconds had spent
            # 5.5M tokens, and silently spending that again is not absorbing a
            # glitch. Past the threshold it is reported and handed back.
            #
            # SL-R1: applies exactly as it would have had the run never
            # tripped — a trip does not disqualify a death from being cheap
            # and unexplained.
            fresh = self.tree.get(node_id)
            # A timeout or a lost exit status is not a cheap glitch: the first
            # spent the whole wall clock, the second is not known to have died.
            if (status == "failed" and said_nothing
                    and not timed_out and not unrecorded
                    and fresh and not fresh.node_id and not fresh.retries
                    and fresh.turn_elapsed() < float(self.config.limits.get(
                        "retry_silent_failure_under_seconds", 60))):
                transport_error = self._transport_refusal(run.provider)
                if transport_error:
                    retry_error = f"free retry refused: {transport_error}"
                    retry_transport_refused = True
                    self.tree.emit(node_id, "retry_failed", detail=transport_error)
                else:
                    self.tree.emit(node_id, "retrying",
                                   reason=f"died in {fresh.turn_elapsed():.0f}s with no output")
                    # Counted on the NODE, not on the Run: _launch replaces the Run,
                    # so a flag kept there resets on every retry and one free retry
                    # becomes an unbounded loop. Found by running it.
                    self.tree.update(node_id, retries=fresh.retries + 1)
                    # PC-R3: the retry is an admission like any launch. With no
                    # slot on its provider it is queued there, and the node,
                    # whose process has ended, holds none while it waits.
                    # Continuity for whoever is waiting on the attempt that
                    # just died: `_launch` starts every relaunch with a fresh
                    # `Run`, but a caller that captured this run (consult(),
                    # or a test driving the agent directly) before the retry
                    # must still be woken when the SECOND attempt finishes,
                    # not left waiting on an event nothing will ever set. Handed
                    # in at construction, not assigned after the fact: `_launch`
                    # publishes the new Run to `self.runs[node_id]` before it
                    # returns, and a post-hoc `retried.done = run.done` would
                    # leave a window where a concurrent reader gets a Run whose
                    # `done` nobody but this line will ever fix up.
                    try:
                        admission = self._pc_retry_admission(run, fresh, session_id)
                        if admission is not None:
                            if admission.get("refused"):
                                raise RuntimeError(admission["refused"])
                            return False
                        # CW-R2: a retry is not a new admission; past the safe
                        # point it waits, and a server that exits meanwhile
                        # leaves the node to the next one's adoption.
                        with await self.gate.enter_when_open():
                            retried = await self._launch(
                                node_id=node_id, spec=run.spec, provider=run.provider,
                                prompt=self._retry_prompt(node_id, run.prompt_file),
                                workdir=Path(fresh.worktree), branch=fresh.branch,
                                parent=fresh.parent, depth=fresh.depth,
                                session_id=session_id or None,
                                done=run.done,
                            )
                    except Exception as exc:
                        # A retry that cannot even start must not propagate out of
                        # `_finalize` — `_consume` would read that as the
                        # post-mortem itself crashing — but neither may it be
                        # silent: the `retrying` event above already promised a
                        # relaunch, and a swallowed failure here used to leave the
                        # node ending with the FIRST death's reason (`exited 1`)
                        # as if no retry had ever been attempted. Recorded like a
                        # failed launch anywhere else: `failed`, with the cause.
                        retry_transport_refused = isinstance(exc, TransportRefused)
                        detail = (str(exc) if retry_transport_refused
                                  else f"{type(exc).__name__}: {exc}"[:300])
                        self.tree.emit(node_id, "retry_failed", detail=detail)
                        retry_error = f"retry launch failed: {detail}"
                    else:
                        retried.startup_progress = retried.startup_progress or run.startup_progress
                        self.tree.set_status(node_id, "running", "retried once after "
                                             "an unexplained early exit")
                        return True

            # Say why, when the provider told us. Three real failures ended with
            # agy emitting {"kind": "result", "status": "ERROR"} — a structured
            # verdict, which _classify read to decide "failed" and then dropped,
            # leaving the orchestrator a node marked failed with an empty
            # reason and nothing to act on.
            reason = ""
            if status == "limited":
                # The provider's own words, when it comes back, and what this
                # run spent getting there. The last one matters more than it
                # looks: on the day this was written, two opus agents filled a
                # freshly-reset five-hour window in THIRTEEN MINUTES, were
                # restarted on the same tasks the moment it reopened, and filled
                # it again. Nothing told the orchestrator that the pair costs a
                # whole window, so it had no way to know not to start both.
                spent = (usage or {}).get("cost_usd") or 0
                node = self.tree.get(node_id)
                started = getattr(node, "started_at", 0) or now()
                ran = (now() - started) / 60
                cost = f", after {ran:.0f}m" + (f" and ${spent:.2f}" if spent else "")
                reason = limited["reason"] + (
                    f"{cost} — back at "
                    f"{time.strftime('%H:%M', time.localtime(limited['until']))}. "
                    f"RESUMABLE: steer_agent({node_id!r}, ...) continues this "
                    f"session on its branch. Reissuing the task instead pays "
                    f"for the whole conversation again — measured at 7.2M "
                    f"cached tokens on a run that had cost 173k.")
            elif status == "refused":
                reason = f"{run.provider.name} {run.refusal_signal or 'reported refusal'}"[:200]
            elif status == "truncated":
                reason = (
                    f"{run.provider.name} stopped its own turn at the time limit "
                    f"and returned partial output. The branch holds real but "
                    f"UNFINISHED work and has not been merged. RESUMABLE: "
                    f"steer_agent({node_id!r}, ...) continues this session on "
                    f"its branch, which is far cheaper than reissuing the task.")
            elif status == "failed" and timed_out:
                wall = run.supervisor.wall_timeout if run.supervisor else 0
                reason = (f"timeout: ended at its {wall:.0f}s wall clock" if wall
                          else "timeout: ended at its wall clock")
            elif status == "failed" and unrecorded and not run.final_result:
                reason = ("process ended without an exit status, before the "
                          "provider reported a result")
            elif status == "failed":
                if retry_error:
                    reason = retry_error
                elif fix_error:
                    reason = f"commit-fix resume refused: {fix_error}"
                elif run.final_status and run.final_status.upper() not in {
                        "SUCCESS", "OK", "COMPLETED"}:
                    reason = f"{run.provider.name} reported {run.final_status}"
                elif code != 0:
                    reason = f"exited {code}"
                else:
                    reason = "produced no output"
            self.tree.set_status(node_id, status, self._with_trip(prior_stuck, reason))

        # RC-R3 (bug-1213a0): a refusal finishes the startup claim as a
        # success — the provider answered — and so resolves a half-open probe
        # it held, rather than re-arming the outage for another cooldown.
        self._startup_finish(run.provider.name, node_id, run.startup_token,
                             failed=status == "failed" and not run.startup_progress
                             and not run.stop_requested and not timed_out
                             and not retry_transport_refused,
                             resolved=status == "refused" or retry_transport_refused,
                             error=(stderr or text).splitlines()[0] if (stderr or text) else status)

        # Auto-merge this agent's own children upward: their work is still
        # quarantined on this agent's branch, so nothing real has changed yet.
        if status == "done" or retry_transport_refused:
            # Children first: this agent's branch should carry their work when
            # it is itself merged upward, rather than stranding it.
            await self._merge_pending_children(node_id)
            if status == "done":
                await self._maybe_merge_into_parent(node_id)

        if (not run.awaiting and not stopped_elsewhere and run.cap_stop is None
                and not session_lost):
            # A parked agent still owns its worktree and will resume in it, and
            # a stopped one is left as a stop leaves it: resumable — a cap
            # stop included, whose branch and worktree are kept (SC-R4, #11).
            self._drop_if_empty(node_id, run.spec)
        return False

    async def _commit_fix_loop(self, run: Run, node: Node, failed: gitops.GitResult,
                               session_id: str
                               ) -> tuple[gitops.GitResult, int, str, dict, dict | None]:
        """CI-R5: resume the agent until the hook accepts the commit, or the
        attempts run out.

        Each fix turn is a relaunch of this same run, as `_finalize`'s free
        retry is: it shares `run.done`, and its own `_finalize` records a
        verdict and hands back to here rather than finishing the run.

        Returns the last commit result, the attempts made, how the loop ended
        ("" when it ran its course, else "stopped", "steered" or "detached"),
        the fix turns' usage, and — when a fix turn was cut off by its
        provider (`limited`, `quota`, `unauthenticated`, `refused`) — that
        turn's verdict, whose status becomes the run's.
        """
        node_id = run.node_id
        limits = self.config.limits
        allowed = int(limit_number(limits, "commit_fix_attempts", zero_ok=True))
        wall = int(limit_number(limits, "commit_fix_timeout"))
        result, attempt, usage = failed, 0, {}
        while not result.ok and result.hook and attempt < allowed:
            transport_error = self._transport_refusal(run.provider)
            if transport_error:
                self.tree.emit(node_id, "commit_fix_failed", detail=transport_error)
                return result, attempt, "", usage, {
                    "status": "failed", "limited": None, "error": transport_error}
            attempt += 1
            output = result.err or result.out
            self.tree.emit(node_id, "commit_fix_attempt", attempt=attempt,
                           hook=result.hook, detail=output[-400:])
            try:
                # CW-R2: like the free retry, waits out a safe point.
                with await self.gate.enter_when_open():
                    fix = await self._launch(
                        node_id=node_id, spec=run.spec, provider=run.provider,
                        prompt=COMMIT_FIX.format(
                            hook=result.hook, attempt=attempt, allowed=allowed,
                            output=output[-COMMIT_FIX_OUTPUT_CHARS:]),
                        workdir=Path(node.worktree), branch=node.branch,
                        parent=node.parent, depth=node.depth,
                        session_id=session_id, timeout=wall, done=run.done,
                    )
            except SpendCapRefused as exc:
                # SC-R4a (#3): a cap refused the fix turn's spawn — the run
                # ends as a cap stop, `limited` and resumable, never `done`.
                self.tree.emit(node_id, "commit_fix_failed",
                               detail=f"refused at spawn: {exc}"[:400])
                return result, attempt, "", usage, {
                    "status": "limited", "limited": exc.refusal, "cap_stop": exc.refusal}
            except TransportRefused as exc:
                self.tree.emit(node_id, "commit_fix_failed", detail=str(exc))
                return result, attempt, "", usage, {
                    "status": "failed", "limited": None, "error": str(exc)}
            except Exception as exc:
                self.tree.emit(node_id, "commit_fix_failed",
                               detail=f"could not resume: {type(exc).__name__}: {exc}"[:400])
                break
            # Set before anything yields: `_launch` has only scheduled the
            # task that will read it.
            fix.fix_turn = True
            # Bounded here: while this server supervises a run, its wall clock
            # is only reported, never enforced. Ending the process lets the
            # turn finish through `_consume` like any other, so it is still
            # accounted — and it counts as an attempt.
            finished, _ = await asyncio.wait({fix.task}, timeout=wall)
            if not finished and fix.handle is not None:
                fix.fix_timed_out = True
                await fix.handle.stop()
                await asyncio.wait({fix.task})
                key = "limits.commit_fix_timeout"
                self._notice(key, wall, "stopped", node_id,
                             notices.provenance(self.config, key, wall),
                             f"fix attempt {attempt} of {node_id} cut short",
                             "raise it there if a fix needs longer", node=node_id)
            if fix.detaching:
                return result, attempt, "detached", usage, None
            if fix.stop_requested:
                return result, attempt, ("steered" if fix.internal_stop else "stopped"), \
                    usage, None
            verdict = fix.fix_verdict
            if verdict is None:
                break                                  # its post-mortem crashed
            usage = _merge_usage(usage, verdict["usage"], run.provider.usage_mode)
            fresh = self.tree.get(node_id)
            if fresh is not None and fresh.status == "cancelled":
                # SV-R10: stopped from another process.
                return result, attempt, "stopped", usage, None
            if verdict.get("requested_session"):
                return result, attempt, "", usage, verdict
            if verdict["status"] in ("limited", "quota", "unauthenticated"):
                return result, attempt, "", usage, verdict   # cannot be resumed again
            if verdict["status"] == "refused":
                # RC-R3 (bug-1213a0, review ag-997df9 finding 1): a refusal is
                # not retried either — the provider answered and declined the
                # turn. The loop ends here and the refusal becomes the run's
                # verdict: the commit could not be fixed because the provider
                # refused, and reporting `done` over that buries it. The
                # evidence lives on the fix turn's Run; this run's record
                # carries it (result.json, reason, events).
                run.refusal = fix.refusal
                run.refusal_signal = fix.refusal_signal
                return result, attempt, "", usage, verdict
            if verdict["status"] == "awaiting_user":
                # The fix turn asked rather than fixed: the question is the
                # run's, as an ordinary turn's is, and nothing is committed
                # while the agent is parked mid-thought.
                run.awaiting = fix.awaiting
                return result, attempt, "", usage, verdict
            # CI-R7: off the event loop, as `_finalize`'s own commit is.
            result = await asyncio.to_thread(
                gitops.commit_all,
                Path(node.worktree), f"{node.agent}: work in progress ({node_id})",
                role=node.agent, agent_id=node_id, git=self.agent_git(node))
        else:
            if not result.ok and result.hook:
                # LN-C6: exhaustion only. The `else` arm runs when the loop's
                # own condition gave out — every attempt was used, or none was
                # allowed, which exhausts a ceiling of zero just the same
                # (finding 4) — never when a relaunch failed mid-loop.
                key = "limits.commit_fix_attempts"
                what = (f"{node_id} used all {allowed} fix attempt(s) and the "
                        f"{result.hook} hook still refuses its commit" if allowed
                        else f"no fix attempt was allowed for {node_id} and the "
                             f"{result.hook} hook refuses its commit")
                self._notice(key, allowed, "stopped", node_id,
                              notices.provenance(self.config, key, allowed),
                              what, "raise it there to allow more", node=node_id)
        return result, attempt, "", usage, None

    async def _finalize_fix_turn(self, run: Run, code: int | None,
                                 usage: dict[str, Any]) -> bool:
        """The end of a CI-R5 fix turn: what the loop needs, nothing more.

        Accounted like any turn — its usage, and what it says about its
        provider — but a turn cut off by `commit_fix_timeout` is our own
        bound, not the provider's failure, and is not held against it.
        Returns True: the run is not over, the loop that launched it is.
        """
        timed_out = run.fix_timed_out or bool(getattr(run.handle, "timed_out", False))
        text = "\n".join(run.text_parts).strip()
        stderr = run.handle.stderr_tail if run.handle else ""
        limited = None
        if run.awaiting:
            # Checked first, as in `_finalize`: the stop that followed the
            # question is not a failure. A question from a fix turn parks the
            # run like any other (CI-R5, after adversarial tester ag-6ceb2b).
            status = "awaiting_user"
        elif run.cap_stop is not None or (
                not timed_out and self._cap_verdict_of(
                    run, self._classify(run, -1 if code is None else code, text, stderr))):
            # SC-R4a (#5): a fix turn a cap stopped — or that failed while its
            # cap binds — is a cap stop: `limited`, never the provider's
            # failure, and the loop ends the run with this verdict.
            status, limited = "limited", run.cap_stop
        elif timed_out:
            # Our bound ended it, so nothing it printed on the way out is the
            # provider's verdict: the run keeps the status it ended with.
            status = "timeout"
        else:
            status = self._classify(run, -1 if code is None else code, text, stderr)
            status, limited = await self._provider_health_after(run, status, text, stderr)
        self._startup_finish(run.provider.name, run.node_id, run.startup_token,
                             failed=status == "failed" and not run.startup_progress
                             and not run.stop_requested and not timed_out,
                             resolved=status == "refused",
                             error=(stderr or text).splitlines()[0] if (stderr or text) else status)

        run.fix_verdict = {"status": status, "limited": limited, "usage": usage,
                           "cap_stop": run.cap_stop if status == "limited" else None}
        if status == "failed" and run.requested_session:
            run.fix_verdict["requested_session"] = run.requested_session
        return True

    async def _provider_health_after(self, run: Run, status: str, text: str,
                                     stderr: str) -> tuple[str, dict | None]:
        """What this run's outcome says about its provider, recorded.

        Two questions in order: did the provider itself stop the run, and has
        this provider now failed often enough in a row to be worth cooling
        down? Returns the possibly-revised status and the limit verdict —
        the caller needs both, because a `limited` run reports when it is back
        and nothing else knows that.
        """
        node_id = run.node_id
        limited = None
        # A run the PROVIDER stopped is not a run that failed. The CLI says so
        # in its own hardcoded words, and until now it said them into an agent's
        # output where nothing was listening: two agents did 42,000 tokens of
        # real work each, ended with "You've hit your monthly spend limit …
        # resets 5:50pm", exited 1, and were filed as failures. Four of those
        # tripped the breaker, whose `check` then reported the provider
        # perfectly authenticated — true, useless, and the reason the day's
        # account of itself was "claude is unreliable" when claude was full.
        if status == "failed":
            limited = await asyncio.to_thread(
                self._limit_verdict, run.provider, run.text_parts)
            if limited:
                status = "limited"
                self.tree.set_cooldown(run.provider.name, limited["until"],
                                       limited["reason"], cause="quota")
                self.tree.emit(node_id, "limited", provider=run.provider.name,
                               until=limited["until"], detail=limited["reason"])

        # Cause-agnostic circuit breaker. A provider whose last few runs all
        # failed is broken whatever the reason, and that is knowable without
        # reading a word of what the agent said — which is the part this
        # project has already got wrong once.
        if status not in ("awaiting_user", "limited", "truncated", "refused"):
            trip = self.tree.note_run_outcome(
                run.provider.name, ok=status in ("done", "merged"),
                threshold=int(self.config.limits.get("provider_failure_threshold", 3)),
                kind=status,
                # PS-R4b: a success proves the login of this run's context.
                context=self._auth_context(self.executor(run.spec)),
                # Both ends of the output. A CLI puts the reason it stopped at
                # the END — the limit message that started all this was in the
                # last 120 characters, and what was recorded was the first 120,
                # which said "I'll start by reading the spec".
                reason=f"{status}: {_both_ends(stderr or text)}",
            )
            if trip:
                # A cooldown rather than a permanent mark: the cause may be
                # transient, and `budget_status` and choose_provider already
                # route around a cooling provider and defer when none is left.
                #
                # How long depends on one structured question, asked of the CLI
                # rather than inferred from what any agent said: is it still
                # authenticated? A rate limit heals by waiting. A revoked token
                # does not, and cycling half-hourly against it wastes runs and
                # hides the fact that only a person can fix it.
                name = run.provider.name
                # PS-R4b: the question "is it still authenticated?" is asked
                # of the login THIS run used — through the run's own executor,
                # not the project default, which a per-agent executor override
                # may not agree with.
                run_executor = self.executor(run.spec)
                authenticated = await asyncio.to_thread(self._auth_ok, name,
                                                        run_executor)
                if authenticated is False:
                    seconds = float(self.config.limits.get(
                        "provider_auth_cooldown_seconds", 6 * 3600))
                    # PS-R4: the whole credential group, in one transaction
                    # that preserves whatever else each member was cooling on
                    # and leaves another context's block standing.
                    self._block_auth(name, seconds,
                                     self._auth_context(run_executor))
                else:
                    seconds = float(self.config.limits.get(
                        "provider_down_cooldown_seconds", 1800))
                    reason = (f"{trip['failures']} runs in a row failed — check "
                              f"`multiagents auth login "
                              f"{self._auth_target(name)[0]}` and "
                              f"`multiagents doctor`")
                    self.tree.set_cooldown(name, now() + seconds, reason,
                                           cause="provider_down")
                self._maybe_cool_family(name, seconds)
        return status, limited

    def _cap_verdict_of(self, run: Run, status: str) -> bool:
        """SC-R4a/R3b: is this ended run a cap stop? It is when a cap stopped
        it, and when it failed while a cap binds — retried, it would launch
        under a spent cap. Sets `run.cap_stop`. A question wins."""
        if run.awaiting:
            return False
        if run.cap_stop is None and status == "failed":
            capped = self._cap_refusal(run.provider.name, run.spec.model or "")
            if capped and capped["cause"] == spendcap.CAUSE:
                run.cap_stop = capped
        return run.cap_stop is not None

    def _cap_stopped_event(self, run: Run) -> None:
        """The runner's durable acknowledgement of a cap stop (SC-R4a), and
        the crossings that stopped it, on the node (SC-R4c, r4 #2)."""
        self._record_stops()
        ids = (run.cap_stop or {}).get("crossing_ids") or []
        if ids:
            # SF-R1/R2: cumulative, never an overwrite — the union survives
            # later stops, steers and resumes, so every crossing that stopped
            # this node stays named.
            with contextlib.suppress(Exception):
                node = self.tree.get(run.node_id)
                prior = list(node.spend_cap_crossings) if node is not None else []
                self.tree.update(run.node_id,
                                 spend_cap_crossings=list(dict.fromkeys([*prior, *ids])))
        verdict = run.cap_stop or {}
        self.tree.emit(run.node_id, "limited", provider=run.provider.name,
                       model=run.spec.model, reason=verdict.get("cause", spendcap.CAUSE),
                       until=verdict.get("until"), caps=verdict.get("caps") or [],
                       detail=verdict.get("reason", ""))

    def _no_output_summary(self, run: Run, code: int) -> str:
        """Why a run that said nothing ended, from the mechanics alone.

        Exit code, elapsed, the last thing it did. Deliberately NOT a story
        about why — inventing intent from a failed run is the mistake that
        once cooled a provider down over the word "quota". These are facts,
        labelled as facts.
        """
        node_id = run.node_id
        tail = [e for e in run.events if e.get("kind") in ("tool", "raw")][-3:]
        trace = "; ".join(
            (f"raw: {str(e.get('raw'))[:160]}" if e.get("kind") == "raw"
             else f"{e.get('name')}({str(e.get('args'))[:80]})")
            for e in tail
        )
        node_now = self.tree.get(node_id)
        elapsed = round(node_now.turn_elapsed()) if node_now else 0
        return (f"[no output] the run ended with exit {code} after {elapsed}s "
                f"and {run.supervisor.steps if run.supervisor else 0} step(s), "
                f"having said nothing."
                + (f" Last activity: {trace}" if trace else ""))

    def _drop_if_empty(self, node_id: str, spec: AgentSpec) -> None:
        """Reclaim a worktree that holds nothing worth keeping.

        Read-only agents get a worktree so that writing anyway is harmless, not
        because their branch is expected to matter. If one produced no commits
        there is nothing to lose, so drop it rather than accumulating dead
        checkouts. A non-writing agent that *did* commit keeps its branch — that
        is a surprise worth being able to inspect.
        """
        node = self.tree.get(node_id)
        if node is None or node.node_id or not node.branch or spec.writes or spec.conversational:
            return                            # a live conversation keeps its worktree
        if self.authority:
            record = self.authority.get(node_id)
            if record and record["seeded"]:
                return
            node = self.authoritative(node, "drop_if_empty")
            if node is None:
                return
        base = self.config.base_branch or gitops.current_branch(self.paths.root)
        root = self.paths.root
        try:
            commits = gitops.commits_on(root, node.branch, base, root=root)
        except gitops.GitError as exc:
            self.git_unreadable(node_id, root, exc)
            return                            # unread is not empty: kept
        if commits == 0:
            pending = self._cleanup(node, completion="discarded")
            if pending is None:
                return
            # A branch the container could not delete stays on the node until
            # the host has deleted it (SG-R2), which then clears it.
            self.tree.update(node_id, worktree="",
                             **({} if pending else {"branch": ""}))
            if self.authority:
                self.authority.clear(node_id, worktree=True, branch=not pending)
        else:
            self.tree.emit(
                node_id, "unexpected_commits",
                branch=node.branch,
                detail="agent is configured writes:false but committed; branch kept",
            )

    async def _watch_timers(self, run: Run) -> None:
        """Detect the conditions no event will announce: silence and wall clock.

        Runs for the whole life of the run — the caller cancels it, this loop
        never returns on its own — because runaway_steps/timeout tripping once
        must not stop silence (or a later timeout re-check) from still being
        reported. One poll's failure is recorded as an event rather than
        ending the loop, since that would silently stop watching the run for
        good.
        """
        assert run.supervisor is not None
        node = self.tree.get(run.node_id)
        progress_dir = (Path(node.worktree) if node and node.worktree
                        and gitops.is_repo(Path(node.worktree)) else None)
        # The working tree at launch is the first reading silence compares
        # against, so a run that has said nothing and changed nothing since it
        # started trips at the first quiet poll rather than the second. Any
        # stream event drops it (`Supervisor.observe`), so a long tool call
        # later in the run still gets its first look.
        if progress_dir is not None:
            with contextlib.suppress(Exception):
                run.supervisor.progress_when_last_quiet = await asyncio.to_thread(
                    _worktree_state, progress_dir, self.paths.root)
        while True:
            await asyncio.sleep(self.WATCH_POLL_SECONDS)
            try:
                # Only while the agent is quiet, and only if it has a tree of
                # its own. The silence check needs a CURRENT reading to tell a
                # long tool call from a stall, and `_consume` refreshes this on
                # tool events, which is exactly what a silent agent is not
                # producing. Threaded, because a blocking git call on this loop
                # would stop the pipe from being drained — a deadlock, not a
                # slowdown.
                if (progress_dir is not None
                        and run.supervisor.quiet_for >= run.supervisor.silence_timeout):
                    run.supervisor.note_progress(
                        await asyncio.to_thread(_worktree_state, progress_dir,
                                                self.paths.root))
                trip = run.supervisor.check_timers()
                if trip:
                    self.tree.set_status(run.node_id, "stuck", f"{trip.reason}: {trip.detail}")
                    self.tree.emit(run.node_id, "stuck", reason=trip.reason, detail=trip.detail)
                    self._trip_notice(run, trip)
                    run.trip_kind = trip.reason
                    run.trip_signature = run.supervisor.last_digest
                    run.trip_progress = run.supervisor.current_progress
                    run.trip_opaque_calls = run.supervisor.opaque_calls
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.tree.emit(run.node_id, "watchdog_poll_error",
                               detail=f"{type(exc).__name__}: {exc}")

    def _did_work(self, run: Run) -> bool:
        """Is there anything to show for this run besides its silence?"""
        node = self.tree.get(run.node_id)
        branch = getattr(node, "branch", "") if node else ""
        if node and node.node_id:
            from .scheduler.results import checkout_tip, MissingCheckout
            try:
                from .scheduler.store import Store
                from .scheduler.engine import attempts
                with Store(self.paths.root).transaction(write=False) as db:
                    activation = attempts(db)[node.attempt_id]
                if checkout_tip(node, self.authority) != activation["input_commit"]:
                    return True
            except MissingCheckout:
                pass
            except (gitops.GitError, OSError, ValueError, KeyError) as exc:
                self.git_unreadable(run.node_id, Path(node.worktree), exc)
                return True  # Unreadable work is unknown, never silent success.
        elif branch:
            try:
                base = self.config.base_branch or gitops.current_branch(self.paths.root)
                if gitops.commits_on(self.paths.root, branch, base,
                                     root=self.paths.root) > 0:
                    return True
            except gitops.GitError as exc:
                self.git_unreadable(run.node_id, self.paths.root, exc)
            except Exception:
                pass
        return (run.supervisor.steps or 0) >= int(
            self.config.limits.get("silent_success_steps", 10))

    def _limit_verdict(self, provider, text_parts: list[str]) -> dict | None:
        """Did the provider stop this run, and until when? Blocking; off-thread.

        Two things must agree before a run is called limited rather than
        failed. The CLI's own marker has to be the LAST thing said — the same
        guard the orchestrator's detector uses, because a marker anywhere else
        is an agent quoting it — and the account's own budget has to corroborate
        it. An advisor's point: matching a string alone would let an agent that
        wrote documentation containing the phrase, and then crashed, take a
        provider offline.
        """
        markers = (getattr(provider, "transcript", None) or {}).get("limit_markers") or []
        # The last few chunks, not strictly the last one. A model that is handed
        # a limit error often answers it — "I have received a usage limit error,
        # I will stop here" — which would push the marker one place back and,
        # under a stricter rule, turn a limit into a failure. The budget check
        # below is what keeps this honest; position alone never was.
        tail = "\n".join((text_parts or [])[-3:]).lower()
        hit = next((m for m in markers
                    if m.get("match", "") and m["match"].lower() in tail), None)
        if hit is None:
            return None

        until = now() + float(self.config.limits.get(
            "provider_down_cooldown_seconds", 1800))
        detail = hit.get("detail") or hit["match"]
        try:
            from .budget import read_provider
            budget = read_provider(provider.name, provider, self.executor(),
                                   global_config_dir(), self.paths.config,
                                   use_cache=False, limits=self.config.limits)
        except Exception:
            budget = None
        if budget is not None and budget.known:
            # It says it is fine: something else ended this run and the
            # sentence was somebody quoting it.
            if budget.headroom is not None and budget.headroom > 0.25:
                return None
            if budget.resets_at:
                try:
                    from datetime import datetime
                    until = max(until, datetime.fromisoformat(
                        str(budget.resets_at)).timestamp())
                except (TypeError, ValueError):
                    pass
        return {"until": until, "detail": detail,
                "reason": f"{provider.name} stopped it: {detail}"}

    @staticmethod
    def _refusal_record(source: str, pattern: str, matched: str) -> dict:
        """RC-R2 (bug-1213a0): the evidence a refusal verdict records — the
        source it matched, the marker that matched, and a bounded excerpt of
        the text itself. The FULL match is redacted before it is bounded:
        slicing first can cut a registered secret in half, and a half secret
        is one scrub can no longer recognise (review ag-997df9, finding 2).
        Enough to check the verdict without keeping the stderr tail around."""
        return {"source": source, "pattern": pattern,
                "excerpt": scrub(matched)[:200]}

    def _record_refusal(self, run: Run, source: str, pattern: str,
                        matched: str) -> str:
        """Set both forms of the refusal evidence on the run: the structured
        record result.json carries, and the one-line signal for the reason."""
        run.refusal = self._refusal_record(source, pattern, matched)
        run.refusal_signal = (f"{source} matched refusal marker {pattern!r}: "
                              f'"{run.refusal["excerpt"]}"')
        return run.refusal_signal

    def _stderr_refusal(self, run: Run, code: int, stderr: str) -> str:
        """RC-R2 (bug-1213a0): a run that exited non-zero with a refusal
        marker in its stderr tail is a refusal, not a failure. Consulted only
        where the run would otherwise be `failed` — a clean exit, a stop we
        requested and a wall clock we imposed all outrank what the CLI printed
        on the way out. `re.search`, unlike the final message's fullmatch:
        stderr is a stream the marker sits inside, not a message it sums up."""
        if code == 0 or getattr(run, "stop_requested", False) \
                or getattr(getattr(run, "handle", None), "timed_out", False):
            return ""
        for pattern in getattr(run.provider, "refusal_markers", []):
            match = re.search(pattern, stderr or "", re.IGNORECASE)
            if match:
                self._record_refusal(run, "stderr", pattern, match.group(0))
                return "refused"
        return ""

    def _classify(self, run: Run, code: int, text: str, stderr: str) -> str:
        # Before everything, including the clean-exit shortcut below. A CLI that
        # cut its own turn short exits 0 with a stream that parses perfectly, so
        # every other signal here says it finished. Only its stderr disagrees.
        marker = next((m for m in (run.provider.truncation_markers or [])
                       if m.lower() in (stderr or "").lower()), None)
        if marker:
            return "truncated"
        if run.final_status.upper() == "TRUNCATED":
            return "truncated"
        if run.final_status.upper() == "REFUSED":
            return "refused"
        message = getattr(run, "final_assistant_message", "").strip()
        for pattern in getattr(run.provider, "refusal_markers", []):
            if re.fullmatch(pattern, message, re.IGNORECASE):
                self._record_refusal(run, "assistant message", pattern, message)
                return "refused"
        succeeded = code == 0 and (
            not run.final_status
            or run.final_status.upper() in {"SUCCESS", "OK", "COMPLETED"}
        )
        # A run that exited cleanly cannot have failed on quota or auth,
        # whatever words appear anywhere. Checked before the sniffers so no
        # marker can override the CLI's own verdict.
        if succeeded and text.strip():
            return "done"
        if looks_like_quota_failure(run.final_status, stderr):
            return "quota"
        # Checked before the generic failure paths: an unauthenticated provider
        # produces an empty response that is otherwise indistinguishable from a
        # model that simply said nothing, and the fix is entirely different.
        if looks_like_auth_failure(run.final_status, stderr):
            return "unauthenticated"
        # RC-R2: the two `failed` verdicts a non-zero exit can reach. Stderr
        # markers are consulted here and nowhere earlier — a structured
        # refusal, a truncation, quota and auth all keep their verdicts.
        if run.final_status and run.final_status.upper() not in {"SUCCESS", "OK", "COMPLETED"}:
            return self._stderr_refusal(run, code, stderr) or "failed"
        if code != 0:
            return self._stderr_refusal(run, code, stderr) or "failed"
        if not text.strip():
            # A headless agent that produced nothing USUALLY hit an auto-denied
            # permission — but not always, and the difference is visible. Nine
            # runs in one project exited 0 after five minutes and fifty-odd
            # steps of editing files and running commands, said nothing at the
            # end, and were filed as failures; the parent merged three of their
            # branches anyway, because the work was there.
            #
            # So the question is not "did it speak" but "did it do anything".
            # Commits are the evidence; steps are the fallback when the agent
            # has no branch of its own.
            if self._did_work(run):
                return "done"
            return "failed"
        return "done"

    async def _merge_pending_children(self, node_id: str) -> None:
        """Merge children that finished while this agent was still working.

        Called once this agent's own run has ended and its worktree has been
        committed, so nothing lands under it mid-task.
        """
        parent = self.tree.get(node_id)
        if self.authority and parent:
            record = self.authority.get(node_id)
            if not record or record["seeded"]:
                return
            if not self.identity_ok(node_id, "merge_pending_children"):
                return
        for child in self.tree.children_of(node_id):
            if child.status == "done" and child.branch:
                await self._maybe_merge_into_parent(child.id, pending=True,
                                                    ending_parent=node_id)

    async def _maybe_merge_into_parent(self, node_id: str, pending: bool = False,
                                       ending_parent: str | None = None) -> None:
        node = self.tree.get(node_id)
        if not node or node.node_id or not node.branch or not node.parent:
            return                            # depth-1 lands via explicit merge
        policy = self.config.project.get("git", {}).get("merge", {})
        if policy.get("inside_tree", "auto") != "auto":
            return
        # Deferral performs no mutation. Keep it available for pre-upgrade and
        # nested nodes even when their eventual merge needs host validation.
        pending_parent = self.tree.get(node.parent)
        if pending_parent is not None and pending_parent.status in {"pending", "running"}:
            self.tree.emit(node_id, "merge_deferred", parent=pending_parent.id,
                           reason="parent is still working in that worktree")
            return
        action = "merge_pending_children" if pending else "auto_merge"
        if ending_parent is not None and node.parent != ending_parent:
            self.mismatch(node.id, action, ["parent"], "child is not linked to ending parent")
            return
        if self.authority:
            if not self.identity_ok(node.id, action):
                return
            record = self.authority.get(node.id)
            if record:
                if record["seeded"] or record.get("parent") != node.parent:
                    self.mismatch(node.id, action, ["parent"], "untrusted parent")
                    return
                node = self.authoritative(node, action)
            elif (not node.branch.startswith("agents/") or
                  self.authority.owns_branch(node.branch)):
                self.mismatch(node.id, action, ["branch"], "untrusted child branch")
                return
            parent_record = self.authority.get(node.parent)
            if not parent_record or parent_record["seeded"]:
                self.mismatch(node.id, action, ["parent"], "parent is not host-recorded")
                return
            if not self.identity_ok(node.parent, action):
                return
        parent = self.tree.get(node.parent)
        if self.authority and parent:
            parent_record = self.authority.get(parent.id)
            parent = replace(parent, branch=parent_record["branch"],
                             worktree=parent_record["worktree"],
                             parent=parent_record["parent"])

        # Never merge into a worktree an agent is actively using. Even with the
        # dirty-tree guard in gitops.merge — which only refuses when there are
        # uncommitted changes — landing commits mid-task silently changes files
        # the parent has already read, invalidating its picture of its own
        # workspace. The merge is deferred to when the parent's run ends.
        if parent is not None and parent.status in {"pending", "running"}:
            self.tree.emit(node_id, "merge_deferred", parent=parent.id,
                           reason="parent is still working in that worktree")
            return

        target = Path(parent.worktree) if parent and parent.worktree else self.paths.root
        if not target.is_dir():
            return
        merged_sha = ""
        if self.authority:
            try:
                with self.authority.pinned_worktree(target) as pinned:
                    status, detail = gitops.merge(
                        pinned, node.branch, f"{node.agent}: {node.task[:72]}",
                        policy.get("style", "squash"), root=self.paths.root,
                        target_branch=parent.branch if parent else "",
                    )
                    if status == "merged":
                        merged_sha = gitops.head_sha(pinned, root=self.paths.root)
            except (OSError, ValueError):
                self.mismatch(node.id, action, ["worktree"],
                              "parent worktree could not be pinned")
                return
        else:
            status, detail = gitops.merge(
                target, node.branch, f"{node.agent}: {node.task[:72]}",
                policy.get("style", "squash"), root=self.paths.root,
                target_branch=parent.branch if parent else "",
            )
            if status == "merged":
                merged_sha = gitops.head_sha(target, root=self.paths.root)
        self.tree.emit(node_id, "merge", result=status, detail=detail[:400], into=str(target))
        if "host_authority_mismatch" in detail:
            self.mismatch(node_id, action, ["worktree", "branch"], detail[:400])
        if status == "merged":
            self.tree.set_status(node_id, "merged")
            self._cleanup(node, completion="merged", commit=merged_sha)
        elif status == "conflict":
            self.tree.set_status(node_id, "done", "merge conflict; branch kept for parent")

    def _cleanup(self, node: Node, *, completion: str = "",
                 commit: str = "") -> bool | None:
        """Remove a finished agent's worktree and branch.

        Only ever called after a successful merge or an explicit discard, so
        force-deleting the branch is safe: its commits are already elsewhere.
        True when the branch is left for the host to delete (SG-R2).
        """
        if node.node_id:
            return None
        if self.authority:
            if not self.identity_ok(node.id, "cleanup"):
                return None
            record = self.authority.get(node.id)
            if record:
                node = replace(node, branch=record["branch"], worktree=record["worktree"])
                if not (record["completion"] or completion):
                    return None
            else:
                if node.worktree and not self.authority.safe_nested_path(Path(node.worktree)):
                    self.mismatch(node.id, "cleanup", ["worktree"], "outside worktree domain")
                    return None
                if node.branch and not self.authority.safe_unrecorded_branch(node.branch):
                    self.mismatch(node.id, "cleanup", ["branch"], "protected branch")
                    return None
        if node.branch and (not node.branch.startswith("agents/") or
                            not gitops.run(self.paths.root, "check-ref-format",
                                           f"refs/heads/{node.branch}").ok):
            if self.authority:
                self.mismatch(node.id, "cleanup", ["branch"],
                              "branch is outside agents namespace")
            return None
        if node.worktree:
            worktree = Path(node.worktree)
            try:
                registered = gitops._registration_branch(self.paths.root, worktree,
                                                        node.branch)
            except gitops.GitError as exc:
                if self.authority:
                    self.mismatch(node.id, "cleanup", ["worktree", "branch"], str(exc))
                return None
            if registered and registered != node.branch:
                if self.authority:
                    self.mismatch(node.id, "cleanup", ["worktree", "branch"],
                                  "host_authority_mismatch: worktree registration differs")
                return None
            if self.authority and (worktree.exists() or worktree.is_symlink()):
                if not self.authority.remove_worktree(worktree, recorded=bool(record)):
                    self.mismatch(node.id, "cleanup", ["worktree"],
                                  "worktree could not be removed within its authorised path")
                    return None
            elif worktree.is_dir():
                gitops.remove_worktree(self.paths.root, worktree, force=True)
            if not worktree.exists() and registered:
                gitops.prune_worktree(self.paths.root, worktree, node.branch)
        if self.authority and record and completion:
            self.authority.complete(node.id, completion, commit)
        if node.branch:
            result = gitops.delete_branch(self.paths.root, node.branch, force=True)
            if gitops.refused_by_packed_refs_lock(result):
                # SG-R2: in the container `.git` is read-only, so no branch
                # can be deleted from here. Not an error: the host deletes it
                # once this node is merged or discarded.
                self.tree.update(node.id, branch_pending_delete=node.branch)
                self.tree.emit(node.id, "branch_pending_delete", branch=node.branch,
                               detail=result.err[:400])
                return True
        return False

    # ----------------------------------------------------------------- query --

    def check(self, agent_id: str, since: int = 0) -> dict[str, Any]:
        node = self.tree.get(agent_id)
        if node is None:
            raise KeyError(f"Unknown agent {agent_id!r}")
        run = self.runs.get(agent_id)
        events = run.events if run else self._read_stream(agent_id)
        window = events[since:since + 80]
        result = {
            "agent_id": agent_id,
            "agent": node.agent,
            "status": node.status,
            "reason": node.reason,
            "elapsed_seconds": round(node.turn_elapsed()),
            "node_elapsed_seconds": round(node.elapsed()),
            "steps": node.steps,
            "total_events": len(events),
            "next_since": since + len(window),
            "usage": node.usage,
            "branch": node.branch or None,
            "events": [_compact(e) for e in window],
        }
        result.update(home_provider=node.home_provider or node.provider,
                      current_provider=node.provider, segments=node.segments,
                      **self._qh_position(node))
        if node.reason == "session_lost":
            result["requested_session"] = node.requested_session
        result.update(self._no_commits_note(node))
        limits = run.limits if run else self.launch_limits.lookup(agent_id)
        if limits:
            result["effective_limits"] = limits
        if run and run.supervisor and node.status in {"running", "pending"}:
            # Only meaningful for a live process. A parked agent's Run survives
            # in self.runs, so this would grow forever and read as silence.
            result["quiet_for_seconds"] = round(run.supervisor.quiet_for)
        if node.status == "awaiting_user":
            question = next(iter(self.tree.open_questions(agent_id)), None)
            if question:
                result["question"] = {k: question[k] for k in
                                      ("id", "topic", "question", "proposed_default")}
        if node.status in {"done", "merged"} and node.summary:
            result["summary"] = node.summary
        filed = [t for t in self.tree.read().get("tickets", [])
                 if t.get("agent") == agent_id and t.get("status") != "declined"]
        if filed:
            result["tickets"] = [{k: t[k] for k in ("id", "severity", "title", "status")}
                                 for t in filed]
        return result

    def _read_stream(self, agent_id: str) -> list[dict]:
        try:
            text = _run_read(self.paths.run_dir(agent_id), "stream.jsonl",
                             STREAM_LOG_MAX_BYTES)
        except OSError:
            return []
        out = []
        for line in text.splitlines():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out

    def readonly_violations(self, node, base: str) -> list[str]:
        """Protected paths this agent's branch MODIFIED, deleted or renamed.

        Additions are not violations — see `gitops.changed_paths`. An unknown
        agent name yields no patterns and therefore no violations, which is the
        right way round: a roster entry deleted mid-run must not make the
        branch unmergeable.
        """
        if not node.branch:
            return []
        spec = self.config.agents.get(node.agent)
        if spec is None:
            return []
        patterns = self.config.readonly_paths_for(spec)
        if not patterns:
            return []
        changed = gitops.changed_paths(self.paths.root, node.branch, base,
                                       root=self.paths.root)
        return [path for path in changed if matches_any(patterns, path)]

    def collect(self, agent_id: str, mode: str = "summary") -> dict[str, Any]:
        node = self.tree.get(agent_id)
        if node is None:
            raise KeyError(f"Unknown agent {agent_id!r}")
        run_dir = self.paths.run_dir(agent_id)
        data: dict[str, Any] = {}
        try:
            data = json.loads(_run_read(run_dir, "result.json"))
        except (OSError, json.JSONDecodeError):
            data = {}

        text = data.get("text", "") or node.summary
        payload = {
            "agent_id": agent_id,
            "agent": node.agent,
            "status": node.status,
            "reason": node.reason,
            "branch": node.branch or None,
            "usage": node.usage,
            "elapsed_seconds": round(node.turn_elapsed()),
            "node_elapsed_seconds": round(node.elapsed()),
            "log_dir": str(run_dir),
            "need_info": [ln for ln in text.splitlines() if ln.strip().startswith("NEED_INFO")],
        }
        payload.update(home_provider=node.home_provider or node.provider,
                       current_provider=node.provider, segments=node.segments,
                       **self._qh_position(node))
        if data.get("warnings") or node.warnings:
            payload["warnings"] = data.get("warnings") or node.warnings
        if node.reason == "session_lost":
            payload["requested_session"] = node.requested_session
        payload.update(self._no_commits_note(node, text))
        filed = [{k: t[k] for k in ("id", "severity", "title", "status")}
                 for t in self.tree.read()["tickets"] if t.get("agent") == agent_id]
        if filed:
            payload["tickets"] = filed
        if mode == "full":
            payload["text"] = text
            payload["stderr_tail"] = data.get("stderr_tail", "")
        else:
            payload["result"] = text[-MAX_SUMMARY_CHARS:]
            if len(text) > MAX_SUMMARY_CHARS:
                payload["truncated"] = True
                payload["hint"] = f"full transcript: {run_dir}/result.json, or collect(mode='full')"
        if node.branch and gitops.is_repo(self.paths.root):
            base = self.config.base_branch or gitops.current_branch(self.paths.root)
            root = self.paths.root
            try:
                payload["commits"] = gitops.commits_on(root, node.branch, base, root=root)
                payload["diff_stat"] = gitops.diff_stat(root, node.branch, base,
                                                        root=root)[:2000]
                # Surfaced HERE as well as at the merge gate, so the
                # orchestrator learns about it while it is still deciding
                # rather than as a surprise in the merge result. The revert
                # happens at merge.
                violations = self.readonly_violations(node, base)
            except gitops.GitError as exc:
                self.git_unreadable(agent_id, root, exc)
                payload["commits"] = None
                payload["git_unreadable"] = str(exc)[:400]
                violations = []
            if violations:
                payload["readonly_violations"] = violations[:50]
                payload["readonly_note"] = (
                    f"{node.agent} modified {len(violations)} file(s) it may not "
                    f"change. They will be reverted to {base} when this branch "
                    f"merges; the rest of its work is unaffected. Read what it "
                    f"was trying to do before you re-run it — a developer "
                    f"editing a test usually means the test and the "
                    f"implementation disagree, and which one is wrong is your "
                    f"call, not its."
                )
        return payload

    def _no_commits_note(self, node: Node, text: str | None = None) -> dict[str, Any]:
        spec = self.config.agents.get(node.agent)
        if (node.status != "done" or not node.branch or spec is None
                or not spec.writes or not gitops.is_repo(self.paths.root)):
            return {}
        if text is None:
            try:
                text = json.loads(_run_read(self.paths.run_dir(node.id), "result.json")).get("text", "")
            except (OSError, json.JSONDecodeError):
                text = node.summary
        text = text or ""
        if "NEED_INFO(" in text or NEED_DECISION.search(text):
            return {}
        base = self.config.base_branch or gitops.current_branch(self.paths.root)
        try:
            if gitops.commits_on(self.paths.root, node.branch, base, root=self.paths.root):
                return {}
        except gitops.GitError:
            return {}
        return {"no_commits": True,
                "no_commits_note": "Writing agent finished without a commit; review its result before merging."}

    # ---------------------------------------------------------------- control --

    def _spec_of(self, node) -> tuple[AgentSpec, Provider]:
        """The spec and provider a node is running as, rebuilt from the node
        the same way `start()` built them: a run routed to a fallback carries
        the fallback's model and options, not the configured ones."""
        spec = self.config.agent(node.agent)
        frozen = self.launch_limits.spec(node.id)
        if frozen:
            current = spec.routed(node.provider)
            limits = {"timeout", "silence_timeout", "max_steps"}
            frozen.update({key: getattr(current, key) for key in limits})
            frozen["set_fields"] = (frozenset(frozen.get("set_fields") or ()) - limits
                                    | (frozenset(current.set_fields or ()) & limits))
            return AgentSpec(**frozen), self.providers[node.provider]
        if node.model_pinned:
            # FO-R1: the pin keeps its model, but the destination's `models:`
            # entry still contributes its options to the relaunch.
            spec = spec.routed(node.provider, pinned_model=node.model)
            spec = spec.replace(provider=node.provider)
        else:
            # FS-R2: a recorded session is resumed where it lives, even on an
            # unlisted sibling — it is never moved (legacy clause).
            routed = self._usable_spec(spec, node.provider, resume=True)
            if routed is None:
                # Followed as it was launched; only a relaunch needs a model, and
                # steer refuses that first (RT-R2).
                alternative, overrides = spec.fallback_for(node.provider)
                routed = spec.replace(model=alternative, **overrides)
            # PS-R7 (review ag-2f0d3e, finding 5): the session is the RECORDED
            # model's. A roster edit since must not substitute another model
            # under a resume — the allowlist admission then judges the model
            # the conversation actually ran and refuses it if it is no longer
            # allowed, instead of quietly moving the session to a new one.
            if node.model and routed.model != node.model:
                routed = routed.replace(model=node.model)
            spec = routed
        # RM-R5a: the effort the node was launched with is what it keeps —
        # a model id that declares its own suffix normalised the configured
        # one, and the normalised spec is what is persisted.
        if node.effort and spec.effort != node.effort:
            spec = spec.replace(effort=node.effort)
        return spec, self.providers[node.provider]

    # ------------------------------------------------------------- survival --

    ADOPTABLE = ("running", "detached", "stuck")

    def _role_of(self, session: str) -> str:
        """The session ROLE a session id belongs to (orchestrator, initializer),
        read off the driver node `driver.py` recorded for it; "" for none."""
        if not session:
            return ""
        for driver in self.tree.drivers():
            if driver.session == session:
                return driver.role
        return ""

    async def adopt(self, *, exclude: set[str] | None = None) -> list[str]:
        """SV-R6: take over the runs a previous server of this role left.

        Only a root server adopts: a nested one's agents are its own spawns,
        and it cancels them when it goes (SV-R3). Only nodes of its own
        session role: the initializer's agents are not the orchestrator's to
        finish. Only nodes nobody holds (SV-R5) — the lock, not the status,
        decides that, so a live but slow server is never robbed.

        Every pass, the root's or not, first settles launch-cleanup holds
        (RM-R1d): a hold whose process has since died — its owner still
        here, or gone with a crash — is lifted, so its slot comes back.
        """
        with contextlib.suppress(Exception):
            self._settle_holds()
        self._pc_reconcile_soon()                       # kicks on a new death
        if self.self_id():
            self._pc_kick()
            return []
        mine = self._role_of(self.session())
        taken = []
        for node in self.tree.active():
            if node.id in (exclude or set()) or node.status not in self.ADOPTABLE or node.id in self._locks:
                continue
            if node.cleanup_hold:
                # RM-R1d (review ag-43f57f): a held node is not a run to
                # follow — its launch failed. Its hold is taken over and
                # ended by `_settle_holds`, with the owner's releases.
                continue
            if self._role_of(node.session) != mine:
                continue
            try:
                if await self._adopt_one(node):
                    taken.append(node.id)
            except Exception as exc:
                await self._unadoptable(node, exc)
        # PC-R3a (review finding 8): the liveness reconciliation drains too —
        # a slot freed by a death nobody announced is found here.
        self._pc_kick()
        return taken

    async def _unadoptable(self, node, exc: Exception) -> None:
        """SV-R6: a node adoption raised on — its spec gone, its command.json
        corrupt — ends here, with the reason, instead of staying adoptable and
        failing again every pass. Its process, if any, is stopped first: a
        `failed` node must not go on spending with nobody reading it.

        Only if this server still holds it, or can take it: a node another
        server has just adopted is that server's to judge."""
        detail = f"{type(exc).__name__}: {exc}"
        run = self.runs.get(node.id)
        if run is not None and run.task is not None:
            # It raised after the follow began: the node IS adopted, and its
            # own `_consume` finishes it and releases the lock.
            self.tree.emit(node.id, "adopt_failed", detail=detail)
            return
        self.runs.pop(node.id, None)
        try:
            if node.id not in self._locks and not self._claim(node.id):
                return
        except OSError:
            return
        try:
            self.tree.emit(node.id, "adopt_failed", detail=detail)
            current = self.tree.get(node.id)
            if current is None or current.status not in self.ADOPTABLE:
                return
            with contextlib.suppress(Exception):
                await asyncio.to_thread(self.stop_detached, current)
            self.tree.set_status(node.id, "failed",
                                 f"could not be adopted after its server exited: "
                                 f"{detail}")
        finally:
            self._release(node.id)

    async def _adopt_one(self, node) -> bool:
        run_dir = self.paths.run_dir(node.id)
        output, status_file = run_dir / "output.ndjson", run_dir / "exit_status"
        spec, provider = self._spec_of(node)
        executor = self._predecessor_executor(node, spec)
        stopper = probe = None
        if getattr(executor, "kind", "local") == "docker" and not executor.inside():
            # The pid on the node is the host's `docker exec` client, which
            # can be gone while the wrapper it started runs on: whether the
            # agent lives is asked of the container, and so is stopping it.
            stopper = lambda grace: executor.kill_detached(node.id, grace)  # noqa: E731
            probe = executor.liveness(node.id)
        live = bool(node.pid) and running(node.pid, getattr(node, "pid_start", ""))
        if not live and probe is not None:
            live = await asyncio.to_thread(probe)
        if live and not output.is_file():
            return False          # started before the wrapper: nothing to follow
        # SV-R8: one already past its wall clock is not taken. Unowned, its
        # wrapper ends it within a second (SV-R4), and the next pass finalises
        # it as the timeout it is; owned, the wrapper would leave it to a
        # watchdog that only reports.
        if live and self._past_deadline(run_dir, node.id, spec):
            return False
        agent_id = node.id
        if not self._claim(agent_id):
            return False
        node = self.tree.get(agent_id)
        if node is None or node.status not in self.ADOPTABLE:
            self._release(agent_id)
            return False
        if not live and not output.is_file() and not status_file.is_file():
            self.tree.set_status(node.id, "orphaned",
                                 "the process is gone and left neither output "
                                 "nor an exit status")
            self._release(node.id)
            return False

        limits = dict(self.launch_limits.lookup(node.id))
        # C7-R1/R3: restore only from the host ledger. Older or evicted
        # records resolve this cap afresh, independently of the timeout.
        steps = limits.get("max_steps")
        resolution = ("restored" if isinstance(steps, dict) and "value" in steps
                      else "fallback")
        if resolution == "fallback":
            steps = self._max_steps_limit(spec, provider.name)
        limits["max_steps"] = {**steps, "source_detail": {
            **steps["source_detail"], "resolution": resolution}}
        # A partial ledger must still report the timeout and silence values
        # that supervise this turn, even when only the step cap is recorded.
        current = self._limits_detail(spec.name, self._limits_for(node.id, spec),
                                      provider.name)
        for name, entry in current.items():
            limits.setdefault(name, entry)
        launched = self.launch_limits.launch_time(node.id)
        wall = float(limits.get("timeout", {}).get("value") or 0)
        if not wall:
            wall = float(self._limits_for(node.id, spec)["timeout"]["value"])
        follow = node.follow or {}
        turn = int(follow.get("turn", 0))
        # SV-R7: what the last server logged past the point it recorded, it
        # never counted; the lines are read again and logged once.
        stream = run_dir / "stream.jsonl"
        if "log" in follow and _size(stream) > int(follow["log"]):
            with contextlib.suppress(FileNotFoundError), \
                    _run_open(run_dir, "stream.jsonl", os.O_RDWR, "r+b") as fh:
                fh.truncate(int(follow["log"]))

        handle = FollowHandle(pid=node.pid or 0, run_dir=run_dir, offset=turn,
                              pid_start=getattr(node, "pid_start", "") or "",
                              stopper=stopper, probe=probe)
        supervisor = self._supervisor(
            spec, provider, wall, limits.get("silence_timeout", {}).get("value"),
            limits["max_steps"]["value"])
        # SV-R8: the wall clock runs from the launch, whoever watched it; the
        # silence clock runs from now, because nobody was listening before.
        # An absent or unreadable host clock grants no fresh budget. Do not
        # recover it from command.json or the container-writable node times.
        elapsed = max(0.0, now() - launched) if launched is not None else wall + 1
        supervisor.started = time.monotonic() - elapsed
        # LN-C2, adversary finding 3: the limits an adopted run trips
        # against come from the host-owned launch record the launching
        # server wrote, never from the node's record in `tree.json`, which
        # the running agent can forge between the two servers.
        run = Run(node_id=node.id, provider=provider, spec=spec, handle=handle,
                  supervisor=supervisor, turn_start=turn,
                  replay_to=int(follow.get("offset", turn)), adopted=True,
                  launched_at=float(launched or 0),
                  prompt_file=self.launch_limits.prompt_file(node.id),
                  startup_token=self.startup.token_for(provider.name, node.id),
                  limits=limits if isinstance(limits, dict) else {})
        reader = getattr(executor, "oom_kill_count", None)
        if callable(reader):
            # LN-C5, finding 2: a docker run taken over mid-flight gets the
            # SIGKILL judgement it could never have had without one. Its
            # baseline is unknown — this server did not read the counter at
            # the launch it did not perform — so a SIGKILL of an adopted run
            # is `kill_uncertain`, never silently absent.
            run.oom_reader = reader
            run.oom_container = str(getattr(executor, "container", "") or "")
            try:
                entry = self.occupancy.rebind(run.oom_container, node.id,
                                              handle.pid, handle.pid_start)
                run.oom_since = entry.get("since")
            except Exception:
                pass               # as at launch: supervision is never the cost
        self.runs[node.id] = run
        if not node.turn_started_at and launched is not None:
            self.tree.update(node.id, turn_started_at=launched)
        if live:
            self.tree.update(node.id, adopted_at=now())
            self.tree.set_status(node.id, "running", "adopted")
            self.tree.emit(node.id, "adopted", pid=node.pid)
        # PC-R2a/R3f: a live adoption transfers the existing slot. A dead
        # adoption must reacquire capacity before beginning its post-mortem.
        if live:
            run.slot_token = self._launch_slot_owner(node.id, provider.name)
        run.task = asyncio.create_task(self._supervise(run))
        if live:
            asyncio.create_task(self._wrap_up_watch(node.id))
            self._start_credential_watch()
        return True

    def _past_deadline(self, run_dir: Path, node_id: str, spec: AgentSpec) -> bool:
        launched = self.launch_limits.launch_time(node_id)
        limits = self.launch_limits.lookup(node_id)
        wall = float(limits.get("timeout", {}).get("value") or 0)
        if not wall:
            wall = float(self._limits_for(node_id, spec)["timeout"]["value"])

        return bool(wall) and bool(launched) and now() >= launched + wall

    async def shutdown(self, *, detach: bool, preserve_status: bool = False) -> None:
        """This server is going. SV-R3: a root server leaves its agents
        running and says so; a nested one ends them, as it always has."""
        # PC (round 6, finding 3): no drain dispatches and no reconciliation
        # kicks from here on; the reconciliation task is ended BEFORE the
        # runs are captured, so nothing it confirms can launch work now.
        self.__dict__["_pc_shutting_down"] = True
        await self._qh_shutdown()
        for waiter in list(self.__dict__.get("_pc_waiters", ())):
            # Consults waiting for a slot wake to the flag and end.
            if not waiter.done():
                with contextlib.suppress(RuntimeError):
                    waiter.get_loop().call_soon_threadsafe(
                        lambda w=waiter: w.done() or w.set_result(None))
        task = self.__dict__.get("_pc_reconcile_task")
        if task is not None and not task.done():
            try:
                same_loop = task.get_loop() is asyncio.get_running_loop()
            except RuntimeError:
                same_loop = False
            if same_loop:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        runs = [run for run in self.runs.values() if run.task and not run.task.done()]
        for run in runs:
            run.detaching = detach
            run.task.cancel()
        for run in runs:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await run.task
        if not detach:
            return
        for run in runs:
            node = self.tree.get(run.node_id)
            if not preserve_status and node and node.status in ("running", "stuck", "pending"):
                self.tree.set_status(
                    run.node_id, "detached",
                    f"left running when its server exited at "
                    f"{time.strftime('%Y-%m-%d %H:%M:%S')}")
                self.tree.emit(run.node_id, "detached", pid=node.pid)
            self._release(run.node_id)

    def stop_detached(self, node) -> bool:
        """Kill an agent this process never started, synchronously.

        Agents run in their own session so that stopping one also stops the
        shells and test runners beneath it. The same property means they
        outlive a server that died without cleaning up, and a recovery has to
        reach them from outside — through the container for a docker agent,
        because killing the `docker exec` client would leave the agent inside
        running.

        SV-R10: a wrapped agent is ended through its wrapper and its recorded
        process group, escalating to KILL, so an agent that ignores TERM goes.
        """
        identity = getattr(node, "exec_identity", None) or {}
        kind = identity.get("kind")
        if kind == "docker":
            executor = self._docker_for(str(identity.get("container") or ""))
        elif kind in {"local", "unknown"}:
            executor = None
        else:
            executor = self.executor(self.config.agents.get(node.agent))
            kind = getattr(executor, "kind", "local")
        if kind in {"docker", "unknown"}:
            killed = False
            with contextlib.suppress(Exception):
                if kind == "docker" and executor is not None:
                    killed = executor.kill_detached(node.id)
            inside = getattr(executor, "inside", None)
            if not (inside and inside()):
                # Every pid in the run dir but `node.pid` was recorded in the
                # container's pid namespace, where it names nothing of ours:
                # signalled from the host it could reach any process group.
                # All the host owns is the `docker exec` client, and ending
                # that alone is the one safe thing left to do.
                start = getattr(node, "pid_start", "")
                if not start or not running(node.pid, start):
                    return killed
                with contextlib.suppress(OSError):
                    os.kill(node.pid, signal.SIGTERM)
                return True
            if killed:
                return True
        run_dir = self.paths.run_dir(node.id)
        if (run_dir / "wrapper.pid").is_file():
            return stop_wrapped(run_dir, node.pid, getattr(node, "pid_start", ""))
        # Checked, not merely attempted. The suppression below makes a signal
        # to a stranger indistinguishable from a signal to the agent, and this
        # runs after a restart — the one moment when every recorded pid may
        # belong to something else. killpg reaches a whole process group, so
        # getting it wrong is not a wasted signal, it is someone else's session.
        if not procs.alive(node.pid, getattr(node, "pid_start", "")):
            return False
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            os.killpg(os.getpgid(node.pid), signal.SIGTERM)
            return True
        return False

    async def suspend(self, agent_id: str) -> dict[str, Any]:
        """NC-R40: end a turn in place, freeing its slot only after proof."""
        from .scheduler import suspension
        from .scheduler.store import Store
        predecessor = self._steer_predecessor(agent_id)
        node = self.tree.get(agent_id)
        store = Store(self.paths.root) if node and node.node_id else None

        def finish(result):
            if store and not suspension.completed(store, agent_id, result):
                # A confirmed suspension or a newer invocation owns this
                # run now. Refused evidence cannot publish a terminal tree
                # entry or release that invocation's slot.
                return {"agent_id": agent_id, "predecessor_death_confirmed": False}
            self._settle_holds()
            self.tree.update(agent_id, status=result["status"], reason="",
                             session_id=result["session_id"])
            self._release(agent_id)
            return {"agent_id": agent_id, "completed": True}

        # A finalized turn or a wrapper's natural exit is independent of the
        # scheduler's stop intent. Still require death proof before releasing.
        result = suspension.natural_result(self.paths, node) if node else None
        if (node and (result or (node.status not in ACTIVE | PAUSED | {"quota_paused"}
                                and node.reason != "window suspended"))
                and await self._steer_predecessor_dead(predecessor)):
            return finish(result or {"id": node.id, "status": node.status,
                                     "session_id": node.session_id,
                                     "turn_started_at": node.turn_started_at})
        if predecessor.absent:
            return {"agent_id": agent_id, "predecessor_death_confirmed": False}
        if store and not suspension.starting(store, agent_id):
            return {"agent_id": agent_id, "completed": True}
        self.tree.set_status(agent_id, "pending", "window stopping")
        await self.stop(agent_id, internal=True)
        confirmed = await self._steer_predecessor_dead(predecessor)
        if confirmed:
            node = self.tree.get(agent_id)
            result = suspension.natural_result(self.paths, node) if node else None
            if result:
                return finish(result)
            if store and not suspension.confirmed(store, agent_id, node):
                raise RuntimeError("suspension could not record termination confirmation")
            self._settle_holds()
            self.tree.set_status(agent_id, "idle", "window suspended")
            self._release(agent_id)
        return {"agent_id": agent_id, "predecessor_death_confirmed": confirmed}

    async def stop(self, agent_id: str, *, internal: bool = False,
                   release_terminal_hold: bool = True) -> dict[str, Any]:
        """End this run's current turn.

        `internal=True` is steer()'s own use: it ends the turn so the same run
        can be respawned under the same id, and must not report the run as
        cancelled while that is happening — see `run.internal_stop` in
        `_consume`. A genuine parent-initiated stop (the default, and the only
        thing `stop_agent` ever asks for) still writes `cancelled` here.
        Automatic timeouts leave terminal unknown-liveness holds intact.
        """
        if not internal:
            node = self.tree.get(agent_id)
            if node and node.node_id and not getattr(self, "_scheduler_supervisor", False):
                from .scheduler import stop_managed
                refusal = await asyncio.to_thread(stop_managed, self.paths.root, agent_id, self.self_id())
                if refusal.get("error"):
                    return refusal
            self._qh_cancel(agent_id)
        if not internal and release_terminal_hold:
            released = await self._stop_blocked_steer(agent_id)
            if released is not None:
                return released
        predecessor = None
        if not internal:
            with contextlib.suppress(Exception):
                predecessor = self._steer_predecessor(agent_id)
        run = self.runs.get(agent_id)
        if run is not None:
            run.stop_requested = True     # recorded before the cancel lands
            run.internal_stop = internal
        if run and run.task and not run.task.done():
            run.task.cancel()
            try:
                await run.task
            except (asyncio.CancelledError, Exception):
                pass
            # A task cancelled before its first step never ran `_consume`, so
            # nothing stopped the process it was about to read.
            if run.handle and run.handle.returncode is None:
                await run.handle.stop()
            # Nor ended the post-mortem claim its launch set (PC-R3f).
            with contextlib.suppress(Exception):
                self._end_slot_claim(agent_id, run.slot_token)
            if not internal:
                # Nor does a cancelled run reach the release at the end of
                # `_consume`, and whoever waits on it (consult, a drain) would
                # wait forever on a run that has ended. steer's internal stop
                # is not an end: it relaunches.
                run.done.set()
                self._pc_kick(run.provider.name)
        elif run and run.handle:
            await run.handle.stop()
        else:
            # Stopping an agent this process did not spawn. node.pid is the
            # local process — under docker that is the `docker exec` CLIENT, and
            # killing it leaves the agent running inside the container spending
            # tokens with nobody reading its output. Ask the executor to reach
            # in, then fall back to the local pid.
            node = self.tree.get(agent_id)
            if node:
                # SV-R10: the verdict before the kill. A server still following
                # this node (another process: `multiagents stop <id>`) sees the
                # exit a moment later, and `_finalize` keeps a `cancelled` it
                # finds rather than filing the kill as a failure.
                if not internal:
                    self.tree.set_status(agent_id, "cancelled", "stopped by parent")
                await asyncio.to_thread(self.stop_detached, node)
        if internal:
            return {"agent_id": agent_id, "status": "stopping"}
        self.tree.set_status(agent_id, "cancelled", "stopped by parent")
        confirmed = False
        if predecessor is not None:
            with contextlib.suppress(Exception):
                confirmed = await self._steer_predecessor_dead(predecessor)
        return {"agent_id": agent_id, "status": "cancelled",
                "predecessor_death_confirmed": confirmed}

    async def _stop_blocked_steer(self, agent_id: str) -> dict[str, Any] | None:
        """LV-R3/SF-R3: stop every known identity before a steer hold release.
        Independent releases still belong to the same retryable owner."""
        hold = self._holds.get(agent_id)
        if hold is None:
            node = self.tree.get(agent_id)
            record = node.cleanup_hold if node else None
            cleanup = record.get("steer_cleanup") if isinstance(record, dict) else None
            if not isinstance(cleanup, dict):
                return None
            self._settle_holds()
            hold = self._holds.get(agent_id)
            if hold is None:
                node = self.tree.get(agent_id)
                if node is not None and not node.cleanup_hold:
                    return None          # adoption settled the cleanup
                return {"agent_id": agent_id, "status": "held",
                        "predecessor_death_confirmed": False,
                        "error": "the hold could not be adopted; retry stop_agent "
                                 "when its owner or storage is available"}
        cleanup = hold.steer
        if cleanup is None:
            return None
        reason = cleanup.get("blocked_reason") or "predecessor death is not confirmed"
        saved = cleanup.get("predecessor") or hold.record
        pid, start = saved.get("pid"), saved.get("pid_start") or ""
        identity = saved.get("executor") or {"kind": "unknown"}
        node = self.tree.get(agent_id)
        if not pid:
            pid, start = hold.pid, hold.pid_start
        elif not start and hold.pid == pid:
            start = hold.pid_start
        if node is not None:
            if not pid:
                pid, start = node.pid, node.pid_start or ""
            elif not start and node.pid == pid:
                start = node.pid_start or ""
        for candidate in (hold.record.get("executor"), node.exec_identity if node else None):
            if identity.get("kind") == "local" or (
                    identity.get("kind") == "docker" and identity.get("container")):
                break
            if isinstance(candidate, dict):
                identity = candidate
        if identity.get("kind") != "local" and not (
                identity.get("kind") == "docker" and identity.get("container")):
            identity = {"kind": "unknown"}
        # A bare pid may have been reused. Docker's container-side records
        # identify its wrapper and agent independently of the host client.
        pid = pid if start else None
        has_container = identity.get("kind") == "docker" and identity.get("container")
        confirmed = False
        if node is not None and (pid or has_container):
            predecessor = replace(node, pid=pid, pid_start=start, exec_identity=identity)
            with contextlib.suppress(Exception):
                await asyncio.to_thread(self.stop_detached, predecessor)
            probe = _unknown_probe
            with contextlib.suppress(Exception):
                probe = self._identity_probe(identity, agent_id)
                if has_container:
                    executor = self._docker_for(str(identity["container"]))
                    verdict = getattr(executor, "wrapper_verdict", None)
                    if verdict is not None:
                        # A daemon/transport failure's exit code is not a
                        # positive answer from the container-side session.
                        probe = lambda: verdict(agent_id)
                confirmed = await self._confirm_ended(pid, start, probe, bound=1.0)
        self.tree.set_status(agent_id, "cancelled", "terminal steer hold released by stop_agent")
        cleanup["operator_release"] = now()
        cleanup["predecessor_death_confirmed"] = confirmed
        hold.record = dict(hold.record, operator_release=cleanup["operator_release"],
                           predecessor_death_confirmed=confirmed)
        cleanup.pop("blocked_reason", None)
        cleanup["steps"] = [s for s in cleanup["steps"] if s not in {"confirm", "inspect"}]
        if not cleanup.get("launch_owned"):
            cleanup["foreign"] = False
        hold.confirmed = True
        hold.then = ("cancelled", "terminal steer hold released by stop_agent")
        self._persist_hold(agent_id, hold)
        self.tree.emit(agent_id, "steer_hold_released", reason=reason, action="stop_agent",
                       predecessor_death_confirmed=confirmed)
        self._settle_holds()
        self._settle_holds()
        result = {"agent_id": agent_id, "status": "cancelled", "released_steer_hold": True,
                  "cleanup_pending": agent_id in self._holds,
                  "predecessor_death_confirmed": confirmed}
        if not confirmed:
            result["warning"] = "hold released by operator; predecessor death is unconfirmed"
        return result

    def _predecessor_executor(self, node: Node | None, spec: AgentSpec | None):
        """SR-R2: ask the execution backend that actually launched the turn."""
        executor = self.executor(spec)
        identity = (node.exec_identity or {}) if node else {}
        if identity.get("kind") == "docker":
            if (getattr(executor, "kind", "") != "docker"
                    or getattr(executor, "container", "") != identity.get("container")):
                recorded = self._docker_for(str(identity.get("container") or ""))
                if recorded is None:
                    raise RuntimeError("the predecessor's container cannot be probed")
                executor = recorded
        elif identity.get("kind") == "local" and getattr(executor, "kind", "") != "local":
            executor = get_executor("local", providers=self.providers)
        elif identity.get("kind") == "unknown":
            raise RuntimeError("the predecessor's execution backend is unknown")
        return executor

    def _steer_predecessor(self, agent_id: str) -> _Predecessor:
        """The identity of the run a steer is about to stop, captured BEFORE
        the stop so its death can be confirmed after it (SF-R3, review r2).
        The in-process Run first — the only predecessor whose supervision
        lock this Runner holds — then the node's recorded pid, for a run
        this process never launched."""
        run = self.runs.get(agent_id)
        node = self.tree.get(agent_id)
        if run is not None and run.handle is not None:
            pid = getattr(run.handle, "pid", None)
            if pid:
                executor = self._predecessor_executor(node, run.spec)
                return _Predecessor(
                    captured=True, pid=pid,
                    pid_start=getattr(run.handle, "pid_start", "")
                    or procs.start_time(pid),
                    probe_raw=self._raw_alive_probe(executor, agent_id),
                    provider=run.provider.name,
                    token=getattr(run, "startup_token", "") or "",
                    executor=executor, handle=run.handle, run=run)
        if node is not None and node.pid:
            executor = self._predecessor_executor(node, self.config.agents.get(node.agent))
            return _Predecessor(
                captured=True, pid=node.pid, pid_start=node.pid_start or "",
                probe_raw=self._raw_alive_probe(executor, agent_id),
                provider=node.provider, executor=executor)
        if node is not None and node.turn_started_at:
            return _Predecessor(captured=True, probe_raw=_unknown_probe)
        return _Predecessor(captured=True)

    async def _steer_predecessor_dead(self, predecessor: _Predecessor) -> bool:
        """SR-R2: use SF's positive-death check, including raw Docker liveness."""
        if not predecessor.captured:
            return False
        if predecessor.absent:
            return True
        return await self._confirm_ended(predecessor.pid, predecessor.pid_start,
                                         predecessor.probe_raw)

    def _settle_steer_cleanup(self, node_id: str, hold: _Hold) -> None:
        """SF-R3/R3a: retry ONE owner's remaining steps. Failed reads and
        writes leave their step owed; only positive death permits release.
        The steer's own resources are independent of predecessor liveness.
        """
        cleanup = hold.steer
        if cleanup.get("operator_release"):
            hold.confirmed = True
        if cleanup.get("blocked_reason") and not (
                set(cleanup["steps"]) & {"claim", "unreserve", "restore"}):
            if not hold.durable:
                self._persist_hold(node_id, hold)
            return
        if cleanup["foreign"] and not cleanup.get("launch_owned"):
            try:
                node = self.tree.get(node_id)
                record = node.cleanup_hold if node is not None else None
                mirrored = (record or {}).get("steer_cleanup")
                if (isinstance(mirrored, dict)
                        and mirrored.get("owner") == cleanup.get("owner")
                        and not mirrored.get("foreign")):
                    # The predecessor owner completed its hold and handed
                    # our mirror over. This cleanup now owns the final lift.
                    hold.record = dict(record)
                    cleanup["foreign"] = False
            except Exception:
                # Independent own releases may proceed, but an unknown
                # predecessor hold cannot be cleared on this pass.
                return
        steps = set(cleanup["steps"])

        def complete(step: str) -> None:
            steps.discard(step)
            cleanup["steps"] = sorted(steps)

        for step in ("claim", "unreserve", "restore"):
            if step not in steps:
                continue
            try:
                if step == "claim":
                    if cleanup["token"] and not self.startup.release(
                            cleanup["provider"], node_id, cleanup["token"]):
                        continue
                elif step == "unreserve":
                    self._pc_unreserve(node_id, cleanup["prior"])
                else:
                    if not hold.durable:
                        self._persist_hold(node_id, hold)
                        if not hold.durable:
                            continue
                    self.tree.restore_deferred(cleanup["queued"])
                complete(step)
            except Exception:
                pass
        if "inspect" in steps:
            try:
                node = self.tree.get(node_id)
                record = node.cleanup_hold if node is not None else None
                if record and not _same_hold(record, hold.record):
                    hold.record = dict(record)
                    then = record.get("then")
                    hold.then = tuple(then) if then else hold.then
                    cleanup["foreign"] = True
                    complete("confirm")
                complete("inspect")
            except Exception:
                pass
        if "confirm" in steps and not cleanup.get("blocked_reason"):
            try:
                if (not cleanup["captured"] and hold.pid
                        and (hold.record.get("executor") or {}).get("kind")
                        in {"local", "docker"}):
                    # Adoption recovered the historical identity, possibly
                    # from wrapper.pid after the owner died before writing it.
                    hold.probe_raw = self._identity_probe(
                        hold.record["executor"], node_id)
                    cleanup["captured"] = True
                    cleanup["absent"] = False
                if not cleanup["captured"]:
                    predecessor = self._steer_predecessor(node_id)
                    hold.pid, hold.pid_start = predecessor.pid, predecessor.pid_start
                    hold.probe_raw = predecessor.probe_raw
                    hold.run = predecessor.run
                    executor = predecessor.executor
                    identity = {"kind": str(getattr(executor, "kind", "local")),
                                "container": str(getattr(executor, "container", ""))}
                    hold.record = dict(hold.record, pid=hold.pid,
                                       pid_start=hold.pid_start, executor=identity)
                    cleanup["captured"] = True
                    cleanup["absent"] = predecessor.absent
                    cleanup["predecessor"] = {
                        "pid": hold.pid, "pid_start": hold.pid_start,
                        "executor": identity}
                if hold.pid and not hold.pid_start:
                    if hold.probe_raw in (None, _unknown_probe):
                        self._block_steer_predecessor(node_id, hold)
                    else:
                        hold.pid = None
                if not cleanup.get("blocked_reason") and (cleanup["absent"] or _positively_ended(
                        hold.pid, hold.pid_start, hold.probe_raw)):
                    hold.confirmed = True
                    complete("confirm")
            except Exception:
                pass
        # A foreign predecessor's hold remains its owner's. Our own steps
        # can finish, but neither its status nor its lock is ours to release.
        if not (steps & {"inspect", "confirm", "claim", "unreserve", "restore"}):
            try:
                if "status" in steps:
                    if cleanup["failure"]:
                        hold.then = ("failed", cleanup["failure"])
                    if cleanup["foreign"]:
                        if hold.then:
                            with self.tree.transaction() as data:
                                raw = data["nodes"].get(node_id) or {}
                                record = raw.get("cleanup_hold")
                                if _same_hold(record, hold.record):
                                    record["then"] = list(hold.then)
                    elif hold.then:
                        current = self.tree.get(node_id)
                        if current is not None and current.status in ACTIVE:
                            self.tree.set_status(node_id, *hold.then)
                    complete("status")
                if "lock" in steps:
                    if not cleanup["foreign"]:
                        self._release(node_id)
                    complete("lock")
                if "lift" in steps:
                    if not cleanup["foreign"]:
                        self._lift_hold(node_id, None, record=hold.record)
                    complete("lift")
            except Exception:
                pass
        if not steps:
            if cleanup.get("launch_owned"):
                hold.steer = None
                hold.record.pop("steer_cleanup", None)
                self._persist_hold(node_id, hold)
                return
            if cleanup["foreign"]:
                # Remove just our mirror, keeping the predecessor hold.
                try:
                    with self.tree.transaction() as data:
                        raw = data["nodes"].get(node_id) or {}
                        record = raw.get("cleanup_hold")
                        if _same_hold(record, hold.record):
                            record.pop("steer_cleanup", None)
                except Exception:
                    return
            self._holds.pop(node_id, None)
            if not cleanup["foreign"] and hold.run is not None:
                if self.runs.get(node_id) is hold.run:
                    self.runs.pop(node_id, None)
                hold.run.done.set()
            self._pc_kick(cleanup["provider"])
            return
        self._persist_hold(node_id, hold)

    async def _steer_release(self, agent_id: str, provider: str, startup_token: str,
                             reserved_from: str | None, queued: dict | None,
                             queued_id: str, live: bool,
                             predecessor: _Predecessor,
                             failure: str = "") -> bool:
        """SF-R3: every refused steer hands its unfinished work to one hold
        BEFORE attempting any cleanup. Settlement and adoption retry the
        same record; no suppressed exception discards a release obligation.
        """
        # Before stopping a live run, startup refusal took no new slot or
        # claim. Its active supervisor still owns the predecessor's lock.
        if live and reserved_from is None and not startup_token:
            return False
        existing = self._holds.get(agent_id)
        mine = (bool(startup_token) and existing is not None
                and existing.phase != "lifting" and existing.token == startup_token)
        if mine:
            # A process actually started: the launch hold already owns all
            # releases, including its claim, until its death is confirmed.
            if failure:
                self._defer_while_held(agent_id, "failed", failure)
            return False
        restore = bool(queued_id and not live and isinstance(queued, dict))
        steps = {"claim", "unreserve", "confirm", "status", "lock", "lift"}
        if restore:
            steps.add("restore")
        cleanup = {"steps": sorted(steps), "provider": provider,
                   "token": startup_token, "prior": reserved_from,
                   "queued": dict(queued) if restore else None,
                   "captured": predecessor.captured,
                   "absent": predecessor.captured and predecessor.absent,
                   "failure": failure, "foreign": False,
                   "launch_owned": False, **self._owner_fields()}
        # Construct without executor/probe calls: a capture that failed must
        # still leave an owner capable of re-capturing when storage recovers.
        executor = predecessor.executor
        identity = {"kind": str(getattr(executor, "kind", "unknown")),
                    "container": str(getattr(executor, "container", ""))}
        cleanup["predecessor"] = {"pid": predecessor.pid,
                                  "pid_start": predecessor.pid_start,
                                  "executor": identity}
        if restore:
            # One receipt per failed admission; a later admission that fails
            # gets a fresh token, while every retry of this cleanup shares it.
            cleanup["queued"]["restore_token"] = os.urandom(16).hex()
            cleanup["queued"].pop("restore_done", None)
        hold = _Hold(
            record={"since": now(), **self._owner_fields(),
                    "pid": predecessor.pid, "pid_start": predecessor.pid_start,
                    "executor": identity, "occupancy": "", "then": None},
            pid=predecessor.pid, pid_start=predecessor.pid_start,
            probe_raw=predecessor.probe_raw, provider=provider, token="",
            phase="cleanup", run=predecessor.run, steer=cleanup)
        if existing is not None and existing.phase != "lifting":
            # The predecessor already has an in-process cleanup owner. Add
            # our obligations to that owner, preserving its task and releases.
            cleanup["foreign"] = True
            cleanup["launch_owned"] = True
            cleanup["steps"] = sorted(steps - {"confirm", "status", "lock", "lift"})
            existing.steer = cleanup
            if failure:
                existing.then = ("failed", failure)
            self._settle_steer_cleanup(agent_id, existing)
            return restore
        try:
            node = self.tree.get(agent_id)
            record = node.cleanup_hold if node is not None else None
            if record and (existing is None or existing.phase != "lifting"):
                # Preserve another launch's durable owner and identity.
                hold.record = dict(record)
                then = record.get("then")
                hold.then = tuple(then) if then else None
                cleanup["foreign"] = True
                remaining = set(cleanup["steps"]) - {"confirm"}
                cleanup["steps"] = sorted(remaining)
        except Exception:
            # Do not replace an unreadable predecessor's durable hold or
            # free its lock. Reading it remains an explicit owed step.
            cleanup["steps"].append("inspect")
        self._holds[agent_id] = hold
        self._settle_steer_cleanup(agent_id, hold)
        return restore

    @property
    def _steer_restores(self) -> dict[str, dict]:
        """Compatibility view; the cleanup holds are the only retry owner."""
        return {h.steer["queued"]["id"]: h.steer["queued"]
                for h in self._holds.values() if h.steer is not None
                and "restore" in h.steer["steps"]}

    @_admitted("the steer")
    async def steer(self, agent_id: str, message: str,
                    queued: dict | None = None, provider: str | None = None) -> dict[str, Any]:
        """SF-R3: per-agent ownership spans predecessor death, transfer and
        launch. The admission gate counts transitions without serialising
        them; no global lock is held across these awaits, so another agent's
        stop/steer/merge can proceed.

        Refuse another steer until this node's handoff and any
        pending cleanup finish. Nothing new is claimed on that refusal."""
        node = self.tree.get(agent_id)
        if provider is not None and node and not node.node_id:
            await self._qh_abort_pending_promotion(
                agent_id, "superseded by explicit provider steer", pinned=provider != "auto")
            node = self.tree.get(agent_id)
        if agent_id in self.__dict__.get("_qh_busy", set()):
            return {"agent_id": agent_id, "error": "quota handover is in progress"}
        if node and (node.handover_attempt or {}).get("state") in ("prepared", "transferred", "launching", "launched"):
            return {"agent_id": agent_id, "error": "quota handover is in progress"}
        if provider is not None and node and node.node_id:
            return {"agent_id": agent_id, "error": "scheduler-managed runs have a frozen provider binding"}
        if provider is not None and node:
            self.tree.update(agent_id, pinned=provider != "auto")
        if provider is not None and node and (provider != node.provider or provider == "auto"):
            active = self.__dict__.setdefault("_steering_nodes", set())
            if agent_id in active:
                return {"agent_id": agent_id, "error": "a steer is in progress"}
            active.add(agent_id)
            try:
                result = await self._qh_manual(node, message, provider)
                if not result.get("steered") and provider != "auto":
                    self.tree.update(agent_id, pinned=node.pinned)
                return result
            finally:
                active.discard(agent_id)
        if node and (self.scheduler_enabled() or node.node_id):
            if node.node_id and agent_id not in self.runs and not getattr(self, "_scheduler_supervisor", False):
                from .scheduler import steer_managed
                return await asyncio.to_thread(steer_managed, self.paths.root, agent_id,
                                               message, self.self_id())
            from .scheduler import resume_admission
            refusal = await asyncio.to_thread(resume_admission, self.paths.root, agent_id,
                                              self.self_id())
            if refusal.get("error") or refusal.get("blocked"):
                return refusal
        if node and node.reason == "session_lost":
            return {"agent_id": agent_id, "steered": False,
                    "error": f"{agent_id}: session_lost — the session is lost. "
                             f"Start a fresh run and give it the old run dir: "
                             f"{self.paths.run_dir(agent_id)}"}
        active = self.__dict__.setdefault("_steering_nodes", set())
        # LV-R4: retry the previous cleanup's death check before refusing a
        # new handoff. A transient unknown is not a permanent steer hold.
        if agent_id not in active:
            self._settle_holds()
        hold = self._holds.get(agent_id)
        cleanup = hold.steer if hold is not None else None
        if cleanup is None:
            node = self.tree.get(agent_id)
            record = node.cleanup_hold if node else None
            cleanup = record.get("steer_cleanup") if isinstance(record, dict) else None
        if agent_id in active or cleanup is not None:
            reason = (cleanup or {}).get("blocked_reason") or (
                "its previous steer or previous steer cleanup is still pending")
            if (agent_id not in active and cleanup is not None
                    and not cleanup.get("blocked_reason") and (
                        "confirm" in cleanup.get("steps", []) or (
                            cleanup.get("operator_release") and not
                            cleanup.get("predecessor_death_confirmed")))):
                reason = ("the predecessor's wrapper or agent is not confirmed dead "
                          "(liveness may be unknown)")
            return {"agent_id": agent_id, "steered": False,
                    "error": f"{agent_id} cannot be steered: {reason}",
                    **({"blocked": True} if queued else {})}
        active.add(agent_id)
        try:
            return await self._steer(agent_id, message, queued)
        finally:
            active.discard(agent_id)

    async def _steer(self, agent_id: str, message: str,
                     queued: dict | None = None, *, window_resume: bool = False) -> dict[str, Any]:
        """Redirect a running agent.

        A subprocess cannot be injected into mid-run, so the honest equivalent
        is to stop the current turn and resume the same session with the
        steering text. The agent keeps its context because both CLIs support
        resuming by session id.

        PC-R3b: a live run keeps its own slot across the handoff; a run that
        holds none (a resume) is admitted on its own provider, and queued
        there when it is full — never moved. `queued` is that resume's entry,
        being drained (PC-R3a).
        """
        node = self.tree.get(agent_id)
        if node is None:
            raise KeyError(f"Unknown agent {agent_id!r}")
        node = self.authoritative(node, "steer")
        if node is None:
            return {"agent_id": agent_id, "steered": False,
                    "error": "entry id differs from its key"}
        if not self.unrecorded_branch_ok(node, "steer"):
            return {"agent_id": agent_id, "steered": False,
                    "error": "branch is outside the container domain"}
        if self.authority and not self.authority.get(agent_id):
            operand = Path(node.worktree) if node.worktree else self.paths.worktree(agent_id)
            if not self.authority.safe_nested_path(operand):
                self.mismatch(agent_id, "steer", ["worktree"],
                              "unrecorded worktree is outside the container domain")
                return {"agent_id": agent_id, "steered": False,
                        "error": "unrecorded worktree is outside the container domain"}
        if not node.session_id:
            return {
                "agent_id": agent_id, "steered": False,
                "error": "no session id captured yet; the agent has not produced "
                         "enough of its stream to be resumable. Try again shortly, "
                         "or stop it and start a fresh run.",
            }
        # Resume as the run is actually executing, not as the agent is
        # configured: a run routed to a fallback at spawn time has a spec and
        # provider that disagree with the static config, and building the
        # resume from the wrong one hands `_launch` the preferred provider's
        # model with the fallback's options still attached (bug-ad011c). The
        # in-process Run carries the mutated spec from spawn, the same pair
        # `_handle_finish`'s silent-failure retry uses; when no Run survives
        # (a restart, or a node this server never itself launched), rebuild it
        # from the live node the same way `start()` built it in the first
        # place.
        run = self.runs.get(agent_id)
        # SR-R3: refuse a stale or missing configuration before stopping the
        # predecessor. Only limits are fresh; the launch's route stays frozen.
        try:
            if getattr(self, "config_error", ""):
                raise ValueError(self.config_error)
            current = self.config.agent(node.agent).routed(node.provider)
        except (KeyError, ValueError) as exc:
            return {"agent_id": agent_id, "steered": False,
                    "error": f"refusing to steer: current config is unavailable: {exc}"}
        if run is not None:
            spec, provider = run.spec, run.provider
        else:
            # RT-R2: the session lives on the recorded provider, so without a
            # model there the steer is refused, not moved: another provider
            # cannot resume it, and `--model ""` is not a model. FS-R2: a
            # session bound to an unlisted family sibling is the exception —
            # it resumes on its recorded provider (`resume=True`), never for
            # new work.
            configured = self.config.agent(node.agent)
            if not self.launch_limits.spec(node.id) and not node.model_pinned and self._usable_spec(
                    configured, node.provider, resume=True) is None:
                return {
                    "agent_id": agent_id, "steered": False,
                    "error": f"agent {node.agent!r} has no model for provider "
                             f"{node.provider!r}, where this run's session "
                             f"lives: `models.{node.provider}` is missing or "
                             f"names no model in agents.yaml. Add it to steer "
                             f"this run, or start a fresh one.",
                }
            spec, provider = self._spec_of(node)

        transport_error = self._transport_refusal(provider)
        if transport_error:
            return {"agent_id": agent_id, "steered": False, "error": transport_error}

        fresh_fields = ("timeout", "silence_timeout", "max_steps")
        spec = replace(spec, **{key: getattr(current, key) for key in fresh_fields},
                       set_fields=(frozenset(spec.set_fields or ()) - set(fresh_fields))
                       | (frozenset(current.set_fields or ()) & set(fresh_fields)))
        try:
            limits = self._limits_for(agent_id, spec)
            # Validate configuration before stopping, but construct the
            # supervisor inside the launch's SF-owned failure region.
            int(spec.max_steps or self.config.limits.get("max_steps", 250))
            repeats = int(self.config.limits.get("doom_loop_repeats", 5))
            int(self.config.limits.get("doom_loop_rearm", repeats))
        except (ValueError, TypeError, OverflowError) as exc:
            return {"agent_id": agent_id, "steered": False,
                    "error": f"refusing to steer: current limits are invalid: {exc}"}

        if node.model_pinned:
            refusal = await self._pin_health(spec.replace(provider=provider.name))
            if refusal:
                return {**refusal, "steered": False}

        # PS-R6: the respawn must be admittable before the live run is
        # stopped. The run was admitted when it started; a roster change
        # since must not be carried into a relaunch of a model the provider
        # no longer allows — and must not cost the live run to find out.
        refusal = self._model_refusal(provider.name, spec.model)
        if refusal:
            message = f"refusing to steer {agent_id}: {refusal}"
            return {"agent_id": agent_id, "steered": False, "reason": message,
                    "error": f"{message} The live run was left untouched."}

        # SC-R3a: a session whose provider or model is capped is never
        # resumed — refused now, naming every binding cap, its spend and the
        # latest reset. Nothing is queued: the same steer resumes it once
        # every cap permits it.
        capped = self._cap_refusal(provider.name, spec.model or "")
        if capped:
            reason = self._with_concurrency(capped["reason"], [provider.name])
            return {"agent_id": agent_id, "steered": False, "cause": capped["cause"],
                    "reason": reason, "caps": capped["caps"],
                    "until": spendcap.iso(capped["until"]),
                    # PC-R3a (#8): a queued resume refused by a cap keeps its
                    # place, re-checked when the cap's reset comes.
                    **({"blocked": True, "retry_after": capped["until"]} if queued else {}),
                    "error": f"refusing to steer {agent_id}: {capped['reason']}. "
                             f"Its session, branch and worktree are kept; the "
                             f"same steer_agent resumes it once the period "
                             f"resets or the cap is raised."}

        # A truncated `writes: false` agent may have had its worktree reclaimed
        # by `_drop_if_empty` once its empty branch made it look worth nothing
        # (bug-97a0c7): `branch` and `worktree` are both written as "", and
        # `Path("")` is `.` — a relative Cwd docker refuses outright. Cut a
        # fresh worktree the same way a pruned conversation gets one back in
        # `consult()`.
        workdir = Path(node.worktree) if node.worktree else None
        if workdir is not None and not workdir.is_absolute():
            workdir = None
        # The directory the session was recorded in, and the one it resumes
        # in: the provider keys its transcripts by it, so a checkout that has
        # to come back comes back HERE.
        cwd = workdir or self.paths.worktree(agent_id)

        # SP-R3: nothing is stopped, cut or relaunched for a session that is
        # not there to resume. Resuming it anyway fails inside the CLI with
        # "No conversation found", after the node has been reported running
        # and its branch forked. A live run is not asked: its CLI holds the
        # session and may not have flushed it yet.
        if node.status not in ("running", "pending"):
            refusal = self._missing_session(node, spec, provider, cwd)
            if refusal:
                return {"agent_id": agent_id, "steered": False, "error": refusal}

        # SP-R4: the checkout counts only if it is a worktree on the node's own
        # branch. A plain directory left at that path, or a checkout of some
        # other branch, is not the work being resumed, and launching into it
        # would resume the session detached from every commit it made. A
        # directory the run was given (no branch of its own) is taken as is.
        branch = node.branch
        root_is_repo = gitops.is_repo(self.paths.root)
        if workdir is None or not workdir.is_dir():
            missing = True
        elif node.node_id and branch:
            from .scheduler.results import checkout_branch
            missing = checkout_branch(node, self.authority) != branch
        elif branch and root_is_repo:
            missing = gitops.worktree_branch(workdir, root=self.paths.root) != branch
        else:
            missing = False
        if missing:
            if node.node_id:
                return {"agent_id": agent_id, "steered": False,
                        "error": "managed checkout is missing or differs from its host record"}
            if not root_is_repo:
                return {
                    "agent_id": agent_id, "steered": False,
                    "error": "this run has no working directory left and the "
                             "project is not a git repository, so a new one "
                             "cannot be cut.",
                }
            workdir = cwd
            own = (f"{self.config.branch_prefix}/{node.agent}/"
                   f"{agent_id.removeprefix('ag-')}")
            if not branch and gitops.branch_exists(self.paths.root, own):
                # A node whose branch field was cleared still has its branch
                # under the name it was cut with.
                branch = own
            if branch:
                # SP-R4: the node's own branch, never a new `-2` cut off base
                # beside it. Its commits are the work being resumed.
                if not gitops.branch_exists(self.paths.root, branch):
                    return {
                        "agent_id": agent_id, "steered": False,
                        "error": f"this run's worktree is gone and its branch "
                                 f"{branch!r} no longer exists, so there is no "
                                 f"work to resume it on. Start a fresh run with "
                                 f"start_agent instead.",
                    }
                aside = None
                if workdir.exists() or workdir.is_symlink():
                    # Kept, never deleted: it may hold the only copy of
                    # something.
                    try:
                        if self.authority:
                            with self.authority.pinned_parent(workdir) as pinned:
                                aside = gitops.move_aside(self.paths.root, pinned).resolve()
                        else:
                            aside = gitops.move_aside(self.paths.root, workdir)
                    except (gitops.GitError, OSError, ValueError) as exc:
                        return {"agent_id": agent_id, "steered": False,
                                "error": f"{workdir} is not a checkout of "
                                         f"{branch!r} and could not be moved "
                                         f"aside: {exc}"}
                try:
                    # Reopen without following symlinks after move_aside, then
                    # hold that parent through checkout creation as well.
                    pinned = (self.authority.pinned_parent(workdir) if self.authority
                              else contextlib.nullcontext(workdir))
                    with pinned as checkout:
                        gitops.attach_worktree(self.paths.root, checkout, branch)
                        if not checkout.samefile(workdir):
                            raise OSError("worktree path changed during checkout creation")
                except (gitops.GitError, OSError, ValueError) as exc:
                    moved = (f" What was at that path is now at {aside}."
                             if aside is not None else "")
                    return {"agent_id": agent_id, "steered": False,
                            "error": f"could not check {branch!r} out again at "
                                     f"{workdir}: {exc}{moved}"}
                self.tree.update(agent_id, worktree=str(workdir), branch=branch)
            elif spec.writes:
                return {
                    "agent_id": agent_id, "steered": False,
                    "error": f"this run's worktree is gone and it has no branch "
                             f"recorded, nor a branch {own!r}, so there is no "
                             f"work to resume it on. Start a fresh run with "
                             f"start_agent instead.",
                }
            else:
                # A `writes: false` run whose empty branch `_drop_if_empty`
                # reclaimed (bug-97a0c7): it had no commits, so there is no
                # work to fork. It gets a checkout under its own name again,
                # exactly that name and never a suffixed one.
                base = self.config.base_branch or gitops.current_branch(self.paths.root)
                try:
                    if self.authority and self.authority.get(agent_id):
                        self.authority.rebind(agent_id, "", workdir)
                    result = gitops.run(self.paths.root, "worktree", "add", "--detach",
                                        str(workdir), base)
                    if not result.ok:
                        raise gitops.GitError(result.err or result.out)
                    branch = ""
                except gitops.GitError as exc:
                    return {"agent_id": agent_id, "steered": False,
                            "error": f"could not cut {own!r} again at "
                                     f"{workdir}: {exc}"}
                self.tree.update(agent_id, worktree=str(workdir), branch=branch)

        # PS-R5a: the provider's ability to take the RELAUNCH is checked —
        # and its startup claim taken — BEFORE the live process is stopped.
        # Steering stops the run and respawns it; a respawn whose claim
        # cannot be had (the provider is startup_down and not claimable as a
        # probe) would kill a healthy, progressing run for nothing, which is
        # how the wrap-up steer once ended nodes as `failed: Provider 'x' is
        # startup_down`. Refused here, the live run is left untouched,
        # pinned or not. The claim is handed to `_launch` so the window
        # between this check and the respawn cannot close.
        if self._held(agent_id):
            # RM-R1d: its previous launch's process is not confirmed dead.
            # Refused before the claim and before the stop below, so nothing
            # is taken and nothing live is ended for a relaunch that cannot
            # happen.
            return {"agent_id": agent_id, "steered": False,
                    "error": _held_refusal(agent_id),
                    **({"blocked": True} if queued else {})}
        # PC-R3/R3b: the slot, decided before anything is claimed or stopped.
        queued_id = (queued or {}).get("id") or ""
        current = self.tree.get(agent_id) or node
        live = self._holds_slot(agent_id)
        limited = self._pc_limit(provider.name) is not None
        reserved_from = None          # the status a resume's reservation replaced
        if live:
            if queued_id:
                # Resumed by other means while it waited: it has its slot.
                self.tree.exit_deferred(queued_id, "restarted", agent_id=agent_id)
        else:
            # PC-R2a: a resume takes a tree-wide and a provider slot, in one
            # transaction, whether or not the provider is limited.
            try:
                reserved_from = self._pc_reserve_resume(spec, current, provider.name,
                                                        queued_id)
            except ProviderFull as full:
                if queued_id or window_resume:
                    return {"agent_id": agent_id, "steered": False, "pc_full": True,
                            "gone": full.gone, "reason": str(full)}
                entry = self.tree.enqueue(
                    provider.name,
                    {"op": "resume", "node_id": agent_id, "agent": node.agent,
                     "session_id": node.session_id, "model": spec.model,
                     "pinned": bool(node.model_pinned), "effort": spec.effort or "",
                     "message": message, "task": message},
                    str(full), deferred_by=self.self_id(),
                    dispatcher=self._owner_fields())
                self._pc_kick(provider.name)
                return {"agent_id": agent_id, "steered": False, "deferred": True,
                        "queued": True, "reason": str(full),
                        "deferred_id": entry["id"], "provider": provider.name,
                        "note": f"queued on {provider.name}, where this run's "
                                f"session lives ({PC_CAUSE}); it resumes by itself "
                                f"when a slot frees there. cancel_deferred removes it."}
            except RuntimeError as exc:
                await self._steer_release(
                    agent_id, provider.name, "", current.status, queued,
                    queued_id, False, _Predecessor())
                return {"agent_id": agent_id, "steered": False, "error": str(exc),
                        **({"blocked": True} if queued_id else {})}
            except BaseException:
                await self._steer_release(
                    agent_id, provider.name, "", current.status, queued,
                    queued_id, False, _Predecessor())
                raise
        try:
            startup_token = self.startup.claim(provider.name, agent_id)
        except StartupUnavailable as exc:
            await self._steer_release(
                agent_id, provider.name, "", reserved_from, queued, queued_id,
                live, _Predecessor())
            if queued_id:
                return {"agent_id": agent_id, "steered": False, "blocked": True,
                        "reason": exc.reason, "retry_after": exc.retry_after}
            refusal = {
                "agent_id": agent_id, "steered": False, "reason": exc.reason,
                "error": f"provider {provider.name!r} is {exc.reason}, so the "
                         f"live run cannot be relaunched after a steer; it was "
                         f"left untouched. Retry once the provider recovers, or "
                         f"stop it and start a fresh run elsewhere.",
            }
            if exc.retry_after:
                refusal["retry_after"] = exc.retry_after
            return refusal
        except BaseException:
            await self._steer_release(
                agent_id, provider.name, "", reserved_from, queued, queued_id,
                live, _Predecessor())
            raise
        # `internal=True`: this ends the turn to respawn the very same run, not
        # a cancellation, and must not report the run as `cancelled` while
        # that is in flight (bug-8195f2) — see `run.internal_stop`.
        predecessor = _Predecessor()
        try:
            if live and limited:
                # PC-R3b: the run keeps its slot across the handoff. `pending`
                # always holds one, so the gap between the old process's death
                # and the new one's start is not a free slot to anyone else.
                reserved_from = self._pc_hold_slot(agent_id)
            # SF-R3 (review r3 finding 2): the capture runs inside this
            # region too — an executor or storage failure here must still
            # give back the claim, the reservation and a claimed queue entry,
            # like every other pre-spawn refusal.
            predecessor = self._steer_predecessor(agent_id)
            await self.stop(agent_id, internal=True)
            # Preserve a concrete launch error even when death is uncertain.
            # This is read-only preflight; no replacement or shared-file
            # cleanup occurs until the gate below has confirmed death.
            if not predecessor.absent and not provider.adapter:
                found = provider.resolve_bin()
                if found.launcher is None:
                    raise FileNotFoundError(provider.bin_error(found))
            if not await self._steer_predecessor_dead(predecessor):
                await self._steer_release(
                    agent_id, provider.name, startup_token, reserved_from,
                    queued, queued_id, live, predecessor)
                return {"agent_id": agent_id, "steered": False,
                        "error": "refusing to steer: the predecessor's wrapper or "
                                 "agent is not confirmed dead (liveness may be unknown)",
                        **({"blocked": True} if queued else {})}
        except BaseException as exc:
            # Nothing was relaunched. SF-R3, review r2 finding 2: this path
            # applies the same rule as a refusal — a predecessor confirmed
            # dead with no hold frees the inherited lock; a live one keeps
            # it, and the node is left to whatever owns the predecessor.
            await self._steer_release(agent_id, provider.name, startup_token,
                                      reserved_from, queued, queued_id, live,
                                      predecessor, failure=str(exc))
            raise
        try:
            await self._launch(
                node_id=agent_id, spec=spec, provider=provider, prompt=message,
                workdir=workdir, branch=branch,
                parent=node.parent, depth=node.depth, session_id=node.session_id,
                startup_token=startup_token,
                # SF-R3 (review r2 finding 1): a steer's inherited lock may
                # still have a live predecessor under it; `_steer_release`
                # decides its fate.
                release_lock=False, window_resume=window_resume,
            )
        except SpendCapRefused as exc:
            # SC-R3a/R3b: a crossing landed since the check above; the
            # session stays resumable. The one release path gives back the
            # startup claim, this steer's own reservation and a queued
            # resume's entry in its place (#8), then the cap verdict settles
            # the node.
            restored = await self._steer_release(
                agent_id, provider.name, startup_token, reserved_from, queued,
                queued_id, live, predecessor)
            self._mark_cap_refused(agent_id, node.session_id, exc.refusal)
            blocked = ({"blocked": True, "retry_after": exc.refusal["until"]}
                       if restored else {})
            return {"agent_id": agent_id, "steered": False, "cause": exc.refusal["cause"],
                    "until": spendcap.iso(exc.refusal["until"]), "error": str(exc),
                    "reason": str(exc), **blocked}
        except RuntimeError as exc:
            # SF-R3: a refusal before the spawn — the shutdown one, a roster
            # or hold refusal — leaves this steer's own resources behind it.
            # The one release path gives them back and settles the node from
            # the predecessor's confirmed liveness (review r2 finding 1): a
            # live predecessor keeps its lock and is never marked failed.
            restored = await self._steer_release(
                agent_id, provider.name, startup_token, reserved_from, queued,
                queued_id, live, predecessor, failure=str(exc))
            return {"agent_id": agent_id, "steered": False, "error": str(exc),
                    **({"blocked": True} if restored else {})}
        except BaseException as exc:
            # Review r1 finding 1: every other pre-spawn failure — an
            # executor that could not start the process, a storage error —
            # releases the same own resources before it propagates. Review
            # r2 finding 3: it settles the node exactly as the RuntimeError
            # branch does, so a dead predecessor is not left reported as an
            # active run.
            await self._steer_release(agent_id, provider.name, startup_token,
                                      reserved_from, queued, queued_id, live,
                                      predecessor,
                                      failure=str(exc) or type(exc).__name__)
            raise
        self.tree.set_status(agent_id, "running", "steered")
        effective_limits = self.runs[agent_id].limits
        self.tree.emit(agent_id, "steer_limits", effective_limits=effective_limits)

        # `_launch` returns when the process has STARTED, which is not the same
        # as it being alive. A run that dies immediately — an unauthenticated
        # provider answers in well under a second — was reported as
        # `{"steered": true, "status": "running"}` against a process already
        # gone, and the caller then waited for progress that could not come.
        # Reported by the bug-reporter as bug-cee638.
        # Wait for whichever comes first: the run producing its first event, or
        # the run ending. A fixed sleep would be a race in both directions —
        # blocking a healthy agent for no reason, and still losing to a failure
        # that takes longer than the timeout. The first event is the closest
        # thing to a "ready" signal these CLIs offer.
        run = self.runs.get(agent_id)
        heard = False
        if run is not None:
            deadline = time.monotonic() + STEER_CONFIRM_SECONDS
            while time.monotonic() < deadline:
                if run.done.is_set() or run.events:
                    heard = bool(run.events)
                    break
                await asyncio.sleep(0.05)
        node = self.tree.get(agent_id)
        if heard and node is not None and node.status in ("done", "idle", "refused"):
            # Not a run that died: one that heard the message, answered it and
            # finished inside the window. Read from a file (SV-R1), a short
            # turn arrives in a single poll, so this is the common case for a
            # quick reply, not a corner of one.
            self.tree.emit(agent_id, "steered", message=message[:400], confirmed=True)
            return {"agent_id": agent_id, "steered": True, "status": node.status,
                    "confirmed": True}
        # bug-1b2612: `stuck` is not terminal — a trip only fires on a process
        # that is actually running, so a respawned run the watchdog flags
        # `stuck` within the confirm window is still live and may go on to
        # finish the instructed work. Only a status in TERMINAL means the
        # process actually ended without acting; `stuck` (and any other
        # ACTIVE/PAUSED status) is reported as steered, not as a failure.
        if node is not None and node.status in TERMINAL:
            return {
                "agent_id": agent_id, "steered": False, "status": node.status,
                "error": f"the respawned run ended immediately "
                         f"({node.status}: {node.reason or 'no reason recorded'}). "
                         f"The message was not acted on.",
            }
        self.tree.emit(agent_id, "steered", message=message[:400], confirmed=heard)
        # Started is not the same as answering. The loop above ends either
        # because the run spoke or because the window ran out, and reporting
        # both as plain "running" is what bug-4374b7 is about: three silent
        # agents were steered, all three returned `steered: true`, one resumed
        # and two never produced another event — and there was no way to tell
        # the cases apart except waiting several more minutes by hand.
        status = node.status if node is not None else "running"
        result = {"agent_id": agent_id, "steered": True, "status": status,
                  "confirmed": heard}
        if node is not None and node.status == "stuck":
            result["note"] = (
                f"the respawned run is alive but the watchdog already flagged "
                f"it stuck ({node.reason or 'no reason recorded'}). That does "
                f"not mean the message was ignored — check it again before "
                f"assuming the steer failed.")
        elif not heard:
            result["note"] = (
                f"the process restarted and is alive, but said nothing within "
                f"{STEER_CONFIRM_SECONDS:.0f}s. That is normal for an agent whose "
                f"first move is a long tool call, and it is also what a wedged "
                f"one looks like. Check it again before assuming the steer landed; "
                f"if it is still silent, stop_agent keeps the branch and worktree.")
        return result

    def _holds_slot(self, node_id: str) -> bool:
        """Does this node hold a slot now, by the one counting rule?"""
        raw = self.tree.read()["nodes"].get(node_id)
        return raw is not None and any(True for _ in self._occupying({node_id: raw}))

    def _pc_reserve_resume(self, spec: AgentSpec, node: Node, provider: str,
                           queued_id: str) -> str:
        """PC-R2a/R3: a relaunch's slots — tree-wide and provider — reserved
        in one transaction with their checks (and with the claim of its queue
        entry, when drained): the node is written `pending`, owned by this
        server until its process runs. Returns the status it replaced, for
        `_pc_unreserve`; raises ProviderFull, or the tree-wide refusal
        (RuntimeError) exactly as start() would."""
        self._settle_holds()
        max_concurrent = int(self.config.limits.get("max_concurrent", 4))
        prior, full = "", None
        claim = self._slot_claim("reservation")
        snapshot = self._slot_snapshot()
        with self.tree.transaction() as data:
            entry = data["nodes"].get(node.id)
            prior = (entry or {}).get("status", "")
            active = self._occupants(data["nodes"], exclude=node.id, snapshot=snapshot)
            if active < max_concurrent:
                full = self._pc_admit(data, provider, node.id, queued_id, snapshot)
                if full is None and entry is not None:
                    entry["status"] = "pending"
                    entry["slot_owner"] = claim
        if active >= max_concurrent:
            raise self._refuse_full(spec, active, max_concurrent)
        if full is not None:
            raise full
        if queued_id:
            self._pc_dequeued(queued_id, provider, node.id, "resume")
        return prior

    def _pc_hold_slot(self, node_id: str) -> str | None:
        """PC-R3b: pin a live run's slot for the steer handoff."""
        claim = self._slot_claim("reservation")
        with self.tree.transaction() as data:
            entry = data["nodes"].get(node_id)
            if entry is None or entry.get("status") not in ACTIVE:
                return None
            prior = entry.get("status", "")
            entry["status"] = "pending"
            entry["slot_owner"] = claim
            return prior

    def _pc_unreserve(self, node_id: str, prior: str | None) -> None:
        """Give back a reservation the steer did not use: only a node still
        `pending` is touched (once running, the slot is the run's)."""
        if prior is None:
            return
        with self.tree.transaction() as data:
            entry = data["nodes"].get(node_id)
            if entry is not None and entry.get("status") == "pending":
                entry["status"] = prior
                entry["slot_owner"] = None

    def _missing_session(self, node, spec: AgentSpec, provider: Provider,
                         cwd: Path) -> str:
        """Why `node`'s session cannot be resumed from `cwd`, or "" if it can.

        Only for a provider that declares where its sessions live; one that
        declares nothing is not second-guessed. Looked for where the host
        really finds it, through the executor the node runs under (SP-R2): a
        docker agent's transcript is in the container's profile, not the
        host's.
        """
        path = session_transcript(provider, cwd, node.session_id, self.executor(spec))
        if path is None:
            return ""
        if not path.is_file():
            return (f"session {node.session_id} cannot be resumed: no transcript "
                    f"for it in {path.parent} (looked for {path.name}). It was "
                    f"lost or never written, and resuming it would start a run "
                    f"with no conversation behind it. Nothing was changed; start "
                    f"a fresh run with start_agent instead.")
        if not _holds_a_record(path):
            return (f"session {node.session_id} cannot be resumed: its "
                    f"transcript {path} holds no complete record, so there is "
                    f"no conversation to resume. Nothing was changed; start a "
                    f"fresh run with start_agent instead.")
        return ""

    # ---------------------------------------------------------- conversation --

    def _find_conversation(self, agent_name: str) -> Node | None:
        """The standing conversation node for this agent, if one exists.

        Found by scanning the shared tree rather than an in-process map, so a
        conversation survives a server restart and is visible to nested agents.
        """
        best: Node | None = None
        for key, raw in self.tree.read()["nodes"].items():
            if raw.get("agent") != agent_name or not raw.get("conversation"):
                continue
            node = node_from_raw(raw, key)
            if best is None or node.created_at > best.created_at:
                best = node
        # Only the newest conversation may be resumed or announced as lost;
        # an older eligible one must not displace a newer failed/cancelled one.
        if best and (best.reason == "session_lost" or
                     (best.status in {"idle", "running", "stuck", "refused"}
                      and best.session_id)):
            return best
        return None

    def _conversation_route(self, spec: AgentSpec, node: Node) -> AgentSpec | None:
        """The spec a standing conversation resumes as, or None if the roster
        no longer allows the provider it lives on (CX-C28).

        Observed at L7: the roster moved an advisor to another provider, and
        the next consult resumed the old session on the old one — a turn spent
        where the roster said not to, answered as the old model. Whether the
        node's provider is still a route is `_usable_spec`'s answer, the same
        one start and steer get (RT-R3).

        FS-R2: a session already bound to a family sibling the agent never
        listed is still resumed there (`resume=True`) — the session lives on
        that provider, and replacing it would strand context the user can no
        longer reach. It is never chosen for new work.
        """
        return self._usable_spec(spec, node.provider, resume=True)

    def _usable_spec(self, spec: AgentSpec, provider: str,
                     pinned_model: str = "",
                     resume: bool = False) -> AgentSpec | None:
        """What `spec` runs as on `provider`, or None when it has no model
        there (RT-R1..R3). Start, steer and consult all ask this, so what
        counts as a usable model cannot drift between them.

        A route is exact — the agent's own provider, or a `models:` entry that
        names a model — or same-family, which resolves the model of a route
        for a listed key (FO-R1c) or, with `resume`, for a session already
        bound to an unlisted sibling; it never adds a candidate (FS-R2). An
        empty fallback model is not a route: it would run `--model ""`.
        """
        routed, _ = self._routed_spec(spec, provider, pinned_model=pinned_model,
                                      resume=resume)
        return routed

    def _routed_spec(self, spec: AgentSpec, provider: str,
                     pinned_model: str = "",
                     resume: bool = False) -> tuple[AgentSpec | None, str]:
        """`_usable_spec`, plus the name of the `models:` route that decided
        it — "" when the agent's own configuration is what runs.

        RM-R5b needs the route, not just the resolution: whether an effort
        was EXPLICITLY written on this destination is judged against the
        route the model came from, which is not always the provider routing
        landed on (a listed family route runs its own model). The walk is
        `_usable_spec`'s own, kept in one place so the two answers cannot
        drift.

        FS-R1/FS-R2: a destination is a route only when it is the agent's own
        provider or a key of its `models:` map. An unlisted family sibling is
        not a route for new work. `resume=True` (steer, a standing
        conversation, `_spec_of`, a queued restart) also resolves one, because
        the session is bound to the provider it was recorded on and is never
        moved (the FS-R2 legacy clause); it is never offered to the chooser.

        FO-R1: the agent's OWN provider is a route too. When `models.P` exists
        its options are merged over the top-level values (through
        `AgentSpec.routed`), and a per-run `pinned_model` wins over the entry's
        model without losing its options.
        """
        if spec.priorities is not None:
            from .config import priority_entries
            for names, priority_model in priority_entries(spec, self.providers):
                if provider in names:
                    legacy = spec.replace(priorities=None)
                    routed, route = self._routed_spec(legacy, provider, resume=True)
                    routed = routed or legacy.routed(provider)
                    return routed.replace(model=pinned_model or priority_model or routed.model), route
            if not resume:
                return None, ""
        if provider == spec.provider:
            return spec.routed(provider, pinned_model=pinned_model), ""
        alternative, _ = spec.fallback_for(provider)
        if alternative:
            return spec.routed(provider), provider
        here = self.providers.get(provider)
        if here is None:
            return None, ""
        listed = provider in (spec.models or {})
        if not resume and not listed:
            return None, ""
        family = here.family or provider
        roster = self.providers.get(spec.provider)
        if roster is not None and (roster.family or spec.provider) == family:
            # FO-R1c: a same-family destination still takes its own entry —
            # options-only included — over the top-level values.
            return spec.routed(provider), ""
        # A listed options-only route shares the model ids of another LISTED
        # route in its family; an unlisted sibling never contributes (FS-R2).
        for name in (spec.models or {}):
            other = self.providers.get(name)
            if other is not None and (other.family or name) == family:
                alternative, _ = spec.fallback_for(name)
                if alternative:
                    routed = spec.routed(name)
                    if provider != name and listed:
                        # FO-R1c: the landed sibling's own entry wins over the
                        # route's model and options, options-only included.
                        routed = routed.routed(provider)
                    return routed, name
        return None, ""

    @contextlib.asynccontextmanager
    async def _conversation_turn(self, agent_name: str, wait: float):
        """Hold one conversation to one turn at a time (CF-R7).

        The refresh and the turn it prepares are one unit: a second consult
        landing mid-turn would otherwise move the worktree under a running
        agent, or both would race for the worktree's index lock. The second one
        waits. An flock rather than an asyncio.Lock because the callers are
        often different processes — every nested agent consulting the same
        advisor runs its own runner — and flock also excludes a second open in
        this process, so one mechanism covers both. It dies with its holder.

        Yields whether the turn is ours: False when the wait ran out. Only
        contention is waited out; any other error (ENOLCK on a filesystem
        without locks) raises _ConsultLockError at once, since waiting cannot
        fix it and running unlocked is the race CF-R7 exists to prevent.
        """
        import errno
        import fcntl

        self.paths.data.mkdir(parents=True, exist_ok=True)
        name = re.sub(r"[^A-Za-z0-9._-]+", "-", agent_name)
        # SG-R7: `.multiagents` is writable from a container. A link or a FIFO
        # planted as the lock is replaced, never opened; anything that still
        # cannot be opened as a regular file refuses the turn, which never
        # runs unlocked.
        try:
            handle = os.fdopen(gitops._open_file_beneath(
                self.paths.data, (), f"consult-{name}.lock", os.O_RDWR | os.O_CREAT,
                0o644, replace=True), "a+")
        except OSError as exc:
            raise _ConsultLockError(
                f"could not open the lock for {agent_name!r} "
                f"({exc.strerror or exc}); this one did not run") from exc
        try:
            deadline = time.monotonic() + wait
            while True:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as exc:
                    if exc.errno not in (errno.EWOULDBLOCK, errno.EAGAIN,
                                         errno.EACCES):
                        raise _ConsultLockError(
                            f"could not lock {agent_name!r} for this turn "
                            f"({exc.strerror or exc}); this one did not run"
                        ) from exc
                    if time.monotonic() >= deadline:
                        yield False
                        return
                    await asyncio.sleep(0.1)
            yield True
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()

    def _conversation_base(self) -> str:
        """Base as it is now — resolved per turn, never remembered (CF terms)."""
        return self.config.base_branch or gitops.current_branch(self.paths.root)

    def _worktree_view(self, worktree: Path, head: str, base_sha: str,
                       behind: int | None) -> dict[str, Any]:
        """What a turn reads, for the caller (CF-R4), from what the turn
        already resolved — base is looked up once per turn, not per use."""
        # The root, not the worktree: a sha names the same commit in both, and
        # the root's git is the one the host trusts (SG-R4).
        return {"commit": (gitops.short_sha(self.paths.root, head) if head else "") or None,
                "base_commit": (gitops.short_sha(self.paths.root, base_sha)
                                if base_sha else "") or None,
                "behind": behind}

    @staticmethod
    def _consult_result(agent_name: str, node_id: str | None, turn: int | None,
                        view: dict[str, Any] | None = None, **rest: Any,
                        ) -> dict[str, Any]:
        """Every consult result has the same keys, null where unknown."""
        return {"agent_id": node_id, "agent": agent_name, "turn": turn, **rest,
                **(view or {"commit": None, "base_commit": None, "behind": None})}

    def _refresh_conversation(self, node: Node, worktree: Path, base: str,
                              base_sha: str) -> tuple[str, str, int | None]:
        """Bring a resumed conversation's worktree to base, if nothing is lost.

        Returns the one-line notice for the turn's prompt — "" when the view
        neither moved nor is stale (CF-R3) — with the HEAD the turn runs on and
        how far behind base that is. Own work is uncommitted changes that git
        does not ignore, or commits made since the worktree was last placed on
        base (`node.placed_on`) whose content base does not already hold; it is
        never touched (CF-R2), and the turn then runs where it is and is told
        how far behind that is. Neither is an ignored file that base now
        tracks with other content. Any git failure takes the same path and is
        recorded, rather than costing the turn (CF-R5).
        """
        # SG-R4: every git call here is pinned to the project's own paths, and
        # the move runs with nothing the agent wrote executing. A tree that
        # cannot be read that way is not moved: it may hold work.
        root = self.paths.root
        head = ""
        behind: int | None = None

        def not_updated(error: str) -> tuple[str, str, int | None]:
            self.tree.emit(node.id, "worktree_refresh_failed", base=base,
                           error=error[:500])
            stale = (f"is {behind} commit{'s' if behind != 1 else ''} behind it"
                     if behind else "may be behind it")
            return (f"[system] Your worktree could not be updated to the current "
                    f"{base or 'base'} and {stale}, still at "
                    f"{head[:9] or 'its old commit'}: re-read a file before "
                    f"relying on or quoting it.\n\n", head, behind)

        def kept(why: str) -> tuple[str, str, int | None]:
            stale = (f"is {behind} commit{'s' if behind != 1 else ''} behind {base}"
                     if behind else f"does not match the current {base}")
            return (f"[system] Your worktree {stale} and was not updated, "
                    f"because {why}.\n\n", head, behind)

        try:
            head = gitops.head_sha(worktree, root=root)
        except gitops.GitError as exc:
            self.git_unreadable(node.id, worktree, exc)
            return not_updated(str(exc))
        if not base_sha:
            return not_updated(f"base {base!r} does not name a commit")
        if not head:
            return not_updated("the worktree has no readable HEAD")
        if head == base_sha:
            return "", head, 0
        current, in_the_way = False, ""
        try:
            behind = gitops.commits_on(worktree, base_sha, head, root=root)
            status = gitops.status(worktree, root=root)
            # A node from before start points were recorded falls back to
            # "commits base does not hold": the old rule, which can only err
            # towards not moving. Its first move records one.
            own_work = status.ok and bool(status.out.strip() or gitops.holds_unmerged_commits(
                worktree, head, base_sha, since=node.placed_on, root=root))
            if status.ok and own_work:
                # Ahead of base with work of its own: current, not stale —
                # unless base went back past where this worktree was placed.
                current = not behind and not (node.placed_on and not gitops.is_ancestor(
                    worktree, node.placed_on, base_sha, root=root))
            elif status.ok:
                in_the_way = gitops.untracked_in_the_way(worktree, head, base_sha,
                                                         root=root)
        except gitops.GitError as exc:
            self.git_unreadable(node.id, worktree, exc)
            return not_updated(str(exc))
        if not status.ok:
            return not_updated(status.err or status.out)
        if own_work:
            if current:
                return "", head, behind
            return kept(f"it holds work of your own (uncommitted changes or "
                        f"commits not on {base})")
        if in_the_way:
            return kept(f"{base} now tracks {in_the_way}, which your worktree "
                        f"holds as a file git does not track (an ignored one, "
                        f"most likely) with other content, and moving would "
                        f"overwrite it")
        # The move itself: the node's own branch, still checked out, now at
        # base. `reset --keep` refuses rather than overwrites if a tracked
        # file changed since the check above, and keeps HEAD attached. It
        # checks HEAD is still the branch, and says what it is if not.
        try:
            moved = gitops.reset_keep(worktree, base_sha, node.branch, root=root)
        except gitops.GitError as exc:
            self.git_unreadable(node.id, worktree, exc)
            return not_updated(str(exc))
        if not moved.ok:
            return not_updated(moved.err or moved.out)
        self.tree.update(node.id, placed_on=base_sha)
        self.tree.emit(node.id, "worktree_refreshed", base=base,
                       old=head[:12], new=base_sha[:12])
        return (f"[system] Your worktree was updated from {head[:9]} to "
                f"{base_sha[:9]} (the current {base}) since your last turn; "
                f"anything you read on earlier turns may have changed, so re-read "
                f"a file before relying on or quoting it.\n\n", base_sha, 0)

    async def consult(
        self, agent_name: str, message: str, timeout: int | None = None,
    ) -> dict[str, Any]:
        """Ask a conversational agent something and wait for its reply.

        Unlike start_agent, this blocks and returns the answer, and the agent
        keeps its context between calls — the session is resumed rather than
        restarted. That is what makes an actual back-and-forth possible instead
        of a series of amnesiac one-shot queries.
        """
        spec = self.config.agent(agent_name)
        if spec.launch:
            raise PermissionError(
                f"Agent {agent_name!r} is the orchestrator and cannot be consulted."
            )
        if not spec.conversational:
            raise ValueError(
                f"Agent {agent_name!r} is not conversational. Use start_agent for "
                f"task agents, or set `conversational: true` in agents.yaml."
            )
        if self.scheduler_enabled():
            from .scheduler import agent_admission, resume_admission
            conversation = self._find_conversation(agent_name)
            if conversation:
                admission = await asyncio.to_thread(resume_admission, self.paths.root,
                                                    conversation.id, self.self_id())
            else:
                admission = await asyncio.to_thread(agent_admission, self.paths.root,
                                                    agent_name, self.self_id())
            if admission.get("error") or admission.get("blocked"):
                return admission
        # Waiting for the other turn is bounded by how long that turn may run.
        # PC-R3b: ONE deadline covers that wait, the wait for a provider slot
        # and the reply; the slot is waited for no longer than the consult's
        # own timeout.
        limit = self.config.effective_limits(spec, timeout)["timeout"]["value"]
        began = time.monotonic()
        wait = limit + self.CONSULT_LOCK_SLACK_SECONDS
        try:
            async with self._conversation_turn(agent_name, wait) as ours:
                if ours:
                    return await self._consult_turn(agent_name, spec, message,
                                                    timeout, slot_by=began + limit,
                                                    deadline=began + wait)
                error = (f"{agent_name!r} was still answering another consult "
                         f"after {wait:.0f}s; this one did not run")
        except _ConsultLockError as exc:
            error = str(exc)
        node = self._find_conversation(agent_name)
        return self._consult_result(agent_name, node.id if node else None, None,
                                    error=error)

    async def _consult_turn(
        self, agent_name: str, spec: AgentSpec, message: str, timeout: int | None,
        slot_by: float | None = None, deadline: float | None = None,
    ) -> dict[str, Any]:
        """One turn, admitted through the safe-point gate (CW-R2) — counted
        while the worktree is refreshed and the turn launched, not while the
        reply is awaited.

        PC-R3/R3b: on a full provider the turn waits for a slot — outside
        the gate, holding nothing but its place in the provider's queue — up
        to `slot_by`. A release in this process wakes it; one elsewhere is
        seen at the next reconciliation. When the time runs out the waiter
        leaves the queue and the error names the provider, its limit and the
        slot holders, and says so when the caller is one of them."""
        waiter = ""
        refused: ProviderFull | None = None
        try:
            while True:
                await self.gate.wait_granted()
                if self.__dict__.get("_pc_shutting_down"):
                    # Round 7: a consult waiting for a slot ends with the server.
                    node = self._find_conversation(agent_name)
                    return self._consult_result(
                        agent_name, node.id if node else None, None,
                        error=f"{agent_name!r}: {SHUT_TEXT}")
                if deadline is not None and time.monotonic() >= deadline:
                    node = self._find_conversation(agent_name)
                    return self._consult_result(
                        agent_name, node.id if node else None, None,
                        error=f"{agent_name!r}: the consult's deadline passed "
                              f"before its turn could be admitted; nothing was launched")
                with self.gate.enter(f"the consult of {agent_name}") as ticket:
                    try:
                        return await self._consult_turn_admitted(
                            agent_name, spec, message, timeout, ticket,
                            waiter=waiter, deadline=deadline)
                    except ProviderFull as full:
                        if self.scheduler_enabled():
                            return admission_block(full)
                        refused = full
                if refused.gone:
                    waiter = ""             # cancelled from the queue: rejoin
                if not waiter:
                    waiter = self.tree.enqueue(
                        refused.provider,
                        {"op": "consult", "agent": agent_name, "task": message[:500]},
                        str(refused), deferred_by=self.self_id(),
                        claim={"pid": os.getpid(), "at": now(),
                               "start": procs.start_time(os.getpid()) or ""})["id"]
                remaining = (slot_by or 0) - time.monotonic()
                if remaining <= 0:
                    node = self._find_conversation(agent_name)
                    return self._consult_result(
                        agent_name, node.id if node else None, None,
                        error=self._pc_consult_error(agent_name, refused))
                # Round 5, finding 1: a waiting consult reconciles too — a
                # nested server has no adoption loop to do it for it.
                self._pc_reconcile_soon()
                await self._pc_wait(min(remaining, PC_RECONCILE_SECONDS))
        finally:
            if waiter and find_deferred(self.tree.read()["deferred"], waiter):
                self.tree.exit_deferred(waiter, "expired",
                                        reason="the consult stopped waiting")
                self.tree.emit("system", PC_CAUSE, action="expired",
                               provider=refused.provider if refused else "",
                               deferred_id=waiter)

    def _pc_consult_error(self, agent_name: str, refused: ProviderFull) -> str:
        """PC-R3b: why a consult never ran — the provider, its limit and who
        holds its slots now; and, when the caller is one of them, that it is
        waiting on itself."""
        provider = refused.provider
        limit = self._pc_limit(provider)
        holders = self._slot_holders(self.tree.read()["nodes"]).get(provider, []) \
            or refused.holders
        text = (f"consult of {agent_name!r} got no slot on provider {provider!r} "
                f"before its deadline ({PC_CAUSE}): max_concurrent="
                f"{limit if limit is not None else refused.limit}, slots held by "
                f"{', '.join(holders) or 'nobody now (queued work is ahead)'}. "
                f"Nothing was launched.")
        me = self.self_id()
        if me and me in holders:
            text += (f" The caller ({me}) itself holds one of those slots: it is "
                     f"waiting for a slot its own run occupies, a deadlock with "
                     f"this limit. Consult from a run on another provider, or "
                     f"raise max_concurrent for {provider}.")
        return text

    async def _consult_turn_admitted(
        self, agent_name: str, spec: AgentSpec, message: str, timeout: int | None,
        ticket, waiter: str = "", deadline: float | None = None,
    ) -> dict[str, Any]:
        node = self._find_conversation(agent_name)
        destination = spec.provider
        if node is not None and node.reason != "session_lost" \
                and self._conversation_route(spec, node) is not None:
            run = self.runs.get(node.id)
            destination = run.provider if run is not None else node.provider
        transport_error = self._transport_refusal(destination)
        if transport_error:
            return self._consult_result(agent_name, node.id if node else None, None,
                                        error=transport_error)
        turn = 1
        # Base is resolved once per turn and reused by the refresh, the result
        # and the recorded start point alike.
        base = self._conversation_base()
        base_sha = gitops.resolve_commit(self.paths.root, base)
        placed = True       # the worktree was just cut from base this turn
        replaced = None     # the conversation this turn replaces (CX-C28)

        if node is not None:
            # PS-R7: a conversation whose recorded provider no longer allows
            # its model is refused BEFORE anything else is decided about it —
            # including the roster having moved the agent elsewhere (CX-C28),
            # which would otherwise quietly replace it with a new
            # conversation (review ag-a50515, finding 5). The node keeps its
            # session, status and turn count, and nothing is launched.
            refusal = self._model_refusal(node.provider, node.model)
            if refusal:
                raise ValueError(
                    f"refusing to resume {agent_name!r}'s conversation "
                    f"{node.id}: {refusal} Nothing was launched and nothing "
                    f"was changed. To start a new conversation, stop this one "
                    f"(stop_agent {node.id}) and consult again.")
            if node.reason == "session_lost" or self._conversation_route(spec, node) is None:
                # Not resumed anywhere: not on the provider the roster dropped,
                # and its session means nothing to any other. A new
                # conversation on the current roster takes its place.
                replaced = node
                node = None

        # RM-R1a: the id of a turn holding a reserved slot, "" for none. The
        # reservation is written `pending`, so the release below only ever
        # restores a node the launch never reached — one already `running`
        # belongs to its run and is left alone.
        reserved = ""
        if node is None:
            # FO-R1: a new conversation runs on the agent's own provider, and
            # that provider's `models:` entry still applies.
            spec = spec.routed(spec.provider)
            provider = self.providers.get(spec.provider)
            if provider is None or not provider.available():
                raise FileNotFoundError(f"provider {spec.provider!r} is unavailable")
            self._preflight(spec)
            parent = self.self_id()
            depth = self.self_depth() + 1
            node_id = new_id()
            worktree_path = self.paths.worktree(node_id)
            # RM-R5a: the model/effort pair is settled before the worktree and
            # node below, the same side-effect-free point start() uses.
            spec = self._settle_effort(spec, provider, node_id)
            branch = gitops.unique_branch(
                self.paths.root,
                f"{self.config.branch_prefix}/{agent_name}/{node_id.removeprefix('ag-')}")
            node = Node(
                id=node_id, agent=agent_name, provider=provider.name, model=spec.model,
                parent=parent, depth=depth, task=message[:500], branch=branch,
                worktree=str(worktree_path), status="pending", conversation=True,
                # A consult's timeout is per turn, so none is recorded as the
                # run's own (LM-R1b): the next turn must not inherit it.
                effort=spec.effort or "",
                limits=self.config.effective_limits(spec), session=self.session(),
            )
            # PC-R2a: the first turn's node enters the tree with its slots —
            # tree-wide and provider — in one transaction, before anything
            # is cut for it; a full provider raises ProviderFull here, with
            # nothing to undo.
            self._admission_add(spec, node, waiter)
            try:
                if self.authority:
                    self.authority.add(node)
                gitops.create_worktree(self.paths.root, worktree_path, branch, base,
                                       unique=False)
                head = gitops.head_sha(worktree_path)
                node = replace(node, placed_on=head)
                self.tree.update(node_id, placed_on=head)
            except BaseException as exc:
                self._mark_launch_failed(
                    node_id, f"consult did not launch: {type(exc).__name__}: {exc}")
                raise
            prompt = self.compose_prompt(spec, message, node, worktree_path)
            session_id = None
            if replaced is not None:
                reason = (f"replaced by {node_id}: the roster no longer runs "
                          f"{agent_name} on {replaced.provider}")
                if replaced.status == "idle":
                    self.tree.set_status(replaced.id, "cancelled", reason)
                self.tree.emit(node_id, "conversation_replaced",
                               old_agent_id=replaced.id,
                               old_provider=replaced.provider,
                               provider=provider.name,
                               **({"reason": "session_lost",
                                   "requested_session": replaced.requested_session}
                                  if replaced.reason == "session_lost" else {}))
        else:
            node_id = node.id
            # RM-R1: a resumed turn occupies a slot like any other start, and
            # is refused like one when the tree is full — before any side
            # effect, so the conversation stays idle, keeps its session and
            # its turn count, and the same consult succeeds once a slot
            # frees. A new conversation is already checked, in `_preflight`.
            # RM-R1a: the check and the reservation are one transaction, so
            # the slot is ours from this moment; every exit below that does
            # not launch gives it back. PC-R2a: the provider slot too.
            self._admission_reserved(spec, node, waiter)
            reserved = node_id
            try:
                node = self.authoritative(node, "conversation_refresh")
                if node is None:
                    self._release_reserved_slot(node_id)
                    return self._consult_result(agent_name, node_id, None,
                                                error="entry id differs from its key")
                if not self.unrecorded_branch_ok(node, "conversation_refresh"):
                    self._release_reserved_slot(node_id)
                    return self._consult_result(agent_name, node_id, None,
                                                error="branch is outside the container domain")
                if self.authority and not self.authority.get(node_id):
                    operand = (Path(node.worktree) if node.worktree
                               else self.paths.worktree(node_id))
                    if not self.authority.safe_nested_path(operand):
                        self.mismatch(node_id, "conversation_refresh", ["worktree"],
                                      "unrecorded worktree is outside the container domain")
                        self._release_reserved_slot(node_id)
                        return self._consult_result(agent_name, node_id, None,
                                                    error="unrecorded worktree is outside the container domain")
                turn = node.turns + 1
                worktree_path = Path(node.worktree)
                prompt = message
                session_id = node.session_id
                # Resume as the conversation is actually running, not as the agent
                # is configured — the same defect and the same fix as steer()'s
                # (bug-ad011c): a conversation routed to a fallback at an earlier
                # turn has a spec and provider that disagree with the static
                # config, and resuming from the wrong one hands `_launch` the
                # preferred provider's model with the fallback's options still
                # attached. Prefer the in-process Run's mutated spec when one
                # survives; otherwise rebuild it from the live node the way
                # `start()` built it originally.
                # PS-R7 (review ag-3644ef, finding 1): rebuilt, the spec
                # carries the RECORDED model — the session is that model's —
                # not the one the roster names for the route today.
                run = self.runs.get(node_id)
                if run is not None:
                    spec, provider = run.spec, run.provider
                else:
                    spec, provider = self._spec_of(node)
                # RM-R5a: the persisted effort is what this conversation runs
                # with — a model id that declares its own suffix normalised the
                # configured one at first launch, and the node carries the
                # result. Re-deriving from the static config would resurrect the
                # contradicted value.
                if node.effort and spec is not None and spec.effort != node.effort:
                    spec = spec.replace(effort=node.effort)
                if provider is None or not provider.available():
                    self._release_reserved_slot(node_id)
                    raise FileNotFoundError(f"provider {node.provider!r} is unavailable")
                # A conversation outlives its worktree: `clean` prunes worktrees,
                # and a standing advisor keeps its idle node and its session id
                # across all of that. Resuming into a directory that is gone made
                # the CLI fail on chdir with an error naming a path, which reads as
                # a container problem rather than a stale checkout. Cut a fresh
                # worktree and carry the session — the context lives in the
                # provider's session, not in the files.
                recreated = False
                if not worktree_path.is_dir() and gitops.is_repo(self.paths.root):
                    try:
                        recreated = True
                        worktree_path = self.paths.worktree(node_id)
                        desired = (f"{self.config.branch_prefix}/{agent_name}/"
                                   f"{node_id.removeprefix('ag-')}")
                        branch = gitops.unique_branch(self.paths.root, desired)
                        if self.authority and self.authority.get(node_id):
                            self.authority.rebind(node_id, branch, worktree_path)
                        branch = gitops.create_worktree(
                            self.paths.root, worktree_path,
                            branch, base, unique=False,
                        )
                        head = gitops.head_sha(worktree_path)
                        self.tree.update(node_id, worktree=str(worktree_path), branch=branch,
                                         placed_on=head)
                    except BaseException:
                        # RM-R1a: this turn holds a reserved slot and will not
                        # launch — give it back before the error escapes.
                        self._release_reserved_slot(node_id)
                        raise
                    node = self.tree.get(node_id) or node
                    # The session remembers files that the new checkout does not
                    # have. Saying so puts the correction IN the conversation;
                    # without it the agent acts on a directory listing from its
                    # memory and then has to invent a reason its work vanished.
                    prompt = (
                        f"[system] Your working directory was recreated at "
                        f"{worktree_path} and is empty — the previous checkout was "
                        f"cleaned up between turns. Anything you wrote there is "
                        f"gone; what you remember of this conversation is intact.\n\n"
                    ) + prompt
                # The conversation outlives the code it last read: work merged into
                # base between turns must reach this turn (bug-7f6ba7). A worktree
                # just recreated above is already on base.
                if not recreated:
                    placed = False
                    if worktree_path.is_dir():
                        if self.authority and not self.authority.get(node_id):
                            try:
                                with self.authority.pinned_worktree(worktree_path) as pinned:
                                    notice, head, behind = self._refresh_conversation(
                                        node, pinned, base, base_sha)
                            except (OSError, ValueError) as exc:
                                self.mismatch(node_id, "conversation_refresh", ["worktree"], str(exc))
                                notice, head, behind = "", "", None
                        else:
                            notice, head, behind = self._refresh_conversation(
                                node, worktree_path, base, base_sha)
                        prompt = notice + prompt
                    else:
                        head, behind = "", None
            except BaseException:
                # RM-R1a: this exit is not a launch — the reserved slot
                # goes back before the error escapes. (An inner handler
                # above may have released already; the release is a no-op
                # once the node is no longer `pending`.)
                self._release_reserved_slot(node_id)
                raise

        try:
            if placed:
                try:
                    behind = (gitops.commits_on(worktree_path, base_sha, head,
                                                root=self.paths.root)
                              if base_sha and head else None)
                except gitops.GitError as exc:
                    self.git_unreadable(node_id, worktree_path, exc)
                    behind = None
            view = self._worktree_view(worktree_path, head, base_sha, behind)
            self.tree.update(node_id, turns=turn)
        except BaseException:
            # RM-R1a: the view or the turn write faulted after the
            # reservation — this turn will not launch, so its slot goes
            # back before the error escapes.
            if reserved:
                self._release_reserved_slot(node_id)
            raise
        if self.__dict__.get("_pc_shutting_down"):
            # Round 7: the server is shutting down — the reservation goes
            # back and nothing launches.
            if reserved:
                self._release_reserved_slot(node_id)
            else:
                self._mark_launch_failed(node_id, SHUT_TEXT)
            return self._consult_result(agent_name, node_id, None, view,
                                        error=SHUT_TEXT)
        if deadline is not None and time.monotonic() >= deadline:
            # PC-R3b (review finding 11): never launch past the one deadline.
            if reserved:
                self._release_reserved_slot(node_id)
            else:
                self._mark_launch_failed(node_id, "consult deadline passed before launch")
            return self._consult_result(agent_name, node_id, None, view,
                                        timed_out=True,
                                        error="the consult's deadline passed before "
                                              "its turn launched; nothing was run")
        try:
            run = await self._launch(
                node_id=node_id, spec=spec, provider=provider, prompt=prompt,
                workdir=worktree_path, branch=node.branch, parent=node.parent,
                depth=node.depth, session_id=session_id, timeout=timeout,
            )
        except SpendCapRefused as exc:
            # SC-R3a: a capped session is refused, and stays resumable.
            self._mark_cap_refused(node_id, session_id, exc.refusal)
            return self._consult_result(agent_name, node_id, turn, view,
                                        error=str(exc))
        except RuntimeError as exc:
            self._mark_launch_failed(node_id, str(exc))
            return self._consult_result(agent_name, node_id, turn, view,
                                        error=str(exc))
        except BaseException as exc:
            # RM-R1a: a reservation whose launch fails is released — the
            # node must not sit `pending` for ever, holding a slot no run
            # will ever account for. (`failed` above releases its own way.)
            # A first turn's node was created `pending` for this launch and
            # is marked failed like start()'s (review ag-43f57f). Either
            # waits for the launch cleanup's confirmation while it holds.
            if reserved:
                self._release_reserved_slot(node_id)
            else:
                self._mark_launch_failed(
                    node_id, f"consult did not launch: {type(exc).__name__}: {exc}")
            raise
        reserved = ""
        ticket.end()            # launched: the reply is not a transition

        limit = self.config.effective_limits(spec, timeout)["timeout"]["value"]
        bound = limit + 30
        if deadline is not None:
            # PC-R3b: the reply shares the consult's one deadline.
            bound = max(0.0, min(bound, deadline - time.monotonic()))
        try:
            await asyncio.wait_for(run.done.wait(), timeout=bound)
        except (asyncio.TimeoutError, TimeoutError):
            await self.stop(node_id, release_terminal_hold=False)
            return self._consult_result(agent_name, node_id, turn, view,
                                        timed_out=True,
                                        error=f"no reply within {limit}s")

        # A free retry inside `_finalize` replaces `self.runs[node_id]` with a
        # new Run sharing this same `done` event (see `_launch`'s `done=`), so
        # the object this `run` name was bound to before the wait can be the
        # dead first attempt — empty text_parts, no awaiting, no ticket. The
        # live one, whichever attempt actually finished, is always the one
        # `self.runs` holds now.
        run = self.runs.get(node_id) or run
        final = self.tree.get(node_id)
        reply = "\n".join(run.text_parts).strip()
        # The caller believed it was continuing a conversation; the reply
        # itself must say the memory it expected is not there (CX-C28).
        # Prefixed after truncation, so a long reply cannot cut it off.
        notice = "" if replaced is None else (
            f"[system] {agent_name} was moved off {replaced.provider}, so this "
            f"is a new conversation ({node_id}, replacing {replaced.id}); "
            f"nothing said earlier was carried over.\n")
        replacement = {}
        if replaced is not None and replaced.reason == "session_lost":
            replacement = {"conversation_replaced": {
                "previous_agent_id": replaced.id, "reason": "session_lost",
                "requested_session": replaced.requested_session}}
            notice = (f"[system] {agent_name} lost its session (session_lost, "
                      f"requested {replaced.requested_session}); this is a new "
                      f"conversation ({node_id}, replacing {replaced.id}). "
                      f"Nothing said earlier was carried over.\n")
        if run.awaiting:
            # The advisor stopped to ask, not to answer. Returning its partial
            # text would read as a considered reply.
            return {
                "agent_id": node_id, "agent": agent_name, "turn": turn,
                "status": "awaiting_user",
                "asked": run.awaiting["question"],
                "topic": run.awaiting["topic"],
                "proposed_default": run.awaiting["proposed"],
                "partial_reply": notice + reply[-2000:],
                **replacement,
                "note": "this agent asked a question instead of answering; "
                        "resolve it with answer_question before relying on this",
                **view,
            }
        if final and final.status == "refused":
            return self._consult_result(agent_name, node_id, turn, view,
                                        status="refused", reason=final.reason,
                                        reply=notice, note="Rephrase the request or consult again.",
                                        **replacement)
        return {
            "agent_id": node_id,
            "agent": agent_name,
            "turn": turn,
            "status": final.status if final else "unknown",
            "reply": notice + reply[-MAX_SUMMARY_CHARS:],
            **replacement,
            "usage": final.usage if final else {},
            "note": "advisory only — you decide whether to act on this",
            **view,
            **({"ticket": run.ticket, "tickets": run.tickets} if run.ticket else {}),
        }

    @_admitted("the answer")
    async def answer_question(self, question_id: str, answer: str,
                              answered_by: str = "orchestrator") -> dict[str, Any]:
        """Answer a parked agent's question and resume it with its context.

        The record is claimed inside the tree's lock before the agent is
        resumed, so two processes cannot both decide they are the one restarting
        it.
        """
        record = self.tree.get_question(question_id)
        if record is None:
            return {"error": f"unknown question {question_id!r}"}
        if record.get("status") == "answered":
            return {"error": f"{question_id} was already answered by "
                             f"{record.get('answered_by') or 'someone'}",
                    "answer": record.get("answer", "")}

        agent_id = record["agent"]
        node = self.tree.get(agent_id)
        if node is None:
            return {"error": f"question {question_id} refers to unknown agent {agent_id}"}
        if not node.session_id:
            return {"error": f"{agent_id} has no resumable session, so it cannot be "
                             f"answered. Discard it and start a fresh agent with the "
                             f"decision included in the task."}

        run = self.runs.get(agent_id)
        transport_error = self._transport_refusal(run.provider if run else node.provider)
        if transport_error:
            return {"question_id": question_id, "agent_id": agent_id,
                    "resumed": False, "error": transport_error}

        if self.scheduler_enabled() or node.node_id:
            from .scheduler import resume_admission
            refusal = await asyncio.to_thread(resume_admission, self.paths.root,
                                              agent_id, self.self_id())
            if refusal.get("error") or refusal.get("blocked"):
                return refusal
        claimed = self.tree.answer_question(question_id, answer, answered_by)
        if claimed is None or claimed.get("already_answered"):
            return {"error": f"{question_id} was answered by someone else first"}

        topic = record.get("topic") or "your question"
        message = (
            f"Answering your question about {topic}.\n\n"
            f"You asked: {record['question']}\n"
            f"The decision is: {answer}\n\n"
            f"Continue from where you stopped, on that basis."
        )
        result = await self.steer(agent_id, message)
        return {"question_id": question_id, "agent_id": agent_id,
                "answered_by": answered_by, "resumed": result.get("steered", False),
                **({"error": result["error"]} if result.get("error") else {})}

    def _idle_capacity_note(self) -> dict[str, Any]:
        """Capacity, plus a nudge when slots are sitting idle.

        Put in front of the orchestrator at the moment it waits, because that
        is when the decision is actually made. In one real session 74% of the
        wall clock had exactly ONE agent running out of four allowed — the work
        was not smaller, it took four times as long.
        """
        capacity = self.capacity()
        if capacity["free_slots"] and capacity["running"]:
            capacity["note"] = (
                f"{capacity['free_slots']} of {capacity['max_concurrent']} slots are "
                f"idle. Waiting costs nothing only when there is nothing else to start — "
                f"if any independent work exists (a different spec, a different set "
                f"of files), start it before you wait again."
            )
        return {"capacity": capacity}

    def capacity(self) -> dict[str, Any]:
        """Slots in use and slots free.

        Reported back on every wait, because that is the moment the decision is
        made: a session that spends its time with one agent running and three
        slots free is not doing less work, it is taking four times as long to
        do it.
        """
        self._settle_holds()
        running = self._occupants(self.tree.read()["nodes"])
        limit = int(self.config.limits.get("max_concurrent", 4))
        result = {"running": running, "max_concurrent": limit,
                  "free_slots": max(0, limit - running)}
        # PC-R4: which providers have no free slot, named only when any.
        full = sorted(name for name, row in self.provider_slots().items() if row["full"])
        if full:
            result["full_providers"] = full
        return result

    def spend_status(self) -> dict[str, Any]:
        """SC-R5: each metered provider's spend this day, week and month from
        the ledger, and, for each cap, the cap, its period, the spend and
        what remains, the reset and whether admission is refused. A period
        that began before the ledger existed is `partial` (SC-R2a)."""
        at = now()
        try:
            self.ledger.refresh()
        except OSError as exc:
            self._ledger_failed(exc)
            return {"note": spendcap.ACCOUNTING_NOTE, "error": f"{exc}"}
        ledger = self.ledger

        def periods(provider: str, model: str = "") -> dict[str, Any]:
            return {period: {"spend": round(ledger.spend(provider, model, period, at), 6),
                             "since": spendcap.iso(spendcap.period_start(at, period)),
                             "resets_at": spendcap.iso(spendcap.period_end(at, period)),
                             "partial": ledger.partial(period, at)}
                    for period in spendcap.PERIODS}

        def cap_view(cap: spendcap.Cap) -> dict[str, Any]:
            state = ledger.describe(cap, at)
            return {"usd": cap.usd, "period": cap.period, "spend": state["spend"],
                    "remaining": state["remaining"],
                    "resets_at": spendcap.iso(state["resets_at"]),
                    "admission_refused": state["reached"]}

        seen = ledger.providers_seen()
        providers: dict[str, Any] = {}
        for name, provider in self._cap_providers().items():
            if provider.billing == "plan" or not (
                    provider.enabled or provider.spend_cap or name in seen):
                continue
            cap = provider.spend_cap
            entry: dict[str, Any] = {"admission_refused": False,
                                     "periods": periods(name)}
            if cap is not None and cap.usd is not None:
                entry["cap"] = cap_view(spendcap.Cap(name, "", cap.usd, cap.period))
                entry["admission_refused"] = entry["cap"]["admission_refused"]
            # #14: a model's cap lives under its own provider instance, so two
            # instances serving the same model id each show their own.
            models: dict[str, Any] = {}
            for model in (cap.models if cap is not None else {}):
                own = [c for c in cap.caps_for(name, model) if c.model]
                if own:
                    models[model] = {"cap": cap_view(own[0]),
                                     "periods": periods(name, model)}
            if models:
                entry["models"] = models
            providers[name] = entry
        return {"note": spendcap.ACCOUNTING_NOTE,
                "ledger_since": spendcap.iso(ledger.created),
                "providers": providers}

    def provider_slots(self) -> dict[str, dict[str, Any]]:
        """PC-R4: each limited provider (or one with work still queued): its
        limit, the slots in use and who holds them, and its queue length."""
        data = self.tree.read()
        holders = self._slot_holders(data["nodes"])
        out: dict[str, dict[str, Any]] = {}
        for name, here in self.providers.items():
            queue = pc_waiting(data["deferred"], name)
            cleanup = any(raw.get("provider") == name and raw.get("cleanup_hold")
                          for raw in data["nodes"].values())
            if here.max_concurrent is None and not queue and not cleanup:
                continue
            held = holders.get(name, [])
            out[name] = {"max_concurrent": here.max_concurrent,
                         "in_use": len(held), "slot_holders": held,
                         "queued": len(queue),
                         "full": here.max_concurrent is not None
                         and len(held) >= here.max_concurrent}
        return out

    def _recover_stale_restarts(self) -> list[dict[str, Any]]:
        """Settle `restarting` entries whose claimer pid is dead (DQ-R8).

        A drain claims an entry before it restarts it and ends the entry only
        in a later transaction, so a drain that dies mid-restart leaves the
        entry claimed by a pid that will never return. The next drain settles
        it: a node carrying the entry's `deferred_id` means the restart did
        happen — count it and write the missing event; no such node means it
        never happened — back to `waiting` for the next drain. Nothing new is
        started here, so this runs before the pause check.

        The pid check reads the tree outside any transaction, so the requeue
        carries the claim it read (DQ-R12): if a concurrent drain resolved the
        entry in between, the re-check inside requeue_deferred's transaction
        refuses, and a `refused` entry is never put back into rotation.
        """
        resolved: list[dict[str, Any]] = []
        for entry in self.tree.read()["deferred"]:
            if not isinstance(entry, dict) or deferred_malformed(entry):
                continue                          # DQ-R9: skipped, never crash
            if entry.get("status") != "restarting":
                continue
            if procs.alive((entry.get("claim") or {}).get("pid")):
                continue
            # SC-R3: a node a cap refused before its spawn gave the identity
            # up (`_cap_raced_start`); any node still carrying it launched, and
            # is the restart whatever its terminal status (r4 #1).
            carrier = next((n for n in self.tree.read()["nodes"].values()
                            if n.get("deferred_id") == entry["id"]), None)
            if carrier is not None:
                if self.tree.exit_deferred(entry["id"], "restarted",
                                           agent_id=carrier["id"]):
                    resolved.append({"agent": (entry.get("spec") or {}).get("agent"),
                                     "agent_id": carrier["id"]})
            else:
                self.tree.requeue_deferred(entry["id"], entry.get("claim"))
        return resolved

    def _prune_unreadable_entries(self) -> None:
        """Drop list debris that is not an entry at all (DQ-R9).

        A non-dict in the `deferred` list has no id to act on, no spec to
        report, and no `cancel_deferred` can ever name it — leaving it in
        would break every scan of the queue for ever. It is removed on sight.
        A malformed entry that IS a dict keeps its place, shown as
        `malformed`, until the orchestrator cancels it.
        """
        with self.tree.transaction() as data:
            if any(not isinstance(d, dict) for d in data["deferred"]):
                data["deferred"] = [d for d in data["deferred"]
                                    if isinstance(d, dict)]

    def _settle_interrupted(self, entry: dict[str, Any], claim: dict[str, Any]) -> None:
        """Release one claimed entry after the drain itself was interrupted.

        DQ-R10: the claim is released on every exit path, cancellation
        included — an entry left `restarting` behind this server's live pid
        would never be touched again, by recovery or by anything else.

        DQ-R11 decides WHICH way it goes. The node carries `deferred_id` from
        the transaction that created it, so a node found for the entry means
        `start()` really ran before the interruption: the restart happened,
        and returning the entry to the queue would start the task a second
        time. It is settled as `restarted` — the event the drain never got to
        write is written now. No node: the start never happened, and the entry
        goes back to `waiting` for the next drain. The caller re-raises.
        """
        carrier = next((n for n in self.tree.read()["nodes"].values()
                        if n.get("deferred_id") == entry["id"]), None)
        if carrier is not None:
            self.tree.exit_deferred(entry["id"], "restarted", agent_id=carrier["id"])
        else:
            self.tree.requeue_deferred(entry["id"], claim)

    # ------------------------------------------------- provider queue (PC) --

    def _pc_kick(self, provider: str = "") -> None:
        """PC-R3a: a slot may have freed (or a limit moved): wake the consults
        waiting in this process, and drain the queue soon — the release-driven
        half of draining. Never raises; outside an event loop it only wakes.
        Nothing at all once shutdown has begun (round 7)."""
        if self.scheduler_enabled():
            return
        if self.__dict__.get("_pc_shutting_down"):
            return
        for waiter in list(self.__dict__.get("_pc_waiters", ())):
            if not waiter.done():
                with contextlib.suppress(RuntimeError):
                    waiter.get_loop().call_soon_threadsafe(
                        lambda w=waiter: w.done() or w.set_result(None))
        if self.gate.closed:
            # Review finding 5: nothing is claimed while the server stops.
            return
        # A release or a refused admission is also when a dead container run
        # may be what holds the slot: reconcile (in the background, bounded).
        self._pc_reconcile_soon()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        try:
            if not pc_waiting(self.tree.read()["deferred"]):
                return
        except Exception:
            return
        current = self.__dict__.get("_pc_drain_task")
        if current is not None and not current.done() and current.get_loop() is loop:
            self.__dict__["_pc_drain_again"] = True
            return
        self.__dict__["_pc_drain_task"] = loop.create_task(self._drain_all())

    async def _pc_wait(self, seconds: float) -> None:
        """Sleep until a release in this process wakes us, or `seconds`
        (the cross-process reconciliation interval) pass."""
        waiter = asyncio.get_running_loop().create_future()
        waiters = self.__dict__.setdefault("_pc_waiters", set())
        waiters.add(waiter)
        try:
            await asyncio.wait_for(asyncio.shield(waiter), max(0.0, seconds))
        except (asyncio.TimeoutError, TimeoutError):
            pass
        finally:
            waiters.discard(waiter)

    def _pc_mine(self, entry: dict) -> bool:
        """Which server launches a queued entry: a server of the identity
        that queued it (any root server for the root's), whose permissions
        and executor context the launch then has. Ownership follows the
        SERVER (review finding 7): once the server that queued it
        (`dispatcher`, pid and start time) is gone — even with its agent
        still running — a root server takes it over, launching it under the
        recorded parent and depth, never as the root's own child."""
        owner = entry.get("deferred_by")
        me = self.self_id()
        if owner == me:
            return True
        if me is not None:
            return False
        dispatcher = entry.get("dispatcher")
        return not (isinstance(dispatcher, dict) and _owner_alive(dispatcher))

    async def _pc_dispatch(self, entry: dict) -> tuple[str, Any]:
        """Launch one queued entry through the path its operation names: a
        start through `start`, a resume through `steer` (never as a fresh
        start, PC-R3a). Answers launched / full / gone / blocked / refused."""
        spec = entry.get("spec") or {}
        op = spec.get("op") or "start"
        agent = spec.get("agent")
        if op == "retry":
            return await self._pc_retry_launch(entry)
        try:
            if op == "resume":
                node_id = spec.get("node_id") or ""
                result = await self.steer(node_id, spec.get("message") or "",
                                          queued=entry)
            else:
                if not agent or agent not in self.config.agents:
                    return "refused", "agent is no longer in agents.yaml"
                invalid = self._pc_recorded_model_problem(spec)
                if invalid:
                    # PC-R3d: never re-resolved; held, and reported, until
                    # the model is valid there again or the entry is cancelled.
                    return "blocked", {"reason": invalid}
                result = await self.start(
                    agent, spec.get("task") or "", workdir=spec.get("workdir"),
                    timeout=spec.get("timeout"),
                    # PC-R3d: the model recorded at queuing, pinned or not.
                    model=spec.get("model") or None,
                    verifies=spec.get("verifies") or "",
                    budget_tag=spec.get("budget_tag") or "",
                    deferred_id=entry["id"], queued=entry)
        except (ValueError, PermissionError, KeyError) as exc:
            return "refused", f"{agent or spec.get('node_id')}: {exc}"
        except Exception as exc:
            return "blocked", f"{type(exc).__name__}: {exc}"[:300]
        if result.get("pc_full"):
            return ("gone" if result.get("gone") else "full"), result
        if result.get("blocked"):
            return "blocked", {"reason": str(result.get("reason") or result.get("error") or ""),
                               "retry_after": result.get("retry_after")}
        if op == "resume":
            # Claimed in the reservation's transaction: gone from the queue
            # means it was admitted, whatever the launch then did.
            if find_deferred(self.tree.read()["deferred"], entry["id"]) is None:
                return "launched", {"agent": agent, "agent_id": spec.get("node_id")}
            return "refused", str(result.get("error") or result.get("reason") or result)
        if result.get("agent_id"):
            return "launched", {"agent": agent, "agent_id": result["agent_id"]}
        if result.get("retry_after"):
            return "blocked", {"reason": str(result.get("reason") or result.get("error") or ""),
                               "retry_after": result.get("retry_after")}
        return "refused", str(result.get("error") or result.get("reason") or result)

    def _retry_prompt(self, node_id: str, prompt_file: str) -> str:
        # PF-R7: missing durable input is an error, never a different turn.
        if not prompt_file:
            prompt_file = self.launch_limits.prompt_file(node_id)
        if not prompt_file or Path(prompt_file).name != prompt_file:
            raise ValueError("failed launch has no recorded prompt file")
        limit = int(config_mod.limit_number(self.config.limits, "prompt_file_max_bytes"))
        return _run_read(self.paths.run_dir(node_id), prompt_file, limit)

    def _pc_retry_admission(self, run: Run, node: Node, session_id: str) -> dict | None:
        """PC-R3: admit a free retry on its own provider, or queue it.

        Admitted, the node is written `pending` in the same transaction as
        the checks — tree-wide and provider (PC-R2a) — and the slot is the
        retry's until it runs; None is returned. Refused by the provider, an
        `op: retry` entry is queued and the node is marked `failed` with the
        entry named in its reason; the entry is returned. Refused by the
        tree-wide limit, there is no retry: the node is marked `failed` with
        that refusal, and a dict saying so is returned. A transport mismatch
        raises TransportRefused so finalization can distinguish it from a
        failed retry admission."""
        provider = run.provider.name
        transport_error = self._transport_refusal(run.provider)
        if transport_error:
            self.tree.set_status(node.id, "failed", f"free retry refused: {transport_error}")
            raise TransportRefused(transport_error)
        try:
            self._pc_reserve_resume(run.spec, node, provider, "")
            return None
        except RuntimeError as exc:
            if not isinstance(exc, ProviderFull):
                self.tree.set_status(
                    node.id, "failed",
                    f"died in {node.elapsed():.0f}s with no output; its free "
                    f"retry was refused: {exc}")
                return {"refused": str(exc)}
            full = exc
            if self.scheduler_enabled():
                self.tree.set_status(node.id, "failed", f"free retry refused: {full}")
                return {"refused": str(full)}
            entry = self.tree.enqueue(
                provider,
                {"op": "retry", "node_id": node.id, "agent": node.agent,
                 "session_id": session_id or "", "model": run.spec.model,
                 "prompt_file": run.prompt_file,
                 "pinned": bool(node.model_pinned), "task": node.task},
                str(full), deferred_by=self.self_id(),
                dispatcher=self._owner_fields())
            self.tree.set_status(
                node.id, "failed",
                f"died in {node.elapsed():.0f}s with no output; its free retry "
                f"is queued on {provider} ({PC_CAUSE}, {entry['id']})")
            return entry

    async def _pc_retry_launch(self, entry: dict) -> tuple[str, Any]:
        """Dispatch a queued free retry: the same node, the failed turn's input,
        relaunched once its slot is claimed (PC-R3a)."""
        spec_ = entry.get("spec") or {}
        node_id = spec_.get("node_id") or ""
        node = self.tree.get(node_id)
        if node is None or node.status != "failed" or entry["id"] not in (node.reason or ""):
            return "refused", f"{node_id} is no longer waiting for its retry"
        spec, provider = self._spec_of(node)
        transport_error = self._transport_refusal(provider)
        if transport_error:
            return "refused", transport_error
        if self.gate.closed:
            return "blocked", "the server is stopping for a safe point"
        # Review finding 5: admitted through the gate BEFORE the entry is
        # claimed, so a closed gate never costs the entry.
        # SC-R3b (#4): a capped retry is blocked and keeps its place — asked
        # before the entry is claimed, and again at the spawn.
        capped = self._cap_refusal(provider.name, spec.model or "")
        if capped:
            return "blocked", {"reason": self._with_concurrency(capped["reason"],
                                                                [provider.name]),
                               "retry_after": capped["until"]}
        with self.gate.enter("the retry"):
            try:
                prior = self._pc_reserve_resume(spec, node, provider.name, entry["id"])
            except ProviderFull as full:
                return ("gone" if full.gone else "full"), str(full)
            except RuntimeError as exc:
                return "blocked", str(exc)
            try:
                await self._launch(
                    node_id=node_id, spec=spec, provider=provider,
                    prompt=self._retry_prompt(node_id, spec_.get("prompt_file") or ""),
                    workdir=Path(node.worktree), branch=node.branch,
                    parent=node.parent, depth=node.depth,
                    session_id=spec_.get("session_id") or None)
            except SpendCapRefused as exc:
                # A crossing landed between the check and the spawn: the
                # node goes back to waiting for its retry, and the entry to
                # its place in the queue.
                self._pc_unreserve(node_id, prior)
                self.tree.restore_deferred(entry)
                return "blocked", {"reason": self._with_concurrency(
                    exc.refusal["reason"], [provider.name]),
                    "retry_after": exc.refusal["until"]}
            except TransportRefused as exc:
                self._mark_launch_failed(node_id, str(exc))
                self.tree.restore_deferred(entry)
                self.tree.exit_deferred(entry["id"], "refused", reason=str(exc))
                return "refused", str(exc)
            except Exception as exc:
                self._mark_launch_failed(
                    node_id, f"retry launch failed: {type(exc).__name__}: {exc}")
                return "launched", {"agent": node.agent, "agent_id": node_id}
        self.tree.set_status(node_id, "running",
                             "retried once after an unexplained early exit")
        return "launched", {"agent": node.agent, "agent_id": node_id}

    def _pc_recorded_model_problem(self, spec: dict) -> str:
        """PC-R3d: why the model a queued start recorded no longer runs on
        its provider (allowlist or catalog), or "" when it still does."""
        provider, model = spec.get("provider") or "", spec.get("model") or ""
        if not model:
            return ""
        refusal = self._model_refusal(provider, model)
        known = {m.get("id") for m in (self.config.models.get(provider) or [])
                 if isinstance(m, dict)}
        if not refusal and known and model not in known:
            refusal = f"{provider} no longer lists {model!r} in its catalog"
        return (f"the recorded model {model!r} is no longer valid on {provider}: "
                f"{refusal}" if refusal else "")

    def _pc_mark(self, entry_id: str, blocked: str, retry_at: Any = None) -> None:
        """PC-R3a: record (or clear) why a head is skipped. It keeps its
        place; reported once per reason, not on every drain. The verdict
        holds until `blocked_until` — the refusal's own retry time when it
        gave one, else one re-check interval — and is never trusted past it
        (review finding 10): admission then counts the head as eligible
        again, and the next drain re-evaluates it first."""
        changed = False
        until = now() + PC_BLOCKED_RECHECK_SECONDS
        if isinstance(retry_at, (int, float)) and not isinstance(retry_at, bool):
            until = min(until, max(float(retry_at), now()))
        with self.tree.transaction() as data:
            entry = find_deferred(data["deferred"], entry_id)
            if entry is not None:
                if blocked:
                    changed = (entry.get("blocked") or "") != blocked
                    entry["blocked"] = blocked
                    entry["blocked_until"] = until
                else:
                    entry.pop("blocked", None)
                    entry.pop("blocked_until", None)
                provider = (entry.get("spec") or {}).get("provider")
        if changed and blocked:
            self.tree.emit("system", PC_CAUSE, action="blocked", provider=provider,
                           deferred_id=entry_id, reason=blocked)

    async def _drain_provider(self, provider: str) -> list[dict]:
        """PC-R3a: launch this provider's queue in order while it has slots.

        A head blocked for another reason is skipped without losing its
        place; a head this process may not launch (another agent's, or a
        consult that serves itself) stops the walk, since nothing may pass
        it. Claiming an entry is its admission (`_pc_admit`)."""
        launched: list[dict] = []
        while True:
            data = self.tree.read()
            queue = pc_waiting(data["deferred"], provider)
            limit = self._pc_limit(provider)
            if not queue or (limit is not None and len(
                    self._slot_holders(data["nodes"]).get(provider, [])) >= limit):
                return launched
            progressed = False
            for entry in queue:
                if self.gate.closed or self.__dict__.get("_pc_shutting_down"):
                    return launched                 # review finding 5; round 6, 3
                claim = entry.get("claim")
                if isinstance(claim, dict):
                    if _claim_alive(claim):
                        continue                    # it takes the slot itself
                    self.tree.exit_deferred(entry["id"], "expired",
                                            reason="its waiting consult ended")
                    progressed = True
                    break
                if not self._pc_mine(entry):
                    # Another live server's: it dispatches it. Not a reason to
                    # stop here — admission alone decides what may pass it.
                    continue
                outcome, info = await self._pc_dispatch(entry)
                if outcome == "launched":
                    launched.append(info)
                    progressed = True
                    break
                if outcome == "full":
                    self._pc_mark(entry["id"], "")
                    return launched
                if outcome == "gone":
                    progressed = True
                    break
                if outcome == "blocked":
                    detail = info if isinstance(info, dict) else {"reason": str(info)}
                    self._pc_mark(entry["id"],
                                  detail.get("reason") or "refused for another reason",
                                  detail.get("retry_after"))
                    continue
                reason = str(info)
                self.tree.exit_deferred(entry["id"], "refused", reason=reason)
                self.tree.emit("system", PC_CAUSE, action="refused", provider=provider,
                               deferred_id=entry["id"], reason=reason)
                progressed = True
                break
            if not progressed:
                return launched

    async def _drain_queues(self) -> list[dict]:
        """PC-R3a: drain every provider's queue — at a release, at every
        `wait_for_agents`, and as reconciliation (the count is re-derived from
        the tree and pid liveness each time, so a death nobody announced
        frees its slot here).

        One drain at a time per process. A call that arrives during one
        joins it — a `wait_for_agents` must not return while the queue it
        drains is still being launched — and makes it look once more. The
        drain itself is shielded: a caller that gives up does not cut a
        launch in half."""
        if self.gate.closed or self.scheduler_enabled():
            return []
        loop = asyncio.get_running_loop()
        current = self.__dict__.get("_pc_drain_task")
        if current is not None and not current.done() and current.get_loop() is loop:
            self.__dict__["_pc_drain_again"] = True
            return list(await asyncio.shield(current))
        task = loop.create_task(self._drain_all())
        self.__dict__["_pc_drain_task"] = task
        return list(await asyncio.shield(task))

    async def _drain_all(self) -> list[dict]:
        launched: list[dict] = []
        # Round 5, finding 2: a drain never waits on container probes; the
        # reconciliation runs beside it and kicks again on a death it confirms.
        self._pc_reconcile_soon()
        while True:
            self.__dict__["_pc_drain_again"] = False
            providers = dict.fromkeys(
                (d.get("spec") or {}).get("provider")
                for d in pc_waiting(self.tree.read()["deferred"]))
            progressed = False
            for provider in providers:
                got = await self._drain_provider(provider or "")
                launched += got
                progressed = progressed or bool(got)
            if not progressed and not self.__dict__.get("_pc_drain_again"):
                return launched

    async def resume_deferred(self) -> dict[str, Any]:
        """Restart tasks whose quota window has passed. Safe to call often.

        The deferred queue existed and nothing ever drained it, so a task
        deferred on quota stayed deferred forever — the system waited for a
        reset it would never notice. This is the other half of pausing: a pause
        nobody lifts is a stop.

        Each due entry is claimed before it is restarted (DQ-R8): the claim is
        one tree transaction, and a concurrent drain skips what it did not
        claim, so two drains never restart the same entry.
        """
        if self.gate.closed:
            return {"paused": False, "restarted": []}
        await self._qh_reconcile()
        await self._qh_promote_check()
        reserve_restarted = await self._qh_drain_reserve()
        # Recovery follows a launched target, never launches that attempt
        # twice. Adoption is also needed when no ordinary quota entry is due.
        if any(n.handover_attempt and n.handover_attempt.get("state") in
               ("launching", "launched") and n.id not in self.runs for n in self._qh_nodes()):
            await self.adopt()
        if self.scheduler_enabled():
            # CW-R2: the server is stopping; the entries stay queued for the
            # next one, untouched.
            return {"paused": False, "restarted": reserve_restarted}
        self._prune_unreadable_entries()
        recovered = self._recover_stale_restarts() + reserve_restarted
        paused = self.tree.pause_state()          # clears itself when expired
        if paused:
            result: dict[str, Any] = {"paused": True,
                                      "reason": paused.get("reason", ""),
                                      "until": paused.get("until"),
                                      "restarted": recovered}
            if recovered:
                # DQ-R2: the drain did something — a restart was settled — so
                # the result must be reportable, pause or not. Without this,
                # wait_for_any would drop the whole `deferred` field and the
                # restart would be reported nowhere in the result.
                result["still_deferred"] = sum(
                    1 for d in self.tree.read()["deferred"]
                    if isinstance(d, dict) and not deferred_malformed(d)
                    and d.get("status", "waiting") == "waiting")
            # DQ-R2a: refused entries never expire and are never retried, so
            # the count of the ones still queued rides on this return while it
            # is non-zero — a paused tree must not hide the entries it will
            # not be draining.
            refused_total = sum(1 for d in self.tree.read()["deferred"]
                                if isinstance(d, dict)
                                and d.get("status") == "refused")
            if refused_total:
                result["refused_total"] = refused_total
            return result

        # LN-C4, finding 5: the pause is gone — lifted or expired — so nothing
        # is being held back by it any more and its deferral notices stop
        # here rather than at the next successful start, which may never come
        # (the deferred task may have been dropped or re-issued).
        notices.clear(self.tree, lambda e: e.get("effect") == "deferred"
                      and e.get("scope") == "tree")

        # SC-R4a (#13): crossings whose claimer died before recording them.
        self._announce_pending()
        due = self.tree.due_deferred()
        # SC-R3b: a cap raised or removed releases its deferrals now.
        due += [entry for entry in self._cap_released()
                if entry["id"] not in {d["id"] for d in due}]
        if not due and not recovered:
            return {"paused": False, "restarted": []}

        budget_mod.invalidate_cache()             # the window moved; re-read it
        restarted, refused, dropped, stopped = [], [], [], ""
        for entry in due:
            claim = self.tree.claim_deferred(entry["id"])
            if not claim:
                continue                          # another drain claimed it first
            task_spec = entry.get("spec") or {}
            agent = task_spec.get("agent")
            if not agent or agent not in self.config.agents:
                # The roster changed while this waited. Reported rather than
                # dropped silently: the orchestrator believes it is still queued.
                reason = "agent is no longer in agents.yaml"
                dropped.append({"agent": agent, "task": task_spec.get("task", "")[:120],
                                "reason": reason})
                self.tree.exit_deferred(entry["id"], "dropped", reason=reason)
                continue
            # PS-R7a: a NEW entry records the provider it was deferred from,
            # and the restart is held to it. If that provider no longer
            # allows the pinned model, the entry is refused under PS-R7 —
            # checked HERE, before start's pin resolution, because start
            # would legitimately carry the pin to a `models:` fallback, and
            # the recorded destination's refusal is exactly what must prevent
            # the silent re-route to another provider. No recorded provider
            # (a legacy entry) routes as it always did.
            recorded = task_spec.get("provider")
            pinned = task_spec.get("model")
            refusal = self._model_refusal(recorded, pinned) \
                if recorded and pinned else None
            if refusal:
                reason = f"{agent}: {refusal}"
                refused.append({"agent": agent, "deferred_id": entry["id"],
                                "reason": reason})
                self.tree.exit_deferred(entry["id"], "refused", reason=reason)
                continue
            try:
                result = await self.start(
                    agent, task_spec.get("task", ""),
                    workdir=task_spec.get("workdir"), timeout=task_spec.get("timeout"),
                    model=task_spec.get("model"), deferred_id=entry["id"],
                    # PS-R7a: a PINNED entry with a recorded destination may
                    # restart on that destination or its family's routing —
                    # never on a family a roster change moved it to.
                    recorded_provider=(recorded or "") if pinned else "",
                )
            except (ValueError, PermissionError) as exc:
                # DQ-R3b: the request itself cannot be honoured, and waiting
                # will not change that. Kept, marked, never retried.
                result = {"error": f"{agent}: {exc}"}
            except Exception as exc:
                # DQ-R3b: a transient failure (anything that is not a
                # ValueError or PermissionError) leaves the entry queued and
                # stops the drain — one failure is almost always systemic
                # (the window closed again mid-drain), and grinding through
                # the rest turns one problem into a batch of them. The claim
                # goes back first (see _settle_interrupted for which way),
                # then `stopped_on` reports where the drain gave up.
                self._settle_interrupted(entry, claim)
                stopped = f"{type(exc).__name__}: {exc}"[:300]
                break
            except BaseException:
                # DQ-R10: only cancellation reaches here — `except Exception`
                # never saw a CancelledError — an MCP client giving up on the
                # wait — so a cancelled drain used to strand the entry
                # `restarting` behind this server's own live pid,
                # unrecoverable for the process's whole life. Whatever the
                # interruption, the claim goes back first (see
                # _settle_interrupted for which way it goes), then the
                # interruption propagates.
                self._settle_interrupted(entry, claim)
                raise
            if result.get("error") and not result.get("deferred"):
                reason = str(result.get("reason") or result["error"])
                if agent not in reason:
                    reason = f"{agent}: {reason}"
                if task_spec.get("model") and task_spec["model"] not in reason:
                    reason += f" (pinned model {task_spec['model']!r})"
                refused.append({"agent": agent, "deferred_id": entry["id"],
                                "reason": reason})
                fields: dict[str, Any] = {"reason": reason}
                if result.get("agent_id"):
                    # DQ-R3c: a refusal leaves no node; if one exists anyway,
                    # the entry records where it is.
                    fields["node_id"] = result["agent_id"]
                self.tree.exit_deferred(entry["id"], "refused", **fields)
                continue
            if result.get("deferred"):
                # A re-deferral from start() is a NEW entry, so ending the old
                # one here is what stops the queue growing. start() names the
                # entry it created (DQ-R11) — matching it by id, never by
                # diffing the queue, which would take an entry someone else
                # deferred inside this window. The new entry keeps the old
                # one's `deferred_by`, `None` (the orchestrator) included: the
                # drain is a courier, not the deferrer (DQ-R8a/DQ-R4a).
                new_id = result.get("deferred_id")
                if new_id:
                    with self.tree.transaction() as data:
                        fresh = next((d for d in data["deferred"]
                                      if isinstance(d, dict)
                                      and d.get("id") == new_id), None)
                        if fresh is not None:
                            fresh["deferred_by"] = entry.get("deferred_by")
                self.tree.exit_deferred(entry["id"], "re_deferred",
                                        new_deferred_id=new_id)
                break                             # the window closed again
            agent_id = result.get("agent_id")
            if agent_id:
                # DQ-R8: the node records where it came from. Written when the
                # node was created, since DQ-R11; this re-write is a no-op
                # that keeps the invariant if that ever changes.
                with self.tree.transaction() as data:
                    node = data["nodes"].get(agent_id)
                    if node is not None:
                        node["deferred_id"] = entry["id"]
            self.tree.exit_deferred(entry["id"], "restarted", agent_id=agent_id)
            restarted.append({"agent": agent, "agent_id": agent_id})
        still = sum(1 for d in self.tree.read()["deferred"]
                    if isinstance(d, dict) and not deferred_malformed(d)
                    and d.get("status", "waiting") == "waiting")
        result = {"paused": False, "restarted": restarted + recovered,
                  "still_deferred": still}
        if refused:
            result["refused"] = refused
        if dropped:
            result["dropped"] = dropped
            result["note"] = ("these were deferred for an agent that is no longer "
                              "in agents.yaml; re-issue them if they still matter")
        if stopped:
            result["stopped_on"] = stopped
        return result

    async def wait_for_any(self, agent_ids: list[str] | None, timeout: float) -> dict[str, Any]:
        """Block until any of the given agents leaves the running state.

        LN-C3: the result carries `limit_notices`, the limit hits and clears
        this caller has not been shown yet — read when the result is built,
        through one cursor per calling node (LN-C4).
        """
        # Drain the deferred queue first. A task waiting on a quota reset is
        # invisible to active(), so without this an orchestrator polling for
        # work is told there is none while tasks sit ready to restart.
        revived = await self.resume_deferred()
        # PC-R3a: the provider queues drain here too, whatever the pause.
        drained = await self._drain_queues()
        if drained:
            revived = dict(revived, restarted=list(revived.get("restarted", [])) + drained)
            revived["still_deferred"] = sum(
                1 for d in self.tree.read()["deferred"]
                if isinstance(d, dict) and not deferred_malformed(d)
                and d.get("status", "waiting") == "waiting")
        result = await self._wait_for_any(agent_ids, timeout, revived)
        # DQ-R2: the drain's outcome rides on every result that followed one —
        # the "no active agents" and timeout returns included, which is where
        # a refused or dropped entry used to go unreported.
        if "still_deferred" in revived:
            result["deferred"] = {"restarted": revived["restarted"],
                                  "refused": revived.get("refused", []),
                                  "dropped": revived.get("dropped", []),
                                  "still_deferred": revived["still_deferred"]}
        # DQ-R2a: refused entries never expire and are never retried, so the
        # count of the ones still queued rides on every result while it is
        # non-zero — including a wait that drained nothing at all.
        refused_total = sum(1 for d in self.tree.read()["deferred"]
                            if isinstance(d, dict) and d.get("status") == "refused")
        if refused_total:
            result.setdefault("deferred", {})["refused_total"] = refused_total
        shown = notices.since(self.tree, self.self_id() or self.session() or "root")
        if shown:
            result["limit_notices"] = shown
        return result

    async def _wait_for_any(self, agent_ids: list[str] | None, timeout: float,
                            revived: dict[str, Any]) -> dict[str, Any]:
        """Block until any of the given agents leaves the running state.

        Polls the shared tree rather than only in-process events, so an
        orchestrator can also wait on agents started by a nested server.
        """
        # A pause stops new work, not the wait: agents already running are
        # waited on as usual, and every result says whether a pause is in
        # force. Read when the result is built, not now — a pause can expire
        # or begin while we wait.
        def pause() -> dict[str, Any]:
            record = self.tree.pause_state()
            if not record:
                return {}
            return {"paused": True, "reason": record.get("reason", ""),
                    "retry_after_seconds": max(0, int((record.get("until") or 0) - now())),
                    "note": "no provider has headroom; deferred work restarts by "
                            "itself when this clears. Wait rather than re-planning."}

        deadline = time.monotonic() + timeout
        requests = [r for r in self.tree.read().get("quota_reserve", [])
                    if r.get("state") == "requested"]
        if requests:
            return {"status": "awaiting_orchestrator", "reserve_requests": requests,
                    "changed": [], "still_running": [], **pause()}
        if agent_ids:
            watched = list(agent_ids)
        else:
            # active() deliberately excludes parked agents, so seeding from it
            # alone would leave an orchestrator waiting 300s on an agent that is
            # already blocked on a question addressed to it.
            watched = [n.id for n in self.tree.active()]
            watched += [q["agent"] for q in self.tree.open_questions()
                        if q["agent"] not in watched]
            # bug-d6310f: an agent that already failed or finished before this
            # call (e.g. a provider crash within the first few seconds of
            # start()) is in neither set above, so it was reported nowhere at
            # all — not here, not in already_finished, not in still_running.
            # tree.unseen() is exactly "parentless nodes with a result nobody
            # has been told about yet", scoped by session the same way
            # driver.py's _busy() scopes it. Folding it in surfaces the miss
            # through the already-finished path below, and server.py's
            # _seen() marks it seen once reported, so it does not repeat on
            # the next wait_for_any(None) call.
            watched += [n.id for n in self.tree.unseen(self.session())
                        if n.id not in watched]
        watched += [r["agent_id"] for r in revived.get("restarted", [])
                    if r.get("agent_id") and r["agent_id"] not in watched]
        if not watched:
            return {"changed": [], "reason": "no active agents",
                    "still_running": [], "capacity": self.capacity(), **pause()}

        # Agents that had already finished before this call are reported, but
        # are NOT what we wait on. Without this split, calling again with the
        # same id list returns the same finished agent forever and a polling
        # loop never advances.
        already: list[dict] = []
        pending: list[str] = []
        for agent_id in watched:
            node = self.tree.get(agent_id)
            if node is None:
                continue
            if _occupies_slot(node):
                pending.append(agent_id)
            else:
                already.append({
                    "agent_id": agent_id, "agent": node.agent,
                    "status": node.status, "reason": node.reason,
                    **self._no_commits_note(node),
                })

        if not pending:
            return {"changed": already, "all_finished": True,
                    "still_running": [], "capacity": self.capacity(),
                    "note": "every agent you named had already finished", **pause()}

        # SL-R5: an agent that was ALREADY stuck-and-live when the wait began
        # is treated as running-equivalent for the whole wait — it is reported
        # once it truly finishes, not the moment it is first observed stuck,
        # since that moment is now (waiting on it would otherwise be a no-op).
        # An agent that BECOMES stuck DURING the wait is a real state change
        # and is reported at once, as before.
        #
        # Recorded as (pid, reason), not just membership: this poll runs once
        # a second, and a dead process's free retry, or a clear-then-re-trip,
        # can both complete inside one gap between polls. Either one leaves
        # the status reading "stuck" at every poll that ever sees it, with
        # nothing to tell the old episode from the new one except that the
        # pid changed (a relaunch) or the reason did (a different trip) —
        # status and baseline-membership alone cannot catch that.
        baseline_stuck = {i: (n.pid, n.reason) for i in pending
                          if (n := self.tree.get(i)) is not None and n.status == "stuck"}

        def classify() -> tuple[list[dict], list[str], list[dict]]:
            changed_, running_, still_stuck_ = [], [], []
            for agent_id in pending:
                node = self.tree.get(agent_id)
                if node is None:
                    continue
                if node.status in {"pending", "running"}:
                    running_.append(agent_id)
                    # It cleared and is live: no longer the baseline stuck
                    # episode, so a later re-trip is a fresh state change.
                    baseline_stuck.pop(agent_id, None)
                elif node.status == "stuck" and agent_id in baseline_stuck:
                    # SL-R4/SL-R5: baseline_stuck is only "running-equivalent"
                    # while it is actually live AND still the same episode
                    # that was live at baseline (same pid, same trip reason).
                    # A dead process, or a different pid/reason under the
                    # same node_id, means the run this wait was watching has
                    # ended and something else is now `stuck` in its place.
                    if _occupies_slot(node) and (node.pid, node.reason) == baseline_stuck[agent_id]:
                        running_.append(agent_id)
                        still_stuck_.append({
                            "agent_id": agent_id, "agent": node.agent,
                            "reason": node.reason,
                        })
                    else:
                        changed_.append({
                            "agent_id": agent_id, "agent": node.agent,
                            "status": node.status, "reason": node.reason,
                            **self._no_commits_note(node),
                        })
                else:
                    changed_.append({
                        "agent_id": agent_id, "agent": node.agent,
                        "status": node.status, "reason": node.reason,
                        **self._no_commits_note(node),
                    })
            return changed_, running_, still_stuck_

        while time.monotonic() < deadline:
            changed, running, still_stuck = classify()
            if changed:
                # Both lists from one read, so an agent finishing between two
                # reads is not dropped from both.
                return {
                    "changed": changed,
                    "already_finished": already,
                    "still_running": running,
                    "still_stuck": still_stuck,
                    "waited_seconds": round(timeout - (deadline - time.monotonic())),
                    **self._idle_capacity_note(),
                    **pause(),
                }
            await asyncio.sleep(1.0)

        _changed, running, still_stuck = classify()
        return {
            "changed": [],
            "timed_out": True,
            # Named agents that had finished before the wait are reported on a
            # timeout too, not only beside a change: else a mixed list never
            # names them at all.
            **({"already_finished": already} if already else {}),
            **self._idle_capacity_note(),
            "still_running": running,
            "still_stuck": still_stuck,
            **pause(),
        }

    # ------------------------------------------------------------------- git --

    @promotion_merge_guard
    def merge_agent(self, agent_id: str, into: str | None = None) -> dict[str, Any]:
        node = self.tree.get(agent_id)
        if node is None:
            raise KeyError(f"Unknown agent {agent_id!r}")
        if agent_id in self.__dict__.get("_qh_busy", set()) or (node.handover_attempt or {}).get("state") in (
                "prepared", "transferred", "launching", "launched"):
            return {"agent_id": agent_id, "merged": False, "error": "quota handover is in progress"}
        if node.node_id:
            return {"error": "managed_run", "hint": "use node ops"}
        node = self.authoritative(node, "merge_agent")
        if node is None:
            return {"agent_id": agent_id, "result": "blocked",
                    "detail": "entry id differs from its key"}
        if not self.unrecorded_branch_ok(node, "merge_agent"):
            return {"agent_id": agent_id, "result": "blocked", "detail": "branch is outside the container domain"}
        if self.authority and not self.authority.get(agent_id) and node.worktree:
            if not self.authority.safe_nested_path(Path(node.worktree)):
                self.mismatch(agent_id, "merge_agent", ["worktree"],
                              "unrecorded worktree is outside the container domain")
                return {"agent_id": agent_id, "result": "blocked",
                        "detail": "unrecorded worktree is outside the container domain"}
        if self.authority:
            record = self.authority.get(agent_id)
            if record and record["seeded"] and record.get("worktree") and not self.authority.safe_seeded_path(Path(record["worktree"])):
                return {"agent_id": agent_id, "result": "blocked", "detail": "seeded worktree is quarantined"}
        if not node.branch:
            return {"agent_id": agent_id, "merged": False, "error": "agent has no branch (writes: false)"}
        target = Path(into).expanduser() if into else self.paths.root
        base_merge = target.resolve() == self.paths.root.resolve()
        target_branch = ""
        if self.authority and not base_merge:
            # HA-R11: the branch a merge into a checkout advances is the
            # host's to name, never whatever that checkout's HEAD says.
            target_branch = self._merge_target_branch(target)
            if not target_branch:
                reason = "merge target has no host-determined branch"
                self.mismatch(agent_id, "merge_agent", ["into"], reason)
                self.tree.emit(agent_id, "merge", result="blocked", detail=reason,
                               into=str(target))
                return {"agent_id": agent_id, "result": "blocked", "branch": node.branch,
                        "detail": f"{reason}: {target} is not the worktree of a "
                                  f"recorded node, nor of an unrecorded one with a "
                                  f"valid agents/* branch. Nothing was merged."}
        policy = self.config.project.get("git", {}).get("merge", {})

        # Revert-and-report, before the merge. The agent's own work still
        # lands; its edits to files it was not allowed to change do not. This
        # runs in the parent's process, outside the agent's worktree, and an
        # agent cannot call merge_agent on itself (see `_may_act_on`) — which
        # is what makes it enforcement rather than an instruction.
        base = self.config.base_branch or gitops.current_branch(self.paths.root)
        reverted: list[str] = []
        revert_failed = ""
        try:
            violations = self.readonly_violations(node, base)
        except gitops.GitError as exc:
            # Which protected files it changed is unknown, so nothing merges.
            self.git_unreadable(agent_id, self.paths.root, exc)
            self.tree.emit(agent_id, "merge", result="blocked", detail=str(exc)[:400])
            return {"agent_id": agent_id, "result": "blocked", "branch": node.branch,
                    "detail": f"the repository could not be read to check "
                              f"{node.agent}'s changes to protected files: {exc}. "
                              f"Nothing was merged."}
        if violations:
            worktree = Path(node.worktree) if node.worktree else None
            if worktree and worktree.is_dir():
                result = gitops.restore_paths(
                    worktree, base, violations,
                    f"revert {node.agent}'s changes to {len(violations)} protected "
                    f"file(s)\n\n{chr(10).join(violations[:50])}",
                    git=self.agent_git(node),
                )
                if result.ok:
                    reverted = violations
                else:
                    revert_failed = result.err or result.out
            else:
                revert_failed = ("the agent's worktree is gone, so its branch cannot "
                                 "be corrected in place")
            if revert_failed:
                # Refusing is the only honest answer: merging now would carry
                # the edits in, and reporting a revert that did not happen is
                # worse than refusing to merge.
                self.tree.emit(agent_id, "merge", result="blocked",
                               detail=revert_failed[:400], protected=len(violations))
                return {
                    "agent_id": agent_id, "result": "blocked", "branch": node.branch,
                    "readonly_violations": violations[:50],
                    "detail": f"{node.agent} modified {len(violations)} protected "
                              f"file(s) and they could not be reverted: "
                              f"{revert_failed}. Nothing was merged.",
                }
            self.tree.emit(agent_id, "readonly_revert", paths=reverted[:50],
                           count=len(reverted), base=base)

        host_hooks = base_merge and bool(policy.get("host_hooks", False))
        host_content = base_merge and bool(policy.get("host_content_programs", False))
        status, detail = gitops.merge(
            target, node.branch, f"{node.agent}: {node.task[:72]}",
            policy.get("style", "squash"), root=self.paths.root,
            host_hooks=host_hooks, host_content_programs=host_content,
            target_branch=target_branch,
        )
        git_detail = detail
        if base_merge and not host_hooks and self.paths.root.resolve() not in _HOST_HOOK_NOTIFIED:
            configured = gitops.run(self.paths.root, "config", "--path", "--get",
                                    "core.hooksPath")
            hooks = (Path(configured.out) if configured.ok and configured.out
                     else self.paths.root / ".git" / "hooks")
            if not hooks.is_absolute():
                hooks = self.paths.root / hooks
            if hooks and hooks.is_dir() and any(p.is_file() and os.access(p, os.X_OK)
                                               for p in hooks.iterdir()):
                notice = "host merge hooks skipped (git.merge.host_hooks: false)"
                detail = f"{detail}\n{notice}" if detail else notice
                self.tree.emit(agent_id, "host_git_notice", detail=notice)
                _HOST_HOOK_NOTIFIED.add(self.paths.root.resolve())
        if not host_content:
            detail = f"{detail}\nHost content programs disabled (git.merge.host_content_programs: false)"
        self.tree.emit(agent_id, "merge", result=status, detail=git_detail[:400], into=str(target))
        if "host_authority_mismatch" in git_detail:
            self.mismatch(agent_id, "merge_agent", ["worktree", "branch"],
                          git_detail[:400])
        if status == "merged":
            self.tree.set_status(agent_id, "merged")
            self._cleanup(node, completion="merged",
                          commit=gitops.head_sha(target, root=self.paths.root))
        reap_pending_branches(self.paths.root, self.tree, self.authority)
        payload = {"agent_id": agent_id, "result": status, "detail": detail[:1000],
                   "branch": node.branch}
        if node.status != "done":
            payload["status_before_merge"] = node.status
            payload["warning"] = f"Agent was not reported complete ({node.status}) before merge."
        if reverted:
            payload["readonly_reverted"] = reverted[:50]
            payload["readonly_note"] = (
                f"{len(reverted)} file(s) {node.agent} may not modify were reverted "
                f"to {base} before merging; everything else it did was merged. If it "
                f"was editing a test to make its code pass, the merged result now "
                f"has that test failing — which is the outcome you want, and yours "
                f"to resolve."
            )
        return payload

    def _merge_target_branch(self, target: Path) -> str:
        """HA-R11: the branch a merge into `target` may advance, or "".

        The recorded branch of the recorded node whose worktree `target`
        resolves to. Failing that, for a checkout inside the container domain,
        the branch of the one unrecorded node there, or else of the one
        registration the host finds by its `gitdir` (never through the
        checkout's own `.git`); either way it must pass HA-R2a. Paths compare
        resolved, so a symlinked spelling of a recorded checkout still finds
        its record.
        """
        try:
            want = target.resolve()
            records = self.authority.read()
            held = [r for r in records.values() if isinstance(r.get("worktree"), str)
                    and r["worktree"] and Path(r["worktree"]).resolve() == want]
            if held:
                branch = held[0].get("branch")
                return (branch if len(held) == 1 and isinstance(branch, str)
                        and branch.startswith("agents/") else "")
            if not self.authority.safe_nested_path(want):
                return ""
            found = [(key, raw) for key, raw in self.tree.read()["nodes"].items()
                     if key not in records and isinstance(raw, dict)
                     and isinstance(raw.get("worktree"), str) and raw["worktree"]
                     and Path(raw["worktree"]).resolve() == want]
        except (OSError, ValueError, RuntimeError):
            return ""
        if len(found) > 1 or (found and self.tree.id_mismatch(found[0][0])):
            return ""
        if found:
            branch = found[0][1].get("branch")
        else:
            try:
                branch = gitops._registration_branch(self.paths.root, want)
            except gitops.GitError:
                return ""
        if not isinstance(branch, str) or not self.authority.safe_unrecorded_branch(branch):
            return ""
        return branch

    def discard_agent(self, agent_id: str, force: bool = False) -> dict[str, Any]:
        node = self.tree.get(agent_id)
        if node is None:
            raise KeyError(f"Unknown agent {agent_id!r}")
        if node.node_id:
            return {"error": "managed_run", "hint": "use node ops"}
        node = self.authoritative(node, "discard_agent")
        if node is None:
            return {"agent_id": agent_id, "discarded": False,
                    "error": "entry id differs from its key"}
        if not self.unrecorded_branch_ok(node, "discard_agent"):
            return {"agent_id": agent_id, "discarded": False, "error": "branch is outside the container domain"}
        if self.authority and not self.authority.get(agent_id) and node.worktree:
            if not self.authority.safe_nested_path(Path(node.worktree)):
                self.mismatch(agent_id, "discard_agent", ["worktree"],
                              "unrecorded worktree is outside the container domain")
                return {"agent_id": agent_id, "discarded": False,
                        "error": "unrecorded worktree is outside the container domain"}
        if self.authority:
            record = self.authority.get(agent_id)
            if record and record["seeded"] and record.get("worktree") and not self.authority.safe_seeded_path(Path(record["worktree"])):
                self.mismatch(agent_id, "discard_agent", ["worktree"], "seeded worktree is quarantined")
                return {"agent_id": agent_id, "discarded": False, "error": "seeded worktree is quarantined"}
        base = self.config.base_branch or gitops.current_branch(self.paths.root)
        try:
            unmerged = (gitops.commits_on(self.paths.root, node.branch, base,
                                          root=self.paths.root) if node.branch else 0)
        except gitops.GitError as exc:
            self.git_unreadable(agent_id, self.paths.root, exc)
            if not force:
                return {"agent_id": agent_id, "discarded": False,
                        "error": f"could not read the branch to count its unmerged "
                                 f"commits: {exc}. Pass force=true to delete it "
                                 f"anyway."}
            unmerged = 0
        if unmerged and not force:
            return {
                "agent_id": agent_id, "discarded": False, "unmerged_commits": unmerged,
                "error": f"branch has {unmerged} unmerged commit(s). Pass force=true to "
                         f"delete this work permanently.",
            }
        if self._cleanup(node, completion="discarded") is None:
            return {"agent_id": agent_id, "discarded": False,
                    "error": "worktree or branch could not be cleaned safely"}
        self.tree.set_status(agent_id, "discarded", "discarded by parent")
        reap_pending_branches(self.paths.root, self.tree, self.authority)
        return {"agent_id": agent_id, "discarded": True, "branch": node.branch}

    def push_branch(self, agent_id: str | None, remote: str | None = None) -> dict[str, Any]:
        target_remote = remote or self.config.remote
        if not target_remote:
            return {"pushed": False, "error": "no remote configured (git.remote is empty in project.yaml)"}
        if agent_id:
            node = self.tree.get(agent_id)
            if node is None or not node.branch:
                return {"pushed": False, "error": f"no branch for {agent_id!r}"}
            node = self.authoritative(node, "push_branch")
            if node is None:
                return {"pushed": False, "error": "entry id differs from its key"}
            if not self.unrecorded_branch_ok(node, "push_branch"):
                return {"pushed": False, "error": "branch is outside the container domain"}
            if not self.config.push_agent_branches:
                return {
                    "pushed": False,
                    "error": "push_agent_branches is false; agent branches are not published "
                             "by default. Merge into the base branch and push that instead, "
                             "or enable it in project.yaml.",
                }
            branch = node.branch
        else:
            branch = gitops.current_branch(self.paths.root)
        result = gitops.push(self.paths.root, target_remote, branch)
        return {"pushed": result.ok, "branch": branch, "remote": target_remote,
                "detail": (result.err or result.out)[:500]}


def _branch_released(node: dict) -> bool:
    """Whether a node's branch is no longer anyone's work (SG-R2): its status
    is terminal, and a `done` node's worktree is gone too. A `done` node that
    still has one is waiting on its parent to merge or discard it."""
    status = node.get("status")
    if not isinstance(status, str) or status not in TERMINAL:
        return False
    if status == "done":
        worktree = node.get("worktree")
        return not (isinstance(worktree, str) and worktree and Path(worktree).is_dir())
    return True


def reap_pending_branches(root: Path, tree: Tree,
                          authority: HostAuthority | None = None) -> int:
    """Delete the branches a container could not (SG-R2), and clear the marks.

    `tree.json` is written by the container too, so a mark is only a request.
    It is carried out when it names the branch of the very node that holds it,
    that node is terminal (a `done` one only once its worktree is gone), no
    node still at work holds the same branch, and the branch is under
    `refs/heads/agents/`. Anything else is left alone, mark included. Once the
    branch is gone, the mark and the node's `branch` are both cleared. Never
    raises: it runs on every Runner start. Returns how many were cleared.
    """
    if authority is None and not DockerExecutor({}, ProjectPaths(root), {}, state_root()).inside():
        authority = HostAuthority(ProjectPaths(root), tree)
    try:
        nodes = tree.read().get("nodes", {})
    except Exception:
        return 0
    marked = [(node_id, n) for node_id, n in nodes.items()
              if isinstance(n, dict) and n.get("branch_pending_delete")]
    if not marked:
        return 0
    # HA-R12: an entry whose branch is not a string holds no branch, and
    # carries out no mark; it is skipped with an event, never raised on.
    held = {n.get("branch") for n in nodes.values()
            if isinstance(n, dict) and isinstance(n.get("branch"), str)
            and not _branch_released(n)}
    cleared = 0
    for node_id, node in marked:
        branch = node.get("branch_pending_delete")
        if not isinstance(branch, str) or not isinstance(node.get("branch", ""), str):
            tree.emit(node_id, "malformed_entry", node=node_id, action="reap",
                      fields=["branch"])
            continue
        record = authority.get(node_id) if authority else None
        completed = bool(record and record.get("completion") and
                         record.get("branch") == branch)
        if (not isinstance(branch, str) or
                (not completed and branch != node.get("branch"))
                or not _branch_released(node) or (branch in held and not completed)
                or not branch.startswith("agents/")):
            continue
        if authority and authority.owns_branch(branch) and not completed:
            continue
        try:
            if not gitops.run(root, "check-ref-format", f"refs/heads/{branch}").ok:
                continue
            if gitops.branch_exists(root, branch):
                result = gitops.delete_branch(root, branch, force=True)
                if not result.ok and gitops.branch_exists(root, branch):
                    continue                  # kept, and so is the mark
            tree.update(node_id, branch_pending_delete="", branch="")
        except Exception:
            continue
        tree.emit(node_id, "branch_deleted", branch=branch)
        cleared += 1
    return cleared


def _merge_usage(current: dict[str, Any], incoming: dict[str, Any],
                 mode: str = "cumulative") -> dict[str, Any]:
    """Fold a provider's usage report into the running total.

    Providers differ, and getting this wrong silently corrupts every number
    above it. agy reports running totals on each step, so they must be taken at
    their maximum; opencode reports per-step deltas, so they must be summed.
    The mode is declared per provider in providers.yaml.
    """
    out = dict(current)
    for key, value in incoming.items():
        if key == "cost_usd":
            continue                    # accumulated separately, never merged
        if isinstance(value, dict):
            nested = out.get(key) if isinstance(out.get(key), dict) else {}
            out[key] = _merge_usage(nested, value, mode)
        elif isinstance(value, (int, float)):
            previous = out.get(key, 0)
            if not isinstance(previous, (int, float)):
                out[key] = value
            elif mode == "delta":
                out[key] = previous + value
            else:
                out[key] = max(previous, value)
    return out


def _compact(event: dict) -> dict:
    """Trim a stream event for return through a tool result."""
    out = {k: v for k, v in event.items() if v not in (None, "", {}, [])}
    if "text" in out and len(out["text"]) > 400:
        out["text"] = out["text"][:400] + "…"
    if "args" in out:
        rendered = json.dumps(out["args"], default=str)
        if len(rendered) > 300:
            out["args"] = rendered[:300] + "…"
    return out
