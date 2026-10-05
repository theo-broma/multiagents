"""MCP server: delegate work to other agent CLIs as supervised subagents.

Registered once, globally, for every agent. Permission to spawn is enforced
*here* — from the ``MULTIAGENTS_CAN_SPAWN`` and ``MULTIAGENTS_DEPTH`` variables
the parent injected — rather than by giving different agents different MCP
configs. That matters because ``agy mcp add`` writes to a global profile and has
no per-agent scoping to give.

Every tool result is scrubbed for credentials on the way out.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import functools
import inspect
import os
import signal
import stat
import sys
import threading
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import Field
import yaml

# The SDK renamed FastMCP to MCPServer in 2.x. The decorator API is identical,
# so support both rather than pinning to one line of the SDK.
try:
    from mcp.server.mcpserver import Context as _ToolContext, MCPServer as _Server
    from mcp.server.mcpserver.exceptions import ToolError
except ImportError:  # pragma: no cover - mcp 1.x
    from mcp.server.fastmcp import Context as _ToolContext, FastMCP as _Server
    from mcp.server.fastmcp.exceptions import ToolError

from . import __version__
from . import auth as auth_mod
from . import budget as budget_mod
from . import bugs
from . import catalog as catalog_mod
from . import findings as findings_mod
from . import gitops
from . import safepoint
from .config import CONFIG_FILES
from .config import load as load_config
from .config import limit_number, seed_project
from .drift import drift_summary
from .executor import executor_for
from .models import refresh_models
from .paths import ProjectPaths, find_project_root, global_config_dir
from .providers import billed_rows
from .quota_handover import quota_handover_settings
from .redact import scrub
from .runner import Runner
from .tree import Tree, deferred_malformed, find_deferred, now
from .transcripts import session_context

mcp = _Server("multiagents", version=__version__)


def _tool_failed(name: str, exc: BaseException) -> str:
    """Record a tool that raised, and say what raised.

    The SDK answers an exception it did not expect with a bare `Error
    executing tool <name>` and logs the traceback to the server's stderr —
    which, for a subagent's server, is the agent CLI's, and read by nobody.
    ag-c65ee1's consult failed exactly so, and left no trace of why. So the
    type and message go back to the caller and into the event log, and a
    subagent's traceback is written under its own run directory. Never
    raises: this runs when something is already broken, possibly the runner
    itself, so it does not use one.
    """
    error = scrub(f"{type(exc).__name__}: {exc}")
    trace = scrub("".join(traceback.format_exception(exc)))
    agent_id = os.environ.get("MULTIAGENTS_AGENT_ID") or ""
    explicit = os.environ.get("MULTIAGENTS_PROJECT")
    paths = (_runner.paths if _runner is not None
             else ProjectPaths(Path(explicit).expanduser()) if explicit else None)
    where = None
    if paths is not None and agent_id:
        where = paths.run_dir(agent_id) / "server-errors.log"
        try:
            where.parent.mkdir(parents=True, exist_ok=True)
            with where.open("a") as handle:
                handle.write(f"--- {now():.3f} tool {name}\n{trace}\n")
        except OSError:
            where = None
    if paths is not None:
        try:
            Tree(paths.tree_file, paths.events_file).emit(
                agent_id, "tool_error", tool=name, error=error,
                traceback=str(where) if where else None)
        except OSError:
            pass
    print(f"multiagents: tool {name} raised\n{trace}", file=sys.stderr, flush=True)
    return f"{error} (traceback: {where})" if where else error


def _reported(fn):
    """`fn` as registered: an unexpected exception becomes a ToolError that
    names it (`_tool_failed`). The SDK's own errors pass through untouched."""
    def fail(exc: Exception):
        if isinstance(exc, ToolError):
            raise exc
        raise ToolError(_tool_failed(fn.__name__, exc)) from exc

    if inspect.iscoroutinefunction(fn):
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            try:
                return await fn(*args, **kwargs)
            except Exception as exc:
                fail(exc)
    else:
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except Exception as exc:
                fail(exc)
    return wrapper


def _tool():
    """`mcp.tool()`, registering the reporting wrapper. The function itself is
    returned unchanged, so calling a tool directly still raises what it raises."""
    register = mcp.tool()

    def decorate(fn):
        permissions = os.environ.get("MULTIAGENTS_NODE_PERMISSIONS")
        allowed = None
        if permissions is not None:
            granted = set(permissions.split(","))
            allowed = {"get_node", "list_nodes", "wait_for_nodes", "scheduler_status", "list_templates"}
            if "delegate" in granted:
                allowed |= {"create_node", "update_node", "cancel_node", "instantiate_template",
                            "start_agent", "consult", "answer_question", "steer_agent", "stop_agent",
                            "wait_for_agents", "check_agent", "collect_agent", "list_agents"}
            if "verdict" in granted:
                allowed.add("give_verdict")
        if allowed is None or fn.__name__ in allowed:
            register(_reported(fn))
        return fn
    return decorate

_runner: Runner | None = None
# CW-R2: the safe-point gate, the server's before any Runner exists, so a
# request seen at startup closes admission for the Runner built later.
_gate = safepoint.Gate()

# The config in use, as a fingerprint of the files it came from, and the last
# fingerprint that failed to load. Kept apart so a broken file is parsed once
# per edit rather than once per tool call, while every call still hears about it.
_loaded: dict[str, tuple[int, int] | None] = {}
_failed: dict[str, tuple[int, int] | None] | None = None
_load_error: str = ""
# Held across detect-and-reload, so concurrent calls that see the same change
# produce one reload, one event and one announcement between them.
_lock = threading.RLock()

# What the current tool call must say about config: set by `runner()` when it
# reloads or while a load error stands, read and cleared by `_ok`. A context
# variable so that concurrent async calls each hear about their own reload.
_notice: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "multiagents_config_notice", default=None)

# Whether the wind-down notice has been given for the current crossing of the
# threshold. Re-armed only by a reading below it; a restart forgets it, which
# is the one repeat allowed.
_wind_down_given = False

CONTEXT_WIND_DOWN = (
    "Your own context window is nearly full ({tokens} tokens; the wind-down "
    "threshold is {threshold}). You are being told now, while there is room to "
    "do it well, because what is only in this conversation does not survive "
    "it being compacted or restarted.\n\n"
    "Do exactly this:\n"
    "1. Finish the merge or decision already in hand — do not open another.\n"
    "2. Record every status and judgement in its durable home: finding "
    "statuses (set_finding_status), tickets, decisions in the spec under "
    "context/, and which agents you are waiting on and why.\n"
    "3. Write the handoff into BRIEF.md — what is done, what is in flight, "
    "what you meant to do next and why.\n"
    "4. Start no new agent.\n\n"
    "The work resumes from BRIEF.md, the ledger, the tickets and the tree, not "
    "from your memory of this conversation."
)


def _layer_files(run: Runner) -> list[Path]:
    """The YAML files of every config layer, present or not."""
    layers = [d.parent for d in run.config.instruction_dirs]
    return [layer / name for layer in layers for name in CONFIG_FILES]


def _config_files(run: Runner) -> list[Path]:
    """Every file the config in use was, or could have been, read from.

    Candidates that do not exist are included on purpose: creating one is a
    change. Instruction briefs count too — the roster reads them at spawn.
    """
    files = _layer_files(run)
    for layer in [d.parent for d in run.config.instruction_dirs]:
        agents = layer / "agents"
        if agents.is_dir():
            files += sorted(p for p in agents.rglob("*.md") if p.is_file())
    for spec in run.config.agents.values():
        for part in run.config.instruction_parts(spec):
            path = Path(part).expanduser()
            if path.is_absolute():
                files.append(path)
    return files


def _fingerprint(run: Runner) -> dict[str, tuple[int, int] | None]:
    """(mtime, size) per config file: a stat each, no parsing.

    None for anything that is not a readable regular file — missing, a
    directory, a broken symlink — so replacing a file with one is a change.
    """
    out: dict[str, tuple[int, int] | None] = {}
    for path in _config_files(run):
        try:
            st = path.stat()
        except OSError:
            out[str(path)] = None
            continue
        out[str(path)] = ((st.st_mtime_ns, st.st_size)
                          if stat.S_ISREG(st.st_mode) else None)
    return out


def _display(run: Runner, path: str) -> str:
    try:
        return str(Path(path).relative_to(run.paths.root))
    except ValueError:
        return path


def _refresh(run: Runner) -> None:
    """Reload the config if any of its files changed since it was loaded.

    A config that fails to load is never swapped in: the previous one stays in
    force and every call reports the error until the file is fixed. Each
    detected change, loaded or not, is recorded once in the events log — the
    project root is writable from inside the container, so an edit that takes
    effect in this session must leave a trace.
    """
    global _loaded, _failed, _load_error
    current = _fingerprint(run)
    if current == _loaded:
        # Back to the files the config in force came from: any remembered
        # failure is over, so the same breakage later is news again.
        _failed, _load_error = None, ""
        run.config_error = ""
        return
    if current == _failed:
        _notice.set({"load_error": _load_error})
        return
    changed = sorted(_display(run, p) for p in set(current) | set(_loaded)
                     if current.get(p) != _loaded.get(p))
    # A layer file that loaded last time and is now gone, a directory or a
    # dangling link would read as an empty layer — silently reverting its
    # settings to defaults. Treat it as a file that fails to load.
    vanished = [_display(run, str(p)) for p in _layer_files(run)
                if _loaded.get(str(p)) is not None and current.get(str(p)) is None]
    try:
        if vanished:
            raise FileNotFoundError(
                f"no longer a readable file: {', '.join(vanished)}")
        run.reload(load_config(run.paths, seed=_seeds()))
    except Exception as exc:
        _failed = current
        _load_error = (
            f"config failed to load after a change to {', '.join(changed)}; the "
            f"previous config stays in force until it is fixed: "
            f"{type(exc).__name__}: {exc}")
        run.config_error = _load_error
        run.tree.emit(run.self_id() or "", "config_reload", files=changed,
                      outcome="load_error", error=_load_error)
        _notice.set({"load_error": _load_error})
        return
    # Re-read after the load, since the new roster may name different briefs,
    # but keep what was seen BEFORE it for every file already known: an edit
    # landing mid-load must still look like a change on the next call.
    _loaded, _failed, _load_error = {**_fingerprint(run), **current}, None, ""
    run.tree.emit(run.self_id() or "", "config_reload", files=changed,
                  outcome="reloaded")
    _notice.set({"reloaded": changed,
                 "note": "config changed on disk and was reloaded before this "
                         "call; agents already running keep the config they "
                         "started with."})


