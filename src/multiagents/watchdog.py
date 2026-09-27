"""Watching the orchestrator from outside it.

`run` execs into the provider's CLI, so the orchestrator *is* that process and
there is nobody left inside to report on it. This module is what runs alongside:
a small loop that samples what can be observed from the outside and writes a
verdict other commands can read.

What it deliberately does **not** do is read the conversation. The transcript
has no structured signal for "quota reached" — checked against a real one, every
apparent hit was the session's own prose about quotas — and inferring state from
content is the mistake this project already made once, when a classifier that
scanned agent output cooled down a provider for fifteen minutes because an
advisor used the word "quota". So only structure is used: does the process
exist, has the transcript grown, are the agents moving, does the provider report
headroom.
"""

from __future__ import annotations

import json
import os
import re
import string
import sys
import time
from pathlib import Path
from typing import Any

from . import procs

# How long a live session may produce nothing before it is called idle rather
# than working. Long enough to cover a slow tool call or a big file read.
IDLE_AFTER = 180.0


def transcript_source(provider: Any, cwd: Path,
                      executor: Any = None) -> tuple[Path, str] | None:
    """`(directory, glob)` where this provider records the session, if it says.

    The directory as the host reads it. Given the executor the agent ran
    under, the declared path is followed to where that executor really keeps
    it (SP-R2): a docker agent's `~/.<cli>/…` is the container's, and the
    host's own profile never saw a line of it. Without one, the
    executor of the project `cwd` belongs to is looked up.

    The first of `transcript_sources`: the slug of `cwd` with symlinks
    resolved, which is the one the CLI writes to.
    """
    sources = transcript_sources(provider, cwd, executor)
    return sources[0] if sources else None


def transcript_sources(provider: Any, cwd: Path,
                       executor: Any = None) -> list[tuple[Path, str]]:
    """Every `(directory, glob)` this session may be recorded in, best first.

    The CLI keys its directory on the working directory with symlinks
    resolved, so that slug comes first; a worktree reached through a symlink
    also tries the path as given, in case the CLI did not (SP-R2).
    """
    if executor is None:
        from .executor import executor_at
        executor = executor_at(cwd, getattr(provider, "name", ""))
    home = container_home(executor)
    directory = _declared_dir(provider, home)
    if directory is None:
        return []
    cwd = Path(cwd)
    try:
        resolved = cwd.resolve()
    except OSError:
        resolved = cwd
    glob = (getattr(provider, "transcript", None) or {}).get("glob", "*")
    out: list[tuple[Path, str]] = []
    for candidate in (resolved, cwd):
        # Every character outside [A-Za-z0-9] becomes '-', not only '/', '.'
        # and '_': a project path with a space, or any other punctuation, used
        # to slug to a directory the CLI never wrote to.
        slug = re.sub(r"[^a-zA-Z0-9]", "-", str(candidate))
        path = _expand(directory.format(slug=slug), home)
        host_path = getattr(executor, "host_path", None)
        if host_path is not None:
            path = host_path(path)
        if (path, glob) not in out:
            out.append((path, glob))
    return out


def container_home(executor: Any = None) -> Path:
    """HOME as an agent run by `executor` sees it: what a declared `~` means.

    The executor's to say, not this process's: the declaration describes the
    agent's view. An executor that says nothing runs agents on the host."""
    home = getattr(executor, "container_home", None)
    return Path(home()) if home is not None else Path.home()


def _expand(directory: str, home: Path) -> Path:
    """`directory` with a leading `~` taken as `home`, lexically normalised."""
    if directory == "~" or directory.startswith("~/"):
        directory = str(home) + directory[1:]
    return Path(os.path.normpath(directory))


