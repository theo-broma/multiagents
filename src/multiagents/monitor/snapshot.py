"""Everything the monitor shows, gathered once into plain data.

Two rules hold this together:

* **Cheap by default.** :func:`snapshot` is polled every couple of seconds by
  both front ends, so it reads files and never shells out. Git and the auth
  scripts are seconds of latency each; they live in :func:`branches` and
  :func:`deep_checks`, which are asked for separately and on purpose.
* **Serialisable.** Everything returned is JSON — the web page gets it over
  HTTP, and the TUI reads the same dicts. A renderer that needed a live object
  would be a renderer the other front end could not have.
"""

from __future__ import annotations

import json
import math
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from ..budget import read_all, reset_display, reset_label, reserved_providers
from ..config import Config
from ..paths import ProjectPaths, global_config_dir
from ..providers import load_providers
from .. import notices as limit_notices
from .. import procs
from ..tree import ACTIVE, PAUSED, Tree, cost_of, token_count

# The tree already draws this line and draws it deliberately: ACTIVE is work in
# progress, PAUSED is "the process has exited but the session is resumable".
# The monitor used to invent a third set spanning both, which put parked
# conversations under RUNNING, printed "process gone" in red beside a state
# that is designed, and raised an orphan alert advising a repair for a system
# that was working. It cost the maintainer a question and me an afternoon.
LIVE = tuple(sorted(ACTIVE))


def _alive(pid: int | None, start: str = "") -> bool:
    """One source for this question: see :mod:`multiagents.procs`.

    Display, but not therefore harmless — a recycled pid shows a dead agent as
    RUNNING, which is the state this panel exists to be trusted about.
    """
    return procs.alive(pid, start)


# --------------------------------------------------------------------------
# providers


# Extras and install probes share a bounded background pool. A busy pool
# defers new work until a later poll instead of queuing unbounded jobs.
_LINE_CACHE: dict[tuple[str, str], tuple[float, str, list[str], str]] = {}
_PROBE_CACHE: dict[tuple[str, str], tuple[float, bool, str]] = {}
_BACKGROUND_PENDING: set[tuple[str, str, str]] = set()
_BACKGROUND_SLOTS = threading.BoundedSemaphore(4)
_LINE_LOCK = threading.Lock()
LINE_TTL = 30.0
PROBE_TTL = 60.0


def _background(kind: str, key: tuple[str, str], refresh: Callable[[], None],
                fresh: Callable[[], bool]) -> None:
    job = (kind, *key)
    with _LINE_LOCK:
        if fresh() or job in _BACKGROUND_PENDING:
            return
        if not _BACKGROUND_SLOTS.acquire(blocking=False):
            return
        _BACKGROUND_PENDING.add(job)

    def run() -> None:
        try:
            refresh()
        finally:
            with _LINE_LOCK:
                _BACKGROUND_PENDING.discard(job)
            _BACKGROUND_SLOTS.release()

    try:
        threading.Thread(target=run, daemon=True,
                         name=f"monitor-{kind}-{key[1]}").start()
    except Exception:
        with _LINE_LOCK:
            _BACKGROUND_PENDING.discard(job)
        _BACKGROUND_SLOTS.release()
        raise


# A script's usage line may carry its reset as the raw UTC ISO string the API
# sent (a provider script's `ms_iso` prints exactly that, and its contract
# test holds the script to it). Q6: a viewer read that UTC wall clock as local
# time and concluded a quota an hour from resetting had already failed to come
# back. The display layer rewrites any ISO token through reset_display, so
# every provider script gets countdown + local clock without growing a
# formatter of its own — and a script that prints a reset in its own words is
# left exactly as it wrote it.
ISO_STAMP = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?")


def _readable_resets(line: str) -> str:
    return ISO_STAMP.sub(lambda m: reset_display(m.group()) or m.group(), line)


