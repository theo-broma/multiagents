"""Everything the monitor can *do*, in one place with one set of rules.

The monitor was asked for with full control: stop an agent, steer it, merge or
discard its branch, answer a question it is blocked on, launch the orchestrator.
That is real power reachable from a page, so the rules are stated here rather
than spread across two front ends that would each have to remember them:

* **Every action is a named function with a fixed signature**, so the web
  handler and the TUI reach the same code and cannot drift apart.
* **Destructive actions declare themselves.** `DESTRUCTIVE` is what a front end
  uses to decide what needs confirming; nothing is confirmed *here*, because a
  confirmation that lives in the layer being called is not a confirmation.
* **Nothing raises at the caller.** Every action returns
  ``{"ok": bool, "message": str, ...}`` — an action that blew up is a message
  in a UI, never a traceback in a server log nobody is reading.
* **Nothing here starts an agent.** Spawning is the orchestrator's job through
  MCP, with its depth, concurrency and budget checks; a button that bypassed
  all of that would be a second, worse scheduler.
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
from typing import Any, Callable

from ..config import load as load_config
from ..tree import Tree

# Actions a front end must confirm before calling. Losing an agent's work and
# rewriting the project's history are not undoable from here.
DESTRUCTIVE = {"discard_agent", "stop_all", "merge_agent"}

# Actions that spend somebody's quota. Not destructive, but not free either,
# and a UI should say so before it happens.
COSTS_MONEY = {"launch_orchestrator", "steer_agent"}


def _runner(paths):
    from ..runner import Runner
    return Runner(paths, load_config(paths))


def _await(coro) -> Any:
    """Run one coroutine to completion from a synchronous handler."""
    return asyncio.run(coro)


def _node_or_error(tree: Tree, agent_id: str):
    node = tree.get(agent_id)
    if node is None:
        return None, {"ok": False, "message": f"no agent {agent_id!r} in this tree"}
    return node, None


# --------------------------------------------------------------------------
# agents


def stop_agent(paths, agent_id: str = "", **_) -> dict:
    """Stop one agent, keeping everything it has done.

    The same stop the orchestrator can ask for: the process group goes, the
    branch, worktree and session id stay, so the work is inspectable and the
    agent is resumable rather than lost.
    """
    tree = Tree(paths.tree_file, paths.events_file)
    node, error = _node_or_error(tree, agent_id)
    if error:
        return error
    runner = _runner(paths)
    try:
        result = _await(runner.stop(agent_id))
    except Exception as exc:
        # A detached agent belongs to a server that is gone, and `stop` needs
        # the live Run object. This is the path that reaches it anyway.
        stopped = runner.stop_detached(node)
        return {"ok": stopped,
                "message": f"stopped {agent_id} (detached)" if stopped
                else f"could not stop {agent_id}: {type(exc).__name__}: {exc}"}
    return {"ok": True, "message": f"stopped {agent_id}", "result": result}


def steer_agent(paths, agent_id: str = "", message: str = "", **_) -> dict:
    """Send a running agent a mid-flight instruction."""
    if not message.strip():
        return {"ok": False, "message": "nothing to say"}
    tree = Tree(paths.tree_file, paths.events_file)
    _, error = _node_or_error(tree, agent_id)
    if error:
        return error
    try:
        result = _await(_runner(paths).steer(agent_id, message))
    except Exception as exc:
        return {"ok": False, "message": f"{type(exc).__name__}: {exc}"}
    return {"ok": not result.get("error"),
            "message": result.get("error") or result.get("detail")
            or f"steered {agent_id}",
            "result": result}


def merge_agent(paths, agent_id: str = "", into: str = "", **_) -> dict:
    """Merge an agent's branch into its parent's, or into the base branch."""
    try:
        result = _runner(paths).merge_agent(agent_id, into or None)
    except Exception as exc:
        return {"ok": False, "message": f"{type(exc).__name__}: {exc}"}
    # The runner reports a merge as a STATUS, not a boolean: "merged",
    # "conflict", "nothing to merge". Reading a missing "ok" key as True would
    # report a conflict as a success, which is the one outcome that must not be
    # quietly agreed with.
    status = result.get("result", "")
    return {"ok": status == "merged",
            "message": f"{status}: {result.get('detail', '')}".strip(": "),
            "result": result}


def discard_agent(paths, agent_id: str = "", force: bool = False, **_) -> dict:
    """Throw away an agent's branch and worktree. This is the undoable one."""
    try:
        result = _runner(paths).discard_agent(agent_id, force=bool(force))
    except Exception as exc:
        return {"ok": False, "message": f"{type(exc).__name__}: {exc}"}
    return {"ok": bool(result.get("discarded")),
            "message": result.get("error") or f"discarded {agent_id}",
            "result": result}


def push_branch(paths, agent_id: str = "", **_) -> dict:
    """Push one agent's branch to the configured remote.

    Publishing, so it is never a side effect of anything else — and it is
    refused outright when no remote is configured, rather than guessing one.
    """
    config = load_config(paths)
    if not config.remote:
        return {"ok": False,
                "message": "no git.remote is configured; nothing to push to"}
    try:
        result = _runner(paths).push_branch(agent_id or None)
    except Exception as exc:
        return {"ok": False, "message": f"{type(exc).__name__}: {exc}"}
    return {"ok": result.get("pushed", True) is not False,
            "message": result.get("error") or "pushed", "result": result}


# --------------------------------------------------------------------------
# questions and tickets