def _seeds() -> bool:
    """Does this server write the shipped defaults into the config layers?

    Only the orchestrator's. Decided from the identity the executor pins on a
    subagent's server (`runner.server_env`), since there is no runner yet.
    """
    return not os.environ.get("MULTIAGENTS_AGENT_ID")


def runner() -> Runner:
    """Resolve the project and build the runner once; reload its config on change."""
    with _lock:
        return _runner_locked()


def _runner_locked() -> Runner:
    global _runner, _loaded, _failed, _load_error
    if _runner is not None:
        _refresh(_runner)
        return _runner

    paths = _project_paths()
    paths.ensure()
    # Seeding is the orchestrator's, done before any agent exists. A
    # subagent's server only reads: under docker it runs in the container,
    # where the machine's config directory is not mounted and the project's
    # is read-only, so seeding from here raised PermissionError on the first
    # call that built the runner (SM-R1, ag-c65ee1's consult).
    seed = _seeds()
    if seed:
        seed_project(paths)
    _runner = Runner(paths, load_config(paths, seed=seed))
    _runner.__dict__["_safe_point_gate"] = _gate
    _loaded, _failed, _load_error = _fingerprint(_runner), None, ""
    # The one drift line, at the moment the runner is built and never again
    # (CD-R4). Detection must not be able to take the server down with it.
    try:
        print(f"multiagents: {drift_summary(paths)}", file=sys.stderr, flush=True)
    except Exception:
        pass
    return _runner


def _project_paths() -> ProjectPaths:
    # A nested agent is told which project it belongs to; a top-level session
    # infers it from the working directory.
    explicit = os.environ.get("MULTIAGENTS_PROJECT")
    root = Path(explicit).expanduser() if explicit else (find_project_root() or Path.cwd())
    return ProjectPaths(root)


def _context_reading(run: Runner) -> int | None:
    """The launched role's own context size, or None when it is not one.

    Only a launched role (depth 0, with a session id) reads anything: a
    subagent's context is not the orchestrator's to sense. The provider is the
    role's roster entry and the directory the project root — not the process
    cwd, which the agent can change. The launched CLI always runs on the host,
    including when the project's agents run in Docker.
    """
    session = run.session()
    if run.self_depth() != 0 or not session:
        return None
    from .driver import _launched_spec, _own_transcripts

    spec = _launched_spec(run.config, os.environ.get("MULTIAGENTS_ROLE") or "orchestrator")
    provider = run.providers.get(spec.provider) if spec is not None else None
    if provider is None:
        return None
    return session_context(provider, run.paths.root, session, _own_transcripts())


def _limit(run: Runner, key: str) -> int:
    """A token threshold; 0 is off, and a malformed one is the shipped default."""
    return int(limit_number(run.config.limits, key, zero_ok=True))


def _context_notice(run: Runner) -> dict | None:
    """The wind-down notice, once per crossing of the threshold."""
    global _wind_down_given
    threshold = _limit(run, "context_wind_down_tokens")
    if threshold <= 0:
        return None
    tokens = _context_reading(run)
    if tokens is None:
        return None                 # no reading is not a reading below, either
    with _lock:
        if tokens < threshold:
            _wind_down_given = False
            return None
        if _wind_down_given:
            return None
        _wind_down_given = True
    run.tree.emit(run.self_id() or "", "context_wind_down", tokens=tokens,
                  threshold=threshold)
    return {"tokens": tokens, "threshold": threshold,
            "instruction": CONTEXT_WIND_DOWN.format(tokens=tokens,
                                                    threshold=threshold)}


def _ok(payload: Any, wind_down: bool = True) -> Any:
    notice = _notice.get()
    if notice is not None:
        _notice.set(None)
        if isinstance(payload, dict):
            payload = {**payload, "config_reload": notice}
    if wind_down and _runner is not None and isinstance(payload, dict):
        given = _context_notice(_runner)
        if given is not None:
            payload = {**payload, "context_wind_down": given}
    return scrub(payload)


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------


@_tool()
def list_agents() -> dict:
    """List the configured subagents you can delegate to.

    Shows each agent's provider, model, whether it writes code on its own
    branch, and whether it may spawn subagents of its own. Edit the roster in
    .multiagents/config/agents.yaml.
    """
    run = runner()
    agents = []
    launched = []
    off_team = []
    for name, spec in sorted(run.config.agents.items()):
        provider = run.providers.get(spec.provider)
        if run.config.team and not run.config.in_team(name) and not spec.launch:
            # Named rather than hidden. An orchestrator that cannot see what it
            # is missing will invent a way round the gap; one that can see it
            # can tell the user the roster is wrong, which is the useful
            # outcome. start_agent still refuses these.
            off_team.append({"name": name, "description": spec.description})
            continue
        if spec.launch:
            # The orchestrator is launched by `multiagents run`, never spawned.
            # Listing it as delegable invites trying it and getting a refusal.
            launched.append({"name": name, "provider": spec.provider,
                             "model": spec.model, "note": "launched, not spawnable"})
            continue
        agents.append({
            "name": name,
            "description": spec.description,
            "provider": spec.provider,
            "model": spec.model,
            "writes": spec.writes,
            "can_spawn": spec.can_spawn,
            "permission": spec.permission,
            "timeout": run.config.effective_limits(spec)["timeout"]["value"],
            "available": bool(provider and provider.available()),
        })
    payload = {
        "agents": agents,
        "how_to_call": (
            "Each of these has an interface contract saying what a task to it "
            "must contain. Read it with how_to_call(<name>) before you delegate "
            "to one for the first time — it costs one short call and it is the "
            "difference between a run that answers your question and one that "
            "answers a different one."
        ),
        "launched_not_spawnable": launched,
        "you_may_spawn": run.can_spawn(),
        "your_depth": run.self_depth(),
        "max_depth": run.config.limits.get("max_depth", 3),
    }
    if run.config.team:
        payload["team"] = run.config.team
        payload["team_description"] = run.config.team_spec().get("description", "")
    if off_team:
        payload["not_in_this_team"] = off_team
        payload["note"] = (
            f"These exist but are outside the {run.config.team!r} team and "
            f"start_agent will refuse them. If you need one, say so in your "
            f"reply — the roster is the user's to change, not yours to route "
            f"around."
        )
    return _ok(payload)


@_tool()
def list_models(provider: str = "") -> dict:
    """List models available to each provider CLI.

    Read from the generated models.yaml. If it is empty or stale, call
    refresh_model_list (or run `multiagents refresh-models`).
    """
    run = runner()
    models = run.config.models
    if provider:
        models = {provider: models.get(provider, [])}
    return _ok({
        "models": models,
        "counts": {k: len(v or []) for k, v in models.items()},
        "hint": "empty means models.yaml has not been generated yet — call refresh_model_list",
    })


@_tool()
def refresh_model_list() -> dict:
    """Regenerate models.yaml by asking each installed CLI what it offers.

    Run this after changing a subscription: the available models change with
    the plan, and a stale list will name models the CLI will reject.
    """
    denied = _root_only("refresh_model_list")
    if denied:
        return _ok({"error": denied})
    run = runner()
    result = refresh_models(run.providers, run.paths.config / "models.yaml",
                            config_dir=global_config_dir(), project_config=run.paths.config,
                            executor_for=executor_for(run.paths, run.config, run.providers))
    runner()                  # picks up models.yaml, keeping the tree and runs
    return _ok(result)


@_tool()
def agent_tree() -> dict:
    """Show the project's agent tree: who spawned whom, and where each stands.

    Every agent can call this to understand its own position. Also available as
    the `tree://project` resource.
    """
    run = runner()
    data = run.tree.read()
    return _ok({
        "rendered": run.tree.render(),
        "you_are": run.self_id() or "the root orchestrator",
        "nodes": len(data.get("nodes", {})),
        "active": [
            {"agent_id": n.id, "agent": n.agent, "status": n.status,
             "elapsed_seconds": round(n.turn_elapsed()),
             "node_elapsed_seconds": round(n.elapsed()), "reason": n.reason,
             # SV-R10: followed by this server across a restart of the last.
             **({"adopted": True} if n.adopted_at else {})}
            for n in run.tree.active()
        ],
        "deferred": len(data.get("deferred", [])),
        "runs": [{"agent_id": key, "home_provider": n.get("home_provider") or n.get("provider"),
                  "current_provider": n.get("provider"), "segments": n.get("segments", []),
                  **run._qh_position(run.tree.get(key))}
                 for key, n in data.get("nodes", {}).items() if not n.get("role")],
    })


# --------------------------------------------------------------------------
# Running agents
# --------------------------------------------------------------------------