def _usage_lines(name: str, provider: Any, executor: Any, budget: dict,
                 paths: ProjectPaths) -> tuple[list[str], str]:
    """Return cached extras immediately, refreshing them in the background."""
    from .. import scripts

    payload = json.dumps(budget, sort_keys=True, default=str)
    key = (str(paths.config), name)

    def refresh() -> None:
        try:
            code, out, err = scripts.run_action(
                name, provider, executor, "usage", global_config_dir(), paths.config,
                timeout=10, extra_env={"MULTIAGENTS_BUDGET": payload})
            if code == 0:
                lines = [_readable_resets(line.rstrip())
                         for line in out.strip().splitlines()[:12]]
            elif code == 64:
                lines = []
            else:
                detail = " ".join((err or out or "").split())
                lines = [f"usage failed ({code}): {detail}"[:120]]
        except Exception as exc:
            lines = [("usage failed: " + " ".join(str(exc).split()))[:120]]
        finally:
            with _LINE_LOCK:
                _LINE_CACHE[key] = (time.time(), payload, lines, "script")

    def fresh() -> bool:
        cached = _LINE_CACHE.get(key)
        return bool(cached and time.time() - cached[0] < LINE_TTL and cached[1] == payload)

    _background("usage", key, refresh, fresh)
    with _LINE_LOCK:
        cached = _LINE_CACHE.get(key)
        return (cached[2], cached[3]) if cached else ([], "built-in")


def _percent(detail: dict) -> float | None:
    for field in ("percent", "used_percent", "headroom"):
        value = detail.get(field)
        if (isinstance(value, (int, float)) and not isinstance(value, bool)
                and math.isfinite(value)):
            return (1 - value) * 100 if field == "headroom" else value
    return None


def _reset_order(stamp: Any) -> float:
    try:
        return datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError, OverflowError):
        return math.inf


def _generic_usage(budget: dict) -> list[str]:
    """Every structured window in the same layout, followed by local notes."""
    windows = {name: detail for name, detail in (budget.get("windows") or {}).items()
               if isinstance(detail, dict)}
    if not windows and budget.get("known") and budget.get("used_percent") is not None:
        windows = {"overall": {"percent": budget["used_percent"],
                               "resets_at": budget.get("resets_at")}}
    candidates = [name for name, detail in windows.items()
                  if detail.get("counted", True) and _percent(detail) is not None]
    tightest = min(candidates, key=lambda name: (
        -_percent(windows[name]), _reset_order(windows[name].get("resets_at")), name
    )) if candidates else None
    lines = []
    for name, detail in windows.items():
        used = _percent(detail)
        if used is None:
            usage = "—"
        else:
            filled = min(10, max(0, int(round(used / 10))))
            usage = f"{'█' * filled}{'░' * (10 - filled)}  {used:.0f}% used"
        marks = " · constraining" if name == tightest else ""
        if not detail.get("counted", True):
            marks += " · not counted"
        if detail.get("account"):
            marks += f" · account {detail['account']}"
        reset = reset_label(detail.get("resets_at")) or "—"
        lines.append(f"{name:<13} {usage}  resets {reset}{marks}")
    if not windows:
        lines.append(budget.get("note") or "no quota surface; spend-only")
    spent = budget.get("spent") or {}
    if spent.get("cost_usd"):
        lines.append(f"spent ${spent['cost_usd']:.2f} here")
    if budget.get("cooldown_remaining"):
        lines.append(f"cooling down {budget['cooldown_remaining'] // 60}m")
    return lines


def _installation(name: str, provider: Any, executor: Any,
                  paths: ProjectPaths) -> tuple[bool, str]:
    if getattr(executor, "kind", "local") != "docker":
        available = bool(getattr(provider, "available", lambda: None)())
        return available, "installed" if available else "not installed"
    from .. import manifest

    key = (str(paths.config), name)

    def refresh() -> None:
        try:
            state = manifest.probe(name, paths, context="docker").state
        except Exception:
            state = "probe_failed"
        label = {"missing": "not installed", "no manifest": "unverified (no manifest)",
                 "container not running": "container not running",
                 "verified": "installed", "unverified": "installed",
                 "overridden": "installed"}.get(state, "probe failed")
        with _LINE_LOCK:
            _PROBE_CACHE[key] = (time.monotonic(), state != "missing", label)

    def fresh() -> bool:
        cached = _PROBE_CACHE.get(key)
        return bool(cached and time.monotonic() - cached[0] < PROBE_TTL)

    _background("probe", key, refresh, fresh)
    with _LINE_LOCK:
        cached = _PROBE_CACHE.get(key)
        return (cached[1], cached[2]) if cached else (True, "checking…")


