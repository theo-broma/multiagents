"""``multiagents`` — setup and housekeeping outside the MCP layer.

Some things are not agent work: first-time setup, refreshing the model list
after a subscription change, checking that the CLIs are installed and
authenticated, cleaning up worktrees after a crash. Doing those through an MCP
tool would mean starting a Claude session for housekeeping — and would leave you
with no way to diagnose the system when the MCP layer itself is what is broken.
"""

from __future__ import annotations

import contextlib
import signal
import asyncio
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import auth as auth_mod
from . import bugs
from . import catalog as catalog_mod
from . import scripts
from . import gitops
from .budget import read_all
from .config import load as load_config
from .config import seed_global, seed_project, sync_layer
from .models import refresh_models, validate_agent_models
from .paths import (ProjectPaths, find_project_root, global_config_dir,
                    known_projects, register_project, state_root)
from .providers import load_providers
from .runner import Runner
from .tree import Tree

GITIGNORE_LINE = ".multiagents/"


def _resolve(explicit: str | None = None) -> ProjectPaths:
    if explicit:
        paths = ProjectPaths(Path(explicit).expanduser().resolve())
    else:
        root = find_project_root()
        if root is None:
            print("No .multiagents/ found here or above. Run `multiagents init` first.",
                  file=sys.stderr)
            raise SystemExit(2)
        paths = ProjectPaths(root)
    # Refreshed on every command that names a project rather than only at init,
    # so `docker status --all` can resolve projects created before the registry
    # existed. A no-op once the entry is current.
    register_project(paths.root)
    return paths


def _mcp_config_path() -> Path:
    return global_config_dir() / "mcp.json"


def _write_mcp_config() -> Path:
    """Write the MCP registration the orchestrator alias points at."""
    path = _mcp_config_path()
    project = Path(__file__).resolve().parents[2]
    config = {
        "mcpServers": {
            "multiagents": {
                "type": "stdio",
                "command": "uv",
                "args": ["run", "--project", str(project), "python", "-m", "multiagents.server"],
            }
        }
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, indent=2) + "\n")
    return path


# --------------------------------------------------------------------------


def _launched_spec(config, role: str):
    """The roster entry launched by a given command, or None.

    Both the orchestrator and the initializer are launched rather than spawned;
    the role says which door they come through.
    """
    for spec in config.agents.values():
        if spec.launch and spec.role == role:
            return spec
    # An entry marked launch: true with no role still serves as the orchestrator,
    # so a config written before roles existed keeps working.
    if role == "orchestrator":
        return next((s for s in config.agents.values() if s.launch and not s.role), None)
    return None


def _launch_context(paths, config, spec) -> dict[str, str]:
    """Everything a provider's launch script needs, as environment.

    The orchestrator's brief is a normal agent instruction file, so it resolves
    through the same three config layers as every other agent's — write it out
    fresh each run so edits take effect without any copying step.
    """
    state = paths.data / "launch"
    state.mkdir(parents=True, exist_ok=True)
    prompt_file = state / "orchestrator-prompt.md"
    prompt_file.write_text(config.instructions_for(spec) or "")

    mcp_path = _write_mcp_config()
    server = json.loads(mcp_path.read_text())["mcpServers"]["multiagents"]
    return {
        "MULTIAGENTS_MODEL": spec.model,
        "MULTIAGENTS_PROMPT_FILE": str(prompt_file),
        "MULTIAGENTS_MCP_CONFIG": str(mcp_path),
        "MULTIAGENTS_MCP_COMMAND": server["command"],
        # \x1f so an argument containing spaces survives the round trip.
        "MULTIAGENTS_MCP_ARGS": "\x1f".join(server["args"]),
        "MULTIAGENTS_LAUNCH_STATE": str(state),
        "MULTIAGENTS_PROJECT": str(paths.root),
    }


# Sent as the opening message of a RESTARTED interactive session. Shorter and
# blunter than the headless nudge: a person is watching this one, and the first
# thing it needs to establish is what survived.
RESUME_PROMPT = (
    "Your previous session ended unexpectedly. This is a restart, not a new "
    "task, and not a decision point. Take stock first: agents left interrupted "
    "in the tree, open questions and tickets, and any branch holding a WIP "
    "commit made by the recovery — that work may be mid-edit and is not a "
    "finished result. Say briefly what you found, then carry straight on with "
    "the work. Do not propose a plan and wait for it to be approved, and do not "
    "ask whether to proceed; nobody may be reading. Stop for the user only "
    "where you would have stopped in any other session — a choice that is "
    "genuinely theirs to make."
)

NUDGE = (
    "Continue where you left off. Before anything else: read list_tickets and "
    "list_questions, answer what is within your remit, and check whether any "
    "agent finished while you were away. Keep the tree busy — start every piece "
    "of independent work you can before you wait. If the brief is genuinely "
    "complete and nothing is left to start, say so plainly and stop."
)


def _terminal_state():
    """Save the terminal's mode so an abnormal child exit cannot wedge it.

    With `exec` the shell cleans this up. Once we stay alive as the parent, a
    child that dies in raw mode leaves the terminal that way and the user gets
    an unusable shell.
    """
    import termios
    try:
        return termios.tcgetattr(sys.stdin.fileno())
    except Exception:
        return None


def _restore_terminal(saved) -> None:
    import termios
    if saved is None:
        return
    try:
        termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, saved)
    except Exception:
        pass


def _run_attached(argv, env) -> int:
    """Run the CLI as a child that owns the terminal, and return its exit code.

    Four things make this behave like the `exec` it replaces:

    * stdio is inherited and **no new session** is created, so the child stays
      in the terminal's foreground process group. A new session would leave it
      unable to read stdin — its first read would raise SIGTTIN and stop it.
    * SIGINT, SIGQUIT and SIGHUP get a do-nothing *handler* here rather than
      SIG_IGN.
      The distinction is the whole thing: SIG_IGN is inherited across exec, so
      ignoring them here made the child ignore them too and Ctrl-C stopped
      reaching the orchestrator entirely. A handler is reset to the default on
      exec, so the child gets normal behaviour while this process keeps waiting
      instead of dying first and orphaning it.
    * the terminal mode is saved and restored around the run.
    * nothing in this process reads stdin, or it would steal the child's keys.
    """
    saved = _terminal_state()
    previous = {}
    # SIGHUP included: when the terminal goes away it reaches the whole
    # foreground group, and a parent that dies with it can do none of the
    # deciding it exists to do. The child still gets the default disposition,
    # because a handler — unlike SIG_IGN — is reset on exec.
    for sig in (signal.SIGINT, signal.SIGQUIT, signal.SIGHUP):
        try:
            previous[sig] = signal.signal(sig, lambda *_: None)
        except (ValueError, OSError):
            pass
    try:
        child = subprocess.Popen(argv, env=env)          # NOT start_new_session
        while True:
            try:
                return child.wait()
            except KeyboardInterrupt:                    # belt and braces
                continue
    finally:
        for sig, handler in previous.items():
            with contextlib.suppress(ValueError, OSError):
                signal.signal(sig, handler)
        _restore_terminal(saved)


def _exit_was_deliberate(code: int) -> tuple[bool, str]:
    """Did a person end this, or did it end on its own?

    This is the whole reason `run` stops exec'ing: as a sibling process you only
    ever learn that the pid went away, and from outside `/exit` and a crash are
    identical. As the parent you get the code, and the three deliberate endings
    are exactly identifiable.
    """
    if code == 0:
        return True, "the session ended normally"
    if code in (-signal.SIGINT, 130):
        return True, "interrupted from the keyboard"
    if code in (-signal.SIGTERM, 143):
        return True, "terminated — `multiagents stop`, or something else asked it to end"
    if code in (-signal.SIGHUP, 129):
        # The terminal went away: a closed window, or a dropped connection.
        # Not deliberate, and the reason a relaunch has to be headless.
        return False, "the terminal was lost"
    return False, f"exited {code}"