def _attach_contract(run, agent: str, result: dict) -> None:
    """Add an agent's calling contract to the result of its FIRST spawn here."""
    spec = run.config.agents.get(agent)
    if spec is None:
        return
    contract = run.config.calling_contract(spec)
    if not contract:
        return
    session = run.session()
    seen = [node for node in run.tree.read()["nodes"].values()
            if node.get("agent") == agent
            and (not session or node.get("session") == session)]
    if len(seen) > 1:                       # the one just started is in there
        return
    result["calling_contract"] = contract
    result["calling_contract_note"] = (
        f"First {agent} of this session, so here is its interface contract. If "
        f"the task you just sent does not meet it, steer_agent now — it is "
        f"cheaper than the run you will otherwise read and discard."
    )


@_tool()
async def start_agent(
    agent: str,
    task: str,
    workdir: str = "",
    timeout: int = 0,
    model: str = "",
    verifies: str = "",
    budget_tag: str = "",
    budget_tokens: int = 0,
    urgent: bool = False,
    request_id: str | None = None,
) -> dict:
    """Start a subagent on a task. Returns immediately with an agent_id.

    Returning immediately is the point: call this again for the next piece of
    independent work before you wait on this one. Several agents on different
    files is the normal state of a healthy tree, not an optimisation.

    A writing agent gets its own git worktree and branch, so it cannot touch
    your working tree or another agent's work. You own that branch: merge it
    with merge_agent when you are satisfied, or discard_agent to throw it away.

    Poll with check_agent, or block efficiently with wait_for_agents.

    Args:
        agent: Name from list_agents.
        task: What to do. Be specific — the agent cannot ask you questions.
        workdir: Run outside the agent's own worktree. Refused unless the
            project sets `limits.allow_workdir_override: true` — it removes the
            branch isolation every other guarantee here rests on.
        timeout: Wall-clock seconds; 0 uses the agent's configured default.
        model: Override the configured model for this run.
        verifies: The agent_id whose work this run checks, when it is a check —
            a reviewer on an implementer's branch, a tester on what was just
            written. Recorded so that "how often did work need redoing" is a
            fact in the tree rather than a guess from branches and timing. Cheap
            to pass and impossible to reconstruct later.
        budget_tag: A named slice of work to spend against — a bounded context
            under review, a feature, whatever you are budgeting. Spend is summed
            across every run carrying the tag.
        budget_tokens: The ceiling for that tag, in tokens. Set once, on the
            FIRST spawn that names the tag; later values are ignored, because a
            ceiling the spender can raise is a suggestion and the agent asking
            to raise it is the one that has just run out. When a tag is spent,
            start_agent refuses: decide what that slice does NOT get, say what
            you covered and what you did not, and move on.
    """
    from . import scheduler
    paths = _project_paths()
    # An established runner retains its last valid configuration when a reload
    # fails. Preserve that behaviour and its limits with the gate off.
    run = runner() if _runner is not None else None
    try:
        gate = scheduler.enabled(paths.root)
    except (yaml.YAMLError, OSError):
        if run is None:
            raise
        # A broken project YAML retains the known configuration, as before.
        # A valid gate still takes effect if a separate provider reload fails.
        gate = run.config.project.get("scheduler", {}).get("enabled", False)
    if gate:
        # Authenticate even an empty run label before touching host authority.
        status = _node_rpc("scheduler_status", {})
        if status.get("error"):
            return status
        if status.get("launch_context") is False:
            return {"error": "not_implemented"}
        from .scheduler import submit
        return await asyncio.to_thread(submit, paths.root, agent, task,
                                       caller=os.environ.get("MULTIAGENTS_AGENT_ID") or None,
                                       request_id=request_id, urgent=urgent,
                                       workdir=workdir or None, timeout=timeout,
                                       model=model or None, verifies=verifies,
                                       budget_tag=budget_tag, budget_tokens=budget_tokens)
    run = run or runner()
    try:
        result = await run.start(
            agent, task,
            workdir=workdir or None,
            timeout=timeout or None,
            model=model or None,
            verifies=verifies,
            budget_tag=budget_tag,
            budget_tokens=budget_tokens,
        )
        # First use of this agent in this session: hand back its calling
        # contract unasked. The brief tells you to read it beforehand, and a
        # model that did will not need this — but one that did not has just
        # written a task without it, and this is the moment that is still
        # recoverable with steer_agent. Cheaper than refusing the spawn until
        # the contract has been read, which would cost a round trip on every
        # first delegation including the ones that were already right.
        _attach_contract(run, agent, result)
        return _ok(result)
    except (PermissionError, RuntimeError, ValueError, KeyError,
            FileNotFoundError) as exc:
        return _ok({"error": f"{type(exc).__name__}: {exc}"})


@_tool()
def how_to_call(agent: str) -> dict:
    """This agent's interface contract: how to write a task it can act on.

    Read it BEFORE you first delegate to an agent in a session. It is written in
    the agent's own brief, by whoever knows best what a task to it must contain,
    and the agent itself never sees it — so it cannot drift from what the agent
    actually needs the way a copy in your brief would.

    `list_agents` answers "should I use this one". This answers "how do I use
    it": what the task must contain, what must stay out of it, what state has to
    exist first, what it hands back, and what to carry over from the run before.
    """
    run = runner()
    spec = run.config.agents.get(agent)
    if spec is None:
        return _ok({"error": f"No agent named {agent!r}. See list_agents."})
    contract = run.config.calling_contract(spec)
    if not contract:
        return _ok({"agent": agent, "description": spec.description,
                    "contract": "",
                    "note": "This agent's brief names no calling contract. Give "
                            "it everything it needs up front — it cannot ask."})
    return _ok({"agent": agent, "description": spec.description,
                "contract": contract})


@_tool()
def record_findings(source: str) -> dict:
    """Ingest a merged findings file into the ledger.

    Call it after merging an auditor's (or characterizer's, or adversary's)
    branch, with the path it wrote — `context/review/C1.md`. Parsing is
    mechanical: you do not have to read the file or transcribe anything, which
    is the point. Your context should hold the index, not the evidence.

    A new id is recorded `open`. An id already in the ledger keeps whatever
    decision it carries — a re-run must not quietly reopen a finding the user
    accepted. But an id that was marked `fixed` and has been filed again comes
    back as a **regression**, and that is the number worth watching: it means
    the fix did not hold, and it is how this loop finds out it is not
    converging instead of generating a fresh id for the same problem forever.
    """
    run = runner()
    denied = _root_only("record_findings")
    if denied:
        return _ok({"error": denied})
    return _ok(findings_mod.record(run.paths.root, source,
                                   gitops.head_sha(run.paths.root)))


@_tool()
def set_finding_status(finding_id: str, status: str, note: str = "") -> dict:
    """Record what became of a finding.

    `open` → `scheduled` when it becomes work, `fixed` when that work merges,
    `accepted` when it is real and nobody will act on it, `deferred` when it is
    real and not now. `regression` is set for you when a fixed finding is filed
    again; you should not need to set it by hand.

    History is appended, never replaced. The findings file itself is never
    touched — it is evidence written against a commit, and editing it to say
    "fixed" leaves its line numbers and its trace pointing at code that has
    since moved.

    Say WHY in the note, especially for `accepted`. The next review reads this,
    and "accepted" with no reason gets relitigated every time.
    """
    run = runner()
    denied = _root_only("set_finding_status")
    if denied:
        return _ok({"error": denied})
    role = os.environ.get("MULTIAGENTS_ROLE") or "orchestrator"
    return _ok(findings_mod.set_status(run.paths.root, finding_id, status,
                                       note=note,
                                       sha=gitops.head_sha(run.paths.root),
                                       by=role))


@_tool()
def list_findings(status: str = "", context: str = "") -> dict:
    """The ledger as an index — id, status, severity, class, one line each.

    Deliberately without the evidence. You are deciding what to work on, and a
    reader that loads every trace to make that decision has spent its context
    before it gets to the decision. Use read_finding for the one you are
    actually deciding about.

    `live` counts what still needs someone: open, scheduled and regressed.
    `accepted` and `deferred` are decisions, not unfinished business. When
    `done` is true there is nothing outstanding that anyone has chosen to
    care about, which is what "this project is reviewed" means.
    """
    return _ok(findings_mod.summary(runner().paths.root, status, context))


@_tool()
def read_finding(finding_id: str) -> dict:
    """One finding's full text and its history, pulled on demand.

    A findings file holds every finding for a context, so opening it to answer
    a question about one of them loads all of them. This keeps a conversation
    about F12 costing the size of F12.
    """
    return _ok(findings_mod.evidence(runner().paths.root, finding_id))


@_tool()
def budget_tag_status(tag: str = "") -> dict:
    """What a named slice of work has spent, and what it has left.

    Check this before deciding how much of a slice to attempt, not after
    start_agent refuses. A refusal is recoverable but wastes the turn, and the
    useful move — deciding what this slice will not get — is one you make
    better before you are out than after.
    """
    run = runner()
    tree = run.tree
    if tag:
        cap = tree.budget_for_tag(tag)
        spent = int(tree.usage_for_tag(tag).get("total", 0) or 0)
        return _ok({"tag": tag, "tokens_spent": spent, "tokens_budgeted": cap,
                    "tokens_left": max(0, cap - spent) if cap else None,
                    "exhausted": bool(cap and spent >= cap)})
    tags = {node.get("budget_tag") for node in tree.read()["nodes"].values()}
    known = sorted(t for t in tags if t) or []
    return _ok({"tags": [
        {"tag": t, "tokens_spent": int(tree.usage_for_tag(t).get("total", 0) or 0),
         "tokens_budgeted": tree.budget_for_tag(t)} for t in known
    ]})