def answer_question(paths, question_id: str = "", answer: str = "", **_) -> dict:
    """Answer the question an agent is blocked on, and let it continue."""
    if not answer.strip():
        return {"ok": False, "message": "an empty answer would unblock nothing"}
    try:
        result = _await(_runner(paths).answer_question(question_id, answer))
    except Exception as exc:
        return {"ok": False, "message": f"{type(exc).__name__}: {exc}"}
    return {"ok": True, "message": f"answered {question_id}", "result": result}


def set_ticket(paths, ticket_id: str = "", status: str = "", note: str = "",
               **_) -> dict:
    """Move a ticket along: open, acknowledged, fixed, closed, wontfix."""
    tree = Tree(paths.tree_file, paths.events_file)
    if tree.get_ticket(ticket_id) is None:
        return {"ok": False, "message": f"no ticket {ticket_id!r}"}
    tree.set_ticket_status(ticket_id, status, note)
    return {"ok": True, "message": f"{ticket_id} is now {status}"}


# --------------------------------------------------------------------------
# the run itself


def resume_pause(paths, **_) -> dict:
    """Lift a pause by hand — a provider came back, or you fixed the account."""
    tree = Tree(paths.tree_file, paths.events_file)
    if not tree.pause_state():
        return {"ok": True, "message": "nothing was paused"}
    tree.resume("lifted from the monitor")
    return {"ok": True, "message": "resumed"}


def stop_all(paths, **_) -> dict:
    """`multiagents stop`: everything down, nothing lost.

    Run as the command rather than reimplemented, because that command already
    knows the whole shutdown order — drivers, agents, supervisors — and a second
    implementation of it would be one that gets a step wrong on the day it is
    needed.
    """
    return _command(paths, ["stop"], "stopped")


def launch_orchestrator(paths, unattended: int = 0, **_) -> dict:
    """Start the orchestrator, detached from this process.

    Detached deliberately: the monitor may be a web server with no terminal to
    give it, and an orchestrator holding a pipe nobody reads is the stall this
    project has already had once. `--unattended` is the headless loop, which is
    the only honest way to start one from a page.
    """
    argv = ["run", "--unattended", str(int(unattended) or 50)]
    return _command(paths, argv, "orchestrator started", detach=True)


def _command(paths, argv: list[str], done: str, detach: bool = False) -> dict:
    """Shell out to our own CLI. One implementation of each command, not two."""
    full = [sys.executable, "-m", "multiagents.cli", "--path", str(paths.root), *argv]
    try:
        if detach:
            log = paths.data / "monitor-launch.log"
            log.parent.mkdir(parents=True, exist_ok=True)
            handle = log.open("a")
            subprocess.Popen(full, stdout=handle, stderr=handle,
                             stdin=subprocess.DEVNULL, start_new_session=True)
            return {"ok": True, "message": f"{done}; output in {log}"}
        result = subprocess.run(full, capture_output=True, text=True, timeout=120)
        return {"ok": result.returncode == 0,
                "message": (result.stdout or result.stderr).strip()[-600:] or done}
    except Exception as exc:
        return {"ok": False, "message": f"{type(exc).__name__}: {exc}"}


def signal_process(paths, pid: int = 0, **_) -> dict:
    """Send SIGTERM to a pid the tree says is ours. Never an arbitrary one."""
    pid = int(pid or 0)
    tree = Tree(paths.tree_file, paths.events_file)
    known = {node.get("pid") for node in tree.read().get("nodes", {}).values()}
    if pid not in known or not pid:
        return {"ok": False, "message": f"pid {pid} is not an agent in this tree"}
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as exc:
        return {"ok": False, "message": f"could not signal {pid}: {exc}"}
    return {"ok": True, "message": f"SIGTERM sent to {pid}"}


# --------------------------------------------------------------------------
# config


def set_setting(paths, file: str = "", path: Any = (), value: Any = None,
                kind: str = "text", **_) -> dict:
    """Write one setting to the project layer, comments intact."""
    from . import settings

    keys = list(path) if isinstance(path, (list, tuple)) else str(path).split(".")
    try:
        result = settings.write(paths, file, keys,
                                settings.coerce(value, kind))
    except Exception as exc:
        return {"ok": False, "message": f"{type(exc).__name__}: {exc}"}
    return {"ok": True, "message": f"{result['key']} = {result['value']}",
            **result}


ACTIONS: dict[str, Callable[..., dict]] = {
    "stop_agent": stop_agent,
    "steer_agent": steer_agent,
    "merge_agent": merge_agent,
    "discard_agent": discard_agent,
    "push_branch": push_branch,
    "answer_question": answer_question,
    "set_ticket": set_ticket,
    "resume_pause": resume_pause,
    "stop_all": stop_all,
    "launch_orchestrator": launch_orchestrator,
    "signal_process": signal_process,
    "set_setting": set_setting,
}


def perform(paths, name: str, payload: dict) -> dict:
    """Run a named action. The only door in, for both front ends."""
    action = ACTIONS.get(name)
    if action is None:
        return {"ok": False, "message": f"unknown action {name!r}"}
    try:
        return action(paths, **{k: v for k, v in (payload or {}).items()
                                if k != "action"})
    except TypeError as exc:                  # a bad payload, not a bug
        return {"ok": False, "message": f"{name}: {exc}"}
    except Exception as exc:
        return {"ok": False, "message": f"{name} failed: {type(exc).__name__}: {exc}"}
