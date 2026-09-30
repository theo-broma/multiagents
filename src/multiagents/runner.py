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
import copy
import fcntl
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
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from . import budget as budget_mod
from . import gitops
from . import config as config_mod
from .config import AgentSpec, Config, budget_number, limit_number, matches_any
from .executor import build_env, get_executor, prepare_home, private_file
from .executor.base import (BASE_ENV_KEYS, FollowHandle, Handle, read_exit_status,
                            running, stop_wrapped)
from . import providers as providers_mod
from . import notices
from . import procs
from . import scripts
from . import paths as paths_mod
from .paths import ProjectPaths, global_config_dir, state_root
from .providers import Event, Provider, get_path, load_providers
from .redact import scrub
from .auth import looks_like_auth_failure
from .startup import StartupHealth, StartupUnavailable
from .authority import HostAuthority
from .launch_limits import LaunchLimits
from .occupancy import ContainerOccupancy
from .executor.docker import DockerExecutor
from .supervisor import Supervisor, looks_like_quota_failure
from .tree import (ACTIVE, DRIVER_ROLES, TERMINAL, Node, Tree,
                   deferred_malformed, new_id, node_from_raw, now)
from .transcripts import session_transcript

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
    """
    if node.status == "pending":
        return True
    if node.status == "running":
        return node.pid is None or procs.alive(node.pid, node.pid_start)
    if node.status == "stuck":
        return node.pid is not None and procs.alive(node.pid, node.pid_start)
    return False

# Matched against an agent's TEXT only, never tool arguments — an agent reading
# a file that mentions the marker must not park itself.
NEED_DECISION = re.compile(r"NEED_DECISION\(([^)]{0,80})\)\s*:\s*(.+)")
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
class Run:
    """In-process state for a run this server started."""

    node_id: str
    provider: Provider
    spec: AgentSpec
    handle: Handle | None = None
    supervisor: Supervisor | None = None
    task: asyncio.Task | None = None
    events: list[dict] = field(default_factory=list)
    text_parts: list[str] = field(default_factory=list)
    final_assistant_message: str = ""
    refusal_signal: str = ""
    final_status: str = ""
    startup_token: str = ""
    startup_progress: bool = False
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
    return out


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


class Runner:
    def __init__(self, paths: ProjectPaths, config: Config):
        self.paths = paths
        self.config = config
        self.providers = load_providers(config.providers)
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
        providers = load_providers(config.providers)
        if config.providers != self.config.providers:
            # A cached reading was taken through the old provider definition.
            # Dropped only when providers changed: a re-read costs a script run
            # per provider, on every spawn's critical path.
            budget_mod.invalidate_cache()
        self.config = config
        self.providers = providers

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
        return os.environ.get("MULTIAGENTS_AGENT_ID") or None

    def session(self) -> str:
        """Which launched session this server belongs to, if any.

        `driver.py` puts it in the CLI's environment before exec'ing, and the
        CLI starts this server as a child, so it arrives by inheritance. Empty
        for a server nobody launched — a bare `python -m multiagents.server`.
        """
        return os.environ.get("MULTIAGENTS_SESSION_ID", "")

    def self_depth(self) -> int:
        try:
            return int(os.environ.get("MULTIAGENTS_DEPTH", "0"))
        except ValueError:
            return 0

    def _auth_ok(self, name: str) -> bool | None:
        """Ask the provider's own `check` action. None = it would not say.

        Structured, not prose: `check` is part of the script contract and
        answers with an exit code (0 authenticated, 10 not). Reading it is the
        opposite of the thing this project refuses to do — it is asking the CLI
        rather than guessing from what a model wrote.
        """
        from . import auth, scripts as scripts_mod

        provider = self.providers.get(name)
        if provider is None:
            return None
        code, out, err = scripts_mod.run_action(
            name, provider, self.executor(), "check", global_config_dir(),
            self.paths.config, timeout=20)
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
            if not state.get("tripped"):
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
            ok = self._auth_ok(name)
            if ok:
                self.tree.clear_cooldown(name)
                self.tree.clear_provider_health(name)
                budget.cooldown_until = None
            else:
                reason = (f"{name} is not authenticated — run "
                          f"`multiagents auth login {name}`")
                self.tree.set_cooldown(name, now() + auth_window, reason,
                                       needs_login=True, cause="auth")
                budget.cooldown_until = now() + auth_window
                budget.note = reason

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
        if self.self_id() is None:
            return True                       # the root orchestrator always may
        return os.environ.get("MULTIAGENTS_CAN_SPAWN", "0") == "1"

    # ------------------------------------------------------------- guardrails --

    def _admission(self, spec: AgentSpec) -> None:
        """The `max_concurrent` occupancy rule (RM-R1, shared with start()).

        A node counts against the tree's slot limit exactly as `_preflight`
        counts it for `start()` — same filter, same refusal text and shape.
        The idle node of a standing conversation holds no slot
        (`_occupies_slot`), so only live agents are measured against the cap.
        """
        limits = self.config.limits
        active = [n for n in self.tree.active() if _occupies_slot(n)]
        max_concurrent = int(limits.get("max_concurrent", 4))
        if len(active) >= max_concurrent:
            raise RuntimeError(self._refused(
                f"{len(active)} agents already running (max_concurrent={max_concurrent}). "
                f"Wait for one to finish or stop it.",
                spec, "limits.max_concurrent", max_concurrent, "tree",
                notices.provenance(self.config, "limits.max_concurrent", max_concurrent),
                f"{len(active)} already running"))

    def _admission_reserved(self, spec: AgentSpec, node: Node) -> None:
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
        """
        limits = self.config.limits
        max_concurrent = int(limits.get("max_concurrent", 4))
        active = 0
        with self.tree.transaction() as data:
            for key, raw in data["nodes"].items():
                if (key == node.id or raw.get("status") not in ACTIVE
                        or raw.get("role", "") in DRIVER_ROLES):
                    continue
                try:
                    other = node_from_raw(raw, key)
                except (TypeError, ValueError):
                    continue                  # HA-R12: a malformed entry is skipped
                if _occupies_slot(other):
                    active += 1
            if active < max_concurrent:
                entry = data["nodes"].get(node.id)
                if entry is not None:
                    entry["status"] = "pending"
                return
        # The refusal is recorded OUTSIDE the transaction: `_refused` writes
        # a notice, and holding the tree's flock while it does would ask the
        # same lock of a second file descriptor.
        raise RuntimeError(self._refused(
            f"{active} agents already running (max_concurrent={max_concurrent}). "
            f"Wait for one to finish or stop it.",
            spec, "limits.max_concurrent", max_concurrent, "tree",
            notices.provenance(self.config, "limits.max_concurrent", max_concurrent),
            f"{active} already running"))

    def _release_reserved_slot(self, node_id: str) -> None:
        """RM-R1a: give back the slot a reserved resumed turn did not use.

        Only a `pending` node is touched — once the turn is `running` the
        slot belongs to the run, and its own finalization releases it.
        """
        with self.tree.transaction() as data:
            entry = data["nodes"].get(node_id)
            if entry is not None and entry.get("status") == "pending":
                entry["status"] = "idle"

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
        parent = self.self_id()
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
                       limits: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        """LN-C2: `effective_limits` as reported, each entry `{value, source,
        source_detail}`. `source` stays LM-R2's layer name; `source_detail`
        says which file and line (or which call argument) it came from."""
        out: dict[str, dict[str, Any]] = {}
        for name, entry in limits.items():
            limit_key = "limits." + config_mod.LIMIT_FIELDS[name][0]
            layer = entry.get("source")
            if layer == "call":
                detail = notices.call_source(name, limit_key)
            else:
                key = f"agents.{agent_name}.{name}" if layer == "agent" else limit_key
                detail = notices.provenance(self.config, key, entry.get("value"), layer)
            out[name] = {**entry, "source_detail": detail}
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

    def _supervisor(self, spec: AgentSpec, provider: Provider,
                    wall_timeout: float, silence_timeout: float | None = None
                    ) -> Supervisor:
        loop_repeats = int(self.config.limits.get("doom_loop_repeats", 5))
        if silence_timeout is None:
            silence_timeout = self.config.effective_limits(spec)["silence_timeout"]["value"]
        return Supervisor(
            silence_timeout=silence_timeout,
            wall_timeout=wall_timeout,
            max_steps=spec.max_steps or int(
                self.config.limits.get("max_steps", 250)),
            loop_repeats=loop_repeats,
            loop_rearm=int(self.config.limits.get("doom_loop_rearm", loop_repeats)),
            declares_turn=_declares_turn(provider),
            opaque_tools=frozenset(provider.opaque_tools),
            opaque_tool_args=tuple(provider.opaque_tool_args),
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
          CLI's own rejection eight seconds later;
        - and a provider with no `effort_suffixes` is unchanged, as is any
          pair that agrees or carries no effort at all (an empty route
          `effort: ""` still just drops the option).
        """
        implied = provider.implied_effort(spec.model)
        if implied is None or not spec.effort or spec.effort == implied:
            return spec
        explicit = False
        if provider.name != spec.provider:
            # RM-R5b: explicitness belongs to the ROUTE the model came from,
            # not to the name routing landed on. When the landed provider is
            # a family sibling of the route the effort was written on, that
            # route travelled with the launch — its model did — and its
            # effort is as explicit here as it would be on the route's own
            # provider.
            _, route = self._routed_spec(spec, provider.name)
            if route:
                _, overrides = spec.fallback_for(route)
                explicit = bool(overrides.get("effort"))
        if explicit:
            raise ValueError(
                f"refusing to start {spec.name!r} on {provider.name}: model "
                f"{spec.model!r} implies effort {implied!r}, but the route "
                f"under `models:` explicitly configures effort "
                f"{spec.effort!r}. The CLI would reject the pair at launch. "
                f"Fix the route's effort (or remove it to inherit the "
                f"model's), then start again."
            )
        self.tree.emit(node_id, "effort_normalised",
                       provider=provider.name, model=spec.model,
                       effort=implied, was=spec.effort,
                       reason=f"model id {spec.model!r} ends in a suffix "
                              f"declaring effort {implied!r}; the inherited "
                              f"effort was normalised to it")
        return spec.replace(effort=implied)

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
        home = None
        if self.config.home_policy == "per-agent":
            home = prepare_home(self.paths.home(node_id), provider.home_links,
                                "per-agent", agent=spec.name,
                                copies=provider.home_copy)
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
        for key, value in (provider.env or {}).items():
            env[key] = os.path.expanduser(os.path.expandvars(str(value)))
        # Identity last: it is what the server's gates trust (SM-R3), so no
        # configuration may restate it.
        env.update(identity)
        executor = self.executor(spec)

        # LN-C2, finding 8: the provenance captured with the values at THIS
        # launch — the file and line as they are now, not as they will be when
        # a trip fires.
        limits = self._limits_detail(spec.name, self._limits_for(node_id, spec,
                                                                 timeout))
        # LN-C2, adversary findings 3/8: written where the container cannot
        # reach, so a later adoption or relaunch reads what THIS launch ran
        # under, not what the node's forgeable record in `tree.json` claims.
        launched = now()
        self.launch_limits.record(node_id, limits, launched)
        wall = limits["timeout"]["value"]
        options = {"effort": spec.effort,
                   **{k: v for k, v in spec.extra.items() if isinstance(v, (str, int))}}
        argv = provider.build_command(
            prompt=prompt, model=spec.model, workdir=str(workdir),
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

        run_dir = self.paths.run_dir(node_id)
        dfd = _run_dir_fd(run_dir)
        try:
            existing = os.listdir(dfd)
        finally:
            os.close(dfd)
        # SM-R1/R2: the server goes to an agent that may spawn, and only to one.
        if spec.can_spawn:
            server_argv, server_env = self._hand_server(node_id, provider, env, home, run_dir)
            argv += server_argv
            env.update(server_env)
        else:
            self._withdraw_server(provider, home)
        # H8: refused before anything starts — the kernel's per-argument
        # limit (checked over the fully assembled argv, adapter and server
        # arguments included).
        providers_mod.check_argv_limit(provider.name, argv)
        turn = len([n for n in existing if n.startswith("prompt") and n.endswith(".md")])
        _run_write(run_dir, f"prompt.{turn}.md" if turn else "prompt.md", prompt)
        launched = now()
        # Environment KEYS only — values may be secret and this file is on disk.
        # `launched_at` and `timeout` are what a server adopting this run
        # restarts its wall clock from (SV-R8).
        _run_write(run_dir, "command.json", json.dumps(scrub({
            "argv": argv, "cwd": str(workdir), "env_keys": sorted(env),
            "provider": provider.name, "model": spec.model,
            "permission": spec.permission, "resumed": bool(session_id),
            "launched_at": launched, "timeout": wall,
        }), indent=2))

        problems = executor.preflight()
        if problems:
            raise RuntimeError("; ".join(problems))
        # SV-R5: owned before it exists, so no other server's adoption pass
        # can find it running and unowned in between.
        if not self._claim(node_id):
            raise RuntimeError(f"{node_id} is supervised by another server")

        # SV-R1/R4: under the launch wrapper, which writes the output and the
        # exit status to the run dir and ends the run at its wall clock even
        # when no server is left to.
        handle = None
        try:
            startup_token = startup_token or self.startup.claim(provider.name, node_id)
            handle = await executor.start(argv, workdir, env, run_dir=run_dir,
                                          deadline=launched + wall if wall else 0,
                                          provider=provider.name)
            self.startup.bind(provider.name, node_id, startup_token, handle.pid,
                              getattr(handle, "pid_start", "") or "")
        except BaseException:
            # Nothing started, so nothing is followed: a lock kept here would
            # make the node unadoptable for this server's whole lifetime.
            if handle is not None:
                await handle.stop()
            self._release(node_id)
            self._startup_finish(provider.name, node_id, startup_token)
            raise
        run = Run(
            node_id=node_id, provider=provider, spec=spec, handle=handle,
            supervisor=self._supervisor(spec, provider, wall,
                                        limits["silence_timeout"]["value"]),
            turn_start=getattr(handle, "offset", 0), limits=limits,
            startup_token=startup_token,
            **({"done": done} if done is not None else {}),
        )
        self.runs[node_id] = run
        await self._track_container_run(run, executor)
        self.tree.update(node_id, pid=handle.pid,
                         pid_start=getattr(handle, "pid_start", "")
                         or procs.start_time(handle.pid),
                         follow={"turn": run.turn_start, "offset": run.turn_start,
                                 "log": _size(run_dir / "stream.jsonl")},
                         adopted_at=None)
        self.tree.set_status(node_id, "running")
        run.task = asyncio.create_task(self._consume(run))
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
        one of ours (finding 1). Nothing here may fail a launch that already
        started a process (adversary finding 2): the run must always get its
        consumer, so a record that cannot be written costs the OOM
        attribution its evidence, not the run its supervision."""
        reader = getattr(executor, "oom_kill_count", None)
        if not callable(reader):
            return                                  # not a container
        run.oom_reader = reader
        run.oom_container = str(getattr(executor, "container", "") or "")
        with contextlib.suppress(Exception):
            run.oom_baseline = await asyncio.to_thread(reader)
        handle_pid = getattr(run.handle, "pid", 0) or 0
        try:
            entry = self.occupancy.register(
                run.oom_container, run.node_id, handle_pid,
                getattr(run.handle, "pid_start", "") or "")
            run.oom_since = entry.get("since")
        except Exception:
            # Unwritable record: `others` fails closed without it, which is
            # `kill_uncertain` — the answer LN-C5 prefers to a wrong `killed`.
            pass

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

    async def _pin_health(self, spec: AgentSpec) -> dict | None:
        problem = self._pin_problem(spec)
        if problem:
            return problem
        if await asyncio.to_thread(self._auth_ok, spec.provider) is False:
            return self._pin_refusal(spec.provider, "provider is not authenticated")
        cooldowns = self.tree.read().get("cooldowns", {})
        budgets = await asyncio.to_thread(
            budget_mod.read_all, self.providers, lambda _name: self.executor(spec),
            global_config_dir(), self.paths.config, None, cooldowns,
            limits=self.config.limits)
        self._half_open(budgets, cooldowns)
        self._wind_down(budgets)
        cfg = self.config.project.get("budget", {})
        chosen, why = budget_mod.choose_provider(
            spec.provider, budgets, [], float(cfg.get("reserve_headroom", 0.15)),
            reserved=budget_mod.reserved_providers(
                self.config.project, self.providers, self._orchestrator_provider()),
            allowed={spec.provider})
        if chosen is None:
            entry = budgets.get(spec.provider)
            return self._pin_refusal(spec.provider, why,
                                     entry.cooldown_until if entry else None)
        return None

    def _startup_finish(self, provider: str, node_id: str, token: str,
                        failed: bool = False, error: str = "") -> None:
        if not token:
            return
        event = self.startup.finish(
            provider, node_id, token, failed=failed, error=error,
            threshold=int(self.config.limits.get("startup_failure_threshold", 2)),
            cooldown=float(self.config.limits.get("provider_down_cooldown_seconds", 1800)))
        if event:
            self.tree.emit(node_id, "startup_down", **event)

    # ----------------------------------------------------------------- start --

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
    ) -> dict[str, Any]:
        spec = self.config.agent(agent_name)
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
                raise ValueError(
                    f"{spec.provider} does not serve a model called {model!r}. "
                    f"A model id belongs to its provider's namespace. "
                    f"{agent_name!r} can also run on: {offers} — naming one of "
                    f"those models here runs it on that provider."
                )
            else:
                spec = spec.replace(model=model)
        if budget_tag and budget_tokens:
            # First value wins, so a re-declaration cannot lift a spent ceiling.
            self.tree.set_budget(budget_tag, budget_tokens,
                                 set_by=self.self_id() or self.session() or "root")
        if model:
            problem = await self._pin_health(spec)
            if problem:
                return problem
        self._preflight(spec, workdir, budget_tag, pinned=bool(model))
        # LN-C4: every refusal limit this start was checked against let it
        # through, so the notices for them in its scopes have stopped.
        passed = {"tree", self.self_id() or "tree", *([budget_tag] if budget_tag else [])}
        notices.clear(self.tree, lambda e: e.get("effect") == "refused"
                      and e.get("scope") in passed)
        provider = self.providers[spec.provider]

        parent = self.self_id()
        depth = self.self_depth() + 1
        node_id = new_id()

        # --- budget routing -------------------------------------------------
        # Budget now shells out to provider scripts, so it must not run on the
        # event loop: a slow provider would freeze every concurrent _consume,
        # wait_for_agents and check_agent. Cached for 60s and offloaded.
        cooldowns = self.tree.read().get("cooldowns", {})
        budgets = await asyncio.to_thread(
            budget_mod.read_all, self.providers,
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
        configured_spec = spec
        while True:
            spec = configured_spec
            provider = self.providers[spec.provider]
            routed_from, routed_why = "", ""
            # Other accounts on the same CLI. Interchangeable without a
            # `models:` entry, because a model id means the same thing on both.
            family = providers_mod.families(self.providers).get(
                self.providers[spec.provider].family
                if spec.provider in self.providers else "", [])
            # A disabled sibling is never a candidate (CX-C6): `read_all` omits
            # it, and a provider with no budget would otherwise read as one
            # with room.
            family = [name for name in family
                      if name == spec.provider or self.providers[name].enabled]
            # RT-R1: a candidate is a provider this agent has a model on — its
            # own, a `models:` entry naming one, or a sibling of either. A key
            # with an empty model is not one: routing there ran `--model ""`.
            chain = list(budget_cfg.get("fallback_chain", []))
            # RM-R2a: the agent's own `models:` routes are their own tier,
            # ahead of the project chain (RM-R2), in the order they are
            # written; a route's family siblings join right after it. They
            # are candidates always, not only when the preferred provider is
            # startup-blocked — a budget-exhausted preferred provider is
            # exactly when its own fallbacks should speak up. A pinned start
            # has no tiers: it asked for one provider.
            # RM-R2b: a disabled sibling is never a route — including the
            # family list's own entry for a listed key. `families` lists
            # every provider, enabled or not, and a disabled one has no
            # budget reading to answer the room question with.
            routes: list[str] = []
            if not model:
                family_of = providers_mod.families(self.providers)
                for name in (spec.models or spec.extra.get("models") or {}):
                    here = self.providers.get(name)
                    if here is not None and not here.enabled:
                        continue
                    if name not in routes:
                        routes.append(name)
                    for sibling in (family_of.get(here.family or name, [])
                                    if here is not None else []):
                        if sibling not in routes and self.providers[sibling].enabled:
                            routes.append(sibling)
            if model:
                family, chain = [], []
            startup_blocked = {}
            for name, candidate in self.providers.items():
                problem = self.startup.availability(name)
                if problem or name in unclaimable:
                    if problem:
                        startup_blocked[name] = problem
                    entry = budgets.setdefault(name, budget_mod.Budget(name, known=False))
                    entry.cooldown_until = max(
                        entry.cooldown_until or 0, now() + 1,
                        (problem or {}).get("retry_after") or 0)
                    entry.note = problem["reason"] if problem else "startup_down"
            usable = {name for name in self.providers
                      if self._usable_spec(spec, name) is not None}
            unmodelled = [name for name in dict.fromkeys([*routes, *chain, *family])
                          if name in self.providers and name not in usable
                          and self.providers[name].enabled]
            load, last_used = self._instance_load()
            reserve = float(budget_cfg.get("reserve_headroom", 0.15))

            def choose(budgets: dict, reserve: float) -> tuple[str | None, str]:
                return budget_mod.choose_provider(
                    spec.provider, budgets, chain, reserve,
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
                )

            chosen, why = choose(budgets, reserve)
            if chosen is None and model:
                entry = budgets.get(spec.provider)
                problem = startup_blocked.get(spec.provider) or {}
                return self._pin_refusal(spec.provider, problem.get("reason") or why,
                                         problem.get("retry_after") or
                                         (entry.cooldown_until if entry else None))
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
                queued = self.tree.defer(
                    {"agent": agent_name, "task": task, "timeout": timeout,
                     "model": model, "workdir": workdir}, retry_at, why,
                    deferred_by=self.self_id())
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
                self.tree.pause(retry_at, why, providers=unavailable or sorted(options),
                                deferral=True)
                return {"deferred": True, "reason": why, "retry_after": retry_at,
                        "paused": True,
                        # DQ-R11: the caller is told which entry this deferral
                        # created, so it never has to identify it by diffing
                        # the queue — another agent may defer in the same
                        # window, and diffing takes that one instead.
                        "deferred_id": queued["id"],
                        "note": "the tree is paused until this clears; deferred tasks "
                                "restart by themselves when it does"}
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
                routed = self._usable_spec(spec, chosen)
                if routed is None:          # choose_provider offers only `allowed`
                    raise RuntimeError(f"routing chose {chosen!r}, where agent "
                                       f"{agent_name!r} has no model")
                provider = self.providers[chosen]
                routed_from, routed_why = spec.provider, why
                if overrides:
                    routed_why += f" ({', '.join(f'{k}={v!r}' for k, v in overrides.items())})"
                spec = routed
            # RM-R7: the model/effort pair is settled BEFORE the startup
            # claim, so a refusal here takes neither the half-open provider's
            # only probe nor a startup.json run record — both would outlive
            # the refused start, because the release in the try/finally below
            # only runs for a claim that was actually taken. The event a
            # normalisation emits is the only trace, and only on success.
            spec = self._settle_effort(spec, provider, node_id)
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
                routed_from=routed_from, routed_why=routed_why,
                effort=spec.effort or "",
                limits=limits, session=self.session(), model_pinned=bool(model),
            )
            if self.authority:
                self.authority.add(node)
            if not workdir:
                gitops.create_worktree(repo, worktree_path, branch, base, unique=False)
            self.tree.add(node)
            if routed_from:
                # Loud enough to find later. This decision changes which model does
                # the work, and until now it left no trace anywhere.
                self.tree.emit(node_id, "routed", **{"from": routed_from,
                                                     "to": provider.name,
                                                     "model": spec.model,
                                                     "reason": routed_why})

            prompt = self.compose_prompt(spec, task, node, worktree_path)
            try:
                run = await self._launch(
                    node_id=node_id, spec=spec, provider=provider, prompt=prompt,
                    workdir=worktree_path, branch=branch, parent=parent, depth=depth,
                    timeout=timeout, startup_token=startup_token,
                )
            except RuntimeError as exc:
                self.tree.set_status(node_id, "failed", str(exc))
                return {"agent_id": node_id, "status": "failed", "error": str(exc)}

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

    # --------------------------------------------------------------- consume --

    async def _consume(self, run: Run) -> None:
        """Read the event stream, log it, supervise it, record the outcome."""
        node_id, provider, handle = run.node_id, run.provider, run.handle
        assert handle is not None and run.supervisor is not None
        run_dir = self.paths.run_dir(node_id)
        stream_log = _run_open(run_dir, "stream.jsonl",
                               os.O_WRONLY | os.O_APPEND | os.O_CREAT, "a")
        stderr_task = asyncio.create_task(handle.drain_stderr())
        watchdog = asyncio.create_task(self._watch_timers(run))
        usage: dict[str, Any] = {}
        cost_total = 0.0
        session_id = ""
        flush = _FlushGate()
        # Only worth sampling where the agent has a worktree of its own; with
        # no repository the state is always "" and the detector falls back to
        # signatures alone, which is what it did before.
        node = self.tree.get(node_id)
        progress_dir = (Path(node.worktree) if node and node.worktree
                        and gitops.is_repo(Path(node.worktree)) else None)
        last_progress = 0.0

        def follow() -> dict[str, int]:
            # SV-R7: written in the same transaction as the counts it stands
            # for, so a server that dies between the two cannot exist.
            return {"turn": run.turn_start, "offset": getattr(handle, "offset", 0),
                    "log": _size(run_dir / "stream.jsonl") if stream_log.closed
                    else stream_log.tell()}

        try:
            async for line in handle.lines():
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

                if event.text:
                    run.text_parts.append(event.text)
                    if event.kind == "text" or (event.kind == "result" and not run.final_assistant_message):
                        run.final_assistant_message = event.text
                if event.tokens:
                    usage = _merge_usage(usage, event.tokens, provider.usage_mode)
                if event.cost:
                    # Cost is always a per-step amount, whichever way a provider
                    # reports its token counts. This is the same number the
                    # opencode web console shows on its usage page.
                    cost_total += event.cost
                    usage["cost_usd"] = round(cost_total, 6)
                captured_session = bool(event.session_id and not session_id)
                if captured_session:
                    session_id = event.session_id
                if event.status:
                    if run.final_status.upper() not in {"REFUSED", "TRUNCATED"}:
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

                # A decision only a human can make: stop now rather than let
                # the agent spend another token building on a guess.
                if event.kind == "text" and event.text:
                    match = NEED_DECISION.search(event.text)
                    if match and run.awaiting is None:
                        default = PROPOSED_DEFAULT.search(event.text)
                        run.awaiting = {
                            "topic": match.group(1).strip(),
                            "question": match.group(2).strip(),
                            "proposed": default.group(1).strip() if default else "",
                        }
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
            stderr_task.cancel()
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
        try:
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
            self._startup_finish(provider.name, node_id, run.startup_token)
            if not relaunched:
                self._release(node_id)
                # LN-C4: the node ended, so its own notices have stopped.
                with contextlib.suppress(Exception):
                    notices.clear_node(self.tree, node_id)
                run.done.set()

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
            key = (f"agents.{agent}.max_steps" if run.spec.max_steps
                   else "limits.max_steps")
            source = notices.provenance(self.config, key, value)
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

        if not stopped_elsewhere:
            status, limited = await self._provider_health_after(run, status, text, stderr)
        else:
            limited = None

        # Commit anything the agent left uncommitted so no work is stranded on
        # an unreferenced worktree. Skipped while parked on a question: the
        # agent is mid-thought and will resume in the same worktree, and a
        # commit per question would both add noise and change what
        # _drop_if_empty decides for every later run.
        node = self.tree.get(node_id)
        if node and node.branch and Path(node.worktree).is_dir() and not run.awaiting:
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
        fix_attempts = 0
        if (commit_result is not None and not commit_result.ok and commit_result.hook
                and not stopped_elsewhere
                and status not in ("limited", "quota", "unauthenticated")
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
                # A status that tells the orchestrator to wait or to
                # re-authenticate wins over the commit failure, which is still
                # reported: the run ends as the cut would end any run, and a
                # `limited` one stays resumable.
                status, limited = fix_cut["status"], fix_cut["limited"]

        # A run that ends with nothing to say still ended for a reason.
        said_nothing = not text.strip()
        if status not in ("done", "merged", "awaiting_user") and said_nothing:
            text = self._no_output_summary(run, code)

        # CI-R2: a failed end-of-run commit must never be a silent no-op —
        # the agent's work would otherwise be stranded, uncommitted, with
        # nothing telling anyone. Reported here rather than by changing
        # `status`: a commit failure doesn't by itself make the run failed.
        if commit_result is not None and not commit_result.ok:
            detail = commit_result.err or commit_result.out
            # CI-R4: bound the git output carried into the result text, so a
            # noisy hook can't bury the agent's own answer.
            detail_for_text = detail if len(detail) <= 500 else detail[:500] + " [truncated]"
            text = f"{text}\n\ncommit failed: {detail_for_text}".strip()
            if fix_attempts:
                text += (f"\n(the agent was resumed {fix_attempts} time(s) to "
                         f"satisfy the git hook; it still refused the commit)")
            self.tree.emit(node_id, "commit_failed", detail=detail[:400])
        elif fix_attempts:
            text = (f"{text}\n\ncommit: a git hook refused the end-of-run commit; "
                    f"it succeeded after {fix_attempts} fix attempt(s).").strip()

        summary = text[-MAX_SUMMARY_CHARS:] if text else ""
        _run_write(run_dir, "result.json", json.dumps(scrub({
            "status": status, "exit_code": code, "session_id": session_id,
            "usage": usage, "text": text, "stderr_tail": stderr,
        }), indent=2))

        self.tree.update(node_id, usage=usage, session_id=session_id, summary=summary[:2000])
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
                    and fresh and not fresh.retries
                    and fresh.elapsed() < float(self.config.limits.get(
                        "retry_silent_failure_under_seconds", 60))):
                self.tree.emit(node_id, "retrying",
                               reason=f"died in {fresh.elapsed():.0f}s with no output")
                # Counted on the NODE, not on the Run: _launch replaces the Run,
                # so a flag kept there resets on every retry and one free retry
                # becomes an unbounded loop. Found by running it.
                self.tree.update(node_id, retries=fresh.retries + 1)
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
                    retried = await self._launch(
                        node_id=node_id, spec=run.spec, provider=run.provider,
                        prompt=_run_read(run_dir, "prompt.md"),
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
                    detail = f"{type(exc).__name__}: {exc}"[:300]
                    self.tree.emit(node_id, "retry_failed", detail=detail)
                    self.tree.set_status(node_id, "failed",
                                         f"retry launch failed: {detail}")
                    return False
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
                if run.final_status and run.final_status.upper() not in {
                        "SUCCESS", "OK", "COMPLETED"}:
                    reason = f"{run.provider.name} reported {run.final_status}"
                elif code != 0:
                    reason = f"exited {code}"
                else:
                    reason = "produced no output"
            self.tree.set_status(node_id, status, self._with_trip(prior_stuck, reason))

        self._startup_finish(run.provider.name, node_id, run.startup_token,
                             failed=status == "failed" and not run.startup_progress
                             and not run.stop_requested and not timed_out,
                             error=(stderr or text).splitlines()[0] if (stderr or text) else status)

        # Auto-merge this agent's own children upward: their work is still
        # quarantined on this agent's branch, so nothing real has changed yet.
        if status == "done":
            # Children first: this agent's branch should carry their work when
            # it is itself merged upward, rather than stranding it.
            await self._merge_pending_children(node_id)
            await self._maybe_merge_into_parent(node_id)

        if not run.awaiting and not stopped_elsewhere:
            # A parked agent still owns its worktree and will resume in it, and
            # a stopped one is left as a stop leaves it: resumable.
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
        provider (`limited`, `quota`, `unauthenticated`) — that turn's
        verdict, whose status becomes the run's.
        """
        node_id = run.node_id
        limits = self.config.limits
        allowed = int(limit_number(limits, "commit_fix_attempts", zero_ok=True))
        wall = int(limit_number(limits, "commit_fix_timeout"))
        result, attempt, usage = failed, 0, {}
        while not result.ok and result.hook and attempt < allowed:
            attempt += 1
            output = result.err or result.out
            self.tree.emit(node_id, "commit_fix_attempt", attempt=attempt,
                           hook=result.hook, detail=output[-400:])
            try:
                fix = await self._launch(
                    node_id=node_id, spec=run.spec, provider=run.provider,
                    prompt=COMMIT_FIX.format(
                        hook=result.hook, attempt=attempt, allowed=allowed,
                        output=output[-COMMIT_FIX_OUTPUT_CHARS:]),
                    workdir=Path(node.worktree), branch=node.branch,
                    parent=node.parent, depth=node.depth,
                    session_id=session_id, timeout=wall, done=run.done,
                )
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
            if verdict["status"] in ("limited", "quota", "unauthenticated"):
                return result, attempt, "", usage, verdict   # cannot be resumed again
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
                             error=(stderr or text).splitlines()[0] if (stderr or text) else status)

        run.fix_verdict = {"status": status, "limited": limited, "usage": usage}
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
                authenticated = await asyncio.to_thread(self._auth_ok, name)
                if authenticated is False:
                    seconds = float(self.config.limits.get(
                        "provider_auth_cooldown_seconds", 6 * 3600))
                    reason = (f"{name} is not authenticated — run "
                              f"`multiagents auth login {name}`")
                else:
                    seconds = float(self.config.limits.get(
                        "provider_down_cooldown_seconds", 1800))
                    reason = (f"{trip['failures']} runs in a row failed — check "
                              f"`multiagents auth login {name}` and "
                              f"`multiagents doctor`")
                self.tree.set_cooldown(name, now() + seconds, reason,
                                       needs_login=authenticated is False,
                                       cause="auth" if authenticated is False
                                       else "provider_down")
                self._maybe_cool_family(name, seconds)
        return status, limited

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
        elapsed = round(node_now.elapsed()) if node_now else 0
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
        if node is None or not node.branch or spec.writes or spec.conversational:
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
            await asyncio.sleep(5)
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
        if branch:
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
                run.refusal_signal = f'response matched refusal marker "{message[:150]}"'
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
        if run.final_status and run.final_status.upper() not in {"SUCCESS", "OK", "COMPLETED"}:
            return "failed"
        if code != 0:
            return "failed"
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
        if not node or not node.branch or not node.parent:
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
            "elapsed_seconds": round(node.elapsed()),
            "steps": node.steps,
            "total_events": len(events),
            "next_since": since + len(window),
            "usage": node.usage,
            "branch": node.branch or None,
            "events": [_compact(e) for e in window],
        }
        result.update(self._no_commits_note(node))
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
            "elapsed_seconds": round(node.elapsed()),
            "log_dir": str(run_dir),
            "need_info": [ln for ln in text.splitlines() if ln.strip().startswith("NEED_INFO")],
        }
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
        if "NEED_INFO(" in text or "NEED_DECISION(" in text:
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
        if node.model_pinned:
            spec = spec.replace(provider=node.provider, model=node.model)
        else:
            routed = self._usable_spec(spec, node.provider)
            if routed is None:
                # Followed as it was launched; only a relaunch needs a model, and
                # steer refuses that first (RT-R2).
                alternative, overrides = spec.fallback_for(node.provider)
                routed = spec.replace(model=alternative, **overrides)
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

    async def adopt(self) -> list[str]:
        """SV-R6: take over the runs a previous server of this role left.

        Only a root server adopts: a nested one's agents are its own spawns,
        and it cancels them when it goes (SV-R3). Only nodes of its own
        session role: the initializer's agents are not the orchestrator's to
        finish. Only nodes nobody holds (SV-R5) — the lock, not the status,
        decides that, so a live but slow server is never robbed.
        """
        if self.self_id():
            return []
        mine = self._role_of(self.session())
        taken = []
        for node in self.tree.active():
            if node.status not in self.ADOPTABLE or node.id in self._locks:
                continue
            if self._role_of(node.session) != mine:
                continue
            try:
                if await self._adopt_one(node):
                    taken.append(node.id)
            except Exception as exc:
                await self._unadoptable(node, exc)
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
        executor = self.executor(self.config.agents.get(node.agent))
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
        if live and self._past_deadline(run_dir, node.id):
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

        spec, provider = self._spec_of(node)
        command: dict[str, Any] = {}
        with contextlib.suppress(OSError, ValueError):
            command = json.loads(_run_read(run_dir, "command.json"))
        
        limits = self.launch_limits.lookup(node.id)
        launched = self.launch_limits.launch_time(node.id)
        wall = float(limits.get("timeout", {}).get("value") or 0)
        
        if not launched:
            launched = float(command.get("launched_at") or node.started_at or now())
        if not wall:
            wall = float(command.get("timeout") or self._limits_for(node.id, spec)["timeout"]["value"])
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
        supervisor = self._supervisor(spec, provider, wall)
        # SV-R8: the wall clock runs from the launch, whoever watched it; the
        # silence clock runs from now, because nobody was listening before.
        supervisor.started = time.monotonic() - max(0.0, now() - launched)
        # LN-C2, adversary finding 3: the limits an adopted run trips
        # against come from the host-owned launch record the launching
        # server wrote, never from the node's record in `tree.json`, which
        # the running agent can forge between the two servers.
        limits = self.launch_limits.lookup(node.id)
        run = Run(node_id=node.id, provider=provider, spec=spec, handle=handle,
                  supervisor=supervisor, turn_start=turn,
                  replay_to=int(follow.get("offset", turn)), adopted=True,
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
        if live:
            self.tree.update(node.id, adopted_at=now())
            self.tree.set_status(node.id, "running", "adopted")
            self.tree.emit(node.id, "adopted", pid=node.pid)
        run.task = asyncio.create_task(self._consume(run))
        if live:
            asyncio.create_task(self._wrap_up_watch(node.id))
            self._start_credential_watch()
        return True

    def _past_deadline(self, run_dir: Path, node_id: str) -> bool:
        launched = self.launch_limits.launch_time(node_id)
        limits = self.launch_limits.lookup(node_id)
        wall = float(limits.get("timeout", {}).get("value") or 0)
        
        if not launched or not wall:
            with contextlib.suppress(OSError, ValueError, TypeError):
                command = json.loads(_run_read(run_dir, "command.json"))
                wall = wall or float(command.get("timeout") or 0)
                launched = launched or float(command.get("launched_at") or 0)
                
        return bool(wall) and bool(launched) and now() >= launched + wall

    async def shutdown(self, *, detach: bool) -> None:
        """This server is going. SV-R3: a root server leaves its agents
        running and says so; a nested one ends them, as it always has."""
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
            if node and node.status in ("running", "stuck", "pending"):
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
        executor = self.executor(self.config.agents.get(node.agent))
        if getattr(executor, "kind", "local") == "docker":
            with contextlib.suppress(Exception):
                if executor.kill_detached(node.id):
                    return True
            inside = getattr(executor, "inside", None)
            if not (inside and inside()):
                # Every pid in the run dir but `node.pid` was recorded in the
                # container's pid namespace, where it names nothing of ours:
                # signalled from the host it could reach any process group.
                # All the host owns is the `docker exec` client, and ending
                # that alone is the one safe thing left to do.
                start = getattr(node, "pid_start", "")
                if not running(node.pid, start):
                    return False
                with contextlib.suppress(OSError):
                    os.kill(node.pid, signal.SIGTERM)
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

    async def stop(self, agent_id: str, *, internal: bool = False) -> dict[str, Any]:
        """End this run's current turn.

        `internal=True` is steer()'s own use: it ends the turn so the same run
        can be respawned under the same id, and must not report the run as
        cancelled while that is happening — see `run.internal_stop` in
        `_consume`. A genuine parent-initiated stop (the default, and the only
        thing `stop_agent` ever asks for) still writes `cancelled` here.
        """
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
            if not internal:
                # Nor does a cancelled run reach the release at the end of
                # `_consume`, and whoever waits on it (consult, a drain) would
                # wait forever on a run that has ended. steer's internal stop
                # is not an end: it relaunches.
                run.done.set()
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
        return {"agent_id": agent_id, "status": "cancelled"}

    async def steer(self, agent_id: str, message: str) -> dict[str, Any]:
        """Redirect a running agent.

        A subprocess cannot be injected into mid-run, so the honest equivalent
        is to stop the current turn and resume the same session with the
        steering text. The agent keeps its context because both CLIs support
        resuming by session id.
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
        if run is not None:
            spec, provider = run.spec, run.provider
        else:
            # RT-R2: the session lives on the recorded provider, so without a
            # model there the steer is refused, not moved: another provider
            # cannot resume it, and `--model ""` is not a model.
            configured = self.config.agent(node.agent)
            if not node.model_pinned and self._usable_spec(configured, node.provider) is None:
                return {
                    "agent_id": agent_id, "steered": False,
                    "error": f"agent {node.agent!r} has no model for provider "
                             f"{node.provider!r}, where this run's session "
                             f"lives: `models.{node.provider}` is missing or "
                             f"names no model in agents.yaml. Add it to steer "
                             f"this run, or start a fresh one.",
                }
            spec, provider = self._spec_of(node)

        if node.model_pinned:
            refusal = await self._pin_health(spec.replace(provider=provider.name))
            if refusal:
                return {**refusal, "steered": False}

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
        elif branch and root_is_repo:
            missing = gitops.worktree_branch(workdir, root=self.paths.root) != branch
        else:
            missing = False
        if missing:
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
        try:
            startup_token = self.startup.claim(provider.name, agent_id)
        except StartupUnavailable as exc:
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
        # `internal=True`: this ends the turn to respawn the very same run, not
        # a cancellation, and must not report the run as `cancelled` while
        # that is in flight (bug-8195f2) — see `run.internal_stop`.
        try:
            await self.stop(agent_id, internal=True)
        except BaseException:
            # Nothing was relaunched, so nothing will release the claim above.
            self._startup_finish(provider.name, agent_id, startup_token)
            raise
        try:
            await self._launch(
                node_id=agent_id, spec=spec, provider=provider, prompt=message,
                workdir=workdir, branch=branch,
                parent=node.parent, depth=node.depth, session_id=node.session_id,
                startup_token=startup_token,
            )
        except RuntimeError as exc:
            self.tree.set_status(agent_id, "failed", str(exc))
            return {"agent_id": agent_id, "steered": False, "error": str(exc)}
        self.tree.set_status(agent_id, "running", "steered")

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
            if raw.get("status") in {"idle", "running", "stuck", "refused"} and raw.get("session_id"):
                node = node_from_raw(raw, key)
                if best is None or node.created_at > best.created_at:
                    best = node
        return best

    def _conversation_route(self, spec: AgentSpec, node: Node) -> AgentSpec | None:
        """The spec a standing conversation resumes as, or None if the roster
        no longer allows the provider it lives on (CX-C28).

        Observed at L7: the roster moved an advisor to another provider, and
        the next consult resumed the old session on the old one — a turn spent
        where the roster said not to, answered as the old model. Whether the
        node's provider is still a route is `_usable_spec`'s answer, the same
        one start and steer get (RT-R3).
        """
        return self._usable_spec(spec, node.provider)

    def _usable_spec(self, spec: AgentSpec, provider: str) -> AgentSpec | None:
        """What `spec` runs as on `provider`, or None when it has no model
        there (RT-R1..R3). Start, steer and consult all ask this, so what
        counts as a usable model cannot drift between them.

        A route is exact — the agent's own provider, or a `models:` entry that
        names a model — or same-family: a sibling instance of a provider with
        an exact route shares its model ids, so it runs that route's model. An
        empty fallback model is not a route: it would run `--model ""`.
        """
        routed, _ = self._routed_spec(spec, provider)
        return routed

    def _routed_spec(self, spec: AgentSpec,
                     provider: str) -> tuple[AgentSpec | None, str]:
        """`_usable_spec`, plus the name of the `models:` route that decided
        it — "" when the agent's own configuration is what runs.

        RM-R5b needs the route, not just the resolution: whether an effort
        was EXPLICITLY written on this destination is judged against the
        route the model came from, which is not always the provider routing
        landed on (a family sibling runs the listed route). The walk is
        `_usable_spec`'s own, kept in one place so the two answers cannot
        drift.
        """
        if provider == spec.provider:
            return spec, ""
        alternative, overrides = spec.fallback_for(provider)
        if alternative:
            return spec.replace(model=alternative, **overrides), provider
        here = self.providers.get(provider)
        if here is None:
            return None, ""
        family = here.family or provider
        roster = self.providers.get(spec.provider)
        if roster is not None and (roster.family or spec.provider) == family:
            return spec, ""
        # A sibling of a listed fallback shares that fallback's model ids.
        for name in (spec.models or {}):
            other = self.providers.get(name)
            if other is not None and (other.family or name) == family:
                alternative, overrides = spec.fallback_for(name)
                if alternative:
                    return spec.replace(model=alternative, **overrides), name
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
        # Waiting for the other turn is bounded by how long that turn may run.
        wait = self.config.effective_limits(spec, timeout)["timeout"]["value"] + 60
        try:
            async with self._conversation_turn(agent_name, wait) as ours:
                if ours:
                    return await self._consult_turn(agent_name, spec, message,
                                                    timeout)
                error = (f"{agent_name!r} was still answering another consult "
                         f"after {wait:.0f}s; this one did not run")
        except _ConsultLockError as exc:
            error = str(exc)
        node = self._find_conversation(agent_name)
        return self._consult_result(agent_name, node.id if node else None, None,
                                    error=error)

    async def _consult_turn(
        self, agent_name: str, spec: AgentSpec, message: str, timeout: int | None,
    ) -> dict[str, Any]:
        node = self._find_conversation(agent_name)
        turn = 1
        # Base is resolved once per turn and reused by the refresh, the result
        # and the recorded start point alike.
        base = self._conversation_base()
        base_sha = gitops.resolve_commit(self.paths.root, base)
        placed = True       # the worktree was just cut from base this turn
        replaced = None     # the conversation this turn replaces (CX-C28)

        route = None
        if node is not None:
            route = self._conversation_route(spec, node)
            if route is None:
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
            if self.authority:
                self.authority.add(node)
            gitops.create_worktree(self.paths.root, worktree_path, branch, base,
                                   unique=False)
            head = gitops.head_sha(worktree_path)
            node = replace(node, placed_on=head)
            self.tree.add(node)
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
                               provider=provider.name)
        else:
            node_id = node.id
            # RM-R1: a resumed turn occupies a slot like any other start, and
            # is refused like one when the tree is full — before any side
            # effect, so the conversation stays idle, keeps its session and
            # its turn count, and the same consult succeeds once a slot
            # frees. A new conversation is already checked, in `_preflight`.
            # RM-R1a: the check and the reservation are one transaction, so
            # the slot is ours from this moment; every exit below that does
            # not launch gives it back.
            self._admission_reserved(spec, node)
            reserved = node_id
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
            run = self.runs.get(node_id)
            if run is not None:
                spec, provider = run.spec, run.provider
            else:
                spec = route
                provider = self.providers.get(node.provider)
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
        try:
            run = await self._launch(
                node_id=node_id, spec=spec, provider=provider, prompt=prompt,
                workdir=worktree_path, branch=node.branch, parent=node.parent,
                depth=node.depth, session_id=session_id, timeout=timeout,
            )
        except RuntimeError as exc:
            self.tree.set_status(node_id, "failed", str(exc))
            return self._consult_result(agent_name, node_id, turn, view,
                                        error=str(exc))
        except BaseException:
            # RM-R1a: a reservation whose launch fails is released — the
            # node must not sit `pending` for ever, holding a slot no run
            # will ever account for. (`failed` above releases its own way.)
            if reserved:
                self._release_reserved_slot(node_id)
            raise
        reserved = ""

        limit = self.config.effective_limits(spec, timeout)["timeout"]["value"]
        try:
            await asyncio.wait_for(run.done.wait(), timeout=limit + 30)
        except (asyncio.TimeoutError, TimeoutError):
            await self.stop(node_id)
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
                "note": "this agent asked a question instead of answering; "
                        "resolve it with answer_question before relying on this",
                **view,
            }
        if final and final.status == "refused":
            return self._consult_result(agent_name, node_id, turn, view,
                                        status="refused", reason=final.reason,
                                        reply="", note="Rephrase the request or consult again.")
        return {
            "agent_id": node_id,
            "agent": agent_name,
            "turn": turn,
            "status": final.status if final else "unknown",
            "reply": notice + reply[-MAX_SUMMARY_CHARS:],
            "usage": final.usage if final else {},
            "note": "advisory only — you decide whether to act on this",
            **view,
            **({"ticket": run.ticket, "tickets": run.tickets} if run.ticket else {}),
        }

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
                f"idle. Waiting is only free when there is nothing else to start — "
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
        running = len([n for n in self.tree.active() if _occupies_slot(n)])
        limit = int(self.config.limits.get("max_concurrent", 4))
        return {"running": running, "max_concurrent": limit,
                "free_slots": max(0, limit - running)}

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
        self._prune_unreadable_entries()
        recovered = self._recover_stale_restarts()
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

        due = self.tree.due_deferred()
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
            try:
                result = await self.start(
                    agent, task_spec.get("task", ""),
                    workdir=task_spec.get("workdir"), timeout=task_spec.get("timeout"),
                    model=task_spec.get("model"), deferred_id=entry["id"],
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
            **self._idle_capacity_note(),
            "still_running": running,
            "still_stuck": still_stuck,
            **pause(),
        }

    # ------------------------------------------------------------------- git --

    def merge_agent(self, agent_id: str, into: str | None = None) -> dict[str, Any]:
        node = self.tree.get(agent_id)
        if node is None:
            raise KeyError(f"Unknown agent {agent_id!r}")
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