def _start_supervisor(paths, role: str, pid: int) -> None:
    """Launch the watcher beside the orchestrator, detached.

    It has to be its own process: the next thing this one does is exec, and
    there would be nothing of ours left running. It exits by itself when the pid
    it watches disappears, so nothing needs to remember to stop it.
    """
    try:
        subprocess.Popen(
            [sys.executable, "-m", "multiagents.cli", "--path", str(paths.root),
             "supervise", "--role", role, "--pid", str(pid)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL, start_new_session=True,
        )
    except OSError as exc:                # observation must never block a launch
        print(f"supervisor   not started: {exc}", file=sys.stderr)


def _launch_agent(paths, config, role: str, resume: bool,
                  unattended: int = 0, supervise: bool = True) -> int:
    """Launch a roster entry as an interactive MCP client.

    Normally execs, so the CLI owns the terminal and this process is gone.
    Under `unattended` it spawns instead and supervises: a turn that ends —
    crash, quota, or the model simply stopping — is followed by another, which
    is the whole point of leaving it running overnight.
    """
    spec = _launched_spec(config, role)
    if spec is None:
        print(f"No agent in agents.yaml is marked `launch: true, role: {role}`.",
              file=sys.stderr)
        return 2
    providers = load_providers(config.providers)
    provider = providers.get(spec.provider)
    if provider is None or not provider.available():
        print(f"{role} provider {spec.provider!r} is unavailable", file=sys.stderr)
        return 2

    executor = _executor_for(paths, config, providers)(spec.provider)
    context = _launch_context(paths, config, spec)
    # Resuming is only possible if this role has been launched here before.
    # Passing --continue on a first run makes the CLI error out with no prior
    # conversation, which would make `run` fail exactly once per project.
    marker = paths.data / "launch" / f"{role}.launched"
    first_time = not marker.is_file()
    context["MULTIAGENTS_RESUME"] = "0" if (first_time or not resume) else "1"
    context["MULTIAGENTS_ROLE"] = role
    # `--fresh` means a new conversation, so it needs a new id: reusing one
    # that already has a transcript would collide with the session it names.
    context["MULTIAGENTS_SESSION_ID"] = _role_session_id(
        paths, role, rotate=not resume)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(str(time.time()))

    code, out, err = scripts.run_action(
        spec.provider, provider, executor, "prepare",
        global_config_dir(), paths.config, timeout=60, extra_env=context,
    )
    if code not in (0, scripts.UNIMPLEMENTED):
        print(f"prepare failed for {spec.provider}: {(err or out).strip()[:300]}",
              file=sys.stderr)
        return 1
    if out.strip():
        print(f"prepare      {out.strip()}")

    built = scripts.exec_action(spec.provider, provider, executor, "launch",
                                global_config_dir(), paths.config, extra_env=context)
    if built is None:
        print(f"no script for provider {spec.provider!r}", file=sys.stderr)
        return 2
    argv, env = built
    print(f"{role:12} {spec.provider}/{spec.model}"
          f"{'' if context['MULTIAGENTS_RESUME'] == '0' else ' (resuming)'}"
          f"{f' · unattended, up to {unattended} turns' if unattended else ''}\n")
    sys.stdout.flush()
    # Recorded before either path: a pid survives exec, so this file names the
    # CLI that replaces us, and under supervision it names the supervisor.
    # Without it `stop` can end the agents and leave the thing that starts more
    # of them running.
    _write_pid(paths, role, os.getpid())
    try:
        if unattended:
            return _supervise(paths, config, role, spec, provider, executor,
                              context, unattended)
        if not supervise:
            # Exec: this process is replaced, so a separate watcher is the only
            # way anything can report on the session.
            _start_supervisor(paths, role, os.getpid())
            os.execvpe(argv[0], argv, env)
        return _run_supervised(paths, config, role, spec, provider, executor,
                               context, argv, env)
    finally:
        _clear_pid(paths, role)
    return 0


def _run_supervised(paths, config, role, spec, provider, executor, context,
                    argv, env) -> int:
    """Hold the terminal for the CLI, and decide what to do when it ends.

    Staying alive as the parent buys exactly one thing, and it is the thing the
    detached watcher could not have: the exit code. A person quitting and a
    dropped connection look identical from outside and are unambiguous from
    here.
    """
    from . import watchdog

    _start_supervisor(paths, role, os.getpid())
    began = time.monotonic()
    code = _run_attached(argv, env)
    ran_for = time.monotonic() - began
    deliberate, why = _exit_was_deliberate(code)
    watchdog.write_status(paths, {
        "at": time.time(), "role": role, "pid": None, "running": False,
        "verdict": "stopped" if deliberate else "dropped", "detail": why,
        "transcript": None, "active_agents": 0, "provider": {},
    })

    if deliberate:
        print(f"\n{why}.")
        return 0 if code == 0 else 1

    # Terminal loss and a crash are not the same event and are not retried on
    # the same terms.
    #
    # A lost terminal — a closed laptop, a dropped connection — leaves a healthy
    # process that was killed by its environment. Restarting that is just
    # picking the session back up, and it is the case this exists for.
    #
    # A non-zero exit is an unhandled error inside the CLI. The advisor's
    # argument, which I take: by the time an error reaches the process boundary
    # the CLI has already exhausted whatever internal retry it has, so the
    # state that produced it is still there and a restart reads it again. Worse,
    # the obvious defence — "only retry if it survived a while" — does not hold:
    # a context-length overrun or an OOM parsing a huge payload takes minutes to
    # arrive and then repeats exactly. So crashes do not retry by default, and
    # the knob to change that is off.
    crash = code not in (-signal.SIGHUP, 129)
    if crash and not bool(config.limits.get("restart_on_crash", False)):
        print(f"\n{role} {why}. Not retrying: a non-zero exit is an error the "
              f"CLI could not\nhandle, so its cause is still there and a "
              f"restart would meet it again.\n`multiagents status` has the last "
              f"observation. Set limits.restart_on_crash\nto true if you want "
              f"it retried anyway.")
        return 1

    attempts = int(config.limits.get("restart_attempts", 5))
    delay = float(config.limits.get("restart_delay_seconds", 60))
    survived = float(config.limits.get("restart_min_runtime_seconds", 60))
    for attempt in range(1, attempts + 1):
        if not sys.stdin.isatty():
            break
        if crash and ran_for is not None and ran_for < survived:
            # Only reachable with restart_on_crash on. Even then, a failure this
            # fast is the same fault being read again.
            print(f"\n{role} {why} after only {ran_for:.0f}s. Stopping: a "
                  f"failure that fast is\nthe same fault being read again, not "
                  f"a passing one.")
            return 1
        print(f"\n{role} {why}. Retrying in {delay:.0f}s "
              f"({attempt}/{attempts}) — Ctrl-C to stop.")
        try:
            time.sleep(delay)
        except KeyboardInterrupt:
            print("\nstopped.")
            return 0

        held = _orchestrator_hold(paths, config)
        if held is not None:
            detail, resets_at = held
            print(f"\n{detail}")
            if not _wait_for_reset(paths, config, resets_at):
                return 3

        retry_env = {**env, "MULTIAGENTS_RESUME": "1",
                     "MULTIAGENTS_RESUME_PROMPT": RESUME_PROMPT}
        began = time.monotonic()
        code = _run_attached(argv, retry_env)
        ran_for = time.monotonic() - began
        deliberate, why = _exit_was_deliberate(code)
        if deliberate:
            print(f"\n{why}.")
            return 0 if code == 0 else 1

    if crash:
        print(f"\n{role} {why}. Not continuing: a crash leaves the state "
              f"unknown,\nand carrying on unattended would build on it. "
              f"`multiagents status` has\nthe last observation; `multiagents "
              f"run` starts again when you have looked.")
        return 1

    # A headless turn supplies the user message the TUI waits for you to type.
    # If nobody ever typed one, there is no work to continue and the nudge would
    # have it invent some from BRIEF.md, unsupervised.
    spoke = watchdog.has_human_turn(provider, paths.root)
    if spoke is False:
        print(f"\n{role} ended unexpectedly: {why}. Not continuing: nothing was "
              f"asked of it\nbefore the session ended, so there is no work to "
              f"carry on. `multiagents run`\nstarts a fresh one.")
        return 1
    if spoke is None:
        print("\n(could not check whether this session had been given any work)")

    print(f"\n{role} ended unexpectedly: {why}.")
    print("Carrying on headlessly — the terminal is gone, so an interactive")
    print("relaunch would have nowhere to run. `multiagents stop` ends it;")
    print("`multiagents status` says what it is doing.")
    # The unattended loop already waits out a quota reset, backs off on repeated
    # failure, and stops when two turns change nothing.
    return _supervise(paths, config, role, spec, provider, executor, context,
                      max_turns=int(config.limits.get("supervised_turns", 50)))


def _role_session_id(paths, role: str, rotate: bool = False) -> str:
    """A stable session id for this role, created once and kept.

    `--continue` resumes the most recent conversation *in the directory*, and
    both launched roles share the project root — so `init-agent` after `run`
    resumed the orchestrator's conversation. Naming the session removes the
    ambiguity entirely: each role owns one id for the life of the project.
    """
    import uuid as _uuid

    path = paths.data / "launch" / f"{role}.session"
    if not rotate:
        try:
            existing = path.read_text().strip()
            if existing:
                return existing
        except OSError:
            pass
    fresh = str(_uuid.uuid4())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(fresh)
    return fresh


def _pid_file(paths, role: str) -> Path:
    return paths.data / "launch" / f"{role}.pid"


def _write_pid(paths, role: str, pid: int) -> None:
    path = _pid_file(paths, role)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(pid))


def _clear_pid(paths, role: str) -> None:
    _pid_file(paths, role).unlink(missing_ok=True)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, ValueError):
        return False
    except PermissionError:
        return True                      # exists, owned by someone else
    return True


def _supervise(paths, config, role, spec, provider, executor,
               context: dict, max_turns: int) -> int:
    """Run the orchestrator turn after turn until there is nothing left to do.

    Each turn is a headless invocation of the same session: the provider's
    launch script adds its own non-interactive flag and the nudge below. This
    is deliberately NOT `exec` in a shell `until` loop — that construction stops
    when the command *succeeds*, so a turn that worked would end the run, and a
    turn that crashed would be retried forever.
    """
    tree = Tree(paths.tree_file, paths.events_file)
    idle_turns = 0
    failures = 0

    for turn in range(1, max_turns + 1):
        held = _orchestrator_hold(paths, config)
        if held is not None:
            detail, resets_at = held
            print(f"\n{detail}")
            if not _wait_for_reset(paths, config, resets_at):
                return 3

        before = _activity_fingerprint(tree)
        turn_env = {**context, "MULTIAGENTS_UNATTENDED": "1",
                    "MULTIAGENTS_NUDGE": NUDGE,
                    # After the first turn there is certainly a session to
                    # resume, whatever the launch marker said going in.
                    "MULTIAGENTS_RESUME": "1" if turn > 1 else context["MULTIAGENTS_RESUME"]}
        built = scripts.exec_action(spec.provider, provider, executor, "launch",
                                    global_config_dir(), paths.config,
                                    extra_env=turn_env)
        argv, env = built
        print(f"\n─── turn {turn}/{max_turns} "
              f"{time.strftime('%H:%M:%S')} " + "─" * 30)
        sys.stdout.flush()
        try:
            child = subprocess.Popen(argv, env=env)
            _write_pid(paths, f"{role}-turn", child.pid)
            code = child.wait()
            _clear_pid(paths, f"{role}-turn")
        except KeyboardInterrupt:
            print("\nstopped.")
            return 0

        # Failure first. A turn that crashed says nothing about whether work
        # remains, so counting it as "idle" would stop the run with the
        # reassuring message that everything was finished.
        if code != 0:
            failures += 1
            # Backoff, because a turn that fails instantly and is retried
            # instantly is a busy loop that spends quota on nothing.
            if failures >= 3:
                print(f"\nthree turns in a row failed (last exit {code}); stopping.")
                return 1
            delay = 30 * failures
            print(f"\nturn exited {code}; retrying in {delay}s "
                  f"({failures}/3 before giving up)")
            try:
                time.sleep(delay)
            except KeyboardInterrupt:
                print("\nstopped.")
                return 0
            continue

        failures = 0
        if _activity_fingerprint(tree) == before:
            idle_turns += 1
            print(f"\n(turn {turn} changed nothing in the tree"
                  f"{' — second in a row' if idle_turns > 1 else ''})")
            if idle_turns >= 2:
                print("nothing left to do; stopping.")
                return 0
        else:
            idle_turns = 0

    print(f"\nreached the {max_turns}-turn limit; stopping.")
    return 0


def _activity_fingerprint(tree) -> tuple:
    """Enough of the tree's state to tell whether a turn did anything.

    Event count moves on any agent activity; the node and merge counts catch a
    turn whose only product was starting or finishing work.
    """
    data = tree.read()
    nodes = data.get("nodes", {})
    events = tree.events_path.stat().st_size if tree.events_path.is_file() else 0
    return (events, len(nodes),
            sum(1 for n in nodes.values() if n.get("status") == "merged"),
            len(data.get("tickets", [])), len(data.get("questions", [])))


def cmd_init_agent(args: argparse.Namespace) -> int:
    """Shape the project with the initializer. Resumable by re-running."""
    paths = _resolve(args.path)
    config = load_config(paths)

    brief = paths.root / "BRIEF.md"
    context_dir = paths.root / "context"
    print(f"brief        {brief}{'' if brief.is_file() else '  (not written yet)'}")
    print(f"context      {context_dir}"
          f"{'' if context_dir.is_dir() else '  (not created yet)'}")
    if brief.is_file():
        print("             resuming — the initializer will re-read it")
    waiting = Tree(paths.tree_file, paths.events_file).open_questions()
    if waiting:
        print(f"\n{len(waiting)} question(s) still open; answer with `multiagents ask`")
    print()
    _report_catalog(config)

    # The same two checks `run` makes, for the same reason. The initializer is
    # told to consult the critic and the advisor, and a consult spawns an agent
    # — so on a docker project with no images it fails partway through a
    # conversation with the user, which is a confusing place to learn that
    # `build` has not been run.
    problems = _executor_problems(paths, config)
    for problem in problems:
        print(f"\nexecutor     {problem}")
    if problems:
        print("\nnot ready: the initializer consults other agents, and they run "
              "in a container\n           this project cannot build yet. Run "
              "`multiagents build` first.")
        return 4

    held = _orchestrator_hold(paths, config)
    if held is not None:
        detail, resets_at = held
        print(f"\n{detail}")
        if not getattr(args, "wait", False):
            print("             `multiagents init-agent --wait` blocks until it resets")
            return 3
        if not _wait_for_reset(paths, config, resets_at):
            return 3

    print()
    return _launch_agent(paths, config, "initializer", resume=args.resume)