# Quota refreshes have their own single flight, independent of slow extras
# and identity probes. No provider I/O runs while this lock is held.
_QUOTA_LOCK = threading.Lock()
_QUOTA_CACHE: dict[str, dict] = {}


def _quota_readings(paths, config, providers, executor_of):
    from .. import budget as budget_module

    key = str(paths.config)
    signature = repr((config.providers, config.project, tuple(providers)))
    now = time.monotonic()
    with _QUOTA_LOCK:
        state = _QUOTA_CACHE.get(key)
        if state is not None and state["signature"] != signature and state["pending"]:
            # Finish the old config before starting another flight. Its
            # account reading must not be shown under the new config.
            return {}, None, False
        if state is None or state["signature"] != signature:
            state = {"signature": signature, "readings": {}, "good": None,
                     "attempt": None, "pending": False, "failed": False}
            _QUOTA_CACHE[key] = state
        due = state["attempt"] is None or now - state["attempt"] >= budget_module._CACHE_TTL
        start = due and not state["pending"]
        if start:
            state["pending"] = True
            state["attempt"] = now

    def refresh():
        try:
            readings = read_all(providers, executor_of, global_config_dir(),
                                paths.config)
        except Exception:
            with _QUOTA_LOCK:
                state["failed"] = True
        else:
            with _QUOTA_LOCK:
                state["readings"] = readings
                state["good"] = time.monotonic()
                state["failed"] = False
        finally:
            with _QUOTA_LOCK:
                state["pending"] = False

    if start:
        try:
            threading.Thread(target=refresh, daemon=True,
                             name="monitor-quota").start()
        except Exception:
            with _QUOTA_LOCK:
                state["pending"] = False
                state["failed"] = True
    with _QUOTA_LOCK:
        age = max(0.0, time.monotonic() - state["good"]) if state["good"] is not None else None
        return dict(state["readings"]), age, state["failed"]


def providers_view(paths: ProjectPaths, config: Config, tree: Tree,
                   with_scripts: bool = True) -> list[dict]:
    """One entry per configured provider: quota, health, and how to show it."""
    from ..executor import executor_for

    providers = load_providers(config.providers)
    executor_of = executor_for(paths, config, providers)
    reserve = float(config.project.get("budget", {}).get("reserve_headroom", 0.15))
    orchestrator = next((spec.provider for spec in config.agents.values()
                         if spec.launch and spec.role == "orchestrator"), "")
    reserved = reserved_providers(config.project, providers, orchestrator)
    health = tree.provider_health()
    spend = spend_by_provider(tree)
    cooldowns = tree.read().get("cooldowns") or {}
    budgets, age, failed = _quota_readings(paths, config, providers, executor_of)

    out = []
    for name, provider in sorted(providers.items()):
        if not getattr(provider, "enabled", True):
            continue
        budget = budgets.get(name)
        data = budget.to_dict() if budget else {
            "provider": name, "known": False, "note": "loading…"}
        # Spend and cooldowns belong to this poll, not the last quota read.
        if budget:
            data["spent"] = {**data.get("spent", {}), **spend.get(name, {})}
        cooldown = cooldowns.get(name)
        if cooldown and cooldown.get("until", 0) > time.time():
            data["cooldown_until"] = cooldown["until"]
            data["severity"] = "critical"
            data["note"] = (data.get("note", "") + " | " if data.get("note") else "") + cooldown.get("reason", "cooling down")
        lines, source = (_generic_usage(data), "built-in")
        if failed and age is not None:
            lines.append(f"stale · last reading {age:.0f}s ago")
        executor = executor_of(name)
        available, install_status = _installation(name, provider, executor, paths)
        if with_scripts:
            extras, source = _usage_lines(name, provider, executor, data, paths)
            note = data.get("note") or ""
            lines += [line for line in extras if line not in lines
                      and not (note in lines and note.startswith(line))]
        entry = health.get(name) or {}
        # Below the reserve a provider is still "usable" and still gets skipped
        # by choose_provider, which is the least obvious state it can be in and
        # the one that quietly moves every agent onto a fallback model.
        headroom = data.get("headroom")
        below_reserve = bool(name in reserved and data.get("known")
                             and headroom is not None and headroom < reserve
                             and data.get("usable"))
        out.append({
            "name": name,
            "billing": getattr(provider, "billing", "metered"),
            "below_reserve": below_reserve,
            "reserve": reserve,
            "available": available,
            "install_status": install_status,
            "budget": data,
            "lines": lines,
            "lines_from": source,
            "consecutive_failures": entry.get("consecutive_failures", 0),
            "last_reason": entry.get("last_reason", ""),
            "spent": spend.get(name, {}),
            "agents": sorted(spec.name for spec in config.agents.values()
                             if spec.provider == name),
        })
    return out


