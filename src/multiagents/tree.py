"""The project agent tree — shared, on-disk, lock-protected.

Recursion forces this design. A stdio MCP server is spawned *per client*, so an
orchestrator that delegates to an agent which delegates again ends up with three
independent server processes. An in-memory registry would give each of them its
own empty world. The tree therefore lives in one JSON file, mutated only under
an exclusive ``flock``, and every process reads the same picture.

``events.jsonl`` sits beside it: append-only, one line per state transition
across all agents, so an external watcher (a ``/loop``, a status line) can follow
a run without touching the tree or the MCP layer at all. Both files are written
through :func:`multiagents.redact.scrub`.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import sys
import time
import uuid
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Iterable, Iterator

from . import gitops
from .redact import depersonalise, scrub

# Terminal states never transition again.
# "limited" is terminal and is NOT "failed": the provider stopped the run, the
# work on its branch is real, and the agent is resumable by session id once the
# window reopens. Filing it as a failure is what made a full account look like
# a broken one.
# "truncated" is terminal and is NOT "failed" either: the CLI stopped its own
# turn early — agy's print mode does this at its deadline — so the work on the
# branch is real but unfinished. It must never be merged as done, and it says
# nothing about the provider's health.
TERMINAL = {"done", "failed", "cancelled", "discarded", "merged", "orphaned",
            "limited", "truncated", "refused"}
# "detached": still running, left by a root server that exited (SV-R3), and
# waiting for the next one to adopt it (SV-R6). Work in flight, so ACTIVE.
ACTIVE = {"pending", "running", "stuck", "detached"}
# `run` and `init-agent` exec into a CLI, so neither is an agent and neither
# must be counted as one. See Node.role.
DRIVER_ROLES = {"orchestrator", "initializer"}
# Two states are neither, for the same reason: the process has exited but the
# session is resumable, so they must not be counted against the concurrency
# limit, reaped as orphans, or cleaned up as finished work.
#   idle           a standing conversation between turns
#   awaiting_user  parked on a question only a human can answer
AWAITING = "awaiting_user"
PAUSED = {"idle", AWAITING}


# Each provider reports usage in its own words, and the words do not overlap:
# opencode sends `total`, agy sends `total_tokens`, and claude sends none of
# either — just the Anthropic API's own parts, `input_tokens`,
# `output_tokens` and two cache counters. Anything reading `total` alone
# therefore reported the most expensive provider in the roster as having spent
# nothing. Measured on one project: nine million tokens dropped, and every
# claude row in `usage` showing 0.
#
# Normalised on READ rather than on write, so trees already on disk are fixed
# by the upgrade rather than by a migration.
TOKEN_TOTALS = ("total", "total_tokens")
TOKEN_PARTS = ("input_tokens", "output_tokens",
               "cache_creation_input_tokens", "cache_read_input_tokens")


def token_count(usage: dict[str, Any] | None) -> int:
    """How many tokens a run spent, whichever provider is describing it."""
    usage = usage or {}
    for key in TOKEN_TOTALS:
        value = usage.get(key)
        if isinstance(value, (int, float)) and value:
            return int(value)
    parts = sum(float(usage.get(key) or 0) for key in TOKEN_PARTS)
    return int(parts)


def cost_of(usage: dict[str, Any] | None) -> float:
    return float((usage or {}).get("cost_usd") or 0.0)


def sum_usage(nodes: Iterable[dict[str, Any]]) -> dict[str, float]:
    """Spend across runs, with ``total`` normalised per run by token_count().

    The raw per-key sum this replaces had two failures, both observed on the
    review's budget tags: a provider that sends no total-shaped key (claude)
    contributed nothing to ``total``, and a later run that did have one
    replaced the figure the others had built up, because only providers
    using that key name fed it. Summing token_count() per run makes spend
    monotonic and mixed-provider honest.

    The remaining keys are raw per-key sums, kept for detail — except the
    total-shaped ones, which token_count() has already folded into
    ``total``. Summing them beside it would double-count every agy and
    opencode run, which is what the old `total + total_tokens` read did.
    """
    total: dict[str, float] = {}
    for node in nodes:
        usage = node.get("usage") or {}
        total["total"] = total.get("total", 0) + token_count(usage)
        for key, value in usage.items():
            if key in TOKEN_TOTALS or not isinstance(value, (int, float)):
                continue
            total[key] = total.get(key, 0) + value
    return total


def new_id() -> str:
    return "ag-" + uuid.uuid4().hex[:6]


def now() -> float:
    return time.time()


# PC-R3: the cause of a deferred entry queued behind a full provider, and the
# kind of the event recorded when one is queued and when it leaves the queue.
PC_CAUSE = "provider_concurrency"


def pc_waiting(entries: Iterable[Any], provider: str | None = None) -> list[dict]:
    """The waiting provider-concurrency entries, in FIFO order (PC-R3a),
    for one provider or all of them. Malformed entries are skipped (DQ-R9)."""
    out = [d for d in entries
           if isinstance(d, dict) and not deferred_malformed(d)
           and d.get("cause") == PC_CAUSE
           and d.get("status", "waiting") == "waiting"
           and (provider is None or (d.get("spec") or {}).get("provider") == provider)]
    return sorted(out, key=lambda d: (d["seq"], _number(d.get("queued_at"))))


def _number(value: Any) -> float:
    """A sort key that never raises on a value another process wrote."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value)


def deferred_malformed(entry: Any) -> bool:
    """Whether a deferred queue entry is too broken to act on (DQ-R9).

    Malformed means any of: not a dict, missing `id`, a missing or non-numeric
    `retry_after`, or a `claim` that is not a dict with an int `pid`. The queue
    sits in a file every agent process can write, so the drain reads it the way
    it reads everything else it did not write itself. A malformed entry is
    skipped by the drain, shown by `list_deferred` as `malformed`, and removed
    only by `cancel_deferred` — it never makes anything raise.
    """
    if not isinstance(entry, dict):
        return True
    if not entry.get("id"):
        return True
    retry = entry.get("retry_after")
    if isinstance(retry, bool) or not isinstance(retry, (int, float)):
        return True
    if entry.get("cause") == PC_CAUSE:
        # PC-R3a: the FIFO order is `seq`; an entry without a usable one
        # cannot be placed, so it is malformed (skipped and reported).
        seq = entry.get("seq")
        if isinstance(seq, bool) or not isinstance(seq, int) or seq < 1:
            return True
    claim = entry.get("claim")
    if claim is not None and (not isinstance(claim, dict)
                              or isinstance(claim.get("pid"), bool)
                              or not isinstance(claim.get("pid"), int)):
        return True
    return False


def find_deferred(entries: Iterable[Any], deferred_id: str) -> dict | None:
    """The entry named `deferred_id`, tolerating malformed neighbours (DQ-R9).

    A scan over the queue is a scan over a file other processes write, so it
    skips what it cannot read rather than raising on it.
    """
    return next((d for d in entries
                 if isinstance(d, dict) and d.get("id") == deferred_id), None)