def _ensure_authenticated(paths, config, providers, interactive: bool = True) -> int:
    """Check every enabled provider and offer to fix what is broken.

    Returns the number still unauthenticated. Login scripts are run as ordinary
    subprocesses with stdio inherited rather than exec'd, so several can be
    repaired in one pass — exec would replace this process at the first one.
    """
    executor_for = _executor_for(paths, config, providers)
    project_config = paths.config if paths else None
    enabled = {n: p for n, p in providers.items() if p.enabled}

    broken = []
    for name, provider in sorted(enabled.items()):
        state = auth_mod.check(name, provider, executor_for(name),
                               global_config_dir(), project_config)
        mark = "ok " if state.ok else "!! "
        print(f"  {mark}{name:10} {state.detail[:70]}")
        if not state.ok:
            broken.append((name, provider))

    if not broken:
        return 0
    if not interactive:
        for name, _ in broken:
            print(f"  fix: multiagents auth login {name}")
        return len(broken)

    still_broken = 0
    for name, provider in broken:
        print(f"\n{name} needs authenticating.")
        try:
            answer = input(f"  run `auth login {name}` now? [Y/n] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            answer = "n"
        if answer in ("n", "no"):
            print(f"  skipped — agents on {name} will fail until you run "
                  f"`multiagents auth login {name}`")
            still_broken += 1
            continue

        built = auth_mod.login_command(name, provider, executor_for(name),
                                       global_config_dir(), project_config)
        if built is None:
            print(f"  no script for {name!r}")
            still_broken += 1
            continue
        argv, env = built
        sys.stdout.flush()
        # stdio is inherited, so the script gets the terminal it needs.
        subprocess.run(argv, env=env)

        recheck = auth_mod.check(name, provider, executor_for(name),
                                 global_config_dir(), project_config)
        print(f"  {name}: {recheck.status}")
        if not recheck.ok:
            still_broken += 1
    return still_broken


def cmd_build(args: argparse.Namespace) -> int:
    """Prepare everything agents need: the container, then authentication."""
    paths = _resolve(args.path)
    config = load_config(paths)
    providers = load_providers(config.providers)

    if config.executor == "docker":
        ex = _docker_executor(paths)
        source = global_config_dir() / "docker"
        if not source.is_dir():
            shutil.copytree(Path(__file__).parent / "defaults" / "docker", source)
        for dockerfile, tag in (("Dockerfile", ex.image),
                                ("Dockerfile.proxy", ex.proxy_image)):
            if dockerfile == "Dockerfile.proxy" and ex.network_mode != "allowlist":
                continue
            if ex.image_exists(tag) and not args.rebuild:
                print(f"  have  {tag}")
                continue
            print(f"building {tag} ...")
            result = ex.build_image(source / dockerfile, tag)
            if not result["ok"]:
                print(result.get("output") or result.get("error"), file=sys.stderr)
                return 1
            print(f"  ok    {tag}")

        state = ex.ensure_running()
        if not state.get("ok"):
            print(state.get("error"), file=sys.stderr)
            return 1
        print(f"container    {ex.container} running ({ex.network_mode} networking)")
    else:
        print(f"executor     {config.executor} — no container to build")

    # Auth comes AFTER the container: a provider whose credentials live inside
    # it (agy) cannot be checked, let alone repaired, until it exists.
    print("\nauth")
    remaining = _ensure_authenticated(paths, config, providers,
                                      interactive=not args.no_auth)

    if remaining:
        print(f"\n{remaining} provider(s) still unauthenticated. Agents on them "
              f"will fail with an empty response.")
    else:
        print("\nready        multiagents run")
    return 0


def _report_catalog(config, provider: str = "opencode-go") -> int:
    """Print the catalog diff. Returns the count of roster-affecting changes."""
    result = catalog_mod.check(global_config_dir(), provider, config.agents)
    if not result.get("ok"):
        print(f"catalog      unreachable — {result.get('error','')}")
        return 0
    if result.get("first_run"):
        catalog_mod.apply(global_config_dir(), provider)
        print(f"catalog      baseline recorded ({result['models']} {provider} models)")
        return 0

    assessment = result.get("assessment", {})
    affecting = assessment.get("affecting_roster", [])
    if not result.get("changed"):
        print(f"catalog      up to date ({result['models']} {provider} models)")
        return 0

    print(f"catalog      {result['changed']} change(s) since "
          f"{result.get('fetched_at_local','?')}  [{assessment.get('severity')}]")
    for item in affecting:
        print(f"             {item['severity'].upper():8} {item['detail']}")
        print(f"                      used by: {', '.join(item['used_by'])}")
    unrelated = assessment.get("unrelated_changes", 0)
    if unrelated:
        print(f"             {unrelated} other change(s) not touching your roster")
    if affecting:
        print("             -> consult the critic before editing agents.yaml")
    print("             record the new baseline with `multiagents catalog --update`")
    return len(affecting)


# Entries that should almost never enter a first commit: credentials, and the
# generated directories that make a repository unusable. Matched against the
# top-level paths `git status` reports, so `node_modules/` is one entry rather
# than forty thousand.
UNSAFE_FIRST_COMMIT = (
    ".env", ".envrc", ".npmrc", ".netrc", "node_modules/", "venv/", ".venv/",
    "__pycache__/", "dist/", "build/", "target/", ".DS_Store", "secrets/",
    "credentials.json", "id_rsa", ".ssh/", ".aws/", ".gnupg/",
)


def _gitignore_add(root: Path, lines: list[str], header: str = "") -> list[str]:
    """Append the lines not already listed. Returns what it actually wrote."""
    path = root / ".gitignore"
    existing = path.read_text() if path.is_file() else ""
    present = set(existing.split())
    missing = [line for line in lines if line not in present]
    if not missing:
        return []
    with path.open("a") as handle:
        handle.write(("" if not existing or existing.endswith("\n") else "\n")
                     + (f"\n{header}\n" if header else "\n")
                     + "\n".join(missing) + "\n")
    return missing


def _confirm(question: str, default: bool = False) -> bool:
    """Ask a yes/no question, returning `default` when nobody can answer.

    `init` is run by hand, but also by `make init` and by scripts. A prompt that
    blocked on a closed stdin would hang a headless setup, so with no terminal
    every offer declines itself and the manual command is printed instead —
    exactly what this command did before it asked anything.
    """
    if not sys.stdin.isatty():
        return default
    try:
        answer = input(f"{question} {'[Y/n]' if default else '[y/N]'} ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return default
    return default if not answer else answer in ("y", "yes")


def _offer_git(root: Path) -> None:
    """Ensure the project is a repository with a commit to branch from.

    This is not a convenience. Without a repository ``runner._launch`` silently
    falls back to running every agent in the project directory itself — no
    branch, no worktree, no isolation between concurrent agents and no way to
    discard a bad run. Printing the command and hoping left that arrangement
    reachable by doing nothing, so init offers to close it.

    Called *after* ``.gitignore`` and ``context/`` are written, so a first commit
    made here excludes ``.multiagents/`` and includes the context directory.
    """
    if not gitops.is_repo(root):
        print("\ngit          not a repository")
        print("             agents work on their own branches, so this project needs one")
        if not _confirm("             run `git init` here now?"):
            print(f"             later: git -C {root} init && "
                  f"git -C {root} add -A && git -C {root} commit -m 'initial commit'")
            return
        result = gitops.init_repo(root)
        if not result.ok:
            print(f"             git init failed: {(result.err or result.out)[:200]}")
            return
        print(f"git          initialised {root}")

    if gitops.has_commits(root):
        print(f"git          {gitops.current_branch(root)} @ {gitops.head_sha(root)[:12]}")
        return

    # A worktree cannot be cut from nothing. An *empty* first commit satisfies
    # that and is still wrong for a project that already has files: agents check
    # out their branch and find none of them. So the first commit is the project.
    print("\ngit          repository has no commits")
    entries = gitops.uncommitted_entries(root)
    unsafe = [e for e in entries if e in UNSAFE_FIRST_COMMIT]
    if entries:
        print(f"             agents see only committed files; "
              f"{len(entries)} path(s) are uncommitted")
    if unsafe:
        print("             these look like they belong in .gitignore first:")
        for entry in unsafe:
            print(f"               {entry}")
        # Default yes, and asked before the commit: committing a .env because a
        # warning scrolled past is precisely the accident this exists to stop.
        # The isatty guard keeps a headless run from silently editing
        # .gitignore — it declines the commit below anyway.
        if sys.stdin.isatty() and _confirm("             add them to .gitignore now?",
                                           default=True):
            _gitignore_add(root, unsafe, "# added by multiagents init")
            print(f"             added to .gitignore: {', '.join(unsafe)}")
            entries = gitops.uncommitted_entries(root)

    question = ("             make an empty initial commit?" if not entries else
                "             commit them all as the initial commit?")
    if not _confirm(question):
        print(f"             later: git -C {root} add -A && "
              f"git -C {root} commit -m 'initial commit'")
        return
    result = gitops.initial_commit(root)
    if not result.ok:
        # Usually an unconfigured user.email, whose own message explains itself.
        for line in (result.err or result.out).splitlines()[:4]:
            print(f"             {line}")
        return
    print(f"git          {gitops.current_branch(root)} @ {gitops.head_sha(root)[:12]}")


DOCKER_DOCS = "https://docs.docker.com/engine/install/"
DOCKER_DESKTOP_DOCS = "https://docs.docker.com/desktop/"


def _docker_install_hint() -> list[str]:
    """How to install docker on *this* machine, best effort.

    Detected rather than generic: "see the docs" is what someone reads when
    they have already given up. The official page is given regardless, because
    a distro guess can be wrong and the hint has to be checkable.
    """
    import platform

    system = platform.system()
    if system == "Darwin":
        return [f"  brew install --cask docker      # or {DOCKER_DESKTOP_DOCS}"]
    if system == "Windows":
        return [f"  winget install Docker.DockerDesktop    # {DOCKER_DESKTOP_DOCS}"]

    distro = ""
    release = Path("/etc/os-release")
    if release.is_file():
        fields = {}
        for line in release.read_text().splitlines():
            if "=" in line:
                key, _, value = line.partition("=")
                fields[key] = value.strip().strip('"')
        distro = (fields.get("ID", "") + " " + fields.get("ID_LIKE", "")).lower()

    if any(name in distro for name in ("ubuntu", "debian", "mint", "pop")):
        return [
            "  curl -fsSL https://get.docker.com | sh      # official convenience script",
            "  sudo usermod -aG docker $USER               # then log out and back in",
        ]
    if any(name in distro for name in ("fedora", "rhel", "centos", "rocky", "alma")):
        return [
            "  sudo dnf install docker-ce docker-ce-cli containerd.io",
            "  sudo systemctl enable --now docker",
            "  sudo usermod -aG docker $USER               # then log out and back in",
        ]
    if "arch" in distro or "manjaro" in distro:
        return [
            "  sudo pacman -S docker",
            "  sudo systemctl enable --now docker",
            "  sudo usermod -aG docker $USER               # then log out and back in",
        ]
    if "suse" in distro:
        return ["  sudo zypper install docker",
                "  sudo systemctl enable --now docker"]
    return []


def _set_executor(paths, kind: str) -> bool:
    """Rewrite `executor.kind` in the project's own config, comments intact.

    A line edit rather than a YAML round trip: the shipped file is mostly
    comments explaining the choices, and dumping it back through a parser would
    throw all of them away.
    """
    import re

    config = paths.config / "project.yaml"
    if not config.is_file():
        return False
    text = config.read_text()
    updated, count = re.subn(r"(?m)^(executor:\n(?:[ \t]*(?:#[^\n]*)?\n)*[ \t]+kind:[ \t]*)\w+",
                             lambda m: m.group(1) + kind, text, count=1)
    if not count:
        return False
    config.write_text(updated)
    return True


def _offer_docker(paths) -> str:
    """Offer the container executor. Returns "" or a reason to come back later.

    Agents run with the permission flags that turn approval off — `--auto`,
    `--dangerously-skip-permissions`, `bypassPermissions`. On the local
    executor that is this user account, so which backend a project uses is a
    security decision and belongs to the person, asked once, at the point they
    are setting the project up.
    """
    from .executor.docker import docker_state

    current = load_config(paths).executor
    if current == "docker":
        print("executor     docker (already set)")
        return ""

    state, detail = docker_state()
    print("\nexecutor     local")
    print("             agents run with approval turned off — `--auto`,")
    print("             `--dangerously-skip-permissions`. On the local executor")
    print("             that is your user account, your files and your keys.")
    print("             docker confines them to a container with no route out.")

    if state == "ok":
        print(f"             docker is available here (server {detail})")
        # Asked only when someone is there to answer. `default=True` on a
        # closed stdin would let `make init` switch a project's execution
        # backend with nobody deciding — the opposite of the point, which is
        # that this choice belongs to a person.
        if sys.stdin.isatty() and _confirm(
                "             use the docker executor for this project?", default=True):
            if _set_executor(paths, "docker"):
                print("executor     docker   (run `multiagents build` before `run`)")
            else:
                print("             could not edit project.yaml; set executor.kind by hand")
            return ""
        print("             staying local — set executor.kind: docker in")
        print("             .multiagents/config/project.yaml to change your mind")
        return ""


    print("             docker is NOT usable here: " + detail)
    if state == "no-daemon":
        print("             the binary is installed but the daemon is unreachable —")
        print("             starting it, or adding yourself to the `docker` group,")
        print("             is usually all that is needed.")
    if not sys.stdin.isatty():
        print("             continuing on the local executor (nobody to ask).")
        return ""
    if _confirm("             continue without docker for now?", default=True):
        print("             continuing on the local executor. Agents will have the")
        print("             access you do; treat their tasks accordingly.")
        return ""
    return detail


def _refuse_nesting(root: Path, allow_nested: bool) -> str:
    """Why this directory must not become a project, or "".

    A project is exactly one git repository, rooted at that repository's root.
    Everything here rests on that: `create_worktree` runs
    `git -C <project root> worktree add`, and git resolves it to the
    REPOSITORY. So a project inside another one hands its agents full checkouts
    of the outer repository and a branch namespace shared with the outer
    project's agents, while counting concurrency, budget and watchdogs
    separately. It looks like it works, which is the problem.
    """
    if allow_nested:
        return ""
    outer = find_project_root(root)
    if outer is None or outer == root:
        return ""
    return (
        f"{outer} is already a multiagents project.\n"
        f"Agents branch and merge in one repository, so a second project inside "
        f"it would share that branch namespace while counting its own limits "
        f"separately.\n"
        f"Work on this directory from {outer}, or make it its own git "
        f"repository first. `--nested` overrides this."
    )


def cmd_init(args: argparse.Namespace) -> int:
    root = Path(args.path or ".").expanduser().resolve()
    refusal = _refuse_nesting(root, args.nested)
    if refusal:
        print(refusal, file=sys.stderr)
        return 2
    root.mkdir(parents=True, exist_ok=True)
    paths = ProjectPaths(root)
    paths.ensure()
    register_project(root)

    seed_global()
    seed_project(paths, force=args.force)

    print(f"project      {root}")
    print(f"data         {paths.data}")
    print(f"config       {paths.config}")
    print(f"worktrees    {paths.worktrees}")

    context_dir = root / "context"
    if not context_dir.exists():
        context_dir.mkdir(parents=True, exist_ok=True)
        (context_dir / "README.md").write_text(
            "# context\n\n"
            "Reference material agents need that is not code: requirements,\n"
            "specifications, links, API docs, design templates, screenshots,\n"
            "exported tickets, brand assets.\n\n"
            "This directory and `BRIEF.md` must be **committed**. Agents work in\n"
            "git worktrees — separate checkouts of their branch — so anything\n"
            "uncommitted or gitignored does not exist for them.\n\n"
            "`multiagents init-agent` fills both in, with you.\n"
        )
        print(f"context      {context_dir}  (created)")

    if _gitignore_add(root, [GITIGNORE_LINE], "# multiagents runtime state"):
        print(f"gitignore    added {GITIGNORE_LINE}")

    # After the two writes above, so a first commit made here picks them up.
    _offer_git(root)

    # Legitimate — a repository whose root is a parent you do not own — but the
    # user should know that every agent worktree will be a checkout of the
    # larger repository, and that branches land in its namespace.
    top = gitops.repo_root(root)
    if top is not None and top != root:
        print(f"\nnote         this is not the repository root")
        print(f"             repository: {top}")
        print(f"             agent worktrees will be full checkouts of it, and "
              f"branches\n             will land in its namespace")

    deferred = _offer_docker(paths)

    providers = load_providers(load_config(paths).providers)
    result = refresh_models(providers, paths.config / "models.yaml")
    for name, count in result["counts"].items():
        print(f"models       {name}: {count}")
    for name, problem in result["problems"].items():
        print(f"models       {name}: {problem}")

    _report_catalog(load_config(paths))

    mcp_path = _write_mcp_config()
    print(f"mcp config   {mcp_path}")
    print("\nNext:")
    print("  1. multiagents init-agent    shape the project (resumable)")
    print("  2. edit .multiagents/config/agents.yaml if you want a different roster")
    print("  3. multiagents build         container environment, if executor is docker")
    print("  4. multiagents run           launch the orchestrator")

    # Everything above ran; this only reports the state it ends in. Without a
    # repository and a commit no agent can be given a branch, so `run` will
    # refuse at the first spawn — and a scripted setup that read exit 0 here
    # would call this project ready. Non-zero is how CI finds out.
    if deferred:
        print(f"\nnot ready: you chose to wait for docker ({deferred}).")
        hints = _docker_install_hint()
        if hints:
            print("\nOn this system:")
            for line in hints:
                print(line)
        print(f"\nOfficial instructions: {DOCKER_DOCS}")
        print("\nThe project is set up and nothing is lost — re-run "
              "`multiagents init` once\ndocker works and it will offer again.")
        return 3

    if not (gitops.is_repo(root) and gitops.has_commits(root)):
        print("\nnot ready: no git repository with a commit, so no agent can be "
              "given a branch.\n             set one up and re-run `multiagents init`.")
        return 1
    return 0


def _executor_problems(paths, config) -> list[str]:
    """What would stop the FIRST delegation, checked before launching.

    The orchestrator itself runs on the host, so nothing about docker is
    exercised until it delegates — which meant a missing image surfaced as a
    failed spawn several minutes into a session, after the briefing, rather
    than as a refusal to start. `preflight` already knew; nobody asked it.
    """
    if config.executor != "docker":
        return []
    try:
        return _docker_executor(paths).preflight()
    except Exception as exc:                 # never block a launch on the check
        return [f"could not check the docker executor: {type(exc).__name__}: {exc}"]


def _orchestrator_hold(paths, config) -> tuple[str, float | None] | None:
    """Is the orchestrator's own provider out of headroom? `(why, until)`.

    Checked before launching rather than after failing, because a CLI that has
    run out of quota reports it as an ordinary error with no reset time in it,
    and the user is left guessing whether the install is broken.
    """
    spec = _launched_spec(config, "orchestrator")
    if spec is None:
        return None
    providers = load_providers(config.providers)
    provider = providers.get(spec.provider)
    if provider is None:
        return None
    executor_for = _executor_for(paths, config, providers)
    budgets = read_all({spec.provider: provider}, executor_for,
                       global_config_dir(), paths.config, {}, {}, use_cache=False)
    budget = budgets.get(spec.provider)
    if budget is None or budget.usable:
        return None
    when = f", resets {budget.resets_at}" if budget.resets_at else ""
    return (f"paused       {spec.provider} has no headroom for the "
            f"orchestrator{when}", budget.cooldown_until)


def _wait_for_reset(paths, config, until: float | None, poll: int = 120) -> bool:
    """Block until the orchestrator's provider has headroom again."""
    print("             waiting — Ctrl-C to stop\n")
    while True:
        try:
            time.sleep(poll)
        except KeyboardInterrupt:
            print("\nstopped waiting.")
            return False
        from .budget import invalidate_cache
        invalidate_cache()
        if _orchestrator_hold(paths, config) is None:
            print("headroom is back; launching.\n")
            return True
        stamp = time.strftime("%H:%M:%S")
        print(f"  {stamp}  still no headroom")


def _reaper(paths):
    """A Runner used only to reach detached processes. Built once, lazily."""
    if not hasattr(_reaper, "_cache") or _reaper._cache[0] != paths.root:
        _reaper._cache = (paths.root, Runner(paths, load_config(paths)))
    return _reaper._cache[1]


def _alive_pid(paths, role: str) -> bool:
    path = _pid_file(paths, role)
    try:
        return _alive(int(path.read_text().strip()))
    except (OSError, ValueError):
        return False


INTERRUPTED_COMMIT = (
    "WIP: {agent} interrupted before it finished ({agent_id})\n\n"
    "Committed by multiagents so the work is not lost, NOT by the agent. The "
    "tree may be mid-edit and syntactically broken through no fault of the "
    "agent — treat it as a checkpoint to inspect, never as a finished result."
)


def _save_interrupted(node) -> bool:
    """Commit an interrupted agent's worktree, marked as what it is.

    A cancelled node is TERMINAL, and `clean --branches` removes worktrees for
    terminal nodes — so "leave it dirty, worktrees are disposable" would mean
    the next routine cleanup deletes it with no record it existed.

    The message matters as much as the commit. Without it the orchestrator can
    pick the branch up later, run tests against half-written files, and spend
    tokens debugging syntax errors caused entirely by the termination.
    """
    worktree = Path(node.worktree) if node.worktree else None
    if not worktree or not worktree.is_dir() or not gitops.is_repo(worktree):
        return False
    if not gitops.is_dirty(worktree):
        return False
    result = gitops.commit_all(worktree, INTERRUPTED_COMMIT.format(
        agent=node.agent, agent_id=node.id))
    return bool(result.ok)


def cmd_resume(args: argparse.Namespace) -> int:
    """Reconcile state after a crash or restart, then reopen the session."""
    paths = _resolve(args.path)
    tree = Tree(paths.tree_file, paths.events_file)

    # Agents are started with start_new_session=True, so that stopping one also
    # stops the shells and test runners beneath it — which means they are in
    # their OWN session and do NOT die with the server. On a clean teardown the
    # cancellation handler kills each group; after a crash nothing runs, and
    # they keep going with their output going nowhere. So this reconciles two
    # different states: processes that are gone, and processes that should be.
    orchestrator_live = _alive_pid(paths, "orchestrator")
    reclaimed, reaped, saved = 0, 0, 0
    for node in tree.active():
        alive = False
        if node.pid:
            try:
                os.kill(node.pid, 0)
                alive = True
            except (ProcessLookupError, PermissionError):
                alive = False
        if alive and not orchestrator_live:
            # An orphan: nobody is reading its stream, so it is spending tokens
            # into a closed pipe. Leaving it running would also let it mutate a
            # worktree the next session is about to work in.
            try:
                _reaper(paths).stop_detached(node)
                reaped += 1
            except Exception as exc:
                print(f"  could not stop {node.id}: {exc}", file=sys.stderr)
        elif alive:
            continue                        # another session owns it
        tree.set_status(node.id, "orphaned",
                        "reaped: left running by a server that is gone" if alive
                        else "process gone; server restarted")
        reclaimed += 1
        # Committed HERE rather than in the cancellation handler: this runs with
        # time, a live loop and full information, where a teardown has none of
        # those and a git call in it can hang for its whole timeout.
        if _save_interrupted(node):
            saved += 1

    print(tree.render())
    if reclaimed:
        print(f"\nreclaimed    {reclaimed} agent(s) whose process no longer exists")
    if reaped:
        print(f"reaped       {reaped} orphaned agent process(es) still running")
    if saved:
        print(f"committed    interrupted work in {saved} worktree(s)")

    data = tree.read()
    stale = [
        n for n in data["nodes"].values()
        if n.get("branch") and n.get("status") in {"orphaned", "stuck", "done", "failed"}
    ]
    if stale:
        print(f"\n{len(stale)} branch(es) still held by unfinished agents:")
        for node in stale:
            commits = gitops.commits_on(
                paths.root, node["branch"], gitops.current_branch(paths.root)
            )
            print(f"  {node['id']:10} {node['agent']:14} {node['branch']:38} {commits} commit(s)")
        print("  merge with merge_agent, or drop with `multiagents clean --branches`")

    if data.get("deferred"):
        print(f"\n{len(data['deferred'])} task(s) deferred on quota")

    waiting = tree.open_questions()
    if waiting:
        # Reported, never a refusal: the first place a question should be
        # answered is inside the orchestrator, so refusing to launch it would
        # make the cheaper path unreachable.
        print(f"\n{len(waiting)} agent(s) waiting on a decision:")
        for q in waiting[:5]:
            print(f"  {q['id']}  {q['agent']} · {q['topic']}: {q['question'][:70]}")
        print("  the orchestrator can answer these, or use `multiagents ask`")

    pause = tree.pause_state()
    if pause:
        waiting = max(0, int(pause["until"] - time.time()))
        print(f"\npaused       {pause.get('reason', 'no provider available')}")
        print(f"             clears in about {waiting // 60}m; deferred work "
              f"restarts by itself")

    config = load_config(paths)

    problems = _executor_problems(paths, config)
    for problem in problems:
        print(f"\nexecutor     {problem}")

    if args.no_launch:
        return 0

    if problems:
        print("\nnot ready: this project runs agents in a container and the "
              "container cannot be\n           built from what is here. The "
              "orchestrator would start fine and fail\n           at its first "
              "delegation.")
        return 4

    # The orchestrator spends the same bucket the user's own session does when
    # it is pinned to `claude`. Launching into an exhausted one produces a CLI
    # error with no reset time in it, which reads as a broken install.
    held = _orchestrator_hold(paths, config)
    if held is not None:
        detail, resets_at = held
        print(f"\n{detail}")
        if not args.wait:
            print("             `multiagents run --wait` blocks until it resets")
            return 3
        if not _wait_for_reset(paths, config, resets_at):
            return 3

    # The catalog baseline is the initializer's concern; re-checking it on every
    # orchestrator launch spends a network round trip on ground that rarely
    # moves. The orchestrator calls check_model_catalog when something suggests
    # it has.
    return _launch_agent(paths, config, "orchestrator", resume=args.resume,
                         unattended=getattr(args, "unattended", 0),
                         supervise=getattr(args, "supervise", True))


def _report_agents(config, providers) -> int:
    """The `doctor` roster section. Returns the number of problems found.

    Extracted so the checks can be tested without a whole project on disk:
    every one of them is about a config file the user edits by hand, which is
    exactly where a silent mistake is most likely.
    """
    problems = 0
    for name, spec in sorted(config.agents.items()):
        provider = providers.get(spec.provider)
        mark = " " if provider and provider.available() else "!"
        if spec.launch:
            command = "init-agent" if spec.role == "initializer" else "run"
            tag = f"[launched by `multiagents {command}`]"
        else:
            where = spec.executor or config.executor
            tag = f"[{where}]" + ("*" if spec.executor else "")
        print(f"  {mark} {name:18} {spec.provider}/{spec.model:30} {tag}")
        instructions = config.instructions_for(spec)
        if spec.instructions and not instructions.strip():
            print(f"    missing instructions file: {spec.instructions}")
            problems += 1
        # A permission name the provider does not define adds NO flags at all,
        # which is not a safe default: agy then auto-denies every tool and
        # returns an empty answer, so the agent looks broken rather than
        # misconfigured. Silent until now, hence its own line.
        profiles = (provider.spawn.get("permission") or {}) if provider else {}
        if profiles and spec.permission not in profiles:
            print(f"    unknown permission {spec.permission!r} for {spec.provider} — "
                  f"choose one of {', '.join(sorted(profiles))}")
            problems += 1

    for warning in validate_agent_models(config):
        print(f"    {warning}")
    if any(spec.executor for spec in config.agents.values()):
        print("    * pinned to an executor in agents.yaml, overriding the project default")
    return problems


def cmd_doctor(args: argparse.Namespace) -> int:
    paths = _resolve(args.path) if find_project_root() else None
    config = load_config(paths)
    providers = load_providers(config.providers)
    problems = 0

    print("providers")
    for name, provider in sorted(providers.items()):
        path = provider.available()
        state = "" if provider.enabled else "  [disabled in providers.yaml]"
        if path:
            print(f"  {name:12} {path}{state}")
        elif not provider.enabled:
            # Disabled and absent is not a problem worth flagging — the user
            # said they do not want it.
            print(f"  {name:12} not installed{state}")
        else:
            print(f"  {name:12} NOT FOUND ({provider.bin} is not on PATH)")
            problems += 1

    print("\nagents")
    problems += _report_agents(config, providers)

    print("\nauth")
    providers_map = load_providers(config.providers)
    executor_for = _executor_for(paths, config, providers_map)
    for name, state in sorted(auth_mod.check_all(
            providers_map, executor_for, global_config_dir(),
            paths.config if paths else None).items()):
        mark = " " if state.ok else "!"
        print(f"  {mark} {name:10} {state.status:18} {state.detail[:60]}")
        if not state.ok:
            problems += 1
            if state.fix:
                print(f"      fix: {state.fix}")

    print("\nbudget")
    _budget = read_all(providers_map, executor_for, global_config_dir(),
                       paths.config if paths else None)
    for name, entry in _budget.items():
        data = entry.to_dict()
        if data.get("known"):
            print(f"  {name:12} {data['used_percent']}% used, resets {data.get('resets_at','?')}")
            # Which bucket is the constraint changes what to do about it: a
            # rolling window clears in hours, a monthly one does not.
            for window, detail in sorted((data.get("windows") or {}).items()):
                print(f"  {'':12}   {window:8} {detail.get('percent', '?'):>5}%  "
                      f"resets {str(detail.get('resets_at', '?'))[:19]}")
        else:
            print(f"  {name:12} headroom unknown — {data.get('note','')}")
    health = Tree(paths.tree_file, paths.events_file).provider_health() if paths else {}
    for name, record in sorted(health.items()):
        if record.get("tripped"):
            print(f"  !! {name:9} stopped after {record['consecutive_failures']} "
                  f"consecutive failures: {record.get('last_reason','')[:70]}")
            problems += 1

    if paths:
        print("\nproject")
        print(f"  root       {paths.root}")
        print(f"  executor   {config.executor}")
        print(f"  home       {config.home_policy}")
        print(f"  remote     {config.remote or '(none — local commits only)'}")
        repo_ok = gitops.is_repo(paths.root) and gitops.has_commits(paths.root)
        print(f"  git        {'ok' if repo_ok else 'NOT READY (needs a repo with at least one commit)'}")
        if not repo_ok:
            problems += 1
        blocked = set(config.env_block)
        leaking = [k for k in config.env_passthrough if k not in blocked]
        print(f"  env        passthrough={leaking or 'none'} blocked={len(blocked)} vars")

    print(f"\n{'ok' if not problems else str(problems) + ' problem(s)'}")
    return 1 if problems else 0


def cmd_refresh_models(args: argparse.Namespace) -> int:
    paths = _resolve(args.path) if find_project_root() else None
    config = load_config(paths)
    target = (paths.config if paths else global_config_dir()) / "models.yaml"
    result = refresh_models(load_providers(config.providers), target)
    print(f"written {result['written']}")
    for name, count in result["counts"].items():
        print(f"  {name:12} {count} models")
    for name, problem in result["problems"].items():
        print(f"  {name:12} {problem}")
    return 0


def cmd_tree(args: argparse.Namespace) -> int:
    paths = _resolve(args.path)
    print(Tree(paths.tree_file, paths.events_file).render())
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    """Tail the global event log — the out-of-band view of a running tree."""
    paths = _resolve(args.path)
    path = paths.events_file
    print(f"watching {path}  (ctrl-c to stop)")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(exist_ok=True)
    with path.open() as handle:
        handle.seek(0, 2)
        try:
            while True:
                line = handle.readline()
                if not line:
                    time.sleep(0.5)
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                stamp = time.strftime("%H:%M:%S", time.localtime(event.get("t", 0)))
                extra = " ".join(
                    f"{k}={v}" for k, v in event.items()
                    if k not in {"t", "agent", "kind"} and v not in (None, "", [])
                )
                print(f"{stamp} {event.get('agent','-'):10} {event.get('kind',''):10} {extra}")
        except KeyboardInterrupt:
            return 0


def cmd_probe(args: argparse.Namespace) -> int:
    """Run a trivial task through a provider and report how its stream parsed.

    The tool for adding an integration: it shows which rules matched and, more
    usefully, which lines fell through to `raw` and still need a rule.
    """
    paths = _resolve(args.path) if find_project_root() else None
    config = load_config(paths)
    providers = load_providers(config.providers)
    provider = providers.get(args.provider)
    if provider is None:
        print(f"unknown provider {args.provider!r}; known: {sorted(providers)}", file=sys.stderr)
        return 2
    if not provider.available():
        print(f"{provider.bin} is not on PATH", file=sys.stderr)
        return 2

    argv = provider.build_command(
        prompt=args.prompt, model=args.model, workdir=str(Path.cwd()), permission="readonly",
    )
    print(f"$ {' '.join(argv[:6])} …\n")
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=args.timeout)

    counts: dict[str, int] = {}
    unmatched: list[str] = []
    for line in proc.stdout.splitlines():
        event = provider.parse_line(line)
        if event is None:
            continue
        counts[event.kind] = counts.get(event.kind, 0) + 1
        if event.kind == "raw":
            unmatched.append(line[:200])

    print("parsed events")
    for kind, count in sorted(counts.items()):
        print(f"  {kind:10} {count}")
    if unmatched:
        print(f"\n{len(unmatched)} unmatched line(s) — add rules for these:")
        for line in unmatched[:8]:
            print(f"  {line}")
    else:
        print("\nevery line matched a rule")
    if proc.returncode != 0:
        print(f"\nexit {proc.returncode}: {proc.stderr[-400:]}")
    return 0


def _machine_state() -> list[Path]:
    """The two directories a teardown removes: config layer and state root."""
    return [global_config_dir(), state_root()]


def _worktree_survey() -> tuple[list[dict], set[Path]]:
    """`(worktrees with uncommitted work, repositories that register them)`.

    Both have to be collected before anything is deleted. The dirty check is
    the only thing standing between `rm -rf` and work that exists nowhere else
    — a commit survives in its repository as a branch, an uncommitted edit does
    not. The repository set is needed afterwards, and cannot be recovered once
    the `.git` files that name it are gone.
    """
    root = state_root() / "worktrees"
    dirty, repos = [], set()
    if not root.is_dir():
        return dirty, repos
    for worktree in sorted(root.glob("*/*")):
        if not worktree.is_dir():
            continue
        owner = gitops.owning_repo(worktree)
        if owner is not None:
            repos.add(owner)
        changed = gitops.run(worktree, "status", "--porcelain")
        if changed.ok and changed.out.strip():
            dirty.append({"path": worktree,
                          "files": len(changed.out.strip().splitlines()),
                          "repo": owner})
    return dirty, repos


def cmd_uninstall(args: argparse.Namespace) -> int:
    """Remove this machine's global config and agent state.

    Per-project `.multiagents/` directories, your repositories and the branches
    agents committed to are all left alone — only machine-level state goes.
    """
    targets = [p for p in _machine_state() if p.exists()]
    dirty, repos = _worktree_survey()

    for path in targets:
        print(f"would remove  {path}")
    if not targets:
        print("nothing to remove; this machine has no multiagents state.")
        return 0
    print(f"registered in {len(repos)} repository(ies), which will be pruned")

    if dirty:
        print(f"\n{len(dirty)} worktree(s) hold uncommitted work:")
        for entry in dirty:
            print(f"  {entry['files']:3} file(s)  {entry['path']}")
        print("\nCommitted work survives — it lives in its repository as a branch.")
        print("These changes do not. Commit or copy them, or pass --force.")
        if not args.force:
            return 1

    if args.dry_run:
        print("\ndry run; nothing was removed.")
        return 0
    if not args.force and not _confirm("\nremove them?"):
        print("cancelled.")
        return 0

    for path in targets:
        shutil.rmtree(path, ignore_errors=True)
        print(f"removed  {path}")
    # After deletion, not before: git only drops a registration once the
    # directory is actually gone, so pruning first would be a no-op and leave
    # every repository listing worktrees that no longer exist.
    pruned = 0
    for repo in sorted(repos):
        if gitops.is_repo(repo):
            gitops.prune_worktrees(repo)
            pruned += 1
    print(f"pruned   stale worktree registrations in {pruned} repository(ies)")
    print("\nPer-project .multiagents/ directories are untouched.")
    return 0


def cmd_clean(args: argparse.Namespace) -> int:
    paths = _resolve(args.path)
    tree = Tree(paths.tree_file, paths.events_file)
    data = tree.read()
    removed = 0

    gitops.prune_worktrees(paths.root)

    if args.branches:
        base = gitops.current_branch(paths.root)
        for node in data["nodes"].values():
            branch = node.get("branch")
            if not branch or node.get("status") in {"running", "pending"}:
                continue
            commits = gitops.commits_on(paths.root, branch, base)
            if commits and not args.force:
                print(f"keep   {branch} ({commits} unmerged commit(s); --force to delete)")
                continue
            worktree = node.get("worktree")
            if worktree and Path(worktree).is_dir():
                gitops.remove_worktree(paths.root, Path(worktree), force=True)
            gitops.delete_branch(paths.root, branch, force=True)
            print(f"delete {branch}")
            removed += 1

    if args.tree:
        from multiagents.tree import TERMINAL
        with tree.transaction() as state:
            drop = [
                nid for nid, n in state["nodes"].items()
                if n.get("status") in TERMINAL and not n.get("branch")
                and not n.get("conversation")
            ]
            for nid in drop:
                state["nodes"].pop(nid, None)
            for n in state["nodes"].values():
                n["children"] = [c for c in n.get("children", []) if c not in drop]
        print(f"pruned {len(drop)} finished node(s) holding no branch")
        removed += len(drop)

    if args.homes:
        for home in paths.homes.glob("ag-*"):
            node = data["nodes"].get(home.name)
            if node and node.get("status") in {"running", "pending"}:
                continue
            shutil.rmtree(home, ignore_errors=True)
            removed += 1

    print(f"\n{removed} item(s) removed")
    return 0


def cmd_catalog(args: argparse.Namespace) -> int:
    paths = _resolve(args.path) if find_project_root() else None
    config = load_config(paths)
    if args.update:
        result = catalog_mod.apply(global_config_dir(), args.provider)
        print(result.get("written") or result.get("error"))
        return 0 if result.get("ok") else 1
    return 0 if _report_catalog(config, args.provider) == 0 else 0


def _docker_executor(paths):
    from .executor.docker import DockerExecutor
    config = load_config(paths)
    return DockerExecutor(
        config.project.get("executor", {}).get("docker", {}),
        paths,
        load_providers(config.providers),
        global_config_dir(),
    )


def _executor_for(paths, config, providers):
    """An executor per provider, honouring any per-agent pin for that provider.

    Auth differs by where the CLI runs: opencode keeps credentials on the host
    even under docker, while agy needs a login inside the container.
    """
    from .executor import get_executor
    from .paths import global_config_dir as _gcd

    def build(provider_name: str):
        kind = config.executor
        for spec in config.agents.values():
            if spec.provider == provider_name and spec.executor:
                kind = spec.executor
                break
        return get_executor(
            kind, config.project.get("executor", {}).get("docker", {}),
            paths=paths, providers=providers, config_dir=_gcd(),
        )
    return build


def _auth_scope(provider, executor) -> str:
    """Where this provider's credentials actually live.

    Not the same as where its agents run: opencode keeps credentials on the
    host even under docker, because the container mounts its data directory
    rather than masking it. Only a provider that declares
    container_private_home authenticates inside the container.
    """
    if getattr(executor, "kind", "local") == "docker" and \
       getattr(provider, "container_private_home", None):
        return "container"
    return "host"


def cmd_upgrade_config(args: argparse.Namespace) -> int:
    """Refresh config copies that were never edited.

    Pinned copies override the shipped defaults for every key, so without this
    an install silently never receives improvements to files it is not using.
    Edited files are left alone and reported.
    """
    from .paths import shipped_defaults_dir
    paths = _resolve(args.path) if find_project_root() else None
    verb = "would " if args.dry_run else ""

    targets = [("global", shipped_defaults_dir(), global_config_dir(), "global")]
    if paths is not None:
        targets.append(("project", global_config_dir(), paths.config, "project"))
    if args.layer != "both":
        targets = [t for t in targets if t[0] == args.layer]

    stale = 0
    for label, source, target, scope in targets:
        report = sync_layer(source, target, force=args.force,
                            dry_run=args.dry_run, scope=scope)
        print(f"{label}  {target}")
        for name in report["added"]:
            print(f"  {verb}add     {name}")
        for name in report["updated"]:
            print(f"  {verb}refresh {name}   (unmodified copy)")
        for name in report["customised"]:
            stale += 1
            print(f"  keep    {name}   (you edited it; shipped version has changed)")
        for path in report.get("backed_up", []):
            print(f"  backup  {path}")
        if not any(report.values()):
            print("  up to date")
    if stale:
        print(f"\n{stale} customised file(s) left alone. Compare them against")
        print(f"  {shipped_defaults_dir()}")
        print("and merge by hand, or pass --force to overwrite.")
    return 0


def cmd_auth(args: argparse.Namespace) -> int:
    paths = _resolve(args.path) if find_project_root() else None
    config = load_config(paths)
    providers = load_providers(config.providers)
    project_config = paths.config if paths else None
    executor_for = _executor_for(paths, config, providers)

    if args.action == "login":
        name = args.provider
        provider = providers.get(name)
        if provider is None:
            print(f"unknown provider {name!r}; known: {sorted(providers)}", file=sys.stderr)
            return 2
        built = auth_mod.login_command(
            name, provider, executor_for(name), global_config_dir(), project_config)
        if built is None:
            print(f"no auth script for {name!r}. Add one to "
                  f"{global_config_dir()}/auth/{name}.sh — see auth/README.md.",
                  file=sys.stderr)
            return 2
        argv, env = built
        sys.stdout.flush()
        os.execvpe(argv[0], argv, env)

    states = auth_mod.check_all(providers, executor_for, global_config_dir(), project_config)
    broken = 0
    for name, state in sorted(states.items()):
        where = _auth_scope(providers[name], executor_for(name))
        mark = "ok " if state.ok else ("!! " if state.status == "not_authenticated" else "?  ")
        print(f"  {mark}{name:10} [{where:9}] {state.detail}")
        if not state.ok:
            broken += 1
            if state.fix:
                print(f"      fix: {state.fix}")
    print()
    print("all providers authenticated" if not broken
          else f"{broken} provider(s) need attention")
    return 1 if broken else 0


def cmd_ask(args: argparse.Namespace) -> int:
    """Answer questions agents have parked on.

    Deliberately WRITE-ONLY. Resuming an agent means owning the asyncio task
    that reads its stdout; this process exits immediately afterwards, which
    would leave the agent running with nobody draining its pipe — it would fill
    and deadlock. So the answer is recorded here, and the agent is resumed by
    whichever process owns a live runner: the orchestrator, next time it calls
    list_questions, answer_question, wait_for_agents or check_agent.
    """
    paths = _resolve(args.path)
    tree = Tree(paths.tree_file, paths.events_file)
    questions = tree.open_questions(args.agent or None)

    if not questions:
        print("Nothing is waiting on a decision.")
        return 0

    if args.list:
        for q in questions:
            waited = (time.time() - q["asked_at"]) / 60
            print(f"  {q['id']}  {q['agent']} · {q['topic']}   asked {waited:.0f}m ago")
            print(f"      {q['question']}")
            if q.get("proposed_default"):
                print(f"      its default: {q['proposed_default']}")
        return 0

    answered = 0
    for q in questions:
        if args.question_id and q["id"] != args.question_id:
            continue
        waited = (time.time() - q["asked_at"]) / 60
        print(f"\n[{q['id']}] {q['agent']} · {q['topic']}   asked {waited:.0f}m ago")
        print(f"  {q['question']}")
        if q.get("proposed_default"):
            print(f"  it would otherwise choose: {q['proposed_default']}")
        try:
            reply = input("  your answer (blank to skip, 'default' to accept its own): ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not reply:
            continue
        if reply == "default" and q.get("proposed_default"):
            reply = q["proposed_default"]
        tree.answer_question(q["id"], reply, answered_by="user")
        answered += 1
        print(f"  recorded. {q['agent']} resumes when the orchestrator next checks.")

    if answered:
        print(f"\n{answered} answered. Run `multiagents run` if no orchestrator is live.")
    return 0


def cmd_usage(args: argparse.Namespace) -> int:
    """Where this project's tokens and dollars actually went.

    Per provider/model rather than per provider, because that is the level at
    which a pin is a decision: no provider reports it, but we parse every
    stream, so it is ours to compute.
    """
    paths = _resolve(args.path)
    rows = Tree(paths.tree_file, paths.events_file).usage_by_model()
    if not rows:
        print("No usage recorded yet.")
        return 0

    total_cost = sum(r["cost_usd"] for r in rows)
    total_tokens = sum(r["tokens"] for r in rows)
    print(f"{'provider/model':42} {'runs':>4} {'tokens':>12} {'cost':>9}  share")
    for r in rows:
        share = (100 * r["cost_usd"] / total_cost) if total_cost else 0
        bar = "#" * int(share / 4)
        print(f"{r['provider'] + '/' + r['model']:42} {r['runs']:>4} "
              f"{r['tokens']:>12,} ${r['cost_usd']:>8.4f}  {share:4.0f}% {bar}")
        if args.agents:
            print(f"{'':42}      {', '.join(r['agents'])}")
    print(f"\n{'total':42} {sum(r['runs'] for r in rows):>4} "
          f"{total_tokens:>12,} ${total_cost:>8.4f}")

    # A provider that bills per token and one that does not are not comparable,
    # so say which is which rather than letting a $0.00 row read as free.
    free = [r["provider"] for r in rows if r["cost_usd"] == 0]
    if free:
        print(f"\n{', '.join(sorted(set(free)))} report no per-token cost "
              f"(subscription); their tokens are real but their dollars are not.")
    return 0


def cmd_tickets(args: argparse.Namespace) -> int:
    """Review and submit bug tickets the agents filed against multiagents.

    Submitting is deliberately a user action by default. The ticket is public
    writing about this machine, and `list`/`show` exist so that the decision is
    made after reading it rather than before.
    """
    paths = _resolve(args.path)
    config = load_config(paths)
    tree = Tree(paths.tree_file, paths.events_file)
    tickets = tree.read().get("tickets", [])

    if args.action == "show":
        ticket = tree.get_ticket(args.ticket_id) if args.ticket_id else None
        if ticket is None:
            print(f"unknown ticket {args.ticket_id!r}", file=sys.stderr)
            return 2
        print(f"{ticket['id']}  [{ticket['severity']}]  {ticket['status']}")
        print(f"{ticket['title']}\n")
        print(bugs.render(ticket))
        return 0

    if args.action == "submit":
        ticket = tree.get_ticket(args.ticket_id) if args.ticket_id else None
        if ticket is None:
            print(f"unknown ticket {args.ticket_id!r}", file=sys.stderr)
            return 2
        ok, why = bugs.can_submit(config)
        if not ok:
            print(f"cannot submit: {why}")
            print(f"\nthe ticket is readable with `multiagents tickets show {ticket['id']}`")
            return 1
        print(bugs.render(ticket))
        print(f"-> {bugs.settings(config)['repo']}")
        if not _confirm("file this as an issue?"):
            print("not submitted.")
            return 0
        sent, result = bugs.submit(config, ticket)
        if not sent:
            print(f"failed: {result}", file=sys.stderr)
            return 1
        tree.set_ticket_status(ticket["id"], "reported", "submitted by the user", result)
        print(f"reported: {result}")
        return 0

    if args.action == "discard":
        if tree.set_ticket_status(args.ticket_id, "declined", "discarded by the user") is None:
            print(f"unknown ticket {args.ticket_id!r}", file=sys.stderr)
            return 2
        print(f"{args.ticket_id} discarded.")
        return 0

    shown = [t for t in tickets
             if args.all or t.get("status") in ("open", "awaiting_user")]
    if not shown:
        print("No open tickets." if tickets else "No tickets have been filed.")
        return 0
    for ticket in shown:
        mark = "!" if ticket["severity"] == "blocking" else " "
        age = (time.time() - ticket["filed_at"]) / 60
        print(f" {mark} {ticket['id']}  {ticket['status']:14} filed {age:.0f}m ago"
              f"  {ticket['title'][:60]}")
        if ticket.get("url"):
            print(f"     {ticket['url']}")
    ok, why = bugs.can_submit(config)
    print(f"\n{len(shown)} ticket(s). `tickets show <id>` to read one, "
          f"`tickets submit <id>` to file it.")
    if not ok:
        print(f"note: {why}")
    return 0


def _docker_status_all() -> int:
    """Every project's containers, with the slugs resolved back to paths.

    Project-scoped commands cannot answer "what is running on this machine",
    and `docker ps` answers it in slugs, which are hashes and do not invert.
    The registry written at init is what turns them back into paths.
    """
    from .executor.docker import docker_state, list_containers

    state, detail = docker_state()
    if state != "ok":
        print(f"docker is not usable: {detail}", file=sys.stderr)
        return 1

    rows = list_containers()
    if not rows:
        print("No multiagents containers on this machine.")
        return 0

    known = known_projects()
    by_slug: dict[str, dict] = {}
    for row in rows:
        entry = by_slug.setdefault(row["slug"], {"workspace": None, "proxy": None})
        entry["proxy" if row["proxy"] else "workspace"] = row

    print(f"{'project':44} {'workspace':16} {'proxy':16}")
    running = 0
    for slug in sorted(by_slug):
        entry = by_slug[slug]
        record = known.get(slug)
        if record:
            path = Path(record["path"])
            label = str(path)
            if not path.is_dir():
                label += "  (gone)"
        else:
            # Registered only from `init` onwards, so a container made before
            # that has no path. Say so rather than printing a bare hash.
            label = f"{slug}  (path unknown)"
        home = str(Path.home())
        if label.startswith(home):
            label = "~" + label[len(home):]

        def short(row):
            if row is None:
                return "-"
            return "running" if row["status"].startswith("Up") else "stopped"

        print(f"{label[:44]:44} {short(entry['workspace']):16} {short(entry['proxy']):16}")
        running += sum(1 for r in entry.values()
                       if r and r["status"].startswith("Up"))

    print(f"\n{len(by_slug)} project(s), {running} container(s) running.")
    print("Each running project holds its resource ceiling whether or not agents "
          "are working:\nstop one with `multiagents --path <project> docker down`.")
    return 0


def cmd_supervise(args: argparse.Namespace) -> int:
    """Watch a launched agent from outside it. Started by `run`; rarely typed."""
    from . import watchdog

    paths = _resolve(args.path)
    return watchdog.supervise(paths, load_config(paths), args.role, args.pid,
                              interval=args.interval, max_seconds=args.max_seconds)


def cmd_status(args: argparse.Namespace) -> int:
    """What the orchestrator is doing, from outside it."""
    from . import watchdog

    paths = _resolve(args.path)
    record = watchdog.read_status(paths)
    if record is None:
        print("No supervisor has reported yet. `multiagents run` starts one.")
        return 0

    age = time.time() - record.get("at", 0)
    stale = "  (stale)" if age > 120 else ""
    print(f"{record['verdict']:14} {record['detail']}")
    print(f"{'':14} observed {age:.0f}s ago{stale}")
    transcript = record.get("transcript") or {}
    if transcript:
        print(f"{'':14} transcript quiet for {transcript.get('quiet_for', '?')}s")
    print(f"{'':14} {record.get('active_agents', 0)} agent(s) running")
    provider = record.get("provider") or {}
    if provider.get("known") and provider.get("headroom") is not None:
        print(f"{'':14} {provider['name']} headroom "
              f"{provider['headroom'] * 100:.0f}%"
              f"{', resets ' + str(provider['resets_at'])[:19] if provider.get('resets_at') else ''}")
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    """Bring everything to a halt without losing any of it.

    The counterpart to `run`. Resumable is the requirement, so this stops
    processes and leaves state: branches, worktrees, sessions, the deferred
    queue, open questions and tickets all survive, and an agent that was
    working keeps a session id that `steer_agent` can pick up.
    """
    paths = _resolve(args.path)
    config = load_config(paths)
    tree = Tree(paths.tree_file, paths.events_file)

    # --- whatever is driving the project -----------------------------------
    stopped_drivers = []
    for role in ("orchestrator", "orchestrator-turn", "initializer",
                 "initializer-turn"):
        path = _pid_file(paths, role)
        if not path.is_file():
            continue
        try:
            pid = int(path.read_text().strip())
        except (OSError, ValueError):
            path.unlink(missing_ok=True)
            continue
        if not _alive(pid):
            path.unlink(missing_ok=True)
            continue
        try:
            os.kill(pid, signal.SIGTERM)
            stopped_drivers.append(f"{role} (pid {pid})")
        except OSError as exc:
            print(f"  could not signal {role} pid {pid}: {exc}", file=sys.stderr)
        path.unlink(missing_ok=True)

    # --- the agents --------------------------------------------------------
    active = tree.active()
    stopped_agents = []
    if active:
        runner = Runner(paths, config)
        for node in active:
            try:
                asyncio.run(runner.stop(node.id))
                stopped_agents.append(node)
            except Exception as exc:                  # never leave the rest unstopped
                print(f"  {node.id} ({node.agent}): {type(exc).__name__}: {exc}",
                      file=sys.stderr)

    # --- nothing an agent wrote may be lost --------------------------------
    # A killed agent never reaches the commit its own run would have made, so
    # its edits sit uncommitted in a worktree nobody will look at again. The
    # branch is what makes the work resumable, so the work has to be on it.
    saved = 0
    for node in stopped_agents:
        worktree = Path(node.worktree) if node.worktree else None
        if not worktree or not worktree.is_dir() or not gitops.is_repo(worktree):
            continue
        if not gitops.is_dirty(worktree):
            continue
        result = gitops.commit_all(worktree, f"{node.agent}: work in progress "
                                             f"when stopped ({node.id})")
        if result.ok:
            saved += 1

    # --- the container, last: the agents were inside it --------------------
    container = ""
    if config.executor == "docker" and not args.keep_containers:
        from .executor.docker import docker_available
        if docker_available():
            container = _docker_executor(paths).stop(remove=False)

    for line in stopped_drivers:
        print(f"stopped      {line}")
    for node in stopped_agents:
        print(f"stopped      {node.id} {node.agent}")
    if saved:
        print(f"committed    work in progress in {saved} worktree(s)")
    if container:
        print(f"container    {container}")
    if not (stopped_drivers or stopped_agents or container):
        print("Nothing was running.")

    waiting = len(tree.open_questions())
    deferred = len(tree.read().get("deferred", []))
    if waiting or deferred:
        print(f"\nkept         {waiting} open question(s), "
              f"{deferred} deferred task(s)")
    print("\nBranches, worktrees and sessions are untouched. `multiagents run` "
          "picks up\nwhere this left off; a stopped agent resumes with "
          "`steer_agent`.")
    return 0


def cmd_docker(args: argparse.Namespace) -> int:
    if args.action == "status" and getattr(args, "all", False):
        return _docker_status_all()
    paths = _resolve(args.path)
    ex = _docker_executor(paths)

    if args.action == "build":
        source = global_config_dir() / "docker"
        if not source.is_dir():
            shutil.copytree(Path(__file__).parent / "defaults" / "docker", source)
        for dockerfile, tag in (("Dockerfile", ex.image), ("Dockerfile.proxy", ex.proxy_image)):
            if dockerfile == "Dockerfile.proxy" and ex.network_mode != "allowlist":
                continue
            print(f"building {tag} from {source / dockerfile} ...")
            result = ex.build_image(source / dockerfile, tag)
            if not result["ok"]:
                print(result.get("output") or result.get("error"), file=sys.stderr)
                return 1
            print(f"  ok  {tag}")
        return 0

    if args.action == "up":
        result = ex.ensure_running()
        print(result if not result.get("ok") else
              f"{result['container']} running ({ex.network_mode} networking)")
        return 0 if result.get("ok") else 1

    if args.action in ("down", "rm"):
        print(ex.stop(remove=args.action == "rm"))
        return 0

    if args.action == "login":
        provider_name = args.provider
        config = load_config(paths)
        providers = load_providers(config.providers)
        provider = providers.get(provider_name)
        if provider is None:
            print(f"unknown provider {provider_name!r}; known: {sorted(providers)}", file=sys.stderr)
            return 2
        state = ex.ensure_running()
        if not state.get("ok"):
            print(state.get("error"), file=sys.stderr)
            return 1
        private = ex.private_state()
        if not private:
            print(f"{provider_name} has no container_private_home in providers.yaml — "
                  f"it uses the host's credentials directly and needs no separate login.")
            return 0
        print(f"Logging {provider_name} in INSIDE the container.")
        print("Its credentials are stored in a container-private directory:")
        for container_path, host_path in private.items():
            print(f"  {container_path}  ->  {host_path}")
        print("Your host credentials are masked and cannot be touched.\n")
        print("Complete the login it offers, then quit the CLI (ctrl-c or /quit).\n")
        # execvp replaces this process; anything still buffered would be lost.
        sys.stdout.flush()
        # Use the absolute binary path rather than the bare name: the CLIs are
        # bind-mounted at their host paths, which are not on the container
        # image's PATH. Forward the host PATH too, so anything the CLI shells
        # out to during login resolves as it would on the host.
        binary = provider.available() or provider.bin
        os.execvp("docker", [
            "docker", "exec", "-it",
            "--user", f"{os.getuid()}:{os.getgid()}",
            "--workdir", str(paths.root),
            "--env", f"HOME={Path.home()}",
            "--env", f"PATH={os.environ.get('PATH', '/usr/local/bin:/usr/bin:/bin')}",
            "--env", "TERM=xterm-256color",
            ex.container, binary,
        ])

    if args.action == "shell":
        os.execvp("docker", ["docker", "exec", "-it",
                             "--user", f"{os.getuid()}:{os.getgid()}",
                             "--workdir", str(paths.root), ex.container, "bash"])

    if args.action == "status":
        print(f"image        {ex.image}  {'built' if ex.image_exists(ex.image) else 'MISSING'}")
        if ex.network_mode == "allowlist":
            built = 'built' if ex.image_exists(ex.proxy_image) else 'MISSING'
            print(f"proxy image  {ex.proxy_image}  {built}")
            print(f"proxy        {ex.proxy_container}  {ex.container_state(ex.proxy_container)}")
            print(f"network      {ex.network} (internal)")
        print(f"container    {ex.container}  {ex.container_state(ex.container)}")
        print(f"networking   {ex.network_mode}")
        print(f"resources    cpus={ex.config.get('cpus','-')} "
              f"memory={ex.config.get('memory','-')} pids={ex.config.get('pids_limit','-')}")
        print("mounts:")
        for path, read_only in ex.mounts():
            print(f"  {'ro' if read_only else 'rw'}  {path}")
        problems = ex.preflight()
        print("\n" + ("ready" if not problems else "\n".join(problems)))
        return 1 if problems else 0

    if args.action == "check":
        # Prove the boundary rather than assuming it: one allowed host should
        # resolve, one denied host should not.
        state = ex.ensure_running()
        if not state.get("ok"):
            print(state.get("error"), file=sys.stderr)
            return 1
        allowed = (ex.config.get("egress_allowlist") or ["opencode.ai"])[0]
        for host, expect in ((allowed, "allow"), ("example.com", "deny")):
            probe = subprocess.run(
                ["docker", "exec", ex.container, "curl", "-sS", "-o", "/dev/null",
                 "-m", "20", "-w", "%{http_code}", f"https://{host}/"],
                capture_output=True, text=True, timeout=60,
            )
            code = probe.stdout.strip() or "-"
            reached = probe.returncode == 0 and code not in ("", "-", "000", "403")
            verdict = "OK" if (reached == (expect == "allow")) else "UNEXPECTED"
            print(f"  {expect:5} {host:24} http={code:4} reached={reached}  {verdict}")
        return 0

    return 2


def cmd_mcp_config(args: argparse.Namespace) -> int:
    path = _write_mcp_config()
    print(path)
    print(path.read_text())
    print("Registered automatically for the configured orchestrator by "
          "`multiagents run`. Point another MCP client at this file to give it "
          "the same tools.")
    return 0


# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="multiagents",
        description="Delegate work to other agent CLIs as supervised, git-isolated subagents.",
    )
    parser.add_argument("--path", help="project directory (default: search upward from cwd)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="set up a project")
    p.add_argument("path", nargs="?", help="project directory (default: cwd)")
    p.add_argument("--force", action="store_true", help="overwrite existing config files")
    p.add_argument("--nested", action="store_true",
                   help="allow a project inside another project's directory tree")
    p.set_defaults(func=cmd_init)

    for name, helptext in (
        ("run", "launch the orchestrator (first run and resume are the same)"),
        ("resume", "alias for `run`"),
    ):
        p = sub.add_parser(name, help=helptext)
        p.add_argument("--no-launch", action="store_true",
                       help="report project state without launching")
        p.add_argument("--fresh", dest="resume", action="store_false", default=True,
                       help="start a new session instead of continuing the last one")
        p.add_argument("--wait", action="store_true",
                       help="if the orchestrator's provider is exhausted, block "
                            "until its quota resets instead of exiting")
        p.add_argument("--no-supervise", dest="supervise", action="store_false",
                       default=True,
                       help="hand the terminal over and exit; nothing carries on "
                            "if the session drops")
        p.add_argument("--unattended", nargs="?", type=int, const=50, default=0,
                       metavar="TURNS",
                       help="run headless, turn after turn, without a terminal: "
                            "waits out quota resets, retries a crashed turn with "
                            "backoff, and stops when two turns change nothing "
                            "(default 50 turns)")
        p.set_defaults(func=cmd_resume)

    p = sub.add_parser("init-agent",
                       help="shape the project with the initializer (resumable)")
    p.add_argument("--fresh", dest="resume", action="store_false", default=True,
                   help="start a new session instead of continuing the last one")
    p.add_argument("--wait", action="store_true",
                   help="if the provider is exhausted, block until its quota resets")
    p.set_defaults(func=cmd_init_agent)

    p = sub.add_parser("build", help="build and start the container environment")
    p.add_argument("--rebuild", action="store_true", help="rebuild images that exist")
    p.add_argument("--no-auth", action="store_true",
                   help="report authentication problems without offering to fix them")
    p.set_defaults(func=cmd_build)

    p = sub.add_parser("doctor", help="check CLIs, agents, models, budget and git")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("refresh-models", help="regenerate models.yaml from the installed CLIs")
    p.set_defaults(func=cmd_refresh_models)

    p = sub.add_parser("tree", help="print the agent tree")
    p.set_defaults(func=cmd_tree)

    p = sub.add_parser("watch", help="tail the global event log")
    p.set_defaults(func=cmd_watch)

    p = sub.add_parser("probe", help="verify a provider's stream parsing rules")
    p.add_argument("provider")
    p.add_argument("--model", required=True)
    p.add_argument("--prompt", default="Reply with exactly: PONG")
    p.add_argument("--timeout", type=int, default=180)
    p.set_defaults(func=cmd_probe)

    p = sub.add_parser("clean", help="remove finished agents' branches, worktrees and homes")
    p.add_argument("--branches", action="store_true", help="delete agent branches")
    p.add_argument("--homes", action="store_true", help="delete per-agent HOME directories")
    p.add_argument("--tree", action="store_true", help="prune finished nodes that hold no branch")
    p.add_argument("--force", action="store_true", help="delete even with unmerged commits")
    p.set_defaults(func=cmd_clean)

    p = sub.add_parser("upgrade-config",
                       help="refresh config copies that were never edited")
    p.add_argument("--dry-run", action="store_true", help="show what would change")
    p.add_argument("--force", action="store_true",
                   help="overwrite edited files too (a .bak is kept)")
    p.add_argument("--layer", choices=["global", "project", "both"], default="both",
                   help="limit to one config layer (default: both)")
    p.set_defaults(func=cmd_upgrade_config)

    p = sub.add_parser("auth", help="check or repair provider authentication")
    p.add_argument("action", nargs="?", default="status", choices=["status", "login"])
    p.add_argument("provider", nargs="?", default="", help="provider to log in")
    p.set_defaults(func=cmd_auth)

    p = sub.add_parser("ask", help="answer questions agents are parked on")
    p.add_argument("question_id", nargs="?", default="", help="answer just this one")
    p.add_argument("--agent", default="", help="only this agent's questions")
    p.add_argument("--list", action="store_true", help="list without answering")
    p.set_defaults(func=cmd_ask)

    p = sub.add_parser("uninstall",
                       help="remove this machine's global config and agent state")
    p.add_argument("--dry-run", action="store_true", help="show what would go")
    p.add_argument("--force", action="store_true",
                   help="remove even when a worktree holds uncommitted work")
    p.set_defaults(func=cmd_uninstall)

    p = sub.add_parser("status", help="what the orchestrator is doing")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("supervise", help="(internal) watch a launched agent")
    p.add_argument("--role", default="orchestrator")
    p.add_argument("--pid", type=int, required=True)
    p.add_argument("--interval", type=float, default=20.0)
    p.add_argument("--max-seconds", dest="max_seconds", type=float, default=0.0)
    p.set_defaults(func=cmd_supervise)

    p = sub.add_parser("stop", help="stop everything for this project, resumably")
    p.add_argument("--keep-containers", action="store_true",
                   help="leave the container running")
    p.set_defaults(func=cmd_stop)

    p = sub.add_parser("usage", help="tokens and cost per provider/model")
    p.add_argument("--agents", action="store_true", help="name the agents behind each row")
    p.set_defaults(func=cmd_usage)

    p = sub.add_parser("tickets", help="review bugs agents filed against multiagents")
    p.add_argument("action", nargs="?", default="list",
                   choices=["list", "show", "submit", "discard"])
    p.add_argument("ticket_id", nargs="?", default="")
    p.add_argument("--all", action="store_true", help="include resolved tickets")
    p.set_defaults(func=cmd_tickets)

    p = sub.add_parser("docker", help="manage the project's agent container")
    p.add_argument("action",
                   choices=["build", "up", "down", "rm", "status", "shell", "check", "login"])
    p.add_argument("provider", nargs="?", default="agy",
                   help="provider to act on; only used by `login` (default: agy)")
    p.add_argument("--all", action="store_true",
                   help="with `status`: every project's containers on this machine")
    p.set_defaults(func=cmd_docker)

    p = sub.add_parser("catalog", help="compare the local model catalog against the live one")
    p.add_argument("--provider", default="opencode-go")
    p.add_argument("--update", action="store_true", help="record the live catalog as the baseline")
    p.set_defaults(func=cmd_catalog)

    p = sub.add_parser("mcp-config", help="write and print the MCP registration")
    p.set_defaults(func=cmd_mcp_config)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