def _seen(run: Runner, result: dict) -> dict:
    """SV-R11: record that the final statuses in `result` reached the caller.

    wait_for_agents, check_agent and collect_agent, per the spec; consult too,
    since its reply is the idle agent's result, and nothing else would ever
    return that to the orchestrator.

    Only for the node's own parent — the root server for a root agent — since
    what this guards is the orchestrator compacting away a result it was never
    shown, and a sibling reading it shows the orchestrator nothing. Written to
    the tree, not held here: a compaction restarts this server.
    """
    caller = run.self_id()
    entries = [result, *result.get("changed", []), *result.get("already_finished", [])]
    for entry in entries:
        agent_id = entry.get("agent_id") if isinstance(entry, dict) else None
        node = run.tree.get(agent_id) if agent_id else None
        if node is not None and node.unseen and (node.parent or None) == caller:
            run.tree.mark_seen(agent_id, entry.get("status"))
    return result


def _plan_node(run: Runner, agent_id: str) -> dict | None:
    """NC-R21: the plan node `agent_id` names, when it is not a run.

    Only with the gate on, only for an id the tree does not know, and only a
    node the caller may read (`get_node` keeps the scope). Run ids never pay
    for the round trip.
    """
    if not isinstance(agent_id, str) or not agent_id.startswith("nd-") \
            or run.tree.get(agent_id) is not None:
        return None
    node = _node_rpc("get_node", {"id": agent_id})
    return node if isinstance(node, dict) and node.get("id") == agent_id else None


def _node_id_refusal(run: Runner, agent_id: str) -> dict | None:
    """NC-R21: a run tool given a node id refuses it, with no side effect."""
    if _plan_node(run, agent_id) is None:
        return None
    return {"error": "node_id",
            "hint": f"{agent_id} is a plan node, not a run: use get_node for its "
                    f"state and its runs' ids, which these tools take."}


@_tool()
def check_agent(agent_id: str, since: int = 0) -> dict:
    """Check a running agent's status and read new stream events.

    Pass the previous `next_since` to get only what is new, so following a long
    run costs almost no context. Status `stuck` means a watchdog fired —
    silence, wall-clock, doom loop, or runaway steps — and the reason says
    which. A stuck agent is NOT killed: decide whether to steer, wait, or stop.
    """
    run = runner()
    refused = _node_id_refusal(run, agent_id)
    if refused:
        return _ok(refused)
    try:
        return _ok(_seen(run, run.check(agent_id, since)))
    except KeyError as exc:
        return _ok({"error": str(exc)})


@_tool()
async def wait_for_agents(agent_ids: list[str] | None = None, timeout: int = 300) -> dict:
    """Block until any of these agents finishes or gets stuck.

    Call this when there is nothing else to start — not as the reflex after
    starting one agent. It returns `capacity`, and if slots are idle it says so:
    waiting with three of four slots free does not make the work smaller, it
    makes it take four times as long.

    Far better than polling in a loop: returns the moment something changes, and
    costs one tool call rather than one per check. With no ids, waits on every
    active agent.
    """
    run = runner()
    # NC-R21: a node id waits for that node's current run, never a later one
    # (that is wait_for_nodes); a node with no run yet is answered at once.
    nodes, no_run = {}, []
    if agent_ids:
        ids = []
        for agent_id in agent_ids:
            node = await asyncio.to_thread(_plan_node, run, agent_id)
            if node is None:
                ids.append(agent_id)
            elif node.get("runs"):
                nodes[agent_id] = node["runs"][-1]["run_id"]
                ids.append(nodes[agent_id])
            else:
                no_run.append(agent_id)
        if not ids:
            return _ok({"changed": [], "still_running": [], "no_run_yet": no_run,
                        "capacity": run.capacity(),
                        "note": "no run has started for these nodes yet; "
                                "wait_for_nodes follows a node, not a run"})
        agent_ids = list(dict.fromkeys(ids))
    result = await run.wait_for_any(agent_ids, float(timeout))
    if nodes:
        result["node_runs"] = nodes
    if no_run:
        result["no_run_yet"] = no_run
    return _ok(_seen(run, result))


@_tool()
def collect_agent(agent_id: str, mode: str = "summary") -> dict:
    """Collect a finished agent's result.

    `summary` returns the tail of its final message plus commit and diff stats.
    `full` returns everything it wrote. The complete transcript always stays on
    disk (see log_dir, or the `run://<agent_id>` resource) rather than being
    dumped into your context — that isolation is the point of delegating.

    Check `need_info`: any NEED_INFO lines mean the agent was blocked on
    something only you or another agent knows. Answer with steer_agent.

    Check `readonly_violations` too. It lists files the agent changed that it
    may not modify — for the coder tiers, the test suite. They are reverted
    when the branch merges, so nothing is at risk, but the attempt is
    information: it usually means the test and the implementation disagree, and
    deciding which is wrong is yours.
    """
    run = runner()
    refused = _node_id_refusal(run, agent_id)
    if refused:
        return _ok(refused)
    try:
        return _ok(_seen(run, run.collect(agent_id, mode)))
    except KeyError as exc:
        return _ok({"error": str(exc)})


@_tool()
async def steer_agent(agent_id: str, message: str, provider: str | None = None) -> dict:
    """Redirect a running or stuck agent without losing its context.

    A subprocess cannot be interrupted mid-turn, so this stops the current turn
    and resumes the same session with your message. The agent keeps everything
    it has learned. Use it on a doom loop ("stop rewriting the parser, just fix
    the null check") or to answer a NEED_INFO question.
    """
    run = runner()
    refused = await asyncio.to_thread(_node_id_refusal, run, agent_id)
    if refused:
        return _ok(refused)
    try:
        return _ok(await run.steer(agent_id, message, **({"provider": provider} if provider is not None else {})))
    except KeyError as exc:
        return _ok({"error": str(exc)})


@_tool()
async def stop_agent(agent_id: str) -> dict:
    """Stop an agent and everything it spawned. Its branch and logs survive."""
    run = runner()
    refused = await asyncio.to_thread(_node_id_refusal, run, agent_id)
    if refused:
        return _ok(refused)
    denied = _may_act_on(agent_id)
    if denied:
        return _ok({"error": denied})
    return _ok(await run.stop(agent_id))


@_tool()
async def allow_reserve(request_id: str) -> dict:
    """Allow one queued task to use the orchestrator's reserved quota floor."""
    run = runner()
    if run.self_id():
        return _ok({"error": "only the orchestrator can allow reserve"})
    return _ok(await run.reserve_answer(request_id))


@_tool()
async def veto_reserve(request_id: str, reason: str) -> dict:
    """Protect the quota floor until the next reset of an eligible instance."""
    run = runner()
    if run.self_id():
        return _ok({"error": "only the orchestrator can veto reserve"})
    return _ok(await run.reserve_answer(request_id, veto=True, reason=reason))


@_tool()
def list_deferred() -> dict:
    """List every task in the deferred queue, waiting or refused.

    `waiting` entries restart by themselves once `retry_after` passes and a
    `wait_for_agents` runs. `refused` entries could not be restarted (see
    `reason`), are never retried, and stay until `cancel_deferred` removes them.
    A status of `restarting` is a drain's transient claim (DQ-R8): the entry is
    being restarted right now, and the next drain settles it if the claiming
    process died. A status of `malformed` (DQ-R9) is an entry too broken to
    act on: it is skipped by the drain and only the orchestrator can remove it.
    Read-only.
    """
    entries = []
    for d in runner().tree.read()["deferred"]:
        if not isinstance(d, dict) or deferred_malformed(d):
            # DQ-R9: shown, with status `malformed`, never a crash. Only the
            # orchestrator can remove it, which is why `deferred_by` reads so.
            spec = d.get("spec") if isinstance(d, dict) else None
            if not isinstance(spec, dict):
                spec = {}
            entries.append({
                "id": (d.get("id") or "") if isinstance(d, dict) else "",
                "agent": spec.get("agent"),
                "task": (spec.get("task") or "")[:200],
                "model": spec.get("model") or "",
                "retry_after": "",
                "status": "malformed",
                "reason": "queue entry is malformed; the drain skips it (DQ-R9)",
                "deferred_by": "orchestrator",
            })
            continue
        spec = d.get("spec") or {}
        entries.append({
            "id": d.get("id"), "agent": spec.get("agent"),
            "task": (spec.get("task") or "")[:200],
            "model": spec.get("model") or "",
            "retry_after": datetime.fromtimestamp(
                d.get("retry_after") or 0, timezone.utc).isoformat(),
            "status": d.get("status", "waiting"),
            "reason": d.get("reason", ""),
            "deferred_by": d.get("deferred_by") or "orchestrator",
            # PC-R3a: a provider-concurrency entry's queue and place in it,
            # and why a skipped head is skipped.
            **({"cause": d["cause"], "provider": spec.get("provider"),
                "seq": d.get("seq")} if d.get("cause") else {}),
            **({"blocked": d["blocked"]} if d.get("blocked") else {}),
        })
    return _ok({"deferred": entries})