# --------------------------------------------------------------------------
# agents


def _node_view(node: dict, now: float, key: str | None = None) -> dict:
    usage = node.get("usage") or {}
    started = node.get("started_at") or node.get("created_at") or 0
    ended = node.get("ended_at")
    alive = _alive(node.get("pid")) if node.get("status") in LIVE else False
    # How long it RAN, which is not how long the node existed. Two ways that
    # comes apart: an agent left marked running by a server that died counts up
    # forever ("running 95h", said confidently), and a finished agent's node
    # ends when the parent MERGES it, which can be hours later — one overnight
    # run showed 277 minutes for an agent that worked for five and then waited
    # for somebody to wake up. The last thing it said is the last thing that
    # happened, either way.
    spoke = node.get("last_event_at")
    if ended and spoke and spoke < ended:
        until = spoke
    else:
        until = ended or (spoke if not alive else None) or now
    return {
        "id": key if key is not None else node.get("id"),   # HA-R9: the key
        "agent": node.get("agent"),
        "status": node.get("status"),
        "provider": node.get("provider"),
        "model": node.get("model"),
        "parent": node.get("parent"),
        "children": list(node.get("children") or []),
        "depth": node.get("depth", 0),
        "branch": node.get("branch", ""),
        "task": (node.get("task") or "")[:400],
        "summary": (node.get("summary") or "")[:2000],
        "reason": node.get("reason", ""),
        "routed_from": node.get("routed_from", ""),
        "routed_why": node.get("routed_why", ""),
        "steps": node.get("steps", 0),
        "events": node.get("events", 0),
        # Every provider names these differently and one of them names them not
        # at all; see tree.token_count.
        "tokens": token_count(usage),
        "cost_usd": cost_of(usage),
        "usage": usage,
        "started_at": started,
        "ended_at": ended,
        # When the parent picked it up, if that was later than the work.
        "settled_at": ended if (ended and spoke and spoke < ended) else None,
        "elapsed": max(0.0, until - started) if started else 0.0,
        "quiet_for": max(0.0, now - node["last_event_at"])
        if node.get("last_event_at") else None,
        "alive": alive,
        "stale": bool(node.get("status") in ACTIVE and not alive),
        # A standing conversation: no process, a resumable session, and the
        # orchestrator's way of asking the same advisor a second question.
        "parked": bool(node.get("status") in PAUSED and node.get("session_id")),
        "until": node.get("until"),
        "last_spoke": node.get("last_event_at") if node.get("last_event_at") is not None else node.get("last_spoke"),
        "has_transcript": True,
    }


def agent_tree(nodes: dict, now: float) -> list[dict]:
    """The forest, parents holding their children.

    Built from `parent` rather than from each node's `children` list, so an
    interrupted write that left one of them half-updated still produces a tree
    with every node in it exactly once.
    """
    views = {nid: _node_view(node, now, nid) for nid, node in nodes.items()}
    for view in views.values():
        view["kids"] = []
    roots = []
    for nid, view in views.items():
        parent = views.get(view["parent"])
        (parent["kids"] if parent else roots).append(view)
    order = lambda v: v["started_at"] or 0          # noqa: E731
    for view in views.values():
        view["kids"].sort(key=order)
    root_order = lambda v: (                        # noqa: E731
        v.get("last_spoke")
        or v.get("until")
        or v.get("ended_at")
        or v.get("started_at")
        or 0
    )
    roots.sort(key=root_order, reverse=True)
    return roots