def transcript_prefix(provider: Any, home: Path | None = None) -> Path | None:
    """The static part of the provider's declared transcript directory: the
    path up to the first component holding a placeholder (SP-R1). One store
    for it covers every agent's session, whatever its slug.

    `~` is `home`, the agent's HOME (the host's when not given). None when
    nothing is declared, or when the declaration is refused (see
    `_declared_dir`)."""
    home = Path(home) if home is not None else Path.home()
    directory = _declared_dir(provider, home)
    if directory is None:
        return None
    return _static_prefix(directory, home)


def _static_prefix(directory: str, home: Path) -> Path | None:
    parts: list[str] = []
    for part in Path(directory).parts:
        if "{" in part:
            break
        parts.append(part)
    if not parts:
        return None
    return _expand(str(Path(*parts)), home)


# Declarations already reported as refused, so a reader called on every tool
# call says so once rather than on every call.
_refused: set[tuple[str, str, str]] = set()


def _declared_dir(provider: Any, home: Path) -> str | None:
    """The provider's `transcript.dir`, or None if it declares none or the
    declaration is refused (SP-R1).

    The static prefix is what the docker executor mounts a store over, so a
    prefix that is not a proper subdirectory would put that store over the
    container's root or its whole HOME. Refused, and treated as no
    declaration at all, when the prefix is the filesystem root, HOME or an
    ancestor of it; when any component, before or after a placeholder, is
    `..`, which could walk the sessions out of whatever holds them; and when
    the directory holds any placeholder but `{slug}`, which nothing here can
    fill in.
    """
    directory = (getattr(provider, "transcript", None) or {}).get("dir")
    if not directory:
        return None
    why = _refusal(str(directory), home)
    if not why:
        return str(directory)
    key = (getattr(provider, "name", ""), str(directory), str(home))
    if key not in _refused:
        _refused.add(key)
        print(f"multiagents: ignoring transcript dir {directory!r} of provider "
              f"{key[0] or '?'}: {why}", file=sys.stderr)
    return None


def _refusal(directory: str, home: Path) -> str:
    try:
        fields = {name for _, name, _, _ in string.Formatter().parse(directory)
                  if name is not None}
    except ValueError as exc:
        return f"it is not a valid template ({exc})"
    if fields - {"slug"}:
        return (f"placeholder(s) {sorted(fields - {'slug'})} are not supported; "
                f"only {{slug}} is")
    if ".." in Path(directory).parts:
        return "it has a '..' component"
    prefix = _static_prefix(directory, home)
    if prefix is None or not prefix.is_absolute():
        return ""
    home = Path(os.path.normpath(home))
    if prefix == Path(prefix.anchor):
        return "its static prefix is the filesystem root"
    if prefix == home:
        return "its static prefix is HOME itself"
    if prefix in home.parents:
        return "its static prefix is an ancestor of HOME"
    return ""


def newest_transcript(provider: Any, cwd: Path, executor: Any = None) -> Path | None:
    for directory, pattern in transcript_sources(provider, cwd, executor):
        try:
            files = [p for p in directory.glob(pattern) if p.is_file()]
        except OSError:
            continue
        stamped = []
        for path in files:
            try:
                stamped.append((path.stat().st_mtime, path))
            except OSError:          # gone between the listing and now
                continue
        if stamped:
            return max(stamped, key=lambda pair: pair[0])[1]
    return None


def alive(pid: int | None, start: str = "") -> bool:
    """See :mod:`multiagents.procs`.

    The watchdog is handed its subject's pid by the parent that just spawned
    it, so there is no reboot between the two and no `start` to compare — but
    it goes through the same door as everything else, so that there is one
    answer to this question and not four.
    """
    return procs.alive(pid, start)