@_tool()
def cancel_deferred(deferred_id: str) -> dict:
    """Remove a task from the deferred queue (waiting or refused).

    Only the agent that deferred it, one of its ancestors, or the orchestrator
    may cancel — an entry recorded with no `deferred_by` predates that field
    and only the orchestrator may cancel it (DQ-R4a). A malformed entry
    (DQ-R9) is likewise the orchestrator's alone. An entry whose restart is
    in progress is refused (DQ-R12): `stop_agent` the run once it exists. The
    exit is recorded as a `deferred_exit` event, and the pause a deferral set
    is lifted if no waiting entry remains.
    """
    run = runner()
    entry = find_deferred(run.tree.read()["deferred"], deferred_id)
    if entry is None:
        return _ok({"error": f"no deferred entry {deferred_id!r}; list_deferred shows the queue"})
    restarting = (f"{deferred_id} is restarting; cancel is refused while a "
                  f"restart is in progress (DQ-R12). stop_agent the run "
                  f"once it exists.")
    caller = run.self_id()
    if deferred_malformed(entry):
        # A broken entry has no owner that can be trusted; whoever defers or
        # drains cannot be recovered from it, so removal is not theirs to do.
        if caller is not None:
            return _ok({"error": f"only the orchestrator may cancel the malformed "
                                 f"entry {deferred_id} (DQ-R9)."})
    elif entry.get("status") == "restarting":
        # DQ-R12: the restart and the cancel would each believe they own the
        # outcome — the record could say cancelled while the run started
        # anyway. Refuse here; stop_agent reaches the run once it exists.
        return _ok({"error": restarting})
    else:
        owner = entry.get("deferred_by")
        if caller is not None and not (owner and caller in run.tree.ancestry(owner)):
            return _ok({"error": f"{caller} may only cancel entries it deferred, or its "
                                 f"descendants' ({deferred_id} was deferred by "
                                 f"{owner or 'the orchestrator'})."})
    outcome = run.tree.exit_deferred(deferred_id, "cancelled")
    if outcome is True:
        return _ok({"cancelled": deferred_id})
    if outcome is False:
        return _ok({"error": f"no deferred entry {deferred_id!r}"})
    # DQ-R12: a drain's claim held the entry when the transaction ran. The
    # claim may dissolve straight after — a transient start failure requeues
    # the entry — so the answer is made for what IS there now, not for the
    # transaction's instant: `waiting` again means no run exists and the
    # caller's cancel still stands, so it is honoured through the same
    # transactional path.
    state = find_deferred(run.tree.read()["deferred"], deferred_id)
    if state is None:
        return _ok({"error": f"no deferred entry {deferred_id!r}"})
    if state.get("status") == "waiting":
        retried = run.tree.exit_deferred(deferred_id, "cancelled")
        if retried is True:
            return _ok({"cancelled": deferred_id})
        if retried is False:
            return _ok({"error": f"no deferred entry {deferred_id!r}"})
    return _ok({"error": restarting})


# --------------------------------------------------------------------------
# Model catalog — is the ground under the roster still where we left it?
# --------------------------------------------------------------------------


@_tool()
def auth_status() -> dict:
    """Check whether each provider CLI has a usable credential.

    **This reads local state, not the provider.** `claude auth status` never
    asks the server, so a revoked token still reports as authenticated — that
    happened, and it misled an hour of diagnosis while every subagent on that
    provider failed with a 401. `stored_login` is what the check knows;
    `last_run` (success / failed / untested) and `recent_failures` come from how
    runs actually ended, and are the stronger evidence when they disagree.
    `untested` means no evidence either way — not a problem.

    Every provider is checked the same way, through its own script, so the
    answer and the fix have the same shape whichever CLI is broken. An agent
    run against an unauthenticated provider fails with an empty response that
    looks like a model saying nothing, so check here before concluding an agent
    is broken.

    Repairing authentication may need a human at a terminal, so it is not
    exposed as a tool: report the `fix` command to the user and let them run it.
    """
    run = runner()
    states = auth_mod.check_all(
        run.providers, lambda name: run.executor(), global_config_dir(),
        run.paths.config,
    )
    out = {name: state.to_dict() for name, state in states.items()}

    # A stored credential is not a working one. Every provider's check reads
    # local state — `claude auth status` never asks the server — so this said
    # "authenticated" through an hour in which every claude subagent failed with
    # a 401, and actively misled the diagnosis. Reported as bug-cee638.
    #
    # The honest signal costs nothing: we already record how each provider's
    # runs ended. Presence and evidence are reported separately rather than
    # blended into one word.
    health = run.tree.provider_health()
    for name, entry in out.items():
        record = health.get(name, {})
        failures = record.get("consecutive_failures", 0)
        # NOT named `credential_*`: redact.py drops any key matching that
        # word wholesale, so the field masked itself to "[redacted]".
        entry["stored_login"] = entry.pop("authenticated", entry.get("ok", False))
        entry["recent_failures"] = failures
        entry["last_success"] = record.get("last_success")
        # Tri-state on purpose. A boolean false would read as "broken" for a
        # provider that simply has not run yet, and send the orchestrator off to
        # debug a healthy system.
        entry["last_run"] = ("failed" if failures else
                             "success" if record.get("last_success") else
                             "untested")
        # Warn at the circuit breaker's threshold, not on a single failure.
        # Agents fail all the time — a watchdog trip, a timeout, a bad task —
        # and calling a provider suspect after one of those conflates ordinary
        # stumbling with a broken provider. The first version of this field did
        # exactly that and had the orchestrator reporting providers as
        # unreachable all day.
        threshold = max(2, int(run.config.limits.get(
            "provider_failure_threshold", 3)) - 1)
        kind = record.get("last_kind") or ""
        entry["last_failure_kind"] = kind or None
        if failures >= threshold:
            # WHY they failed, not just how many. An orchestrator told "4 runs
            # in a row failed" concluded the provider was unsafe to route to
            # and stopped using it — correctly, on that evidence. All four were
            # the provider saying it was full, which is a different fact with a
            # different remedy: wait, do not avoid.
            if kind == "limited":
                entry["warning"] = (
                    f"{failures} run(s) in a row ended because this provider "
                    f"was out of quota, not because it is broken. It is "
                    f"usable again when its window resets — check "
                    f"`budget_status` rather than routing around it."
                )
            elif kind == "unauthenticated":
                entry["warning"] = (
                    f"{failures} run(s) in a row failed to authenticate. Only a "
                    f"person can fix that: `multiagents auth login`."
                )
            else:
                entry["warning"] = (
                    f"{failures} run(s) in a row failed on this provider "
                    f"({record.get('last_reason', '')[:90]}). A stored credential is "
                    f"not a working one — this check reads local state only."
                )
    broken = [n for n, s_ in states.items() if not s_.ok]
    degraded = [n for n, e in out.items() if e.get("warning")]
    if broken:
        note = "run the `fix` command in a terminal; it may require a browser"
    elif degraded:
        note = (f"credentials are present for every provider, but runs on "
                f"{', '.join(degraded)} are failing. This check reads local "
                f"state and cannot see a revoked or cached-stale token — treat "
                f"repeated failures as the stronger evidence.")
    else:
        note = "all providers authenticated"
    return _ok({
        "providers": out,
        # Named for what it measures. `all_authenticated` read as "everything
        # works", which is exactly the claim this check cannot make.
        "all_stored_logins_ok": not broken,
        "all_authenticated": not broken,
        "degraded": degraded,
        "needs_attention": broken,
        "note": note,
    })


@_tool()
def check_model_catalog(provider: str = "opencode-go") -> dict:
    """Compare the local model-catalog snapshot against the live public catalog.

    Call this at the start of a session. `agents.yaml` pins specific model ids,
    so a model that disappears, loses tool-calling, or has its context halved
    breaks an agent in a confusing way rather than an obvious one.

    Writes nothing. Read `assessment.severity`:

    * `none`    — nothing changed, or nothing that touches your roster
    * `info`    — roster models changed in ways that probably do not matter
    * `warning` — a roster model was repriced or resized
    * `critical`— a roster model was removed or lost tool_call

    If anything touches the roster, the expected next step is to consult the
    critic with what you intend to do about it, decide, and then act. Recording
    the new baseline is update_model_catalog.
    """
    run = runner()
    result = catalog_mod.check(
        global_config_dir(), provider, run.config.agents,
    )
    return _ok(result)


@_tool()
def update_model_catalog(provider: str = "opencode-go") -> dict:
    """Record the live catalog as the new local baseline.

    Do this once you have looked at what changed. Until you do, every session
    will keep reporting the same diff.
    """
    denied = _root_only("update_model_catalog")
    if denied:
        return _ok({"error": denied})
    return _ok(catalog_mod.apply(global_config_dir(), provider))


# --------------------------------------------------------------------------
# Conversation
# --------------------------------------------------------------------------


@_tool()
async def consult(agent: str, message: str, timeout: int = 0) -> dict:
    """Ask a conversational agent something and wait for its reply.

    Unlike start_agent, this blocks and returns the answer, and the agent keeps
    its context between calls — so this is a real back-and-forth, not a series
    of one-shot questions.

    Use it with `critic` before any decision worth a second opinion: changing
    agents.yaml, picking between approaches, deciding whether a stuck agent
    should be steered or discarded. Tell it what you intend to do and why, not
    just what the problem is — it can only critique a proposal it can see.

    The reply is advice. You decide, including deciding against it. Nothing
    here gates anything, and you do not need the critic's agreement to act.
    """
    run = runner()
    try:
        return _ok(_seen(run, await run.consult(agent, message, timeout or None)))
    except (ValueError, KeyError, FileNotFoundError, PermissionError, RuntimeError) as exc:
        return _ok({"error": f"{type(exc).__name__}: {exc}"})


# --------------------------------------------------------------------------
# Branch lifecycle — the parent's responsibility
# --------------------------------------------------------------------------