# --------------------------------------------------------------------------
# totals


def spend_by_provider(tree: Tree) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for node in tree.read().get("nodes", {}).values():
        usage = node.get("usage") or {}
        bucket = out.setdefault(node.get("provider") or "?", {})
        bucket["total"] = bucket.get("total", 0) + token_count(usage)
        if cost_of(usage):
            bucket["cost_usd"] = round(bucket.get("cost_usd", 0) + cost_of(usage), 4)
    return out


def totals(nodes: dict, plan: set[str] | frozenset[str] = frozenset()) -> dict:
    """Rolled up three ways, because "what did last night cost" is three
    different questions depending on what you are about to change.

    A `by_model` row of a provider in `plan` carries `"billing": "plan"`
    (CX-C5): its dollars are not a bill, and the row says so."""
    by_agent: dict[str, dict] = {}
    by_day: dict[str, dict] = {}
    by_model: dict[str, dict] = {}
    grand = {"tokens": 0, "cost_usd": 0.0, "runs": 0}

    for node in nodes.values():
        usage = node.get("usage") or {}
        tokens, cost = token_count(usage), cost_of(usage)
        stamp = node.get("started_at") or node.get("created_at") or 0
        day = time.strftime("%Y-%m-%d", time.localtime(stamp)) if stamp else "?"
        for key, bucket in ((node.get("agent") or "?", by_agent),
                            (day, by_day),
                            (f"{node.get('provider')}/{node.get('model')}", by_model)):
            row = bucket.setdefault(key, {"tokens": 0, "cost_usd": 0.0, "runs": 0})
            if bucket is by_model and node.get("provider") in plan:
                row["billing"] = "plan"
            row["tokens"] += tokens
            row["cost_usd"] = round(row["cost_usd"] + cost, 4)
            row["runs"] += 1
        grand["tokens"] += tokens
        grand["cost_usd"] = round(grand["cost_usd"] + cost, 4)
        grand["runs"] += 1

    return {"grand": grand, "by_agent": by_agent, "by_model": by_model,
            "by_day": dict(sorted(by_day.items(), reverse=True)[:30])}


# --------------------------------------------------------------------------
# alerts — the point of the whole thing