def verdict(*, running: bool, quiet_for: float | None, quota_known: bool,
            quota_left: float | None, active_agents: int,
            supported: bool = True, limit: dict | None = None) -> tuple[str, str]:
    """Turn the samples into one word and a sentence.

    The quota reading is what separates the two cases that matter and that the
    transcript alone cannot tell apart: a session that ended because it ran out,
    and one that ended because it broke.

    `limit` — the CLI's own limit message, last in the log — outranks all of it.
    A quota reader can go blind (the vendor moved the field, and this one did),
    and when it does an orchestrator stopped dead by its provider reads as
    "idle, probably waiting for you", which is the most misleading sentence
    this function can produce.
    """
    exhausted = quota_known and quota_left is not None and quota_left <= 0.02

    if limit:
        if running:
            return "limited", (f"alive but stopped by its provider: "
                               f"{limit.get('detail', 'usage limit')}")
        return "out_of_quota", (f"gone, stopped by its provider: "
                                f"{limit.get('detail', 'usage limit')}")

    if not running:
        if exhausted:
            return "out_of_quota", ("the orchestrator is gone and its provider has "
                                    "no headroom — it ran out rather than crashed")
        return "stopped", ("the orchestrator is gone while its provider still had "
                           "headroom — it exited or crashed")

    if quiet_for is None:
        # Two different blind spots, and telling them apart is the difference
        # between "this provider cannot be watched" and "it has not started".
        why = ("this provider publishes no session log, so only the process and "
               "the agents can be seen" if not supported else
               "no session log for this directory yet — nothing has been said")
        return ("working" if active_agents else "unknown", f"running; {why}")

    if quiet_for < IDLE_AFTER:
        return "working", f"producing output {quiet_for:.0f}s ago"
    if exhausted:
        return "stalled", (f"alive but silent for {quiet_for / 60:.0f}m and its "
                           f"provider has no headroom")
    if active_agents:
        return "waiting", (f"silent for {quiet_for / 60:.0f}m with {active_agents} "
                           f"agent(s) still running — probably waiting on them")
    return "idle", (f"alive, silent for {quiet_for / 60:.0f}m, nothing running — "
                    f"probably waiting for you")


def sample(paths, config, provider, role: str, pid: int | None,
           budget: Any = None, executor: Any = None) -> dict:
    """One observation, as a plain dict. Transcripts are read through
    `executor`, the one the role runs under (SP-R2)."""
    from .tree import Tree

    supported = provider is not None and transcript_source(provider, paths.root, executor) is not None
    transcript = newest_transcript(provider, paths.root, executor) if provider else None
    quiet_for = None
    record = {}
    if transcript is not None:
        try:
            stat = transcript.stat()
            quiet_for = max(0.0, time.time() - stat.st_mtime)
            record = {"bytes": stat.st_size, "quiet_for": round(quiet_for, 1)}
        except OSError:
            transcript = None

    tree = Tree(paths.tree_file, paths.events_file)
    active = len(tree.active())
    running = alive(pid)
    quota_known = bool(getattr(budget, "known", False))
    quota_left = getattr(budget, "headroom", None)

    limit = limit_reached(provider, paths.root, executor) if provider is not None else None
    state, detail = verdict(running=running, quiet_for=quiet_for,
                            quota_known=quota_known, quota_left=quota_left,
                            active_agents=active, supported=supported,
                            limit=limit)
    return {
        "at": time.time(),
        "role": role,
        "pid": pid,
        # What makes that pid answerable after a reboot: see
        # :mod:`multiagents.procs`. Without it the reconciliation below reads a
        # recycled number as the driver still running.
        "pid_start": procs.start_time(pid),
        "running": running,
        "verdict": state,
        "detail": detail,
        "transcript": record or None,
        "limit": limit,
        "active_agents": active,
        "provider": {
            "name": getattr(provider, "name", ""),
            "known": quota_known,
            "headroom": quota_left,
            "resets_at": getattr(budget, "resets_at", None),
        },
    }


# Two roles can drive a project — `run` launches the orchestrator, `init-agent`
# launches the initializer — and both are supervised the same way. One file for
# both meant the second to write won and the first was reported as the second:
# with init-agent running, `status` and the monitor showed the INITIALIZER's
# state under the orchestrator's name, with no way to tell.
DRIVERS = ("orchestrator", "initializer")