def _may_act_on(agent_id: str) -> str | None:
    """Is the caller allowed to merge, discard or stop this agent?

    Every stdio MCP server treats a client with no MULTIAGENTS_AGENT_ID as the
    root orchestrator, so once `prepare` registers the server globally for
    opencode or agy — which have no per-invocation MCP scope — every SUBAGENT of
    those providers inherits these tools too. Only start_agent and consult were
    ever gated, so a subagent could discard a sibling's branch.

    The rule is ownership, not rank: a parent may act on its own descendants,
    because the recursive design makes each agent responsible for its children's
    branches. Returns an error string, or None if allowed.
    """
    run = runner()
    caller = run.self_id()
    if caller is None:
        return None                       # the root orchestrator owns everything
    if agent_id == caller:
        return None
    if caller in run.tree.ancestry(agent_id):
        return None
    return (f"{caller} may only act on its own descendants; {agent_id} is not one. "
            f"Branch lifecycle belongs to an agent's own parent.")


def _root_only(action: str, initializer_too: bool = False) -> str | None:
    """Refuse an action that changes state outside any one agent's subtree."""
    caller = runner().self_id()
    if caller is not None:
        return f"{action} is reserved for the orchestrator; {caller} is a subagent."
    if initializer_too and os.environ.get("MULTIAGENTS_ROLE") == "initializer":
        return (f"{action} is not available while shaping the project. "
                f"Nothing is published during initialisation.")
    return None


@_tool()
def list_questions(agent_id: str = "") -> dict:
    """List questions agents have parked on, waiting for a decision.

    An agent that emits NEED_DECISION stops immediately rather than guessing,
    keeping its branch and session. Check this whenever wait_for_agents reports
    `awaiting_user`, and at the start of a session.

    Answer anything within your remit with answer_question — that is the point
    of you seeing these first. Leave only genuinely user-level choices, which
    the user resolves with `multiagents ask`.
    """
    run = runner()
    questions = run.tree.open_questions(agent_id or None)
    return _ok({
        "open": questions,
        "count": len(questions),
        "note": ("answer what is within your remit; leave the rest for the user"
                 if questions else "nothing is waiting on a decision"),
    })


@_tool()
async def answer_question(question_id: str, answer: str) -> dict:
    """Answer a parked agent's question and resume it where it stopped.

    The agent continues with its full context — its session is resumed, not
    restarted. Answer only what you can genuinely settle: guessing here defeats
    the reason the agent stopped.
    """
    run = runner()
    return _ok(await run.answer_question(question_id, answer, answered_by="orchestrator"))


@_tool()
def list_plans() -> dict:
    """Read plans and the count of user notes not yet considered by a plan.

    Reports malformed or refused files. This tool never writes or imports plans.
    """
    from .plans import list_plans as read_plans

    return _ok(read_plans(_project_paths().root))


@_tool()
def list_tickets(status: str = "open") -> dict:
    """Bug tickets the bug-reporter has filed against multiagents itself.

    This is your queue for defects in the tooling, separate from your work.
    Read it whenever an agent's result mentions a ticket, and again at every
    natural stopping point — a `minor` ticket is meant to wait for one of those,
    while a `blocking` one is why you stopped.

    The stored text is already depersonalised: what you read is what would be
    published. Check it anyway before submitting — you know what this project is
    about and the scrubber does not.
    """
    run = runner()
    tickets = run.tree.read().get("tickets", [])
    if status and status != "all":
        wanted = {"open": ("open", "awaiting_user")}.get(status, (status,))
        tickets = [t for t in tickets if t.get("status") in wanted]
    conf = bugs.settings(run.config)
    ok, why = bugs.can_submit(run.config)
    return _ok({
        "tickets": tickets,
        "count": len(tickets),
        "blocking": [t["id"] for t in tickets if t.get("severity") == "blocking"],
        "automatic_reporting": conf["automatic"],
        "can_submit": ok,
        "note": why or ("submit_ticket files these upstream" if conf["automatic"]
                        else "automatic reporting is off: submit_ticket parks the "
                             "ticket for the user to send with `multiagents tickets "
                             "submit <id>`"),
    })


@_tool()
def submit_ticket(ticket_id: str) -> dict:
    """Report a filed bug upstream, or hand it to the user to send.

    With `bug_reporting.automatic: true` this creates the issue. With it false —
    the default — nothing leaves the machine: the ticket is parked for the user.
    That is not a failure and you should not work around it; say so and carry
    on.
    """
    run = runner()
    ticket = run.tree.get_ticket(ticket_id)
    if ticket is None:
        return _ok({"error": f"unknown ticket {ticket_id!r}"})
    if ticket.get("status") in ("reported", "declined"):
        return _ok({"ticket_id": ticket_id, "status": ticket["status"],
                    "url": ticket.get("url", ""), "note": "already resolved"})

    conf = bugs.settings(run.config)
    if not conf["automatic"]:
        run.tree.set_ticket_status(ticket_id, "awaiting_user",
                                   "automatic reporting is off")
        return _ok({
            "ticket_id": ticket_id, "status": "awaiting_user", "submitted": False,
            "user_action": f"multiagents tickets submit {ticket_id}",
            "note": "parked for the user by policy, not by error",
        })

    ok, result = bugs.submit(run.config, ticket)
    if not ok:
        run.tree.set_ticket_status(ticket_id, "awaiting_user", result)
        return _ok({"ticket_id": ticket_id, "status": "awaiting_user",
                    "submitted": False, "error": result,
                    "user_action": f"multiagents tickets submit {ticket_id}"})
    run.tree.set_ticket_status(ticket_id, "reported", "submitted automatically", result)
    return _ok({"ticket_id": ticket_id, "status": "reported", "submitted": True,
                "url": result})


@_tool()
def resolve_ticket(ticket_id: str, outcome: str, note: str = "") -> dict:
    """Close a ticket you have dealt with locally.

    `fixed` when you changed the project so the bug no longer bites — the ticket
    still describes a real upstream defect, so report it too rather than
    treating your workaround as the end of it. `declined` when it turned out not
    to be a bug; say why in the note, because the next agent will hit the same
    thing.
    """
    if outcome not in ("fixed", "declined"):
        return _ok({"error": "outcome must be 'fixed' or 'declined'"})
    run = runner()
    record = run.tree.set_ticket_status(ticket_id, outcome, note)
    if record is None:
        return _ok({"error": f"unknown ticket {ticket_id!r}"})
    return _ok({"ticket_id": ticket_id, "status": outcome,
                "note": "still worth reporting upstream" if outcome == "fixed" else ""})


@_tool()
def merge_agent(agent_id: str, into: str = "") -> dict:
    """Merge a finished agent's branch, then remove its worktree and branch.

    Squash-merges by default, so an agent's messy history becomes one commit
    named after it. A conflict is aborted cleanly and reported — the branch is
    left intact for you to resolve, never half-merged.

    If the agent changed a file it may not modify (`readonly_paths` — the test
    suite, for the coder tiers), that file is reverted to the base branch first
    and the rest of its work merges normally. The result then carries
    `readonly_reverted`. Read it: a developer editing a test usually means the
    test and the implementation disagree, and which of them is wrong is your
    decision. The merged result will have that test failing, which is the
    outcome you want rather than a green suite that was quietly weakened.
    """
    run = runner()
    refused = _node_id_refusal(run, agent_id)
    if refused:
        return _ok(refused)
    try:
        denied = _may_act_on(agent_id)
        if denied:
            return _ok({"error": denied})
        return _ok(run.merge_agent(agent_id, into or None))
    except KeyError as exc:
        return _ok({"error": str(exc)})


@_tool()
def discard_agent(agent_id: str, force: bool = False) -> dict:
    """Throw away an agent's branch and worktree.

    Refuses if the branch has unmerged commits unless force is true — deleting
    work an agent actually did should be a deliberate act.
    """
    run = runner()
    refused = _node_id_refusal(run, agent_id)
    if refused:
        return _ok(refused)
    try:
        denied = _may_act_on(agent_id)
        if denied:
            return _ok({"error": denied})
        return _ok(run.discard_agent(agent_id, force))
    except KeyError as exc:
        return _ok({"error": str(exc)})


@_tool()
def push_branch(agent_id: str = "", remote: str = "") -> dict:
    """Push a branch to the configured remote.

    Never automatic. Publishing is outward-facing and effectively irreversible,
    so it is always an explicit call. With no remote configured, nothing in this
    system ever leaves the machine.
    """
    denied = _root_only("push_branch", initializer_too=True)
    if denied:
        return _ok({"error": denied})
    run = runner()
    return _ok(run.push_branch(agent_id or None, remote or None))


@_tool()
def git_status() -> dict:
    """Show the project's git state: branch, cleanliness, and agent branches."""
    run = runner()
    repo = run.paths.root
    if not gitops.is_repo(repo):
        return _ok({"error": f"{repo} is not a git repository"})
    branches = gitops.run(repo, "branch", "--list", f"{run.config.branch_prefix}/*")
    # A failed pinned read is reported, never raised and never "clean" (SG-R4).
    try:
        dirty: bool | None = gitops.is_dirty(repo, root=repo)
        unreadable = None
    except gitops.GitError as e:
        dirty, unreadable = None, str(e)
    result = {
        "branch": gitops.current_branch(repo),
        "dirty": dirty,
        "head": gitops.head_sha(repo)[:12],
        "remote": run.config.remote or None,
        "agent_branches": [b.strip("* ").strip() for b in branches.out.splitlines() if b.strip()],
    }
    if unreadable is not None:
        result["error"] = unreadable
    return _ok(result)


# --------------------------------------------------------------------------
# Budget
# --------------------------------------------------------------------------