def alerts(paths: ProjectPaths, config: Config, tree: Tree,
           providers: list[dict]) -> list[dict]:
    """What is wrong right now, worst first.

    This exists because of a specific morning: an orchestrator stopped by its
    provider sat for three hours while the only status anyone could see said
    "idle — probably waiting for you". Anything that can cost a morning belongs
    here, and nothing that cannot belongs here at all — a banner that cries
    wolf gets scrolled past exactly as fast as no banner.
    """
    from .. import watchdog

    out = []
    # Both drivers, named. `run` launches the orchestrator and `init-agent` the
    # initializer; reading only the first meant that while init-agent was
    # running, its state was reported as the orchestrator's.
    for role, status in (watchdog.read_all_status(paths) or {}).items():
        verdict = status.get("verdict")
        age = time.time() - float(status.get("at") or 0)
        if verdict in ("limited", "out_of_quota", "stalled"):
            out.append({"level": "error", "kind": "driver",
                        "text": f"{role} {verdict}: {status.get('detail', '')}",
                        "detail": (status.get("limit") or {}).get("said", "")})
        elif verdict == "dropped":
            out.append({"level": "warn", "kind": "driver",
                        "text": f"{role} {status.get('detail', 'ended')}"})
        elif verdict and age > 300 and status.get("running"):
            out.append({"level": "warn", "kind": "driver",
                        "text": f"no {role} report for {age / 60:.0f}m"})
        if status.get("misfiled_in"):
            out.append({"level": "info", "kind": "driver",
                        "text": f"a supervisor from before roles had separate "
                                f"status files is still reporting the {role} "
                                f"into the {status['misfiled_in']}'s file",
                        "detail": "it stops when that process ends; two writers "
                                  "on one file would alternate until then"})

    pause = tree.pause_state()
    if pause:
        left = max(0, pause.get("until", 0) - time.time())
        out.append({"level": "warn", "kind": "pause",
                    "text": f"paused: {pause.get('reason', '')}",
                    "detail": f"{left / 60:.0f} min left"
                    f"{' · ' + ', '.join(pause['providers']) if pause.get('providers') else ''}"})

    for entry in providers:
        budget = entry["budget"]
        failures = entry["consecutive_failures"]
        if failures >= 3:
            out.append({"level": "error", "kind": "provider",
                        "text": f"{entry['name']}: {failures} runs failed in a row",
                        "detail": entry["last_reason"]})
        elif budget.get("known") and not budget.get("usable"):
            out.append({"level": "error", "kind": "provider",
                        "text": f"{entry['name']} has no headroom",
                        "detail": f"resets {reset_label(budget.get('resets_at')) or '?'}"})
        elif entry.get("below_reserve"):
            # The state that sent a question to the maintainer: one provider's
            # short window was empty while its long one still had room, and
            # every implementer was silently running on the fallback.
            out.append({"level": "warn", "kind": "provider",
                        "text": f"{entry['name']} is below the "
                                f"{entry.get('reserve', 0.15) * 100:.0f}% reserve — "
                                f"agents are being routed to a fallback",
                        "detail": f"{budget.get('used_percent', 0):.0f}% of its "
                                  f"tightest window used"})
        elif budget.get("severity") == "warning":
            out.append({"level": "warn", "kind": "provider",
                        "text": f"{entry['name']} at "
                                f"{budget.get('used_percent', 0):.0f}% of its window"})
        if not entry["available"]:
            out.append({"level": "warn", "kind": "provider",
                        "text": f"{entry['name']} is configured but not installed"})

    data = tree.read()
    open_questions = [q for q in data.get("questions", []) if not q.get("answered_at")]
    if open_questions:
        out.append({"level": "warn", "kind": "question",
                    "text": f"{len(open_questions)} agent(s) waiting on an answer",
                    "detail": open_questions[0].get("question", "")[:160]})
    deferred = data.get("deferred") or []
    if deferred:
        out.append({"level": "info", "kind": "deferred",
                    "text": f"{len(deferred)} task(s) deferred, waiting to retry"})

    for node in data.get("nodes", {}).values():
        # ACTIVE only. A PAUSED conversation whose process has exited is the
        # designed state, not an orphan, and telling somebody to run
        # `multiagents resume` over it is advice that would reopen the
        # orchestrator's own session for no reason.
        if node.get("status") in ACTIVE and node.get("pid") and not _alive(node["pid"]):
            out.append({"level": "error", "kind": "orphan",
                        "text": f"{node['id']} ({node['agent']}) is marked "
                                f"{node['status']} but its process is gone",
                        "detail": "`multiagents resume` reconciles this"})

    # LN-C3: a limit that is constraining work right now, until it clears.
    for notice in limit_notices.active(data):
        count = int(notice.get("count", 1))
        out.append({"level": "warn", "kind": "limit",
                    "text": notice.get("message", ""),
                    "detail": f"{count} time(s) since "
                              f"{time.strftime('%H:%M:%S', time.localtime(notice.get('first_hit') or 0))}"})

    rank = {"error": 0, "warn": 1, "info": 2}
    out.sort(key=lambda a: rank.get(a["level"], 3))
    return out


# --------------------------------------------------------------------------


