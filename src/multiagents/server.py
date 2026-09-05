"""MCP server: delegate work to other agent CLIs as supervised subagents.

Registered once, globally, for every agent. Permission to spawn is enforced
*here* — from the ``MULTIAGENTS_CAN_SPAWN`` and ``MULTIAGENTS_DEPTH`` variables
the parent injected — rather than by giving different agents different MCP
configs. That matters because ``agy mcp add`` writes to a global profile and has
no per-agent scoping to give.

Every tool result is scrubbed for credentials on the way out.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

# The SDK renamed FastMCP to MCPServer in 2.x. The decorator API is identical,
# so support both rather than pinning to one line of the SDK.
try:
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # pragma: no cover - mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server

from . import __version__
from . import auth as auth_mod
from . import budget as budget_mod
from . import catalog as catalog_mod
from . import gitops
from .config import load as load_config
from .config import seed_project
from .models import refresh_models
from .paths import ProjectPaths, find_project_root, global_config_dir
from .redact import scrub
from .runner import Runner

mcp = _Server("multiagents", version=__version__)

_runner: Runner | None = None


def runner() -> Runner:
    """Resolve the project and build the runner, once."""
    global _runner
    if _runner is not None:
        return _runner

    # A nested agent is told which project it belongs to; a top-level session
    # infers it from the working directory.
    explicit = os.environ.get("MULTIAGENTS_PROJECT")
    root = Path(explicit).expanduser() if explicit else (find_project_root() or Path.cwd())
    paths = ProjectPaths(root)
    paths.ensure()
    seed_project(paths)
    _runner = Runner(paths, load_config(paths))
    return _runner


def _ok(payload: Any) -> Any:
    return scrub(payload)


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------


@mcp.tool()
def list_agents() -> dict:
    """List the configured subagents you can delegate to.

    Shows each agent's provider, model, whether it writes code on its own
    branch, and whether it may spawn subagents of its own. Edit the roster in
    .multiagents/config/agents.yaml.
    """
    run = runner()
    agents = []
    for name, spec in sorted(run.config.agents.items()):
        provider = run.providers.get(spec.provider)
        agents.append({
            "name": name,
            "description": spec.description,
            "provider": spec.provider,
            "model": spec.model,
            "writes": spec.writes,
            "can_spawn": spec.can_spawn,
            "permission": spec.permission,
            "timeout": spec.timeout,
            "available": bool(provider and provider.available()),
        })
    return _ok({
        "agents": agents,
        "you_may_spawn": run.can_spawn(),
        "your_depth": run.self_depth(),
        "max_depth": run.config.limits.get("max_depth", 3),
    })


@mcp.tool()
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


@mcp.tool()
def refresh_model_list() -> dict:
    """Regenerate models.yaml by asking each installed CLI what it offers.

    Run this after changing a subscription: the available models change with
    the plan, and a stale list will name models the CLI will reject.
    """
    run = runner()
    result = refresh_models(run.providers, run.paths.config / "models.yaml")
    _reset()
    return _ok(result)


@mcp.tool()
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
             "elapsed_seconds": round(n.elapsed()), "reason": n.reason}
            for n in run.tree.active()
        ],
        "deferred": len(data.get("deferred", [])),
    })


# --------------------------------------------------------------------------
# Running agents
# --------------------------------------------------------------------------


@mcp.tool()
async def start_agent(
    agent: str,
    task: str,
    workdir: str = "",
    timeout: int = 0,
    model: str = "",
) -> dict:
    """Start a subagent on a task. Returns immediately with an agent_id.

    A writing agent gets its own git worktree and branch, so it cannot touch
    your working tree or another agent's work. You own that branch: merge it
    with merge_agent when you are satisfied, or discard_agent to throw it away.

    Poll with check_agent, or block efficiently with wait_for_agents.

    Args:
        agent: Name from list_agents.
        task: What to do. Be specific — the agent cannot ask you questions.
        workdir: Override the working directory (rarely needed).
        timeout: Wall-clock seconds; 0 uses the agent's configured default.
        model: Override the configured model for this run.
    """
    run = runner()
    try:
        return _ok(await run.start(
            agent, task,
            workdir=workdir or None,
            timeout=timeout or None,
            model=model or None,
        ))
    except (PermissionError, RuntimeError, KeyError, FileNotFoundError) as exc:
        return _ok({"error": f"{type(exc).__name__}: {exc}"})


@mcp.tool()
def check_agent(agent_id: str, since: int = 0) -> dict:
    """Check a running agent's status and read new stream events.

    Pass the previous `next_since` to get only what is new, so following a long
    run costs almost no context. Status `stuck` means a watchdog fired —
    silence, wall-clock, doom loop, or runaway steps — and the reason says
    which. A stuck agent is NOT killed: decide whether to steer, wait, or stop.
    """
    run = runner()
    try:
        return _ok(run.check(agent_id, since))
    except KeyError as exc:
        return _ok({"error": str(exc)})


@mcp.tool()
async def wait_for_agents(agent_ids: list[str] | None = None, timeout: int = 300) -> dict:
    """Block until any of these agents finishes or gets stuck.

    Far better than polling in a loop: returns the moment something changes, and
    costs one tool call rather than one per check. With no ids, waits on every
    active agent.
    """
    run = runner()
    return _ok(await run.wait_for_any(agent_ids, float(timeout)))


@mcp.tool()
def collect_agent(agent_id: str, mode: str = "summary") -> dict:
    """Collect a finished agent's result.

    `summary` returns the tail of its final message plus commit and diff stats.
    `full` returns everything it wrote. The complete transcript always stays on
    disk (see log_dir, or the `run://<agent_id>` resource) rather than being
    dumped into your context — that isolation is the point of delegating.

    Check `need_info`: any NEED_INFO lines mean the agent was blocked on
    something only you or another agent knows. Answer with steer_agent.
    """
    run = runner()
    try:
        return _ok(run.collect(agent_id, mode))
    except KeyError as exc:
        return _ok({"error": str(exc)})


@mcp.tool()
async def steer_agent(agent_id: str, message: str) -> dict:
    """Redirect a running or stuck agent without losing its context.

    A subprocess cannot be interrupted mid-turn, so this stops the current turn
    and resumes the same session with your message. The agent keeps everything
    it has learned. Use it on a doom loop ("stop rewriting the parser, just fix
    the null check") or to answer a NEED_INFO question.
    """
    run = runner()
    try:
        return _ok(await run.steer(agent_id, message))
    except KeyError as exc:
        return _ok({"error": str(exc)})


@mcp.tool()
async def stop_agent(agent_id: str) -> dict:
    """Stop an agent and everything it spawned. Its branch and logs survive."""
    run = runner()
    return _ok(await run.stop(agent_id))


# --------------------------------------------------------------------------
# Model catalog — is the ground under the roster still where we left it?
# --------------------------------------------------------------------------


@mcp.tool()
def auth_status() -> dict:
    """Check whether each provider CLI is authenticated.

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
    broken = [n for n, s_ in states.items() if not s_.ok]
    return _ok({
        "providers": out,
        "all_authenticated": not broken,
        "needs_attention": broken,
        "note": ("run the `fix` command in a terminal; it may require a browser"
                 if broken else "all providers authenticated"),
    })


@mcp.tool()
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


@mcp.tool()
def update_model_catalog(provider: str = "opencode-go") -> dict:
    """Record the live catalog as the new local baseline.

    Do this once you have looked at what changed. Until you do, every session
    will keep reporting the same diff.
    """
    return _ok(catalog_mod.apply(global_config_dir(), provider))


# --------------------------------------------------------------------------
# Conversation
# --------------------------------------------------------------------------


@mcp.tool()
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
        return _ok(await run.consult(agent, message, timeout or None))
    except (ValueError, KeyError, FileNotFoundError, PermissionError, RuntimeError) as exc:
        return _ok({"error": f"{type(exc).__name__}: {exc}"})


# --------------------------------------------------------------------------
# Branch lifecycle — the parent's responsibility
# --------------------------------------------------------------------------


@mcp.tool()
def merge_agent(agent_id: str, into: str = "") -> dict:
    """Merge a finished agent's branch, then remove its worktree and branch.

    Squash-merges by default, so an agent's messy history becomes one commit
    named after it. A conflict is aborted cleanly and reported — the branch is
    left intact for you to resolve, never half-merged.
    """
    run = runner()
    try:
        return _ok(run.merge_agent(agent_id, into or None))
    except KeyError as exc:
        return _ok({"error": str(exc)})


@mcp.tool()
def discard_agent(agent_id: str, force: bool = False) -> dict:
    """Throw away an agent's branch and worktree.

    Refuses if the branch has unmerged commits unless force is true — deleting
    work an agent actually did should be a deliberate act.
    """
    run = runner()
    try:
        return _ok(run.discard_agent(agent_id, force))
    except KeyError as exc:
        return _ok({"error": str(exc)})


@mcp.tool()
def push_branch(agent_id: str = "", remote: str = "") -> dict:
    """Push a branch to the configured remote.

    Never automatic. Publishing is outward-facing and effectively irreversible,
    so it is always an explicit call. With no remote configured, nothing in this
    system ever leaves the machine.
    """
    run = runner()
    return _ok(run.push_branch(agent_id or None, remote or None))


@mcp.tool()
def git_status() -> dict:
    """Show the project's git state: branch, cleanliness, and agent branches."""
    run = runner()
    repo = run.paths.root
    if not gitops.is_repo(repo):
        return _ok({"error": f"{repo} is not a git repository"})
    branches = gitops.run(repo, "branch", "--list", f"{run.config.branch_prefix}/*")
    return _ok({
        "branch": gitops.current_branch(repo),
        "dirty": gitops.is_dirty(repo),
        "head": gitops.head_sha(repo)[:12],
        "remote": run.config.remote or None,
        "agent_branches": [b.strip("* ").strip() for b in branches.out.splitlines() if b.strip()],
    })


# --------------------------------------------------------------------------
# Budget
# --------------------------------------------------------------------------


@mcp.tool()
def budget_status() -> dict:
    """Report quota headroom and spend per provider, and what it implies.

    `known: false` means spend is tracked but capacity is not — never treat that
    as "plenty left". Claude exposes real subscription state; agy exposes none
    (exhaustion is detected reactively); opencode reports no headroom but does
    report real per-step dollar cost, which is accumulated here and matches the
    figures on its web console's usage page.

    Use this to route: when your own five-hour bucket is tight, delegating to an
    unrationed provider is the highest-value thing you can do.
    """
    run = runner()
    data = run.tree.read()
    spend: dict[str, dict[str, Any]] = {}
    for node in data.get("nodes", {}).values():
        provider = node.get("provider", "")
        if not provider:
            continue
        usage = node.get("usage") or {}
        entry = spend.setdefault(provider, {"tokens": 0, "cost_usd": 0.0})
        total = usage.get("total") or usage.get("total_tokens") or 0
        if isinstance(total, (int, float)):
            entry["tokens"] += int(total)
        cost = usage.get("cost_usd")
        if isinstance(cost, (int, float)):
            entry["cost_usd"] = round(entry["cost_usd"] + cost, 6)

    budgets = budget_mod.read_all(spend, data.get("cooldowns", {}))
    reserve = float(run.config.project.get("budget", {}).get("reserve_headroom", 0.15))
    advice = []
    for name, entry in budgets.items():
        if entry.cooldown_until:
            advice.append(f"{name} is cooling down; route elsewhere or defer")
        elif entry.known and entry.headroom is not None and entry.headroom < reserve:
            advice.append(f"{name} is below the {reserve:.0%} reserve — delegate rather than run work yourself")
    return _ok({
        "providers": {k: v.to_dict() for k, v in budgets.items()},
        "tree_usage": run.tree.rollup_usage(),
        "deferred_tasks": len(data.get("deferred", [])),
        "advice": advice or ["all providers have headroom"],
    })


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


def _reset() -> None:
    global _runner
    _runner = None


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