def status_file(paths, role: str = "orchestrator") -> Path:
    return paths.data / f"{role}-status.json"


def write_status(paths, record: dict, role: str = "") -> None:
    path = status_file(paths, role or record.get("role") or "orchestrator")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(record, indent=2) + "\n")
    os.replace(tmp, path)


def read_status(paths, role: str = "orchestrator") -> dict | None:
    try:
        return json.loads(status_file(paths, role).read_text())
    except (OSError, ValueError):
        return None


def read_all_status(paths) -> dict[str, dict]:
    """Every driver that has reported here, newest report first.

    Keyed by the role the RECORD claims, not by the file it was found in. A
    supervisor started before this split writes the initializer's state into
    the orchestrator's file, and the honest reading of that file is what it
    says about itself — otherwise the transition period reports exactly the
    confusion this change exists to end.
    """
    out: dict[str, dict] = {}
    for role in DRIVERS:
        record = read_status(paths, role)
        if not record:
            continue
        claimed = str(record.get("role") or role)
        if claimed != role:
            # Honest about both halves: the label follows the payload, and the
            # disagreement is reported rather than smoothed over. A supervisor
            # from before roles had their own files is still writing here, and
            # if a new one starts writing the same file the two will alternate.
            record = {**record, "misfiled_in": role}
        # A record says "running" until its supervisor writes again — and a
        # supervisor that was killed never does. The pid it recorded settles
        # it: without this a status file outlives its process and reports a
        # driver that has not existed for hours.
        if record.get("running") and record.get("pid") and \
                not alive(record["pid"], record.get("pid_start", "")):
            record = {**record, "running": False, "verdict": "stopped",
                      "detail": f"{record.get('detail', '')} — process is gone".strip(" —")}
        if (record.get("at") or 0) >= (out.get(claimed, {}).get("at") or 0):
            out[claimed] = record
    return dict(sorted(out.items(), key=lambda kv: -(kv[1].get("at") or 0)))


def ended_uncleanly(paths, role: str = "orchestrator") -> bool:
    """Did the last session of this role stop without anyone recording an end?

    A supervisor samples until the process it watches is gone and then writes
    one last record saying so, so `running: false` is the signature of an
    ending that something was present for — a quit, a `stop`, a crash the
    parent outlived. A record still claiming `running: true` for a process
    that is not there is the opposite: nothing was left to write the last line.
    A power cut, an OOM kill, `kill -9`.

    This is the only durable evidence of the difference. The pid file is not:
    `run` execs, so on a clean quit the CLI that inherited the pid exits with
    no `finally` left to clean up after it, and a leftover pid file means
    nothing at all. Nor is the tree: a session can stop uncleanly with no agent
    running — which is exactly what a power cut on 2026-09-15 did — and leave
    an empty `active()` behind it.
    """
    record = read_status(paths, role)
    if not record or not record.get("running"):
        return False
    return not alive(record.get("pid"), record.get("pid_start", ""))


def supervise(paths, config, role: str, pid: int, interval: float = 20.0,
              max_seconds: float = 0.0) -> int:
    """Sample until the watched process is gone, then record how it ended.

    Runs as its own process because `run` execs: there is no thread of ours
    left in the orchestrator to do this. It stops on its own when the pid it
    watches disappears, so nothing has to remember to clean it up.
    """
    from .budget import invalidate_cache, read_provider
    from .paths import global_config_dir
    from .executor import executor_for
    from .providers import load_providers

    providers = load_providers(config.providers)
    spec = next((a for a in config.agents.values()
                 if a.launch and a.role == role), None)
    provider = providers.get(spec.provider) if spec else None
    executor = (executor_for(paths, config, providers)(spec.provider)
                if provider is not None else None)
    started = time.time()
    budget = None
    last_quota = 0.0

    while True:
        # Quota is the slow sample and the one that matters least often, so it
        # is read on its own, longer cadence rather than every pass.
        if provider is not None and time.time() - last_quota > 120:
            last_quota = time.time()
            try:
                invalidate_cache()
                budget = read_provider(provider.name, provider, None,
                                       global_config_dir(), paths.config,
                                       use_cache=False)
            except Exception:
                budget = None

        record = sample(paths, config, provider, role, pid, budget, executor)
        write_status(paths, record, role)
        if not record["running"]:
            return 0
        if max_seconds and time.time() - started > max_seconds:
            return 0
        time.sleep(interval)