def snapshot(paths: ProjectPaths, config: Config,
             with_scripts: bool = True) -> dict:
    """One poll: everything cheap enough to read every two seconds."""
    from .. import watchdog

    tree = Tree(paths.tree_file, paths.events_file)
    data = tree.read()
    nodes = data.get("nodes", {})
    now = time.time()

    provider_rows = providers_view(paths, config, tree, with_scripts=with_scripts)
    roots = agent_tree(nodes, now)
    running = [_node_view(n, now, k) for k, n in nodes.items()
               if n.get("status") in ACTIVE]


    running.sort(key=lambda v: v["started_at"] or 0)
    import shutil
    has_tmux = shutil.which("tmux") is not None
    for r in running:
        r["actions"] = r.get("actions", [])
        if has_tmux:
            r["actions"].append("tmux_open")
    # Shown apart, and shown at all: it is state the orchestrator will act on,
    # and invisible state is how the last several surprises happened.
    parked = [_node_view(n, now, k) for k, n in nodes.items()
              if n.get("status") in PAUSED and n.get("session_id")]
    parked.sort(key=lambda v: -(v["last_spoke"] or 0))

    drivers = watchdog.read_all_status(paths) or {}
    status = drivers.get("orchestrator", {})
    # NC-R43: the plan scheduler, only while the gate is on; absent otherwise.
    scheduler = {}
    if (config.project.get("scheduler") or {}).get("enabled"):
        from ..scheduler import overview
        scheduler = {"scheduler": overview.section(paths.root, config)}
    return {
        "at": now,
        "project": {
            "root": str(paths.root),
            "name": paths.root.name,
            "executor": config.executor,
            "branch_prefix": config.branch_prefix,
        },
        # Every role that drives this project, each under its own name. The
        # `orchestrator` key stays for anything that only knows about that one.
        "drivers": [
            {"role": role,
             "verdict": record.get("verdict"),
             "detail": record.get("detail", ""),
             "running": bool(record.get("running")),
             "observed_ago": round(now - float(record.get("at") or now)),
             "active_agents": record.get("active_agents", 0),
             "limit": record.get("limit")}
            for role, record in drivers.items()
        ],
        "orchestrator": {
            "verdict": status.get("verdict"),
            "detail": status.get("detail", ""),
            "running": bool(status.get("running")),
            "observed_ago": round(now - float(status.get("at") or now)),
            "limit": status.get("limit"),
        },
        "alerts": alerts(paths, config, tree, provider_rows),
        "providers": provider_rows,
        "running": running,
        "conversations": parked,
        "history": roots,
        "totals": totals(nodes, {name for name, p in load_providers(config.providers).items()
                                 if p.billing == "plan"}),
        "tickets": data.get("tickets", []),
        "questions": data.get("questions", []),
        "deferred": data.get("deferred", []),
        "pause": tree.pause_state(),
        # LN-C3: active notices, and the last ones that cleared.
        "limit_notices": {"active": limit_notices.active(data),
                          "cleared": limit_notices.recently_cleared(data)},
        "counts": {
            "nodes": len(nodes),
            "running": len(running),
            "conversations": len(parked),
            "open_tickets": sum(1 for t in data.get("tickets", [])
                                if t.get("status") == "open"),
            "open_questions": sum(1 for q in data.get("questions", [])
                                  if not q.get("answered_at")),
        },
        **scheduler,
    }


# --------------------------------------------------------------------------
# the expensive views, asked for separately


def branches(paths: ProjectPaths, config: Config) -> list[dict]:
    """Every agent branch, and whether its work is still only on it.

    Git calls, so this is not in the poll. It answers the question you actually
    have in front of an agent's output — is this still mine to keep or throw
    away, and how much of it is there.
    """
    from .. import gitops

    base = config.base_branch or gitops.current_branch(paths.root) or "main"
    tree = Tree(paths.tree_file, paths.events_file)
    result = gitops.run(paths.root, "branch", "--list",
                        f"{config.branch_prefix}/*", "--format=%(refname:short)")
    names = [line.strip() for line in (result.out or "").splitlines() if line.strip()]

    merged = gitops.run(paths.root, "branch", "--merged", base,
                        "--format=%(refname:short)")
    merged_set = {line.strip() for line in (merged.out or "").splitlines()}

    by_branch = {n.get("branch"): (k, n) for k, n in tree.read().get("nodes", {}).items()
                 if n.get("branch")}
    out = []
    for name in sorted(names):
        agent_id, node = by_branch.get(name) or (None, {})
        out.append({
            "branch": name,
            "agent_id": agent_id,
            "agent": node.get("agent"),
            "status": node.get("status"),
            "merged": name in merged_set,
            "commits": gitops.commits_on(paths.root, name, base, root=paths.root),
            "diffstat": gitops.diff_stat(paths.root, name, base,
                                         root=paths.root).strip()[-200:],
        })
    return out