@dataclass
class Node:
    id: str
    agent: str
    provider: str
    model: str
    parent: str | None
    depth: int
    task: str = ""
    status: str = "pending"
    reason: str = ""
    warnings: list[str] = field(default_factory=list)
    branch: str = ""
    worktree: str = ""
    session_id: str = ""
    requested_session: str = ""     # explicit resume mismatch, retained after session loss
    home_provider: str = ""
    segments: list[dict[str, Any]] = field(default_factory=list)
    segment_usage_base: dict[str, Any] = field(default_factory=dict)
    handover_attempt: dict | None = None
    quota_stops: dict[str, float] = field(default_factory=dict)
    # Provider read_at at each quota stop, or {"local": t[, "anchor": read_at]}
    # when the stop's reading was unstamped (QH-R30.5).
    quota_stop_readings: dict[str, float | dict | None] = field(default_factory=dict)
    reserve_request: str = ""
    on_reserve_floor: bool = False
    pinned: bool = False
    rank: int | None = None
    promotion: dict | None = None
    promotion_refusals: dict[str, float | None] = field(default_factory=dict)
    # Last promotion attempt or switch, including a same-provider rollback.
    promotion_dwell_at: float | None = None
    model_pinned: bool = False
    pid: int | None = None
    # What makes `pid` an identity rather than a number: see
    # :mod:`multiagents.procs`. Recorded beside it so a recovery after a reboot
    # can tell this agent's pid from whatever now holds it.
    pid_start: str = ""
    # RM-R1d: a failed-launch cleanup that could not confirm this node's
    # death holds its concurrency slot DURABLY — `{"since": ..., "owner_pid":
    # ...}` while held, None once death is confirmed. `_occupies_slot`
    # counts it in every Runner, whatever the pid's liveness: the hold is
    # tree state, not one process's memory.
    cleanup_hold: dict | None = None
    # PC-R2a: who holds this node's slot while no process of its own does —
    # a pre-launch reservation (`kind: reservation`) or the post-mortem of a
    # run that has exited (`kind: finalizing`): `{"kind", "owner_pid",
    # "owner_start", "owner"}`. It counts while that server lives, and a
    # `pending` reservation whose server died counts no more, so a crash
    # between reserving and launching never strands the slot.
    slot_owner: dict | None = None
    # PC-R2a: where the recorded process runs — `{"kind": "local"}` or
    # `{"kind": "docker", "container": ...}` — written with `pid` at launch,
    # so a dead local pid (a host `docker exec` client) is never taken for
    # the end of a run whose wrapper lives on in its container.
    exec_identity: dict | None = None
    # SC-R4c: the spend-cap crossings that stopped this run, when one did.
    spend_cap_crossings: list[str] = field(default_factory=list)
    children: list[str] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    steps: int = 0
    events: int = 0
    conversation: bool = False
    # Which launched session this node belongs to, and what kind of thing it is.
    #
    # `session` is NOT `parent`. open-questions.md §3b proposed parenting every
    # agent to its driver so the tree would record who asked; measuring that
    # found `parent` is load-bearing in five guards, two of them badly —
    # `_preflight` skips the `max_children` cap entirely while a spawner has no
    # parent, and `_maybe_merge_into_parent` returns early on a missing parent,
    # which IS the explicit merge_agent() gate the whole design rests on. A
    # driver node has no worktree, so giving depth-1 agents a parent would have
    # resolved the merge target to the project root and landed their branches
    # in the user's tree by itself.
    #
    # The need was "no record of who asked or in which session", and the
    # load-bearing word there is session. So it is its own field: agents stay
    # roots, `parent` keeps meaning git isolation, and grouping is a view.
    session: str = ""
    # "orchestrator" | "initializer" for a driver, "" for an agent. Declared,
    # not inferred: guessing from `parent is None and branch == ""` would call
    # a read-only agent an orchestrator the first time one is spawned as a root.
    role: str = ""
    # "this run is a check on that agent's work", declared by whoever spawned
    # it. Declared rather than inferred: branch-and-timing guesses break the
    # moment two checks run at once or a branch is reused, and the orchestrator
    # already knows the answer at the point of asking.
    verifies: str = ""
    # A verifier's declared outcome: "approved" | "rejected" | "" (none given).
    verdict: str = ""
    defects: int = 0
    # Survives a relaunch, which the in-process Run does not: _launch builds a
    # fresh Run, so a "have I retried?" flag kept there resets on every retry
    # and the guard becomes an infinite loop.
    retries: int = 0        # a standing dialogue, resumed each turn
    # bug-c050b0: also survives a relaunch, and for the same reason. A
    # "have I asked this node to wrap up?" flag kept on the in-process Run
    # resets on every steer/relaunch, since those replace the Run — which is
    # how one drain sent the same wrap-up 6 times in a second. `wrap_up_headroom`
    # is the headroom reading at the moment it was asked, so a later reading
    # showing MORE headroom (a window reset) can re-arm it.
    wrap_up_asked: bool = False
    wrap_up_headroom: float | None = None
    # Where this agent was MEANT to run, when that is not where it ran. Budget
    # routing silently moved work to a fallback provider and recorded nothing,
    # so the only way to find out why an implementer was on the wrong model was
    # to ask someone to read the code.
    routed_from: str = ""
    routed_why: str = ""
    # RM-R5a: the effort this run actually carries, after a model id that
    # declares its own suffix normalised the configured one. Persisted so a
    # steer and a consult resume reuse the normalised value instead of
    # re-deriving the contradicted one from the static config.
    effort: str = ""
    # LM-R1/R2: this run's timeout, max_children and silence_timeout as
    # `{value, source}`, resolved when it started. The cap its own children
    # are counted against is read from here, so a roster edit after launch
    # does not rewrite it (LM-R1a); a steer keeps a `call` timeout (LM-R1b).
    limits: dict[str, Any] = field(default_factory=dict)
    # A named slice of work this run is spent against — a review team's bounded
    # context, a feature, whatever the parent is budgeting. Spend is summed per
    # tag and a tag can be given a ceiling, which is what makes a phase
    # TERMINATE rather than hope to: an orchestrator told "you have 50k left
    # here" will skip the utility folder, and one merely asked to watch its
    # budget will not.
    budget_tag: str = ""
    # The deferred entry this run restarts, when it is a drain's restart
    # (DQ-R8). Written in the SAME transaction as the node itself (DQ-R11): a
    # drain that dies after start() returns leaves a node that recovery can
    # find, so the task is never started a second time.
    node_id: str = ""
    attempt_id: str = ""
    deferred_id: str = ""
    turns: int = 0
    # The base commit a conversation's worktree was last placed on: where it
    # was cut on turn 1, then every refresh that moved it. Own work is what
    # came after this point, so a base amended or moved backwards is never
    # mistaken for the agent's (bug-7f6ba7). "" on nodes from before it was
    # recorded, which fall back to "whatever base does not hold".
    placed_on: str = ""
    paused_at: float | None = None    # entered idle / awaiting_user at
    created_at: float = field(default_factory=now)
    started_at: float | None = None
    ended_at: float | None = None
    turn_started_at: float | None = None
    turn_ended_at: float | None = None
    last_event_at: float | None = None
    summary: str = ""
    # SV-R7: how far into `runs/<id>/output.ndjson` the usage, steps and
    # events above account for — `offset` — plus where the current turn
    # starts in it (`turn`) and how long `stream.jsonl` was at that point
    # (`log`). Written in the same transaction as what it accounts for, so a
    # server that adopts the node resumes exactly where the last one stopped.
    follow: dict[str, int] = field(default_factory=dict)
    # SV-R10: when this node was last adopted by a server that did not start it.
    adopted_at: float | None = None
    # SV-R11: a run of this node ended with a result its parent's server has
    # not yet returned to the caller (wait_for_agents, check_agent,
    # collect_agent — see `mark_seen`). Durable, because what it guards is a
    # compaction, after which nothing in memory is left to remember it.
    unseen: bool = False
    # SG-R2: the branch a deletion could not remove, because the container's
    # `.git` is read-only and git takes `packed-refs.lock` there for every ref
    # deletion. The host deletes it once the node is merged or discarded
    # (`runner.reap_pending_branches`) and clears this.
    branch_pending_delete: str = ""

    def elapsed(self) -> float:
        start = self.started_at or self.created_at
        return (self.ended_at or now()) - start

    def turn_elapsed(self) -> float:
        start = self.turn_started_at or self.started_at or self.created_at
        end = self.turn_ended_at or self.ended_at or now()
        return max(0.0, end - start)


_NODE_FIELDS = {f.name for f in fields(Node)}


def node_from_raw(raw: dict[str, Any], key: str | None = None) -> Node:
    """Build a Node ignoring unknown keys for forward compatibility.

    HA-R9: `key`, the key the entry is stored under, is the node's identity.
    The entry's own `id` is container-written data and never overrides it;
    `Tree.id_mismatch` is how a host action finds out that they disagree.
    """
    kept = {k: v for k, v in raw.items() if k in _NODE_FIELDS}
    if key is not None:
        kept["id"] = key
    return Node(**kept)


_node_from_raw = node_from_raw


# A tree file bigger than this is not read (SG-R7): it is under `.multiagents`,
# which a docker agent can write, and it is read on every transaction.
TREE_MAX_BYTES = 256 * 1024 * 1024



# PS-R4/R4a/R4b: a provider's cooldown record is a COMPOSITE when it holds an
# authentication block. Stored on it:
# - `auth`: one block per execution context (`{context: {until, reason}}`).
#   The host's login and the container's are different credentials, so a
#   failure in one never replaces, and a recovery in one never clears, the
#   other's block;
# - `also_quota`: the one non-auth block (quota, provider_down, family) the
#   provider is cooling on as well. It coexists with the auth blocks instead
#   of overwriting them or being overwritten by them, and is what remains
#   when they are recovered.
# The top-level fields every reader already understands (`until`, `reason`,
# `cause`, `needs_login`, `context`) are DERIVED from those two, by
# `_compose_cooldown`, on every read: a live auth block wins (the latest
# expiry among them), then a live non-auth block, then the latest expired
# auth block — so an auth block that lapsed under a live quota cooldown
# reads as the quota cooldown, and comes back as the auth block once the
# quota one is over, for the half-open probe to check.
# A record with no auth block is the plain record it always was.

def _split_cooldown(record: dict | None) -> tuple[dict[str, dict], dict | None]:
    """`(auth blocks by context, the non-auth block)` of a cooldown record of
    any shape: composite, a single auth record from before contexts, or
    plain."""
    if not record:
        return {}, None
    if "auth" in record:
        return dict(record["auth"]), record.get("also_quota")
    if record.get("cause") == "auth":
        return ({record.get("context", ""): {"until": record.get("until", 0),
                                             "reason": record.get("reason", "")}},
                record.get("also_quota"))
    return {}, dict(record)


def _compose_cooldown(auth: dict[str, dict], other: dict | None,
                      moment: float) -> dict | None:
    """The record for these parts, with its derived top-level fields; None
    when nothing is left. Expired parts are pruned, except the latest auth
    block while no auth block is live — it is still owed a probe."""
    if other is not None and other.get("until", 0) <= moment:
        other = None
    if not auth:
        return dict(other) if other else None
    live = {c: b for c, b in auth.items() if b.get("until", 0) > moment}
    if not live:
        latest = max(auth, key=lambda c: auth[c].get("until", 0))
        auth = {latest: auth[latest]}
    else:
        auth = live
    record: dict = {"auth": auth}
    if other:
        record["also_quota"] = dict(other)
    if live or not other:
        context = max(auth, key=lambda c: auth[c].get("until", 0))
        record.update(until=auth[context].get("until", 0),
                      reason=auth[context].get("reason", ""),
                      needs_login=True, cause="auth", context=context)
    else:
        record.update({key: other[key] for key in ("until", "reason", "cause")
                       if key in other})
    return record