def has_human_turn(provider: Any, cwd: Path, executor: Any = None) -> bool | None:
    """Did anyone actually say anything in this session? None if unknowable.

    The headless handover replays the session with a nudge as its user turn, so
    a session that dropped *before* anyone typed would be handed "continue where
    you left off" with nowhere to continue from — and would invent work from
    BRIEF.md, unattended, with agents that hold bypass permissions.

    A typed message is a `user` record whose content is a plain string; a tool
    result is a `user` record whose content is a list of tool_result blocks.
    Structure again, not content: this reads the shape and never the words.
    """
    path = newest_transcript(provider, cwd, executor)
    if path is None:
        return None
    try:
        with path.open() as handle:
            for line in handle:
                if '"user"' not in line:
                    continue                      # cheap reject before parsing
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if record.get("type") != "user":
                    continue
                content = (record.get("message") or {}).get("content")
                if isinstance(content, str) and content.strip():
                    return True
                if isinstance(content, list) and any(
                        isinstance(block, dict) and block.get("type") == "text"
                        for block in content):
                    return True
    except OSError:
        return None
    return False


def _typed_by_a_person(record: dict) -> bool:
    """A `user` record someone actually typed, rather than a tool result."""
    if record.get("type") != "user":
        return False
    content = (record.get("message") or {}).get("content")
    if isinstance(content, str):
        return bool(content.strip())
    return isinstance(content, list) and any(
        isinstance(block, dict) and block.get("type") == "text"
        for block in content)


def limit_reached(provider: Any, cwd: Path, executor: Any = None) -> dict | None:
    """The CLI's own "I have stopped" message, if it is the last thing said.

    Not a classifier over model prose. These strings are hardcoded by the CLI's
    error handler, and the only reason they must be read out of a chat log is
    that the CLI chose the chat log as its error channel rather than stderr or
    an exit code. Same object as a stack trace, worse address.

    Position matters as much as content: it must be the last assistant message,
    with nothing a person typed after it. A limit hit and then recovered from is
    history, an agent quoting the string mid-conversation is not the CLI saying
    it, and a limit someone has already replied to is theirs, not ours.
    """
    markers = (getattr(provider, "transcript", None) or {}).get("limit_markers") or []
    if not markers:
        return None
    path = newest_transcript(provider, cwd, executor)
    if path is None:
        return None
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return None

    for line in reversed(lines[-400:]):
        if '"assistant"' not in line and '"user"' not in line:
            continue                       # cheap reject before parsing
        try:
            record = json.loads(line)
        except ValueError:
            continue
        # Someone spoke after it. Whatever the CLI said before that, a person is
        # here and has taken it from us — acting now would end a session while
        # its owner is using it. Tool results are `user` records too, so this
        # asks who typed it, never what it says.
        if _typed_by_a_person(record):
            return None
        if record.get("type") != "assistant":
            continue                       # `system`, `mode`, hooks: not a turn
        content = (record.get("message") or {}).get("content")
        if isinstance(content, list):
            content = " ".join(str(b.get("text", "")) for b in content
                               if isinstance(b, dict))
        text = str(content or "")
        for marker in markers:
            if marker.get("match", "") and marker["match"].lower() in text.lower():
                return {"detail": marker.get("detail") or marker["match"],
                        "resets": bool(marker.get("resets", True)),
                        "said": text.strip()[:300]}
        return None            # the last assistant message is not a limit
    return None