def deep_checks(paths: ProjectPaths, config: Config) -> list[dict]:
    """Authentication and executor readiness: seconds, so asked for by hand."""
    from .. import auth
    from ..cli import _executor_problems
    from ..executor import executor_for

    out = []
    for problem in _executor_problems(paths, config):
        out.append({"level": "error", "kind": "executor", "text": problem})
    if config.executor == "docker":
        try:
            from ..cli import _docker_executor
            for line in _docker_executor(paths).mount_drift():
                out.append({
                    "level": "warn", "kind": "container",
                    "text": "the running container predates the current "
                            "configuration",
                    "detail": f"{line} — mounts are fixed at creation; "
                              f"`multiagents docker rm && ... up` replaces it"})
            for entry in _docker_executor(paths).credential_drift():
                out.append({
                    "level": "error", "kind": "credentials",
                    "text": f"the container is reading an old "
                            f"{Path(entry['path']).name} — agents there will "
                            f"fail as if the credential were revoked",
                    "detail": "`multiagents docker down && up`, or `run`, "
                              "which repairs it when nothing is busy"})
        except Exception:
            pass
    try:
        providers = load_providers(config.providers)
        states = auth.check_all(providers, executor_for(paths, config, providers),
                                global_config_dir(), paths.config)
        for name, state in states.items():
            if not state.ok:
                out.append({"level": "warn", "kind": "auth",
                            "text": f"{name}: {state.detail or state.status}",
                            "detail": state.fix})
    except Exception as exc:                # a check must never be the failure
        out.append({"level": "info", "kind": "auth",
                    "text": f"could not check authentication: {type(exc).__name__}"})
    return out


# --------------------------------------------------------------------------
# transcripts


def tail_lines(path: Path, limit: int, chunk: int = 262144) -> list[str]:
    """The last `limit` lines, read backwards from the end.

    `read_text().splitlines()[-limit:]` is the obvious version and it loads the
    whole file to throw nearly all of it away. These files are agent streams:
    this project has seen an 11 MB one, and the button that opens it is the one
    you press when something has already gone wrong.
    """
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            end = handle.tell()
            data = b""
            while end > 0 and data.count(b"\n") <= limit:
                step = min(chunk, end)
                end -= step
                handle.seek(end)
                data = handle.read(step) + data
    except OSError:
        return []
    return data.decode(errors="replace").splitlines()[-limit:]


def transcript(paths: ProjectPaths, agent_id: str, limit: int = 400) -> dict:
    """One agent's run, as text.

    Reads the stream we recorded rather than the provider's own log: it is the
    same conversation, it is already normalised across providers, and it is the
    only one that exists for an agent whose CLI keeps no transcript at all.
    """
    from ..redact import scrub

    run_dir = paths.run_dir(agent_id)
    stream = run_dir / "stream.jsonl"
    entries: list[dict] = []
    if stream.is_file():
        for line in tail_lines(stream, limit):
            try:
                event = json.loads(line)
            except ValueError:
                continue
            kind = event.get("kind") or event.get("type") or "raw"
            text = event.get("text") or event.get("content") or ""
            if not isinstance(text, str):
                text = json.dumps(text)[:2000]
            entries.append({
                "kind": kind,
                "tool": event.get("tool") or event.get("name") or "",
                "text": scrub(text)[:4000],
                "at": event.get("t") or event.get("at"),
            })

    prompt = run_dir / "prompt.md"
    result = run_dir / "result.json"
    return {
        "id": agent_id,
        "prompt": scrub(prompt.read_text(errors="replace")[:20000])
        if prompt.is_file() else "",
        "result": json.loads(result.read_text()) if result.is_file() else None,
        "entries": entries,
        "truncated": stream.is_file() and len(entries) >= limit,
    }


def events(paths: ProjectPaths, limit: int = 200) -> list[dict]:
    """The tail of the global event log — what happened, in order."""
    path = paths.events_file
    if not path.is_file():
        return []
    out = []
    for line in tail_lines(path, limit):
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out