@_tool()
def budget_status() -> dict:
    """Report quota headroom and spend per provider, and what it implies.

    `known: false` means spend is tracked but capacity is not — never treat that
    as "plenty left". Claude and agy expose real subscription state; opencode
    does not, so its exhaustion is only detected reactively.

    Where a provider reports several windows they are listed under `windows`,
    and `headroom` is the worst of them, because the fullest bucket is what
    will actually stop a run — but which one it is changes what to do: a
    rolling or 5-hour window clears in hours, a weekly or monthly one does not.
    agy's `windows` also carry buckets with `counted: false`: it resells Claude
    and GPT models from a pool separate from its Gemini one, and only the pool
    the agent actually spends against feeds `headroom`.

    `by_model` breaks spend down per provider/model from our own stream
    accounting, joined to the agents that spent it. No provider offers that, and
    it is what tells you whether an expensive pin is earning its cost.

    Use this to route: when your own five-hour bucket is tight, delegating to an
    unrationed provider is the highest-value thing you can do.
    """
    run = runner()
    data = run.tree.read()
    spend: dict[str, dict[str, Any]] = {}
    for node in data.get("nodes", {}).values():
        rows = node.get("segments") or [node]
        for row in rows:
            provider = row.get("provider", "")
            if not provider:
                continue
            usage = row.get("usage") or (node.get("usage") if len(rows) == 1 else {}) or {}
            entry = spend.setdefault(provider, {"tokens": 0, "cost_usd": 0.0})
            total = usage.get("total") or usage.get("total_tokens") or 0
            if isinstance(total, (int, float)):
                entry["tokens"] += int(total)
            cost = usage.get("cost_usd")
            if isinstance(cost, (int, float)):
                entry["cost_usd"] = round(entry["cost_usd"] + cost, 6)

    budgets = budget_mod.read_all(
        run.providers, lambda name: run.executor(), global_config_dir(),
        run.paths.config, spend, data.get("cooldowns", {}),
    )
    reserve = float(run.config.project.get("budget", {}).get("reserve_headroom", 0.15))
    # This read-only view needs configuration, not the runner's handover machinery.
    handover = quota_handover_settings(run.config.project)
    tokens = _context_reading(run)
    advice = []
    for name, entry in budgets.items():
        if entry.cooldown_until:
            advice.append(f"{name} is cooling down; route elsewhere or defer")
        elif entry.known and entry.headroom is not None and entry.headroom < reserve:
            advice.append(f"{name} is below the {reserve:.0%} reserve — delegate rather than run work yourself")
    # The reading is reported here, in `context`; the notice itself is left
    # for the next other tool call rather than spent on this one.
    return _ok({
        "providers": {k: v.to_dict() for k, v in budgets.items()},
        "tree_usage": run.tree.rollup_usage(),
        "quota_handover": {"reserved_instance": handover["reserved_instance"],
                           "reserve_fraction": handover["reserve_fraction"]},
        "by_model": billed_rows(run.tree.usage_by_model(), run.providers),
        "deferred_tasks": len(data.get("deferred", [])),
        # PC-R4: per-provider concurrency, for the providers that limit it.
        **({"provider_concurrency": slots} if (slots := run.provider_slots()) else {}),
        # SC-R5: period spend of every metered provider, and the caps.
        "spend": run.spend_status(),
        "advice": advice or ["all providers have headroom"],
        # The calling session's own window. `known: false` is not room to spare.
        "context": {"known": tokens is not None, "tokens": tokens,
                    "wind_down_at": _limit(run, "context_wind_down_tokens"),
                    "compact_at": _limit(run, "compact_at_tokens")},
    }, wind_down=False)


@_tool()
def mcp_overhead(hours: float = 24.0) -> dict:
    """What running this project costs the human's own Claude subscription.

    `budget_status` reports the agents' spend and the providers' headroom. This
    reports the other budget, the one no stream of ours can see: our MCP server
    is attached to a Claude Code session, and its tool results sit in that
    session's context on every request for the rest of it. That is paid for out
    of the subscription running the orchestrator.

    Computed from Claude Code's local transcripts, not from asking it: its own
    `/usage` panel reports this only as rounded prose with a truncated server
    list, and each probe opens a session, which inflates the session count it
    reports.

    `share` is a FLOOR. An MCP server also puts its whole tool schema in context
    before any tool is called, and the transcript never records that, so the
    real cost is higher than the number here — by more in sessions that keep the
    server attached without calling it much.

    Use it to decide whether a chatty tool is worth its context: a result you
    fetch once and re-read ten times is paid for eleven times.
    """
    from .transcripts import BIG_CONTEXT, analyse

    report = analyse(window_hours=hours)
    payload = report.to_dict()
    advice = []
    for name, entry in payload["mcp_servers"].items():
        if entry["share"] >= 0.20:
            advice.append(
                f"{name} carries at least {entry['share']:.0%} of the window's "
                f"usage; prefer fewer, larger tool calls over many small ones")
    if payload[f"share_above_{BIG_CONTEXT // 1000}k_context"] >= 0.75:
        advice.append(
            "most of the spend is at high context; /compact between tasks is "
            "worth more here than switching models")
    payload["advice"] = advice or ["no MCP server is a material share of usage"]
    return _ok(payload)


# --------------------------------------------------------------------------
# Resources — full transcripts stay out of tool results
# --------------------------------------------------------------------------


@mcp.resource("tree://project")
def tree_resource() -> str:
    """The project agent tree as readable text."""
    return runner().tree.render()


@mcp.resource("run://{agent_id}")
def run_resource(agent_id: str) -> str:
    """One agent's full transcript."""
    run = runner()
    node = run.tree.get(agent_id)
    if node is None:
        return f"Unknown agent {agent_id!r}"
    result = run.collect(agent_id, mode="full")
    lines = [
        f"# {node.agent} ({agent_id}) — {node.status}",
        f"{node.provider}/{node.model} · branch {node.branch or '-'} · {round(node.elapsed())}s",
        "",
        f"## Task\n{node.task}",
        "",
        f"## Result\n{result.get('text', '')}",
    ]
    if result.get("stderr_tail"):
        lines += ["", f"## stderr\n{result['stderr_tail']}"]
    return "\n".join(lines)


_node_rpc_namespace = uuid.uuid4().hex


def _node_rpc(op: str, args: dict, ctx=None) -> dict:
    """Gate and credentials for nodes, independent of legacy run authority."""
    from . import scheduler
    paths = _project_paths()
    try:
        gate = scheduler.enabled(paths.root)
    except (yaml.YAMLError, OSError):
        if _runner is None:
            return {"error": "scheduler_unavailable"}
        gate = _runner.config.project.get("scheduler", {}).get("enabled", False)
    if not gate:
        pending = scheduler.pending_nodes(paths.root)
        return {"error": "scheduler_disabled", **({"pending_nodes": len(pending)} if pending else {})}
    if "MULTIAGENTS_AGENT_ID" in os.environ:
        token = os.environ.get("MULTIAGENTS_RPC_TOKEN")
        if not token:
            return {"error": "unauthenticated"}
    else:
        try:
            token = scheduler.root_capability(paths.root)
        except OSError:
            return {"error": "scheduler_unavailable"}
    request_id = None
    if ctx is not None:
        try:
            request_id = f"mcp:{_node_rpc_namespace}:{ctx.request_id}"
        except (ValueError, RuntimeError):
            pass  # SDK calls made outside a request have an empty context.
    try:
        reply = scheduler.call(paths.root, op, args, token, request_id=request_id)
        return reply["result"] if reply.get("ok") else reply["error"]
    except (OSError, ValueError):
        return {"error": "scheduler_unavailable"}


@_tool()
def create_node(kind: str, plan_revision: int, agent: str | None = None,
                task: str | None = None, parent: str | None = None,
                pins: dict | None = None, session: str | None = None,
                children: list[str] | None = None, loop: dict | None = None,
                depends_on: list[dict] | None = None, inputs: list[dict] | None = None,
                urgent: bool = False, locks: list[str] | None = None,
                window: dict | None = None, ctx: _ToolContext = None) -> dict:
    """Deposit a planned node, using the current list_nodes plan_revision."""
    fields = dict(kind=kind, plan_revision=plan_revision, parent=parent, urgent=urgent)
    fields.update({k: v for k, v in dict(agent=agent, task=task, pins=pins, session=session,
                  children=children, loop=loop, depends_on=depends_on, inputs=inputs,
                  locks=locks, window=window).items() if v is not None})
    return _node_rpc("create_node", fields, ctx)


# Field's factory preserves omission through MCP argument validation as well
# as direct calls. Explicit null remains distinct, and the wire schema exposes
# only the contract's types.
_NODE_UNSET = Field(default_factory=lambda: _NODE_UNSET)


@_tool()
def update_node(id: str, revision: int, task: str = _NODE_UNSET,
                pins: dict = _NODE_UNSET, depends_on: list[dict] = _NODE_UNSET,
                inputs: list[dict] = _NODE_UNSET, urgent: bool = _NODE_UNSET,
                locks: list[str] = _NODE_UNSET, children: list[str] = _NODE_UNSET,
                loop: dict = _NODE_UNSET, session: str | None = _NODE_UNSET,
                window: dict | None = _NODE_UNSET, ctx: _ToolContext = None) -> dict:
    """Edit a node using its current revision. Null window/session clears it."""
    fields = {k: v for k, v in dict(task=task, pins=pins, depends_on=depends_on,
              inputs=inputs, urgent=urgent, locks=locks, children=children,
              loop=loop, session=session, window=window).items() if v is not _NODE_UNSET}
    return _node_rpc("update_node", {"id": id, "revision": revision, **fields}, ctx)


@_tool()
def cancel_node(id: str, revision: int, ctx: _ToolContext = None) -> dict:
    """Cancel a node and its non-terminal descendants, retaining results."""
    return _node_rpc("cancel_node", {"id": id, "revision": revision}, ctx)


