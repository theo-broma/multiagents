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
import time
from pathlib import Path
from typing import Any

# How long a live session may produce nothing before it is called idle rather
# than working. Long enough to cover a slow tool call or a big file read.
IDLE_AFTER = 180.0


def transcript_source(provider: Any, cwd: Path) -> tuple[Path, str] | None:
    """`(directory, glob)` where this provider records the session, if it says."""
    spec = getattr(provider, "transcript", None) or {}
    directory = spec.get("dir")
    if not directory:
        return None
    slug = str(cwd).replace("/", "-").replace(".", "-").replace("_", "-")
    return Path(directory.format(slug=slug)).expanduser(), spec.get("glob", "*")


def newest_transcript(provider: Any, cwd: Path) -> Path | None:
    source = transcript_source(provider, cwd)
    if source is None:
        return None
    directory, pattern = source
    try:
        files = [p for p in directory.glob(pattern) if p.is_file()]
    except OSError:
        return None
    return max(files, key=lambda p: p.stat().st_mtime, default=None)


def alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True
    return True


def verdict(*, running: bool, quiet_for: float | None, quota_known: bool,
            quota_left: float | None, active_agents: int,
            supported: bool = True) -> tuple[str, str]:
    """Turn the samples into one word and a sentence.

    The quota reading is what separates the two cases that matter and that the
    transcript alone cannot tell apart: a session that ended because it ran out,
    and one that ended because it broke.
    """
    exhausted = quota_known and quota_left is not None and quota_left <= 0.02

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
           budget: Any = None) -> dict:
    """One observation, as a plain dict."""
    from .tree import Tree

    supported = provider is not None and transcript_source(provider, paths.root) is not None
    transcript = newest_transcript(provider, paths.root) if provider else None
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

    state, detail = verdict(running=running, quiet_for=quiet_for,
                            quota_known=quota_known, quota_left=quota_left,
                            active_agents=active, supported=supported)
    return {
        "at": time.time(),
        "role": role,
        "pid": pid,
        "running": running,
        "verdict": state,
        "detail": detail,
        "transcript": record or None,
        "active_agents": active,
        "provider": {
            "name": getattr(provider, "name", ""),
            "known": quota_known,
            "headroom": quota_left,
            "resets_at": getattr(budget, "resets_at", None),
        },
    }


def status_file(paths) -> Path:
    return paths.data / "orchestrator-status.json"


def write_status(paths, record: dict) -> None:
    path = status_file(paths)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(record, indent=2) + "\n")
    os.replace(tmp, path)


def read_status(paths) -> dict | None:
    try:
        return json.loads(status_file(paths).read_text())
    except (OSError, ValueError):
        return None


def supervise(paths, config, role: str, pid: int, interval: float = 20.0,
              max_seconds: float = 0.0) -> int:
    """Sample until the watched process is gone, then record how it ended.

    Runs as its own process because `run` execs: there is no thread of ours
    left in the orchestrator to do this. It stops on its own when the pid it
    watches disappears, so nothing has to remember to clean it up.
    """
    from .budget import invalidate_cache, read_provider
    from .paths import global_config_dir
    from .providers import load_providers

    providers = load_providers(config.providers)
    spec = next((a for a in config.agents.values()
                 if a.launch and a.role == role), None)
    provider = providers.get(spec.provider) if spec else None
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

        record = sample(paths, config, provider, role, pid, budget)
        write_status(paths, record)
        if not record["running"]:
            return 0
        if max_seconds and time.time() - started > max_seconds:
            return 0
        time.sleep(interval)


def has_human_turn(provider: Any, cwd: Path) -> bool | None:
    """Did anyone actually say anything in this session? None if unknowable.

    The headless handover replays the session with a nudge as its user turn, so
    a session that dropped *before* anyone typed would be handed "continue where
    you left off" with nowhere to continue from — and would invent work from
    BRIEF.md, unattended, with agents that hold bypass permissions.

    A typed message is a `user` record whose content is a plain string; a tool
    result is a `user` record whose content is a list of tool_result blocks.
    Structure again, not content: this reads the shape and never the words.
    """
    path = newest_transcript(provider, cwd)
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