def _put_cooldown(cooldowns: dict, provider: str, auth: dict[str, dict],
                  other: dict | None, moment: float) -> None:
    record = _compose_cooldown(auth, other, moment)
    if record is None:
        cooldowns.pop(provider, None)
    else:
        cooldowns[provider] = record

class Tree:
    """Read/modify/write access to ``tree.json`` under an exclusive lock."""

    def __init__(self, tree_file: Path, events_file: Path):
        self.path = tree_file
        self.events_path = events_file
        self.backup_path = tree_file.with_name(tree_file.name + ".bak")
        self.lock_path = tree_file.with_suffix(".lock")

    # ------------------------------------------------------------------ io --

    def _empty(self) -> dict:
        return {"version": 1, "nodes": {}, "deferred": [], "cooldowns": {},
                "pause": {}, "provider_health": {},
                "questions": [], "tickets": []}

    # SG-R7: every file here sits under `.multiagents`, which a docker agent
    # can write, and the tree is written by the host and by the nested server
    # in the container alike. So each is opened relative to its directory
    # (trusted: the container cannot replace it), through no link and never
    # blocking; a link or a FIFO planted in place of one is never gone
    # through, and a file the tree owns is replaced rather than written into.

    def _raw(self, path: Path) -> bytes | None:
        """`path`'s bytes, if it is a regular file of a sane size."""
        return gitops._read_beneath(path.parent, (), path.name, TREE_MAX_BYTES)

    def _replace(self, path: Path, data: bytes, *, durable: bool = False) -> None:
        """Replace `path` with `data` through a temp file beside it, renamed
        over it. Only ever under the lock, so the temp file's one name is
        safe; whatever stands there is removed first, never opened."""
        dfd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            tmp = self.path.with_suffix(".tmp").name
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp, dir_fd=dfd)
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
                         | os.O_CLOEXEC, 0o666, dir_fd=dfd)
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                if durable:
                    os.fsync(handle.fileno())
            os.replace(tmp, path.name, src_dir_fd=dfd, dst_dir_fd=dfd)
            if durable:
                try:
                    os.fsync(dfd)
                except OSError:
                    pass                  # not all filesystems allow it
        finally:
            os.close(dfd)

    def _read_unlocked(self) -> dict:
        try:
            os.lstat(self.path)
        except FileNotFoundError:
            return self._empty()
        raw = self._raw(self.path)
        try:
            if raw is None:
                raise OSError(f"{self.path} is not a readable regular file")
            data = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError, OSError):
            data = self._recover()
        if data is None:
            return self._empty()
        data.setdefault("nodes", {})
        data.setdefault("deferred", [])
        data.setdefault("pause", {})
        data.setdefault("provider_health", {})
        data.setdefault("cooldowns", {})
        # PS-R4: composite records re-derive their head against the clock.
        moment = now()
        for provider, record in list(data["cooldowns"].items()):
            if isinstance(record, dict) and ("auth" in record
                                             or record.get("cause") == "auth"):
                _put_cooldown(data["cooldowns"], provider,
                              *_split_cooldown(record), moment)
        data.setdefault("questions", [])
        data.setdefault("tickets", [])
        return data

    def _recover(self) -> dict | None:
        """Fall back to the previous copy after a corrupt read, loudly.

        Emptying the tree silently was the old behaviour, and it is the worst
        possible one: every session id, open question, queued ticket and
        deferred task disappears, and the next command reports a clean project
        as though nothing had been lost. The damaged file is kept, because the
        first thing anyone will want is to see what was in it.
        """
        stamp = time.strftime("%Y%m%d-%H%M%S")
        kept = self.path.with_name(f"{self.path.name}.corrupt-{stamp}")
        damaged = self._raw(self.path)
        try:
            if damaged is None:
                raise OSError("nothing readable to keep")
            self._replace(kept, damaged)
        except OSError:
            kept = None

        restored = None
        backup = self._raw(self.backup_path)
        if backup is not None:
            try:
                restored = json.loads(backup)
            except (json.JSONDecodeError, UnicodeDecodeError):
                restored = None

        where = f" kept at {kept.name}" if kept else ""
        if restored is not None:
            # Heal it. Without writing the recovery back, every later read
            # re-recovers and re-warns — and the project stays one bad read away
            # from the empty case for as long as the damaged file sits there.
            # Safe here: _read_unlocked only ever runs while the lock is held.
            try:
                self._write_unlocked(restored)
            except OSError:
                pass
            print(f"multiagents: {self.path} was unreadable{where}; recovered "
                  f"{len(restored.get('nodes', {}))} agent(s) from "
                  f"{self.backup_path.name}", file=sys.stderr)
        else:
            print(f"multiagents: {self.path} was unreadable and no usable backup "
                  f"exists{where}. Starting from an empty tree — sessions, open "
                  f"questions and deferred work from before this point are lost. "
                  f"events.jsonl still holds the history.", file=sys.stderr)
        return restored

    def _write_unlocked(self, data: dict) -> None:
        """Replace the tree, durably, keeping the previous copy.

        `os.replace` is atomic, so no reader ever sees half a file — but atomic
        is not durable. Without the fsync below, a power cut can land the rename
        while the temp file's *contents* are still in the page cache, leaving a
        zero-length tree. The directory is synced too, or the rename itself can
        be lost.

        The `.bak` is the second half: fsync narrows the window and cannot close
        it, so there has to be something to fall back to.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        previous = self._raw(self.path)
        if previous:
            try:
                self._replace(self.backup_path, previous)
            except OSError:
                pass                      # a missing backup must not stop a write
        self._replace(self.path,
                      json.dumps(scrub(data), indent=2, sort_keys=True).encode(),
                      durable=True)

    @contextlib.contextmanager
    def _locked(self) -> Iterator[Any]:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        # A regular file is never replaced, so every process flocks the same
        # one; only a link or a FIFO planted in its place is (SG-R7).
        handle = os.fdopen(gitops._open_file_beneath(
            self.lock_path.parent, (), self.lock_path.name,
            os.O_RDWR | os.O_CREAT, replace=True), "a+")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX)
            yield handle
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(handle, fcntl.LOCK_UN)
            handle.close()

    @contextlib.contextmanager
    def transaction(self) -> Iterator[dict]:
        """Exclusive read-modify-write. Mutate the yielded dict in place."""
        with self._locked():
            data = self._read_unlocked()
            # SF-R3: an owner's stale in-memory mirror must not erase an
            # acknowledgement transferred when its restored entry left.
            acknowledged = [dict(queued) for queued in self._steer_queues(data)
                            if queued.get("restore_done")]
            yield data
            queued_owners = self._steer_queues(data)
            for queued in queued_owners:
                if any(old.get("id") == queued.get("id") and old["restore_done"]
                       == queued.get("restore_token") for old in acknowledged):
                    queued["restore_done"] = queued["restore_token"]
            receipts = data.get("deferred_restores", {})
            waiting = {d.get("id") for d in data["deferred"] if isinstance(d, dict)}
            for entry_id in list(receipts):
                if entry_id in waiting:
                    continue
                # The acknowledgement now belongs only to an unfinished
                # cleanup, and disappears when that owner finishes.
                for queued in queued_owners:
                    if (queued.get("id") == entry_id
                            and queued.get("restore_token") in receipts[entry_id]):
                        queued["restore_done"] = queued["restore_token"]
                receipts.pop(entry_id)
            self._write_unlocked(data)

    @staticmethod
    def _steer_queues(data: dict) -> list[dict]:
        queued = []
        for node in data["nodes"].values():
            held = node.get("cleanup_hold") if isinstance(node, dict) else None
            cleanup = held.get("steer_cleanup") if isinstance(held, dict) else None
            entry = cleanup.get("queued") if isinstance(cleanup, dict) else None
            if isinstance(entry, dict):
                queued.append(entry)
        return queued

    def read(self) -> dict:
        with self._locked():
            return self._read_unlocked()

    # --------------------------------------------------------------- events --

    def emit(self, agent_id: str, kind: str, /, **fields: Any) -> None:
        """Append one line to the global event log. Never raises."""
        try:
            self.emit_checked(agent_id, kind, **fields)
        except OSError:
            pass

    def emit_checked(self, agent_id: str, kind: str, /, **fields: Any) -> None:
        """`emit`, raising OSError when the line could not be written — for
        an event whose delivery is itself recorded (SC-R4c)."""
        entry = scrub({"t": now(), "agent": agent_id, "kind": kind, **fields})
        self.events_path.parent.mkdir(parents=True, exist_ok=True)
        fd = gitops._open_file_beneath(
            self.events_path.parent, (), self.events_path.name,
            os.O_RDWR | os.O_APPEND | os.O_CREAT, replace=True)
        with os.fdopen(fd, "a") as handle:
            # A torn last line (a writer that died mid-line) is closed first,
            # so this event is a line of its own and not glued onto it.
            size = os.fstat(fd).st_size
            torn = size > 0 and os.pread(fd, 1, size - 1) != b"\n"
            handle.write(("\n" if torn else "") + json.dumps(entry) + "\n")

    # ---------------------------------------------------------------- nodes --

    def add(self, node: Node) -> Node:
        with self.transaction() as data:
            data["nodes"][node.id] = asdict(node)
            if node.parent and node.parent in data["nodes"]:
                kids = data["nodes"][node.parent].setdefault("children", [])
                if node.id not in kids:
                    kids.append(node.id)
        self.emit(
            node.id, "created",
            agent=node.agent, provider=node.provider, model=node.model,
            parent=node.parent, depth=node.depth, branch=node.branch,
        )
        return node

    def get(self, agent_id: str) -> Node | None:
        raw = self.read()["nodes"].get(agent_id)
        return _node_from_raw(raw, agent_id) if raw else None

    def id_mismatch(self, agent_id: str) -> bool:
        """HA-R9: whether the entry stored under `agent_id` names another id."""
        raw = self.read()["nodes"].get(agent_id)
        return isinstance(raw, dict) and raw.get("id") != agent_id

    def update(self, agent_id: str, **fields: Any) -> None:
        """Replace node fields verbatim; usage here is the whole-node total.

        Turn-local usage belongs to note_event (base plus current turn in
        the last segment). Finalizers pass the sum of closed segments here;
        adding that total to a segment would count earlier providers twice.
        """
        with self.transaction() as data:
            node = data["nodes"].get(agent_id)
            if node is None:
                return
            node.update(fields)

    def set_status(self, agent_id: str, status: str, reason: str = "") -> None:
        with self.transaction() as data:
            node = data["nodes"].get(agent_id)
            if node is None:
                return
            if node.get("status") == status and node.get("reason") == reason:
                return
            previous = node.get("status")
            if status in TERMINAL or status in ("idle", "awaiting_user"):
                node["turn_ended_at"] = node.get("turn_ended_at") or now()
            node["status"] = status
            # Always written, including "": a clear (stuck -> running) or a
            # clean finish (-> done) passes reason="" meaning "no reason
            # now", and a stale trip message must not survive past it.
            node["reason"] = reason
            if status == "running" and not node.get("started_at"):
                node["started_at"] = now()
                # The claim made when this agent was routed has done its job:
                # the node counts for itself now, and leaving both would have
                # the instance look twice as busy as it is.
                self._spend_claim(data, node.get("provider") or "")
            node["paused_at"] = now() if status in PAUSED else None
            if status in TERMINAL:
                node["ended_at"] = now()
            else:
                # Leaving a terminal state must clear it. steer() stops an agent
                # (terminal: cancelled) and relaunches it, and without this the
                # node's elapsed() stays frozen at the moment of the stop for the
                # rest of its life. answer_question() uses the same path.
                node["ended_at"] = None
            # SV-R11: leaving ACTIVE is a run producing a result, except a
            # cancel, which is somebody's stop and carries no result to read.
            # Anything after that (done -> merged) is not a new result, and
            # entering ACTIVE again starts a run whose result is still to come.
            if status in ACTIVE:
                node["unseen"] = False
            elif previous in ACTIVE and status != "cancelled":
                node["unseen"] = True
        self.emit(agent_id, "status", status=status, reason=reason)

    def mark_seen(self, agent_id: str, status: str | None = None) -> None:
        """SV-R11: the node's result has been returned to its parent's server.

        Cleared only while the node still holds the status that was returned
        (`None`: any status outside ACTIVE), so a result read just before a
        newer run ended does not hide the newer one.
        """
        with self.transaction() as data:
            node = data["nodes"].get(agent_id)
            if node is None or not node.get("unseen"):
                return
            current = node.get("status")
            if (current == status) if status is not None else current not in ACTIVE:
                node["unseen"] = False

    def unseen(self, session: str = "") -> list[Node]:
        """Root agents with a result the orchestrator has not seen (SV-R11).

        Only parentless nodes: a nested agent's result goes to the agent that
        started it, not to the orchestrator. With `session`, only that
        session's agents, and those recorded before sessions were.
        """
        return [_node_from_raw(n, k) for k, n in self.read()["nodes"].items()
                if n.get("unseen") and not n.get("parent")
                and n.get("role", "") not in DRIVER_ROLES
                and (not session or n.get("session", "") in {"", session})]

    def _spend_claim(self, data: dict, provider: str) -> None:
        """Drop one claim: the node it stood in for is now visible as running."""
        claims = (data.get("claims") or {}).get(provider)
        if claims:
            claims.pop(0)

    def note_event(self, agent_id: str, steps: int | None = None,
                   usage: dict | None = None, session_id: str | None = None,
                   events: int = 1, follow: dict | None = None) -> None:
        """Flush accumulated stream progress for one agent.

        `events` is a batch count, not a single increment. Every call here
        flocks, reads and rewrites the whole tree, so calling it per stream line
        turned a 39-event run into 39 full rewrite cycles — and with several
        agents streaming at once that is lock contention on the one file every
        nested server shares. The reader batches; this writes the total.
        """
        learned = ""
        with self.transaction() as data:
            node = data["nodes"].get(agent_id)
            if node is None:
                return
            node["events"] = node.get("events", 0) + events
            node["last_event_at"] = now()
            if steps is not None:
                node["steps"] = steps
            if usage:
                node["usage"] = usage
                segments = node.get("segments") or []
                if segments:
                    base = node.get("segment_usage_base") or {}
                    segments[-1]["usage"] = (sum_usage([{"usage": base}, {"usage": usage}])
                                              if base else usage)
                    node["usage"] = (sum_usage(segments) if len(segments) > 1
                                     else segments[-1]["usage"])
            if follow is not None:
                node["follow"] = follow
            if session_id and not node.get("session_id"):
                node["session_id"] = session_id
                learned = session_id
            if session_id and node.get("segments"):
                node["segments"][-1]["session_id"] = session_id
        if learned:
            # The one field that cannot be reconstructed from anywhere else. A
            # tree lost to a corrupt write takes every session with it unless
            # the id also reached the append-only log.
            self.emit(agent_id, "session", session_id=learned)

    def _nodes(self, keep, entries: Iterable[tuple[str, Any]], pass_: str) -> list[Node]:
        """HA-R12: the entries `keep` selects, as Nodes, skipping malformed ones.

        `tree.json` is container-writable, so an entry may not be a mapping,
        may lack a field Node requires, or may hold a value of a type the
        filter cannot compare. Such an entry is left out with an event, never
        raised out of a pass that iterates the whole tree.
        """
        out = []
        for key, raw in entries:
            try:
                if not isinstance(raw, dict):
                    raise TypeError(f"entry is {type(raw).__name__}, not a mapping")
                if keep(raw):
                    out.append(_node_from_raw(raw, key))
            except (TypeError, ValueError) as exc:
                self.emit(key, "malformed_entry", node=key, action=pass_,
                          reason=f"{type(exc).__name__}: {exc}"[:300])
        return out

    def children_of(self, agent_id: str) -> list[Node]:
        data = self.read()
        node = data["nodes"].get(agent_id, {})
        return [_node_from_raw(data["nodes"][c], c) for c in node.get("children", []) if c in data["nodes"]]

    def active(self) -> list[Node]:
        """Active AGENTS. Drivers are excluded — see `DRIVER_ROLES`.

        Every caller but the display ones means "work the project is doing",
        and a driver counted among them costs a `max_concurrent` slot, and is
        waited on forever by `wait_for_any` because nothing in this process
        will ever finish it.
        """
        return self._nodes(lambda n: n.get("status") in ACTIVE
                           and n.get("role", "") not in DRIVER_ROLES,
                           self.read()["nodes"].items(), "active")

    def drivers(self) -> list[Node]:
        """The launched sessions — what `active()` deliberately leaves out."""
        return self._nodes(lambda n: n.get("role", "") in DRIVER_ROLES,
                           self.read()["nodes"].items(), "drivers")

    def ancestry(self, agent_id: str) -> list[str]:
        """Root-first chain of ids down to `agent_id`, for cycle and depth checks."""
        nodes = self.read()["nodes"]
        chain, cursor, seen = [], agent_id, set()
        while cursor and cursor in nodes and cursor not in seen:
            seen.add(cursor)
            chain.append(cursor)
            cursor = nodes[cursor].get("parent")
        return list(reversed(chain))

    def usage_by_model(self) -> list[dict[str, Any]]:
        """Spend and tokens per provider/model, from our own stream accounting.

        The providers do not offer this: opencode's usage endpoint reports three
        whole-account windows and no breakdown, and agy's `/usage` reports
        whole-pool buckets that say nothing about which model drained them.
        We already parse per-run usage out of every stream, so the finer figure
        is ours to compute — and it is more useful than a vendor's would be,
        because it is joined to the agent that spent it.
        """
        rows: dict[tuple[str, str], dict[str, Any]] = {}
        entries = []
        for node in self.read()["nodes"].values():
            segments = node.get("segments") or []
            if len(segments) > 1:
                entries.extend({**segment, "agent": node.get("agent")} for segment in segments)
            else:
                entries.append(node)
        for node in entries:
            usage = node.get("usage") or {}
            if not usage:
                continue
            key = (node.get("provider") or "?", node.get("model") or "?")
            row = rows.setdefault(key, {
                "provider": key[0], "model": key[1], "runs": 0,
                "tokens": 0, "cost_usd": 0.0, "agents": set(),
            })
            row["runs"] += 1
            row["agents"].add(node.get("agent") or "?")
            row["tokens"] += token_count(usage)
            row["cost_usd"] += cost_of(usage)
        out = []
        for row in rows.values():
            row["agents"] = sorted(row["agents"])
            row["cost_usd"] = round(row["cost_usd"], 6)
            out.append(row)
        out.sort(key=lambda r: (-r["cost_usd"], -r["tokens"]))
        return out

    def usage_for_tag(self, tag: str) -> dict[str, float]:
        """Total spend across every run tagged with this slice of work.

        ``total`` is each run's `token_count()` summed, so a provider that
        reports no total-shaped key still spends its tag; see `sum_usage`.
        """
        return sum_usage(n for n in self.read()["nodes"].values()
                         if n.get("budget_tag") == tag)

    def budget_for_tag(self, tag: str) -> int:
        return int((self.read().get("budgets") or {}).get(tag, {}).get("tokens", 0) or 0)

    def budget_record(self, tag: str) -> dict[str, Any]:
        """A tag's ceiling as recorded: tokens, when, and who set it."""
        return dict((self.read().get("budgets") or {}).get(tag) or {})

    def set_budget(self, tag: str, tokens: int, set_by: str = "") -> dict[str, Any]:
        """Record a tag's ceiling. The FIRST value wins.

        Deliberately not raisable. A ceiling the spender may lift on its own is
        a suggestion, and the agent asking to lift it is the one that has just
        run out — which is exactly when it is least able to judge. Raising one
        is a human edit, and a tag that genuinely needs more can be given a new
        name and a new budget, which at least leaves a record of the decision.
        """
        result: dict[str, Any] = {}
        with self.transaction() as state:
            budgets = state.setdefault("budgets", {})
            existing = budgets.get(tag)
            if existing:
                result = {"tag": tag, "tokens": int(existing.get("tokens", 0)),
                          "created": False,
                          "note": "already set; a budget cannot be raised from here"}
            else:
                budgets[tag] = {"tokens": int(tokens), "set_at": now(),
                                **({"set_by": set_by} if set_by else {})}
                result = {"tag": tag, "tokens": int(tokens), "created": True}
        return result

    def rollup_usage(self, agent_id: str | None = None) -> dict[str, int]:
        """Total token usage for the whole tree, or one subtree.

        ``total`` is each run's `token_count()` summed, so every provider's
        shape counts; see `sum_usage`.
        """
        nodes = self.read()["nodes"]
        if agent_id is None:
            selected = list(nodes.values())
        else:
            stack, selected = [agent_id], []
            while stack:
                current = stack.pop()
                if current in nodes:
                    selected.append(nodes[current])
                    stack.extend(nodes[current].get("children", []))
        total = sum_usage(selected)
        # Token counts are whole; cost is dollars and must keep its fraction —
        # rounding it to int silently reported every run as free.
        return {
            k: (round(v, 6) if k.endswith("_usd") else int(v))
            for k, v in total.items()
        }

    # ------------------------------------------------------------ questions --
    #
    # Authoritative records live here rather than in a side file: several server
    # processes hold this tree (one per nested agent), it is already flock-
    # protected and already scrubbed on write, and answering is a read-modify-
    # write that a plain append cannot do safely. events.jsonl keeps the
    # append-only audit trail, mirroring the tree.json / events.jsonl split.

    def add_question(self, agent_id: str, topic: str, question: str,
                     proposed: str = "") -> dict:
        record = {
            "id": "q-" + uuid.uuid4().hex[:6],
            "agent": agent_id,
            "topic": topic,
            "question": question,
            "proposed_default": proposed,
            "asked_at": now(),
            "status": "open",
            "answer": "",
            "answered_at": None,
            "answered_by": "",
        }
        with self.transaction() as data:
            data["questions"].append(record)
        self.emit(agent_id, "question", topic=topic, question=question[:300],
                  proposed=proposed[:200], question_id=record["id"])
        return record

    def open_questions(self, agent_id: str | None = None) -> list[dict]:
        return [
            q for q in self.read()["questions"]
            if q.get("status") == "open" and (agent_id is None or q.get("agent") == agent_id)
        ]

    def get_question(self, question_id: str) -> dict | None:
        return next((q for q in self.read()["questions"] if q["id"] == question_id), None)

    def answer_question(self, question_id: str, answer: str,
                        answered_by: str = "user") -> dict | None:
        """Record an answer. Returns the updated record, or None if unknown.

        Claiming and answering happen in one locked transaction so two
        processes cannot both decide they are the one resuming the agent.
        """
        with self.transaction() as data:
            record = next((q for q in data["questions"] if q["id"] == question_id), None)
            if record is None:
                return None
            if record.get("status") == "answered":
                return dict(record, already_answered=True)
            record.update({"status": "answered", "answer": answer,
                           "answered_at": now(), "answered_by": answered_by})
            result = dict(record)
        self.emit(result["agent"], "answered", question_id=question_id,
                  answered_by=answered_by, answer=answer[:300])
        return result

    # -------------------------------------------------------------- tickets --
    #
    # Bugs in multiagents *itself*, written up by the bug-reporter agent and
    # queued for the orchestrator. They live beside questions for the same
    # reasons — one lock, one scrub, read-modify-write on status — but they
    # differ in a way that matters: a question stays on this machine, and a
    # ticket is written to be published. Everything stored here has been
    # depersonalised on the way in, so what the orchestrator reads is already
    # what would be posted.

    TICKET_SEVERITIES = ("blocking", "minor")

    @staticmethod
    def _tooling_version() -> str:
        """Which multiagents a ticket was filed against.

        A running orchestrator filed two blocking tickets describing bugs that
        had been fixed hours earlier the same day. It could not have known, and
        neither could the person reading them later without checking each one
        by hand. The commit is cheap to record and turns "is this still true?"
        into a comparison.
        """
        from . import __version__

        sha = ""
        try:
            import subprocess
            here = Path(__file__).resolve().parent
            result = subprocess.run(
                ["git", "-C", str(here), "rev-parse", "--short", "HEAD"],
                capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                sha = result.stdout.strip()
        except Exception:
            sha = ""
        return f"{__version__}+{sha}" if sha else __version__

    def add_ticket(self, agent_id: str, title: str, body: str,
                   severity: str = "minor", proposed_fix: str = "",
                   project_root=None) -> dict:
        record = {
            "id": "bug-" + uuid.uuid4().hex[:6],
            "agent": agent_id,
            "title": title[:200],
            "body": body,
            "proposed_fix": proposed_fix,
            "severity": severity if severity in self.TICKET_SEVERITIES else "minor",
            "filed_at": now(),
            # Which multiagents this was observed against, so "is it still
            # true?" is a comparison rather than an investigation.
            "tooling": Tree._tooling_version(),
            # open -> reported (submitted upstream) | fixed (handled locally)
            # | declined (the user said no) | awaiting_user (needs a decision)
            "status": "open",
            "url": "",
            "note": "",
            "resolved_at": None,
        }
        record = depersonalise(record, project_root)
        with self.transaction() as data:
            data["tickets"].append(record)
        self.emit(agent_id, "ticket", ticket_id=record["id"],
                  severity=record["severity"], title=record["title"])
        return record

    def open_tickets(self, severity: str | None = None) -> list[dict]:
        return [
            t for t in self.read()["tickets"]
            if t.get("status") in ("open", "awaiting_user")
            and (severity is None or t.get("severity") == severity)
        ]

    def get_ticket(self, ticket_id: str) -> dict | None:
        return next((t for t in self.read()["tickets"] if t["id"] == ticket_id), None)

    def set_ticket_status(self, ticket_id: str, status: str, note: str = "",
                          url: str = "") -> dict | None:
        with self.transaction() as data:
            record = next((t for t in data["tickets"] if t["id"] == ticket_id), None)
            if record is None:
                return None
            record.update({"status": status, "note": note or record.get("note", "")})
            if url:
                record["url"] = url
            if status in ("reported", "fixed", "declined"):
                record["resolved_at"] = now()
            result = dict(record)
        self.emit(result["agent"], "ticket_status", ticket_id=ticket_id,
                  status=status, url=url)
        return result

    # ------------------------------------------------------- provider health --
    #
    # A circuit breaker, and deliberately cause-agnostic. When a provider's
    # credentials were revoked mid-session, thirteen agents failed identically
    # before anyone noticed — and the only evidence was prose in the agents' own
    # output, which this project already learned not to classify on: a
    # classifier reading agent text once cooled a provider down because an
    # advisor used the word "quota" in a sentence.
    #
    # Counting consecutive failures needs none of that. A provider whose last
    # three runs all failed is broken whatever the reason, and continuing to
    # spawn into it is the failure worth preventing.

    def note_run_outcome(self, provider: str, ok: bool, threshold: int = 3,
                         reason: str = "", kind: str = "",
                         context: str = "") -> dict | None:
        """Record how a run ended. Returns trip details when the breaker opens.

        `context` is the execution context the run used (PS-R4b): a success
        proves that login, and only that one."""
        if not provider:
            return None
        with self.transaction() as data:
            health = data["provider_health"].setdefault(
                provider, {"consecutive_failures": 0, "last_reason": ""})
            if ok:
                health["consecutive_failures"] = 0
                health["last_reason"] = ""
                health["last_success"] = now()
                health.pop("tripped", None)
                health.pop("trial_at", None)
                health.pop("last_kind", None)
                # The health record is not what routing reads. Leaving the
                # cooldown behind kept a working provider out of the pool for
                # the rest of its penalty box.
                auth, other = _split_cooldown(data["cooldowns"].get(provider))
                if not auth:
                    data["cooldowns"].pop(provider, None)
                else:
                    # PS-R4a/R4b: the run proves the login of its own
                    # context. Blocks from other contexts, and the non-auth
                    # block preserved beside them, stay.
                    auth.pop(context, None)
                    _put_cooldown(data["cooldowns"], provider, auth, other, now())
                return None
            health["consecutive_failures"] += 1
            health["last_reason"] = reason[:200]
            # What KIND of failure, so a reader can tell a full provider from a
            # broken one. An orchestrator told only "4 runs in a row failed"
            # concluded, correctly on that evidence, that claude was unsafe to
            # route to — when all four were the provider saying it was full.
            health["last_kind"] = str(kind or "failed")
            count = health["consecutive_failures"]
            # Latching on `tripped` alone meant the breaker opened once and
            # never again: after its cooldown lapsed, every further failure was
            # free, so a provider with a revoked token was retried all evening.
            # Past the threshold, what suppresses a trip is an ACTIVE cooldown
            # — which is the half-open state, one trial at a time.
            cooling = ((data.get("cooldowns") or {}).get(provider) or {}
                       ).get("until", 0) > now()
            if cooling:
                return None                   # already routed around
            # Half-open: once it has tripped, the trial after a lapsed cooldown
            # decides on its own. Requiring the threshold again would allow
            # three runs per cycle into a provider already known to be in
            # trouble — and it is what let the failure count keep climbing
            # while nothing was learned from it.
            if not health.get("tripped") and count < threshold:
                return None
            health["tripped"] = now()
            trip = {"provider": provider, "failures": count, "reason": reason[:200]}
        self.emit("system", "provider_down", **trip)
        return trip

    def provider_health(self) -> dict:
        return self.read().get("provider_health", {})

    def clear_provider_health(self, provider: str) -> None:
        with self.transaction() as data:
            data["provider_health"].pop(provider, None)

    def begin_trial(self, provider: str) -> None:
        """A cooldown has lapsed: the next run decides, so the count starts over.

        `consecutive_failures` is cleared by a SUCCESS and by nothing else,
        which deadlocked a real session: four failures marked the provider
        unsafe, the orchestrator read that and refused to route there, and the
        success that would have cleared it could therefore never happen. The
        breaker has already done its work by then — a failed trial re-trips
        immediately, so nothing is lost by letting the counter start fresh.
        """
        with self.transaction() as data:
            health = data["provider_health"].get(provider)
            if not health:
                return
            health["consecutive_failures"] = 0
            health["last_reason"] = "cooldown lapsed; the next run is the trial"
            health.pop("last_kind", None)
        self.emit("system", "provider_trial", provider=provider)

    # ---------------------------------------------------------------- pause --
    #
    # A whole-tree stop, used when there is no provider left to run anything on.
    # Deliberately NOT a per-agent state: the failure it represents is global,
    # and a system that keeps spawning what it can while its checking agents are
    # unreachable is worse than one that stops — it writes code it cannot review
    # and nobody notices which half is missing.
    #
    # It lives in the tree rather than in memory because every nested agent runs
    # its own server process, and a pause only one of them knows about is not a
    # pause.

    def pause(self, until: float, reason: str, providers: list[str] | None = None,
             cause: str | None = None, deferral: bool = False) -> dict:
        """Record that there is nothing to run `providers` work on until `until`.

        Keeps the EARLIEST reset of any active pause, not the latest. Waking
        early costs one wasted check and an immediate re-pause; waking late
        blocks tasks whose provider came back ten minutes ago, and nothing
        would notice.

        `cause` names why: `"quota"`, `"auth"`, `"provider_down"`, `"family"`,
        `"spend_limit"`, or omitted. A pause with no cause, or written before
        this parameter existed, is never treated as a quota pause — see
        `context/specs/quota-freshness.md`.

        `deferral` marks a pause set by deferring a task (DQ-R6): only that
        kind is lifted when the last waiting deferred entry leaves.
        """
        record = {"until": until, "reason": reason, "since": now(),
                  "providers": sorted(providers or [])}
        if deferral:
            record["deferral"] = True
        if cause is not None:
            record["cause"] = cause
        with self.transaction() as data:
            existing = data.get("pause") or {}
            if existing.get("until", 0) and existing["until"] <= until:
                return dict(existing)
            data["pause"] = record
        self.emit("system", "paused", reason=reason, until=until)
        return record

    def pause_state(self) -> dict:
        """The active pause, or {} — expired pauses clear themselves on read."""
        record = self.read().get("pause") or {}
        if not record:
            return {}
        if record.get("until", 0) <= now():
            self.resume("the window it was waiting for has passed")
            return {}
        return record

    def resume(self, reason: str = "") -> None:
        with self.transaction() as data:
            if not data.get("pause"):
                return
            data["pause"] = {}
        self.emit("system", "resumed", reason=reason)

    # ------------------------------------------------------------- deferred --

    def defer(self, spec: dict, retry_after: float, reason: str,
              deferred_by: str | None = None, cause: str | None = None,
              extra: dict | None = None) -> dict:
        """`cause` names why when it is not a quota window (`spend_cap`,
        SC-R3); `extra` is that cause's own fields."""
        record = {"id": "df-" + uuid.uuid4().hex[:6], "spec": spec,
                  "retry_after": retry_after, "reason": reason, "queued_at": now(),
                  "status": "waiting", "deferred_by": deferred_by,
                  **({"cause": cause} if cause else {}), **(extra or {})}
        with self.transaction() as data:
            data["deferred"].append(record)
        self.emit(spec.get("agent", "?"), "deferred", reason=reason, retry_after=retry_after)
        return record

    def enqueue(self, provider: str, spec: dict, reason: str,
                deferred_by: str | None = None, claim: dict | None = None,
                dispatcher: dict | None = None) -> dict:
        """PC-R3a: queue work behind a full provider, durably.

        An entry of the deferred queue with the cause `provider_concurrency`:
        it is listed and cancelled like any other, but it has no window —
        it leaves when a slot frees (`Runner._drain_queues`), never by
        `due_deferred`. `seq` is per provider, increasing, and written in the
        transaction that appends the entry, so FIFO order survives a restart
        and two processes never draw the same number. `claim` marks an entry
        its own waiting process serves (a consult), with that process's pid.
        """
        with self.transaction() as data:
            counters = data.setdefault("pc_seq", {})
            seq = int(counters.get(provider) or 0) + 1
            counters[provider] = seq
            record = {"id": "df-" + uuid.uuid4().hex[:6],
                      "spec": dict(spec, provider=provider),
                      "cause": PC_CAUSE, "seq": seq,
                      "retry_after": now(), "reason": reason, "queued_at": now(),
                      "status": "waiting", "deferred_by": deferred_by}
            if claim is not None:
                record["claim"] = claim
            if dispatcher is not None:
                # The server that queued it, which dispatches it while it
                # lives; once it is gone a root server takes it over.
                record["dispatcher"] = dispatcher
            data["deferred"].append(record)
        self.emit(spec.get("node_id") or spec.get("agent") or "?", PC_CAUSE,
                  action="queued", provider=provider, deferred_id=record["id"],
                  op=spec.get("op"), seq=seq, reason=reason)
        return record

    def restore_deferred(self, entry: dict | None) -> bool:
        """Put back a provider-concurrency entry that was claimed but whose
        launch never happened — same id, same `seq`, so the same place."""
        if not isinstance(entry, dict) or not entry.get("id"):
            return False
        token = entry.get("restore_token")
        if token and entry.get("restore_done") == token:
            return False
        wrote = False
        with self.transaction() as data:
            done = False
            if token:
                # SF-R3: a write may commit and then raise. This receipt is
                # committed WITH the entry. On removal its pending cleanup
                # retains the acknowledgement, while this receipt is pruned.
                receipts = data.setdefault("deferred_restores", {})
                done = token in receipts.get(entry["id"], []) or any(
                    q.get("id") == entry["id"] and q.get("restore_done") == token
                    for q in self._steer_queues(data))
                if not done:
                    receipts.setdefault(entry["id"], []).append(token)
            if not done and find_deferred(data["deferred"], entry["id"]) is None:
                data["deferred"].append(dict(entry))
                wrote = True
        if token:
            entry["restore_done"] = token
        return wrote

    def due_deferred(self) -> list[dict]:
        """Waiting entries whose window has passed. **Does not remove them.**

        It used to pop, which made any exception between the pop and the
        restart delete the whole remaining batch permanently. The caller ends
        each entry with exit_deferred once it has actually dealt with it, so a
        crash leaves work queued rather than losing it. A refused entry is never
        due: it leaves only through cancel_deferred (DQ-R3). A malformed entry
        is never due either — the drain skips it (DQ-R9).
        """
        current = now()
        return [d for d in self.read()["deferred"]
                if isinstance(d, dict) and not deferred_malformed(d)
                and d.get("status", "waiting") == "waiting"
                and d.get("cause") != PC_CAUSE
                and d["retry_after"] <= current]

    def claim_deferred(self, deferred_id: str) -> dict | None:
        """Claim a due entry for this drain, in one transaction (DQ-R8).

        The entry moves from `waiting` to `restarting` and records who claimed
        it, so a concurrent drain — which selects only `waiting` entries, and
        re-checks inside this transaction — skips it. Returns the claim written
        (the caller needs it to release the same claim later), or None when
        another drain won the race or the entry is gone.
        """
        with self.transaction() as data:
            entry = find_deferred(data["deferred"], deferred_id)
            if entry is None or entry.get("status", "waiting") != "waiting":
                return None
            claim = {"pid": os.getpid(), "at": now()}
            entry["status"] = "restarting"
            entry["claim"] = claim
            return claim

    def requeue_deferred(self, deferred_id: str,
                         claim: dict | None = None) -> bool:
        """Return a claimed entry to `waiting`, with no event.

        The claim is released rather than the entry ended: a transient failure
        (DQ-R3) or an unresolved recovery (DQ-R8) means nothing was dealt with,
        and the next drain must find the entry due again.

        `claim` is the claim the caller believes it holds (DQ-R12): the
        re-check happens inside this one transaction, so a drain that read the
        entry before another resolved it cannot put a `refused` — or re-claimed
        — entry back into rotation. When `claim` is not given, only the entry's
        existence is checked.
        """
        with self.transaction() as data:
            entry = find_deferred(data["deferred"], deferred_id)
            if entry is None:
                return False
            if claim is not None and (entry.get("status") != "restarting"
                                      or entry.get("claim") != claim):
                return False
            entry["status"] = "waiting"
            entry.pop("claim", None)
            return True

    def exit_deferred(self, deferred_id: str, outcome: str,
                      **fields: Any) -> bool | str:
        """End one entry's stay in the queue, and say so (DQ-R1).

        The only way an entry leaves, so no exit goes unrecorded. `refused`
        keeps the entry, marked, and every other outcome removes it. When no
        waiting entry is left the pause a deferral set is lifted in the same
        step (DQ-R6); a pause of another origin is left alone (DQ-R6a). A
        `restarting` entry holds the pause as a `waiting` one does (DQ-R8a).

        Returns True when the exit happened, False when there is no such
        entry, and why it was refused when it was — today only a `cancelled`
        exit of a `restarting` entry (DQ-R12), which returns "restarting".
        The reason is decided inside the transaction, so the caller answers
        for the state at the removal, never for a later read. Only
        `cancelled` can be refused, so callers passing any other outcome
        still read this as the plain bool it always was.
        """
        lifted = False
        with self.transaction() as data:
            entry = find_deferred(data["deferred"], deferred_id)
            if entry is None:
                return False
            if (outcome == "cancelled" and entry.get("status") == "restarting"
                    and not deferred_malformed(entry)):
                # DQ-R12: a drain claimed this entry between the caller's
                # snapshot read and this transaction. Removing it here would
                # record `cancelled` while the claimed run starts anyway.
                # The claim wins; stop_agent reaches the run once it exists.
                # A malformed entry is never claimed (the drain skips it,
                # DQ-R9), so its cancel cannot race, and DQ-R9 leaves its
                # removal to the orchestrator alone.
                return "restarting"
            if outcome == "refused":
                entry["status"] = "refused"
                entry["reason"] = fields.get("reason", "")
                if fields.get("node_id"):
                    # DQ-R3c: a refusal should have left no node behind; if one
                    # exists anyway, the entry is where it is found from.
                    entry["node_id"] = fields["node_id"]
            else:
                data["deferred"] = [d for d in data["deferred"] if d is not entry]
            # PC-R3a: an entry queued behind a full provider set no pause, so
            # it holds none either.
            holding = any(isinstance(d, dict) and not deferred_malformed(d)
                          and d.get("status", "waiting") in ("waiting", "restarting")
                          and d.get("cause") != PC_CAUSE
                          for d in data["deferred"])
            if not holding and (data.get("pause") or {}).get("deferral"):
                data["pause"] = {}
                lifted = True
        self.emit("system", "deferred_exit", deferred_id=deferred_id,
                  agent=(entry.get("spec") or {}).get("agent"), outcome=outcome, **fields)
        if lifted:
            self.emit("system", "resumed", reason="no deferred task is waiting")
        return True

    def drop_deferred(self, deferred_id: str) -> bool:
        """Remove an entry with no event. Prefer exit_deferred (DQ-R1)."""
        with self.transaction() as data:
            before = len(data["deferred"])
            data["deferred"] = [d for d in data["deferred"]
                                if not (isinstance(d, dict)
                                        and d.get("id") == deferred_id)]
            return len(data["deferred"]) < before

    def set_cooldown(self, provider: str, until: float, reason: str,
                     needs_login: bool = False, cause: str | None = None,
                     context: str = "", also_quota: dict | None = None) -> None:
        """Cool `provider` until `until`.

        An authentication block (`cause="auth"` or `needs_login`) is written
        for its execution `context` beside any others (see `_compose_cooldown`);
        `also_quota` sets the non-auth block it coexists with. Any other block
        is the provider's non-auth block: it replaces the previous one, and
        never an authentication block (PS-R4a) — a quota or provider_down
        write that landed on a live auth block used to erase it, reopening a
        provider whose login was still broken."""
        if needs_login or cause == "auth":
            with self.transaction() as data:
                auth, other = _split_cooldown(data["cooldowns"].get(provider))
                auth[context] = {"until": until, "reason": reason}
                _put_cooldown(data["cooldowns"], provider, auth,
                              dict(also_quota) if also_quota else other, now())
            self.emit("-", "cooldown", provider=provider, until=until,
                      reason=reason, needs_login=True)
            return
        record = {"until": until, "reason": reason}
        if cause is not None:
            # See `pause`'s cause note — same taxonomy, same "no cause means
            # not quota" rule.
            record["cause"] = cause
        with self.transaction() as data:
            auth, _ = _split_cooldown(data["cooldowns"].get(provider))
            _put_cooldown(data["cooldowns"], provider, auth, record, now())
        self.emit("-", "cooldown", provider=provider, until=until, reason=reason,
                  needs_login=False)

    def clear_cooldown(self, provider: str) -> bool:
        """Let a provider back in early, because it demonstrably works.

        Without this, a provider that was fixed stayed rejected for the rest of
        its penalty box: the breaker's own health record was cleared by a
        success and the cooldown, which is what choose_provider actually reads,
        was not.
        """
        with self.transaction() as data:
            gone = data["cooldowns"].pop(provider, None) is not None
        if gone:
            self.emit("-", "cooldown_cleared", provider=provider)
        return gone

    def block_auth(self, reasons: dict[str, str], until: float, context: str,
                   supersedes: set[str] | frozenset[str] = frozenset()) -> None:
        """PS-R4: mark each member of `reasons` unauthenticated in `context`,
        in ONE transaction for the whole credential group.

        Each member keeps its blocks from other contexts and its non-auth
        block. `supersedes` names contexts this one replaces — the blocks
        written before contexts were recorded belong to the default one."""
        with self.transaction() as data:
            moment = now()
            for member, reason in reasons.items():
                auth, other = _split_cooldown(data["cooldowns"].get(member))
                for old in supersedes:
                    auth.pop(old, None)
                auth[context] = {"until": until, "reason": reason}
                _put_cooldown(data["cooldowns"], member, auth, other, moment)
        for member, reason in reasons.items():
            self.emit("-", "cooldown", provider=member, until=until,
                      reason=reason, needs_login=True)

    def clear_auth(self, members: list[str] | tuple[str, ...],
                   contexts: set[str] | frozenset[str]) -> dict[str, dict]:
        """PS-R4a/R4b: lift the members' authentication blocks observed in
        `contexts`, and nothing else, in ONE transaction. Returns `{member:
        its record before}` for each member that had one lifted.

        Judged on the live record: blocks from other contexts stay, and the
        non-auth block — a quota cooldown, even one that landed while the
        check ran — is what the member is left with, until its own expiry."""
        removed: dict[str, dict] = {}
        with self.transaction() as data:
            moment = now()
            for member in members:
                record = data["cooldowns"].get(member)
                auth, other = _split_cooldown(record)
                if not any(context in auth for context in contexts):
                    continue
                removed[member] = record
                for context in contexts:
                    auth.pop(context, None)
                _put_cooldown(data["cooldowns"], member, auth, other, moment)
        for member in removed:
            self.emit("-", "cooldown_cleared", provider=member)
        return removed

    def clear_cooldown_restoring(self, provider: str, cause: str | None = None,
                                 context: str | None = None) -> dict | None:
        """`clear_auth` for one provider and context: its record before, or
        None when it held no such block."""
        if cause not in (None, "auth"):
            return None
        return self.clear_auth([provider], {context or ""}).get(provider)

    def cooldown(self, provider: str) -> dict | None:
        entry = self.read()["cooldowns"].get(provider)
        if entry and entry.get("until", 0) > now():
            return entry
        return None

    def clear_quota(self, usable: set[str] | frozenset[str]) -> dict:
        """Lift cooldowns and the pause caused by a provider's quota, once a
        fresh reading proves it has room again (QF-R4/QF-R6).

        `usable` names the providers a fresh read has just proven both known
        and usable — only cooldowns/pauses whose `cause` is `"quota"` AND
        whose provider(s) are all in `usable` are touched. A pause naming
        several providers is a single fact about the whole set it names, so it
        is lifted only when every one of them is in `usable`, not just one.
        Anything with another cause, or none, is left alone.
        """
        cleared: dict[str, Any] = {"cooldowns": [], "pause": False}
        with self.transaction() as data:
            moment = now()
            for provider in list(data["cooldowns"]):
                auth, other = _split_cooldown(data["cooldowns"][provider])
                if provider in usable and (other or {}).get("cause") == "quota":
                    # PS-R4a: only the quota block; an auth block beside it
                    # is not quota's to lift.
                    _put_cooldown(data["cooldowns"], provider, auth, None, moment)
                    cleared["cooldowns"].append(provider)
            pause = data.get("pause") or {}
            if (pause.get("cause") == "quota" and pause.get("providers")
                    and all(p in usable for p in pause["providers"])):
                data["pause"] = {}
                cleared["pause"] = True
        for provider in cleared["cooldowns"]:
            self.emit("-", "cooldown_cleared", provider=provider)
        if cleared["pause"]:
            self.emit("system", "resumed", reason="fresh reading shows quota room")
        return cleared

    # How a provider's window is being spent, sampled wherever a budget is
    # already being read. Kept in the tree because every agent runs its own
    # server process and the window is shared by all of them: one process's
    # view of the burn rate is not the burn rate.
    HEADROOM_SAMPLES = 24

    def note_headroom(self, provider: str, headroom: float | None,
                      spent: float | None = None) -> None:
        if headroom is None:
            return
        with self.transaction() as data:
            series = data.setdefault("headroom", {}).setdefault(provider, [])
            if series and now() - series[-1][0] < 20:
                return                       # already sampled this moment
            series.append([now(), round(float(headroom), 4),
                           round(float(spent or 0), 4)])
            del series[:-self.HEADROOM_SAMPLES]

    def burn(self, provider: str, window: float = 3600.0, *,
             min_span_seconds: float = 300.0, min_samples: int = 3) -> dict:
        """How fast this provider's window is draining, and what that implies.

        Measured globally, which is the only way it means anything: the window
        belongs to the account, not to an agent, so an agent reasoning about
        its own consumption is reasoning about a fraction of the thing that
        will stop it. An advisor's point, and the reason this lives here rather
        than in a Run.

        `window_dollars` is derived the same way — from how much percentage a
        known amount of spending moved — because the provider never states what
        a window is worth. It is an estimate and is reported as one.

        bug-c050b0: `seconds_to_wall` is withheld unless the samples it is
        drawn from span at least `min_span_seconds` AND number at least
        `min_samples`. Two readings 39 s apart, taken while four agents loaded
        their first context, projected a wall in 174 s that was not real — a
        burst is not a burn rate. `headroom` and `points_per_minute` are still
        reported with as little as one sample; nothing acts on those alone.
        Callers pass the configured minimums (`budget.burn_min_span_seconds`,
        `budget.burn_min_samples`); a caller with no config, such as a bare
        `Tree`, gets the shipped defaults.
        """
        series = [s for s in (self.read().get("headroom", {}).get(provider) or [])
                  if now() - s[0] <= window]
        if not series:
            return {"samples": 0}
        out = {"samples": len(series), "headroom": series[-1][1]}
        if len(series) < 2:
            return out
        first, last = series[0], series[-1]
        span = last[0] - first[0]
        minutes = span / 60
        if minutes <= 0:
            return out
        drop = (first[1] - last[1]) * 100          # percentage points spent
        rate = drop / minutes                       # points per minute
        out["points_per_minute"] = round(rate, 3)
        if (rate > 0.01 and len(series) >= max(0, min_samples)
                and span >= max(0.0, min_span_seconds)):
            out["seconds_to_wall"] = max(0.0, last[1] * 100 / rate * 60)
        spent = last[2] - first[2]
        if drop > 1 and spent > 0:
            out["window_dollars"] = round(spent / (drop / 100), 2)
        return out

    def claim_instance(self, provider: str, window: float = 120.0) -> None:
        """Record that work has just been sent to this account.

        Routing counts running agents, and a node is not running — not even
        recorded — until after the choice is made. So five spawns in the same
        instant all read the same counts and all pick the same account, which
        is precisely the pile-up the counting was meant to prevent. A claim is
        written under the tree's own lock, and counts toward the load until the
        node it belongs to shows up as running.
        """
        with self.transaction() as data:
            claims = data.setdefault("claims", {})
            recent = [t for t in claims.get(provider, []) if now() - t < window]
            recent.append(now())
            claims[provider] = recent[-64:]

    def recent_claims(self, window: float = 120.0) -> dict[str, int]:
        current = now()
        return {name: len([t for t in stamps if current - t < window])
                for name, stamps in (self.read().get("claims") or {}).items()}

    def trial_pending(self, provider: str, window: float = 120.0) -> bool:
        """Whether the single trial a lapsed cooldown allows is already taken.

        The read half of `claim_trial`, for a caller that must decide without
        spending it. An admission check is such a caller: it reads the breaker
        and launches nothing, so claiming there would leave the routing pass
        that does launch finding its own claim held and refusing the provider
        the pin had just been admitted for (bug-521be6).
        """
        health = (self.read().get("provider_health") or {}).get(provider) or {}
        return now() - float(health.get("trial_at") or 0) < window

    def claim_trial(self, provider: str, window: float = 120.0) -> bool:
        """Take the single retry allowed when a cooldown has just lapsed.

        "One trial at a time" was a comment rather than a fact: when the timer
        lapsed, every deferred task woke at once, all found the provider usable,
        and all failed against it before the first could establish a new
        cooldown — a synchronised barrage, not a half-open breaker. The claim is
        made under the same lock the tree is written with, so exactly one caller
        gets it.
        """
        with self.transaction() as data:
            health = data["provider_health"].setdefault(
                provider, {"consecutive_failures": 0, "last_reason": ""})
            if now() - float(health.get("trial_at") or 0) < window:
                return False
            health["trial_at"] = now()
        return True

    # -------------------------------------------------------------- display --

    def render(self) -> str:
        """The tree as indented text — used by ``multiagents tree`` and the
        ``tree://project`` MCP resource."""
        data = self.read()
        nodes = data["nodes"]
        roots = [n for n in nodes.values() if not n.get("parent") or n["parent"] not in nodes]
        # Not an early return: a project with no live agents can still have a
        # question parked or a bug ticket queued, and those are exactly what
        # someone runs this to find.
        lines: list[str] = [] if nodes else ["(no agents)"]

        def walk(node: dict, indent: str, last: bool, top: bool) -> None:
            # Only non-root nodes get a connector. Keying this off "have we
            # printed anything yet" made the second root render as a child of
            # the first.
            branch_glyph = "" if top else ("└─ " if last else "├─ ")
            status = node.get("status", "?")
            usage = node.get("usage") or {}
            tokens = usage.get("total") or usage.get("total_tokens") or 0
            cost = usage.get("cost_usd") or 0
            label = f"[{status}]"
            if node.get("conversation"):
                label = f"[{status} · {node.get('turns', 0)} turns]"
            if status == AWAITING:
                # An agent parked overnight would otherwise read as a runaway,
                # because elapsed() keeps growing while it waits on a human.
                waited = now() - (node.get("paused_at") or now())
                label = f"[awaiting you · {waited / 60:.0f}m]"
            bits = [f"{node['id']}", f"{node.get('agent','?')}", label]
            if node.get("reason"):
                bits.append(f"({node['reason']})")
            if tokens:
                bits.append(f"{tokens:,}tok")
            if cost:
                bits.append(f"${cost:.4f}")
            if node.get("branch"):
                bits.append(node["branch"])
            lines.append(indent + branch_glyph + " ".join(bits))
            kids = [nodes[c] for c in node.get("children", []) if c in nodes]
            child_indent = indent if top else indent + ("   " if last else "│  ")
            for i, kid in enumerate(kids):
                walk(kid, child_indent, i == len(kids) - 1, top=False)

        # Grouped by session, so two nights' work stops interleaving. The
        # nesting is done HERE, in the view, and not by giving agents a parent:
        # `parent` drives git isolation and the merge gate, and a session is a
        # different question from "whose branch does this land on". Sessions
        # newest first; anything from before sessions were recorded, or from a
        # server nobody launched, keeps the old flat listing under no heading.
        by_session: dict[str, list[dict]] = {}
        for node in roots:
            by_session.setdefault(node.get("session") or "", []).append(node)

        def session_order(item: tuple[str, list[dict]]) -> float:
            return -max((n.get("created_at") or 0) for n in item[1])

        for session, members in sorted(by_session.items(), key=session_order):
            driver = next((n for n in members if n.get("role")), None)
            if session and driver is not None:
                started = driver.get("started_at") or driver.get("created_at") or 0
                when = time.strftime("%d %b %H:%M", time.localtime(started))
                lines.append(f"{driver['agent']} session · {when} "
                             f"· {driver.get('status', '?')}")
            for node in members:
                if node is driver and len(members) > 1:
                    continue             # the heading already named it
                walk(node, "  " if (session and driver is not None) else "",
                     True, top=True)
        pending = [q for q in data.get("questions", []) if q.get("status") == "open"]
        if pending:
            lines.append("")
            for q in pending[:6]:
                lines.append(f"  ? {q['agent']} asks about {q['topic']}: {q['question'][:70]}")
            lines.append(f"  answer with `multiagents ask`  ({len(pending)} open)")

        tickets = [t for t in data.get("tickets", [])
                   if t.get("status") in ("open", "awaiting_user")]
        if tickets:
            lines.append("")
            for t in tickets[:6]:
                mark = "!" if t.get("severity") == "blocking" else "·"
                lines.append(f"  {mark} bug {t['id']}: {t['title'][:66]}")
            lines.append(f"  review with `multiagents tickets`  ({len(tickets)} open)")

        pause = data.get("pause") or {}
        if pause and pause.get("until", 0) > now():
            waiting = int(pause["until"] - now())
            lines.append("")
            lines.append(f"  || PAUSED  {pause.get('reason', '')[:60]}")
            lines.append(f"     clears in ~{waiting // 60}m; deferred work "
                         f"restarts by itself")

        rollup = self.rollup_usage()
        total_cost = rollup.get("cost_usd", 0)
        # rollup's `total` is already normalised across every provider's
        # usage shape (see sum_usage), so agy's spend is inside it — adding
        # a `total_tokens` term here counted those runs twice.
        total_tokens = rollup.get("total", 0)
        if total_tokens or total_cost:
            lines.append(f"\ntotal: {int(total_tokens):,} tokens, ${total_cost:.4f}")
        pending = len(data.get("deferred", []))
        if pending:
            lines.append(f"{pending} deferred task(s) waiting on quota")
        return "\n".join(lines)