@_tool()
def get_node(id: str) -> dict:
    """Read a planned node within the caller's scope."""
    return _node_rpc("get_node", {"id": id})


@_tool()
def list_nodes(state: str | None = None, parent: str | None = None,
               eligible: bool | None = None) -> dict:
    """List visible nodes and the plan revision for the next deposit."""
    return _node_rpc("list_nodes", {k: v for k, v in dict(state=state, parent=parent,
                     eligible=eligible).items() if v is not None})


@_tool()
def register_template(yaml: str, ctx: _ToolContext = None) -> dict:
    """Register a host-owned declarative template (available in M4)."""
    return _node_rpc("register_template", {"yaml": yaml}, ctx)


@_tool()
def list_templates() -> dict:
    """List templates available to the host scheduler."""
    return _node_rpc("list_templates", {})


@_tool()
def instantiate_template(name: str, params: dict, plan_revision: int,
                         parent: str | None = None, urgent: bool = _NODE_UNSET,
                         window: dict | None = None, depends_on: list[dict] | None = None,
                         inputs: list[dict] | None = None, locks: list[str] | None = None,
                         ctx: _ToolContext = None) -> dict:
    """Deposit an expanded template instance at the current plan_revision.

    Takes the template `name` and its `params`, the `plan_revision` from
    list_nodes and the `parent` to deposit under, plus `urgent`, `window`,
    `depends_on`, `inputs` and `locks`, which apply to the instance's root node
    exactly as they do on create_node for a composite node. A root field the
    caller leaves out is the template's own, as on a plain call.
    """
    fields = dict(name=name, params=params, plan_revision=plan_revision, parent=parent)
    if urgent is not _NODE_UNSET:
        fields["urgent"] = urgent
    fields.update({k: v for k, v in dict(window=window, depends_on=depends_on,
                  inputs=inputs, locks=locks).items() if v is not None})
    return _node_rpc("instantiate_template", fields, ctx)


@_tool()
def wait_for_nodes(cursor: int | None = None, node_ids: list[str] | None = None,
                   timeout: float = 0) -> dict:
    """Read durable transitions, using the acknowledged cursor by default."""
    args = {"timeout": timeout}
    if cursor is not None:
        args["cursor"] = cursor
    if node_ids is not None:
        args["node_ids"] = node_ids
    return _node_rpc("wait_for_nodes", args)


@_tool()
def ack_nodes(cursor: int, ctx: _ToolContext = None) -> dict:
    """Acknowledge transitions durably for the root orchestrator."""
    return _node_rpc("ack_nodes", {"cursor": cursor}, ctx)


@_tool()
def scheduler_status() -> dict:
    """Read the scheduler pid, startup time, gate and node counts."""
    return _node_rpc("scheduler_status", {})


@_tool()
def give_verdict(node_id: str, generation_seq: int, commit: str, verdict: str,
                 findings: list[dict], ctx: _ToolContext = None) -> dict:
    """Give a verdict as a designated node run (available in M4)."""
    return _node_rpc("give_verdict", dict(node_id=node_id, generation_seq=generation_seq,
                     commit=commit, verdict=verdict, findings=findings), ctx)


@_tool()
def relaunch_node(id: str, revision: int, max_rounds: int | None = None,
                  pins: dict | None = None, new_session: list[str] | None = None,
                  retry: str = "verdict_child", ctx: _ToolContext = None) -> dict:
    """Reopen a held or finished node (available in M4)."""
    args = dict(id=id, revision=revision, retry=retry)
    args.update({k: v for k, v in dict(max_rounds=max_rounds, pins=pins,
                 new_session=new_session).items() if v is not None})
    return _node_rpc("relaunch_node", args, ctx)


@_tool()
def close_node(id: str, revision: int, outcome: str, ctx: _ToolContext = None) -> dict:
    """Record the root's decision on a node (available in M4)."""
    return _node_rpc("close_node", dict(id=id, revision=revision, outcome=outcome), ctx)


@_tool()
def merge_node(id: str, force: bool = False, ctx: _ToolContext = None) -> dict:
    """Publish a top-level node through protected host git (available in M3)."""
    return _node_rpc("merge_node", {"id": id, "force": force}, ctx)


@_tool()
def dispose_node(id: str, revision: int, ctx: _ToolContext = None) -> dict:
    """Dispose retained node results (available in M3)."""
    return _node_rpc("dispose_node", {"id": id, "revision": revision}, ctx)


def _reset() -> None:
    global _runner, _loaded, _failed, _load_error, _wind_down_given, _gate
    _runner = None
    _gate = safepoint.Gate()
    _loaded, _failed, _load_error = {}, None, ""
    _wind_down_given = False
    _notice.set(None)


async def _adopt_forever() -> None:
    """SV-R6: at startup, before any handshake, and every few seconds after —
    an owner that dies while this server runs leaves its nodes to it (SV-R5)."""
    from .runner import ADOPT_SECONDS
    # A failing pass is retried, not fatal — but said once per distinct error
    # on stderr (stdout is the protocol), so a pass that can never succeed is
    # visible instead of being retried silently forever.
    reported: set[str] = set()
    while True:
        try:
            await asyncio.get_running_loop().run_in_executor(None, runner)
            if _runner is not None:
                await _runner.adopt()
                await _runner._qh_promote_check()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            if error not in reported:
                reported.add(error)
                print(f"multiagents: adoption pass failed, retrying every "
                      f"{ADOPT_SECONDS}s\n{traceback.format_exc()}",
                      file=sys.stderr, flush=True)
        check_seconds = min([ADOPT_SECONDS, *[
            _runner._qh_settings(_runner.config.agent(n.agent)).get("promote_check_seconds", 60)
            for n in _runner._qh_nodes() if n.agent in _runner.config.agents]]) if _runner else ADOPT_SECONDS
        await asyncio.sleep(check_seconds)


def _safe_point_limit() -> float:
    """How long a safe-point latch may hold: the driver's own bound, plus
    room — past it no genuine request is still waiting (see `safepoint`)."""
    limits = _runner.config.limits if _runner is not None else {}
    return limit_number(limits, "compact_safe_point_seconds") + 30


def _safe_point_register(paths: ProjectPaths, session: str, reported: set) -> bool:
    """Register this server; say a failure once on stderr. The driver sees an
    unregistered server in `/proc` and does not stop it (CW-R2a)."""
    try:
        safepoint.register(paths, session)
    except OSError as exc:
        error = f"{type(exc).__name__}: {exc}"
        if error not in reported:
            reported.add(error)
            print(f"multiagents: cannot register for the safe point, retrying: "
                  f"{error}", file=sys.stderr, flush=True)
        return False
    return True


async def _safe_point_watch(paths: ProjectPaths, session: str, registered: bool,
                            acked: list) -> None:
    """CW-R2: answer the driver's safe-point requests for this session.

    Polls, because the driver has no other way in: it holds the CLI, and the
    CLI holds this server's stdio. A request closes admission on the Runner;
    the acknowledgement is written once nothing admitted is still in flight.
    A loop frozen in a slow launch answers late or not at all, and the driver
    then cancels — the safe direction.
    """
    reported: set = set()
    while True:
        if not registered:
            registered = _safe_point_register(paths, session, reported)
        with contextlib.suppress(Exception):
            safepoint.observe(paths, session, _gate, acked, _safe_point_limit())
        await asyncio.sleep(safepoint.POLL_SECONDS)


async def _serve() -> None:
    loop = asyncio.get_running_loop()
    root = not os.environ.get("MULTIAGENTS_AGENT_ID")
    session = os.environ.get("MULTIAGENTS_SESSION_ID", "")
    # CW-R2a: registered, and the request looked for, before anything is
    # served: the driver writes its request before it lists the servers, so
    # one that registers after the listing finds the request here and starts
    # with admission closed.
    registered = None
    acked: list = []
    # CW-R2b: the safe-point key leaves the environment before anything this
    # process starts could inherit it.
    safepoint.adopt_key()
    if root and session:
        registered = _project_paths()
        # CW-R2b r9: closed until the driver that launched this CLI grants it.
        safepoint.await_grant(_gate)
        ok = _safe_point_register(registered, session, set())
        try:
            safepoint.observe(registered, session, _gate, acked, _safe_point_limit())
        except Exception:
            # Unverified is closed: the watcher reopens it once it can read.
            _gate.close("unverified", committed=False)
    serving = asyncio.create_task(mcp.run_stdio_async())
    # SV-R3: stdin EOF, SIGTERM and SIGHUP all end the server the same way —
    # through the shutdown below, never by dying with the agents' fate
    # decided by whatever the default signal action happens to do to them.
    # The transport is not cancelled, only abandoned: it reads stdin on a
    # worker thread, and cancelling it waits for a read that may never return.
    signalled = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGHUP):
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(sig, signalled.set)
    adopter = asyncio.create_task(_adopt_forever()) if root else None
    watcher = (asyncio.create_task(_safe_point_watch(registered, session, ok, acked))
               if registered is not None else None)
    try:
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait({serving, asyncio.create_task(signalled.wait())},
                               return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in (adopter, watcher):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        if _runner is not None:
            with contextlib.suppress(Exception):
                await _runner.shutdown(detach=root)
        if registered is not None:
            safepoint.unregister(registered)
        with contextlib.suppress(Exception):
            sys.stdout.flush()
            sys.stderr.flush()
        # Not a normal return, for the same reason: that thread would keep
        # this process alive until the client closes a pipe it may never close.
        os._exit(0)


def main() -> None:
    asyncio.run(_serve())


if __name__ == "__main__":
    main()
