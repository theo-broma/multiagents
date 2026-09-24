"""Driving a provider CLI as the orchestrator or the initializer.

`run` and `init-agent` do not spawn an agent: they hand this terminal to
somebody else's CLI and then have to survive it. That is a different job from
parsing arguments, and it is most of what used to sit above the first `cmd_`
function in `cli.py` — the launch context, the termios save and restore, the
exec and the attached and supervised variants of it, the pid files, the
session-id rotation, the supervisor process, and the waiting a provider's quota
imposes before any of it can start.

Kept here so that the code with the behaviour in it stops living in a file
named after argument parsing, and so the known gap in `docs/open-questions.md`
§3b — the drivers never appearing in the agent tree, which is why an agent can
show up with no record of who asked — has one place to be fixed.

Names keep their leading underscore: this module is package-private and the
move was meant to be a move and nothing else.
"""

from __future__ import annotations

import contextlib
import json
from dataclasses import replace
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from . import procs, scripts
from .budget import read_all, reset_label
from .config import limit_number
from .executor import executor_for
from .paths import global_config_dir
from .providers import load_providers
from .transcripts import session_context, session_transcript
from .tree import Node, Tree, now as tree_now


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

def _launched_spec(config, role: str, team: str = ""):
    """The roster entry launched by a given command, or None.

    Both the orchestrator and the initializer are launched rather than spawned;
    the role says which door they come through.

    The ORCHESTRATOR's brief varies by team — that is most of what a team is —
    so an active team's `orchestrator:` briefs replace the roster entry's. The
    INITIALIZER's never does, and that is deliberate rather than an oversight:
    it is the agent that decides which team comes next, so a team-scoped
    initializer could not pivot a project from reviewing to building. It is not
    a team member; it is what hires the team, and it runs outside them.
    """
    for spec in config.agents.values():
        if spec.launch and spec.role == role:
            if role == "orchestrator":
                briefs = config.team_spec(team).get("orchestrator")
                if briefs:
                    spec = replace(spec, instructions=briefs)
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
    context = {
        "MULTIAGENTS_MODEL": spec.model,
        "MULTIAGENTS_PROMPT_FILE": str(prompt_file),
        "MULTIAGENTS_MCP_CONFIG": str(mcp_path),
        "MULTIAGENTS_MCP_COMMAND": server["command"],
        # \x1f so an argument containing spaces survives the round trip.
        "MULTIAGENTS_MCP_ARGS": "\x1f".join(server["args"]),
        "MULTIAGENTS_LAUNCH_STATE": str(state),
        "MULTIAGENTS_PROJECT": str(paths.root),
    }
    # Same key a spawned agent gets through `spawn.optional` (providers.yaml) —
    # here it reaches the launch action as environment instead of argv, because
    # `launch` is exec'd by the script, not built into a command list by us.
    autocompact = spec.extra.get("autocompact")
    if autocompact not in (None, ""):
        context["MULTIAGENTS_AUTOCOMPACT"] = str(autocompact)
    return context


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

# How often the parent checks whether a live session has stopped being a working
# one. Long enough to be free, short enough that nobody watches a dead prompt
# for an hour.
STALL_POLL_SECONDS = 60.0

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


def _run_attached(argv, env, stalled=None) -> int:
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
    * the terminal mode is saved and restored around the run — on SIGTERM too
      (P0-R8f.10), which is turned into an exit so the restore runs. The CLI
      is stopped first: it would otherwise stay on the terminal, orphaned, with
      nobody left to put the terminal back after it.
    * nothing in this process reads stdin, or it would steal the child's keys.

    `stalled` is polled while the child runs and, if it ever returns true, the
    child is stopped. Without it a session that stops working without exiting is
    invisible: the supervisor waits on a process that will never end.

    If `stalled` has a `stopping` attribute, it is called just before that
    stop, and only if the child is still running when it comes: a CLI that exited by itself while `stalled` was
    deciding was ended by its user, not by us, and its caller must be able to
    tell the two apart (P0-R8f.12).
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
        # Not ignored: whoever sent it wants this process gone, and it goes —
        # through the `finally` below, which a default SIGTERM would skip.
        previous[signal.SIGTERM] = signal.signal(signal.SIGTERM, _exit_on_sigterm)
    except (ValueError, OSError):
        pass
    child = None
    try:
        child = subprocess.Popen(argv, env=env)          # NOT start_new_session
        while True:
            try:
                return child.wait(timeout=STALL_POLL_SECONDS)
            except subprocess.TimeoutExpired:
                # Still running — but running is not working. A CLI that hits a
                # usage limit prints and waits rather than exiting, so every
                # exit-code path in here is blind to the most common way a
                # session stops being useful.
                if stalled is not None and stalled():
                    if child.poll() is not None:
                        return child.returncode      # it ended by itself
                    stopping = getattr(stalled, "stopping", None)
                    if stopping is not None:
                        stopping()
                    return _stop_child(child)
            except KeyboardInterrupt:                    # belt and braces
                continue
    except BaseException:
        if child is not None and child.poll() is None:
            _stop_child(child)
        raise
    finally:
        for sig, handler in previous.items():
            with contextlib.suppress(ValueError, OSError):
                signal.signal(sig, handler)
        _restore_terminal(saved)


def _exit_on_sigterm(signum, _frame):
    raise SystemExit(128 + signum)


def _stop_child(child: subprocess.Popen) -> int:
    """Terminate the attached CLI, kill it if it will not go, and reap it."""
    child.terminate()
    with contextlib.suppress(subprocess.TimeoutExpired):
        child.wait(timeout=20)
    if child.poll() is None:
        child.kill()
    return child.wait()


def _hand_over(argv: list[str], env: dict, script: Path | None) -> int:
    """Replace this process with the provider's script, or say why not.

    `login` and `launch` take the terminal, so there is nothing left to format
    an error with once the handover succeeds — and nothing to catch one if it
    fails. A script that is not executable, or is executable with no shebang,
    otherwise surfaces here as a raw OSError traceback at the moment the user
    was expecting their CLI to open.
    """
    try:
        os.execvpe(argv[0], argv, env)
    except OSError as exc:
        where = script or Path(argv[0])
        print(f"cannot run {where}: {scripts.why_it_would_not_run(where, exc)}",
              file=sys.stderr)
        return 126
    return 0                              # unreachable: execvpe does not return


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


DRIVER_CLASH = (
    "{other} is already running here (pid {pid}).\n"
    "\n"
    "They are not meant to overlap. The initializer shapes BRIEF.md and\n"
    "context/ in the project itself, while the orchestrator builds against\n"
    "them and branches agents from that same tree — so running both means\n"
    "building against a moving target, in one worktree, with one tree.json.\n"
    "\n"
    "Finish or stop that one first (`multiagents stop`), or pass --force if\n"
    "you know why you want both."
)


def _other_driver_running(paths, role: str) -> tuple[str, int] | None:
    """Is the project's OTHER driver alive? `(role, pid)`."""
    from . import watchdog

    for other in watchdog.DRIVERS:
        if other == role:
            continue
        recorded = _read_pid(paths, other)
        if recorded is None:
            continue
        if procs.alive(*recorded):
            return other, recorded[0]
    return None


def _launch_agent(paths, config, role: str, resume: bool,
                  unattended: int = 0, supervise: bool = True,
                  force: bool = False, unclean: bool = False) -> int:
    """Launch a roster entry as an interactive MCP client.

    Normally execs, so the CLI owns the terminal and this process is gone.
    Under `unattended` it spawns instead and supervises: a turn that ends —
    crash, quota, or the model simply stopping — is followed by another, which
    is the whole point of leaving it running overnight.
    """
    # The orchestrator's brief is team-dependent; the initializer's never is,
    # because it is the agent that decides which team comes next.
    spec = _launched_spec(config, role, config.team)
    if spec is None:
        print(f"No agent in agents.yaml is marked `launch: true, role: {role}`.",
              file=sys.stderr)
        return 2
    clash = None if force else _other_driver_running(paths, role)
    if clash:
        print(DRIVER_CLASH.format(other=clash[0], pid=clash[1]), file=sys.stderr)
        return 2
    providers = load_providers(config.providers)
    provider = providers.get(spec.provider)
    if provider is None or not provider.available():
        print(f"{role} provider {spec.provider!r} is unavailable", file=sys.stderr)
        return 2

    executor = executor_for(paths, config, providers)(spec.provider)
    problem = _auth_problem(paths, config, spec)
    if problem:
        print(f"not ready: {problem}", file=sys.stderr)
        return 2
    _clear_limit_pause(paths, spec)
    context = _launch_context(paths, config, spec)
    # Resuming is only possible if this role has been launched here before.
    # Passing --continue on a first run makes the CLI error out with no prior
    # conversation, which would make `run` fail exactly once per project.
    marker = paths.data / "launch" / f"{role}.launched"
    first_time = not marker.is_file()
    context["MULTIAGENTS_RESUME"] = "0" if (first_time or not resume) else "1"
    # A session that was stopped without anything left to record the ending
    # comes back knowing nothing about it: the reconciliation `run` just did is
    # printed to the TERMINAL, to the person, while the agent that has to act
    # on it is resumed into an empty prompt and told nothing. RESUME_PROMPT was
    # written for exactly this and reached only the in-process retry loop —
    # the one path a power cut cannot take, because it takes the process too.
    if unclean and context["MULTIAGENTS_RESUME"] == "1":
        context["MULTIAGENTS_RESUME_PROMPT"] = RESUME_PROMPT
    context["MULTIAGENTS_ROLE"] = role
    # `--fresh` means a new conversation, so it needs a new id: reusing one
    # that already has a transcript would collide with the session it names.
    context["MULTIAGENTS_SESSION_ID"] = _role_session_id(
        paths, role, rotate=not resume)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(str(time.time()))
    _driver_node(paths, role, spec, context["MULTIAGENTS_SESSION_ID"])

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
            # Returns only when the handover FAILED; execvpe does not come back.
            return _hand_over(argv, env, scripts.resolve(
                spec.provider, provider, global_config_dir(), paths.config))
        return _run_supervised(paths, config, role, spec, provider, executor,
                               context, argv, env)
    finally:
        _clear_pid(paths, role)
    return 0


class _AttachedCompaction:
    """P0-R8f: compacting a session a person is attached to.

    A live interactive CLI holds its conversation in memory, so compacting its
    session from outside while it runs would fork the transcript. Instead the
    session is stopped at a closed boundary, compacted, and resumed — the same
    shape as the usage-limit stop, with an announcement and a grace period in
    front of it, because somebody may be about to type.

    `due()` is polled on the attached child's existing stall poll and says
    whether to stop the child now; `compact()` runs after it has stopped. The
    state lives for the whole driver run, across relaunches, because two of
    the rules are about the run and not about one process: after a failure or
    a "cannot" it is off for good, and after a success it waits for the reading
    to fall below the threshold and cross it again.

    The driver sees the transcript, never the keyboard: only a SENT message
    changes the file, so only a sent message can cancel.
    """

    def __init__(self, paths, config, spec, provider, executor, context: dict):
        self.paths, self.config, self.spec = paths, config, spec
        self.provider, self.executor, self.context = provider, executor, context
        self.tree = Tree(paths.tree_file, paths.events_file)
        self.threshold = _limit_number(config, "compact_at_tokens", zero_ok=True)
        self.idle = _limit_number(config, "compact_idle_seconds")
        self.grace = _limit_number(config, "compact_grace_seconds")
        self.bell = _limit_flag(config, "compact_bell", True)
        self.disabled = False       # a failure or a "cannot": not again this run
        self.spent = False          # compacted in this crossing of the threshold
        self.probed = None          # the transcript state the probe answered for
        self.scheduled = None       # (state, announced at, tokens)
        self.requested = None       # tokens, once the grace period ran out
        self.since = time.time()    # when the attached CLI was last launched

    def launched(self) -> None:
        """A (re)launch: a fresh prompt is not a quiet one yet."""
        self.since = time.time()
        self.scheduled = self.requested = None

    def _who(self) -> str:
        session = self.context.get("MULTIAGENTS_SESSION_ID", "")
        return next((n.id for n in self.tree.drivers() if n.session == session),
                    self.spec.name)

    def _busy(self) -> bool:
        return bool(self.tree.active() or self.tree.read().get("deferred"))

    def _state(self):
        path = session_transcript(self.provider, self.paths.root,
                                  self.context.get("MULTIAGENTS_SESSION_ID", ""))
        if path is None:
            return None
        try:
            st = path.stat()
        except OSError:
            return None
        return (st.st_ino, st.st_mtime_ns, st.st_size)

    def _cancel(self) -> None:
        # A new episode either way (R8f.14): a cancel for a busy tree leaves
        # the transcript as the probe saw it, and must not suppress the next
        # proposal once the tree is quiet again.
        self.scheduled = self.probed = None
        print("\ncompaction cancelled — the session is in use; it will be "
              "proposed again once it is quiet.")
        sys.stdout.flush()
        self.tree.emit(self._who(), "compact_cancelled")

    def due(self) -> bool:
        """One poll. True means: stop the child now, for a compaction."""
        if self.disabled or self.threshold <= 0 or not sys.stdin.isatty():
            return False
        state = self._state()
        if self.scheduled is not None:
            announced_for, at, tokens = self.scheduled
            if state != announced_for or self._busy():
                self._cancel()
                return False
            if time.monotonic() - at < self.grace:
                return False
            self.scheduled, self.requested = None, tokens
            return True
        if state is None:
            return False
        tokens = session_context(self.provider, self.paths.root,
                                 self.context.get("MULTIAGENTS_SESSION_ID", ""))
        if tokens is None:
            return False
        if tokens < self.threshold:
            self.spent = False          # below again: the next crossing counts
            return False
        # At rest since the file last changed, not since we noticed it — but
        # never since before the CLI was launched: somebody who has just been
        # handed a prompt is the person most likely to be typing into it.
        rest = time.time() - max(state[1] / 1e9, self.since)
        if self.spent or rest < self.idle:
            return False
        if self._busy() or state == self.probed:
            return False
        # Once per rest episode: a "not now" is not asked again until the
        # session has changed and come back to rest.
        self.probed = state
        code, _, _ = scripts.run_action(
            self.spec.provider, self.provider, self.executor, "compact",
            global_config_dir(), self.paths.config,
            extra_env={**self.context, "MULTIAGENTS_COMPACT_CHECK": "1"},
            cwd=self.paths.root)
        if code == scripts.UNIMPLEMENTED:
            self.disabled = True
            return False
        if code != 0:
            return False
        self.scheduled = (state, time.monotonic(), tokens)
        bell = "\a" if self.bell else ""
        print(f"\ncompacting this session in {self.grace:g}s ({tokens:,} tokens, "
              f"nothing running) — send any message (e.g. \"wait\") to "
              f"cancel{bell}")
        sys.stdout.flush()
        self.tree.emit(self._who(), "compact_scheduled", tokens=tokens)
        return False

    def compact(self) -> None:
        """The child has stopped for `due()`: compact, and settle the latches."""
        tokens, self.requested = self.requested, None
        code = _compact_session(self.paths, self.config, self.spec, self.provider,
                                self.executor, self.context, self.tree, tokens)
        if code == 0:
            self.spent = True
        else:
            # The session is relaunched either way; what a failure changes is
            # that nobody is stopped again for it in this run.
            self.disabled = True


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

    # A usage limit is the one stop that never reaches the exit code: the CLI
    # prints it into the chat log and sits at the prompt, alive and idle, so
    # everything below — which keys on the process ending — waits forever. This
    # watches for the CLI's own hardcoded message and ends the session so a
    # decision can be made about it.
    limit: dict = {}
    warned: list = []

    def _limit_hit() -> bool:
        found = watchdog.limit_reached(provider, paths.root)
        if found is None:
            warned.clear()
            return False
        if not warned:
            # One poll of grace, and a way out of it. Ending a session someone
            # is sitting in front of would be worse than the stall this fixes,
            # and anything typed clears the detection — the limit only counts
            # while nobody has answered it.
            warned.append(found)
            print(f"\n{spec.provider} says: {found.get('detail')}\n"
                  f"Ending the session in {STALL_POLL_SECONDS:.0f}s so it can be "
                  f"restarted — type anything to keep it.")
            return False
        limit.clear()
        limit.update(found)
        return True

    compaction = _AttachedCompaction(paths, config, spec, provider, executor,
                                     context)

    def _stalled() -> bool:
        if _limit_hit():
            return True
        if warned:
            return False                 # a pending limit wins (R8f.2.4)
        return compaction.due()

    stopped_by_driver: list = []          # last _attached() call: did WE stop it?

    def _attached(run_env) -> tuple[int, float]:
        """Run the CLI until it ends for a reason other than a compaction.

        A stop for compaction is none of the endings below — not a crash, not
        a deliberate exit, not a restart attempt — so it never reaches them:
        the terminal is already restored when `_run_attached` returns, the
        session is compacted, and the same session is relaunched with resume
        on and NO prompt, whatever happens to the compaction. The orchestrator
        was idle at its prompt and comes back idle at its prompt.
        """
        while True:
            began = time.monotonic()
            compaction.launched()
            stopped: list = []
            _stalled.stopping = lambda: stopped.append(True)
            code = _run_attached(argv, run_env, stalled=_stalled)
            stopped_by_driver[:] = stopped
            if compaction.requested is None or not stopped:
                # The user's own exit wins over a compaction that was due
                # (R8f.12): only a CLI we stopped for it is compacted.
                compaction.requested = None
                return code, time.monotonic() - began
            compaction.compact()
            run_env = {k: v for k, v in run_env.items()
                       if k != "MULTIAGENTS_RESUME_PROMPT"}
            run_env["MULTIAGENTS_RESUME"] = "1"
            sys.stdout.flush()

    code, ran_for = _attached(env)
    if limit and not stopped_by_driver:
        # The user's own exit wins over a usage-limit stop too (R8f.19, the
        # rule of R8f.12 extended): `stopping()` fires only when we actually
        # had to stop a still-running child, so its absence here means the CLI
        # ended on its own between the limit being detected and us acting on
        # it — nothing is left to wait out or resume.
        limit.clear()
    deliberate, why = _exit_was_deliberate(code)
    if limit:
        # We sent the SIGTERM, so the exit code says "deliberate" and means it
        # about us, not about the user.
        deliberate, why = False, f"stopped by {spec.provider}: {limit['detail']}"
    watchdog.write_status(paths, {
        "at": time.time(), "role": role, "pid": None, "running": False,
        "verdict": "limited" if limit else ("stopped" if deliberate else "dropped"),
        "detail": why,
        "transcript": None, "active_agents": 0, "provider": {},
    })

    crash_override = False
    limit_waits_before_loop = 0
    if limit:
        stop = _limit_stop(paths, config, spec, limit)
        if stop is not None:
            return stop
        limit_waits_before_loop = 1
        crash_override = True            # a limit is not a fault to protect from

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
    crash = not crash_override and code not in (-signal.SIGHUP, 129)
    if crash and not bool(config.limits.get("restart_on_crash", False)):
        print(f"\n{role} {why}. Not retrying: a non-zero exit is an error the "
              f"CLI could not\nhandle, so its cause is still there and a "
              f"restart would meet it again.\n`multiagents status` has the last "
              f"observation. Set limits.restart_on_crash\nto true if you want "
              f"it retried anyway.")
        return 1

    attempts = int(_limit_number(config, "restart_attempts", zero_ok=True))
    delay = _limit_number(config, "restart_delay_seconds", zero_ok=True)
    survived = _limit_number(config, "restart_min_runtime_seconds")
    # Waiting out a usage window is not a restart attempt and must not spend
    # them. The window is FIVE HOURS on this provider; five attempts backing off
    # from a minute would give up in the middle of it, having proved only that
    # the limit was still there — which was never in doubt.
    limit_waits = limit_waits_before_loop
    limit_budget = int(_limit_number(config, "limit_max_waits", zero_ok=True))
    after_limit = bool(limit_waits_before_loop)
    attempt = 0
    while attempt < attempts:
        if not sys.stdin.isatty():
            break
        attempt += 1
        if crash and ran_for is not None and ran_for < survived:
            # Only reachable with restart_on_crash on. Even then, a failure this
            # fast is the same fault being read again.
            print(f"\n{role} {why} after only {ran_for:.0f}s. Stopping: a "
                  f"failure that fast is\nthe same fault being read again, not "
                  f"a passing one.")
            return 1
        if after_limit:
            # _limit_stop has already waited out the window and said so; another
            # minute of "retrying in 60s (1/5)" would be noise about a counter
            # that a wait does not move.
            after_limit = False
            print(f"\n{role}: the wait is over, starting it again.")
        else:
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
        limit.clear()
        code, ran_for = _attached(retry_env)
        if limit and not stopped_by_driver:
            # The user's own exit wins over a usage-limit stop too (R8f.19):
            # the same rule applies to the attached CLI of a retry.
            limit.clear()
        deliberate, why = _exit_was_deliberate(code)
        if limit:
            # We sent the SIGTERM; "terminated" would be true about us and
            # misleading about the session.
            why = f"stopped by {spec.provider}: {limit['detail']}"
            limit_waits += 1
            if limit_waits > limit_budget:
                print(f"\n{spec.provider} is still limited after "
                      f"{limit_waits - 1} waits; stopping. `multiagents run` "
                      f"picks it back up.")
                return 3
            stop = _limit_stop(paths, config, spec, limit, attempt=limit_waits)
            if stop is not None:
                return stop
            crash = False
            after_limit = True
            attempt -= 1                 # a wait is not one of the five tries
            continue
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
                      max_turns=int(_limit_number(config, "supervised_turns")))


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


def _driver_node(paths, role: str, spec, session: str) -> str:
    """Put this session in the tree, so the agents it starts have a `why`.

    open-questions.md §3b: `run` and `init-agent` exec into a CLI, so neither
    left a node, and the two decision-makers were the only things in the
    project doing work with no record of it. An agent appeared as a root with
    nothing saying who asked for it.

    The node is a ROOT with no parent and no branch, and it is marked with its
    role so that `Tree.active()` leaves it out. That exclusion is the whole
    reason the role is a field: a driver counted as an active agent takes a
    `max_concurrent` slot away from real work, and `wait_for_any` would wait on
    it until its timeout, because this process is about to be replaced by
    `execvpe` and nothing here will ever mark it finished.

    Reused across resumes rather than added per launch. A session id is stable
    for the life of the role unless `--fresh` rotates it, so a project that has
    been resumed thirty times gets one node and not thirty.

    Which means the spec is re-read on every resume and the node must be too.
    It used to carry only status and started_at, so provider and model stayed
    as they were the day the node was created: edit the roster, restart, and
    the orchestrator genuinely runs on the new model while `multiagents
    monitor` — the one place that prints it — keeps naming the old one. The
    roster change looks like it was ignored when it was in fact applied, which
    is the expensive direction for this to be wrong in.
    """
    tree = Tree(paths.tree_file, paths.events_file)
    existing = next((n for n in tree.drivers() if n.session == session), None)
    if existing is not None:
        tree.update(existing.id, status="running", started_at=tree_now(),
                    provider=spec.provider, model=spec.model)
        return existing.id

    node = Node(
        id=f"dr-{session[:6]}", agent=role, provider=spec.provider,
        model=spec.model, parent=None, depth=0, status="running",
        task=f"{role} session", session=session, role=role,
        started_at=tree_now(),
    )
    tree.add(node)
    return node.id


def _auth_problem(paths, config, spec) -> str:
    """Why the orchestrator's own provider cannot be used, or "".

    `run` checked the quota and the container images and never once asked
    whether the CLI it was about to hand the terminal to was signed in. On
    2026-09-14 it launched against an expired claude token and the only sign
    was every delegation coming back `401 OAuth access token has expired` —
    which reads as a broken install, not an expired login.

    Asks about the HOST profile, because that is the one being launched. This
    function hands the terminal to a CLI on this machine — `_launch_agent`
    execs it here — and it does so whether or not agents run in a container.
    Without naming the profile, a docker project asked about the CONTAINER's
    login and cleared a launch that then 401'd on its first turn: the check
    was satisfied about an account the orchestrator was never going to use.
    """
    from . import auth as auth_mod

    providers = load_providers(config.providers)
    provider = providers.get(spec.provider)
    if provider is None:
        return ""                             # a different check already says so
    executor = executor_for(paths, config, providers)(spec.provider)
    state = auth_mod.check(spec.provider, provider, executor,
                           global_config_dir(), paths.config,
                           profile=auth_mod.HOST)
    if state.status != "not_authenticated":
        return ""                             # authenticated, or it would not say
    return (f"{spec.provider} is not authenticated on this machine, where the "
            f"orchestrator runs — {state.detail}. Run `{state.fix}`.")


def _pid_file(paths, role: str) -> Path:
    return paths.data / "launch" / f"{role}.pid"


def _write_pid(paths, role: str, pid: int) -> None:
    """Record the pid AND what makes it identifiable after a reboot.

    Two fields, space separated, because a pid on its own is not an identity:
    see :mod:`multiagents.procs`. A file written by an older install has one
    field and still reads, with the guard simply unavailable for it.
    """
    path = _pid_file(paths, role)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{pid} {procs.start_time(pid)}".strip())


def _read_pid(paths, role: str) -> tuple[int, str] | None:
    """`(pid, start_time)` from a role's pid file, or None.

    One parser, because there were four copies of `int(read_text().strip())`
    and a second field would have been silently dropped by three of them.
    """
    try:
        parts = _pid_file(paths, role).read_text().split()
        return int(parts[0]), (parts[1] if len(parts) > 1 else "")
    except (OSError, ValueError, IndexError):
        return None


def _clear_pid(paths, role: str) -> None:
    _pid_file(paths, role).unlink(missing_ok=True)


def _role_alive(paths, role: str) -> bool:
    """Is the process this role's pid file names still that process?"""
    recorded = _read_pid(paths, role)
    return recorded is not None and procs.alive(*recorded)


def _alive(pid: int, start: str = "") -> bool:
    return procs.alive(pid, start)


def _supervise(paths, config, role, spec, provider, executor,
               context: dict, max_turns: int) -> int:
    """Run the orchestrator turn after turn until there is nothing left to do.

    Each turn is a headless invocation of the same session: the provider's
    launch script adds its own non-interactive flag and the nudge below. This
    is deliberately NOT `exec` in a shell `until` loop — that construction stops
    when the command *succeeds*, so a turn that worked would end the run, and a
    turn that crashed would be retried forever.
    """
    from . import watchdog

    tree = Tree(paths.tree_file, paths.events_file)
    idle_turns = 0
    failures = 0
    limit_waits = 0
    # Set once the provider says it cannot compact; not asked again this run.
    compact_unsupported: list = []

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
            # Polled, not blocked on: a headless turn that hits a usage limit
            # can sit there indefinitely, and an unattended loop is exactly
            # where nobody is watching it do that.
            while True:
                try:
                    code = child.wait(timeout=STALL_POLL_SECONDS)
                    break
                except subprocess.TimeoutExpired:
                    if watchdog.limit_reached(provider, paths.root):
                        child.terminate()
                        with contextlib.suppress(subprocess.TimeoutExpired):
                            child.wait(timeout=20)
                        if child.poll() is None:
                            child.kill()
                        code = child.wait()
                        break
            _clear_pid(paths, f"{role}-turn")
        except KeyboardInterrupt:
            print("\nstopped.")
            return 0

        limit = watchdog.limit_reached(provider, paths.root)
        if limit:
            limit_waits += 1
            if limit_waits > int(_limit_number(config, "limit_max_waits", zero_ok=True)):
                print(f"\n{spec.provider} is still limited after "
                      f"{limit_waits - 1} waits; stopping.")
                return 3
            stop = _limit_stop(paths, config, spec, limit, attempt=limit_waits)
            if stop is not None:
                return stop
            # Not a failed turn: nothing ran. `failures` counts a CLI that
            # broke, and three of those stop the loop — a limit is not one.
            failures = 0
            continue

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
        # Judged before compacting: the events a compaction writes are not
        # activity, and must not make an idle turn look productive.
        idle = _activity_fingerprint(tree) == before
        # Before deciding whether to stop, so the last turn of a run — the
        # second idle one, or the one at the turn limit — compacts too.
        _compact_if_due(paths, config, spec, provider, executor, context, tree,
                        compact_unsupported)
        if idle:
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


def _limit_number(config, key: str, zero_ok: bool = False) -> float:
    """A numeric `limits` value; a malformed one is the shipped default."""
    return limit_number(config.limits, key, zero_ok=zero_ok)


def _limit_flag(config, key: str, default: bool) -> bool:
    """A boolean `limits` value; anything but a YAML boolean is the default."""
    value = config.limits.get(key, default)
    return value if isinstance(value, bool) else default


def _compact_if_due(paths, config, spec, provider, executor, context: dict,
                    tree, unsupported: list) -> None:
    """Compact the session at a closed boundary, if it has grown past the mark.

    Called only after a turn that exited 0 and was not stopped by a limit. A
    compaction cannot be undone, so it happens only with nothing in flight: no
    agent active and nothing deferred. A turn that ended with agents running
    ended with reasoning in the orchestrator's head that is not on disk.

    The provider script does the work and answers with its exit code: 0
    compacted, 64 cannot (asked no more this run), anything else failed — which
    is reported and is not a failed turn.
    """
    threshold = _limit_number(config, "compact_at_tokens", zero_ok=True)
    if threshold <= 0 or unsupported:
        return
    session = context.get("MULTIAGENTS_SESSION_ID", "")
    tokens = session_context(provider, paths.root, session)
    if tokens is None or tokens < threshold:
        return
    if tree.active() or tree.read().get("deferred"):
        return
    code = _compact_session(paths, config, spec, provider, executor, context,
                            tree, tokens)
    if code == scripts.UNIMPLEMENTED:
        unsupported.append(True)


def _compact_session(paths, config, spec, provider, executor, context: dict,
                     tree, tokens: int) -> int:
    """Run the provider's `compact` action once, report it, return its code.

    Shared by the headless loop (R8c) and the attached stop-and-resume (R8f),
    which must say the same things about the same outcomes.
    """
    session = context.get("MULTIAGENTS_SESSION_ID", "")
    who = next((n.id for n in tree.drivers() if n.session == session), spec.name)
    timeout = _limit_number(config, "compact_timeout_seconds")
    # Never a probe, whatever this process inherited: a check-mode answer of 0
    # would be reported as a compaction that did not happen.
    code, out, err = scripts.run_action(
        spec.provider, provider, executor, "compact", global_config_dir(),
        paths.config, timeout=timeout,
        extra_env={**context, "MULTIAGENTS_COMPACT_CHECK": "0"}, cwd=paths.root)
    if code == 0:
        detail = next((line.strip() for line in out.splitlines() if line.strip()), "")
        print(f"\ncompacted    {detail[:500]}")
        tree.emit(who, "compacted", tokens_before=tokens, detail=detail[:500])
    elif code == scripts.UNIMPLEMENTED:
        print(f"\n{spec.provider} cannot compact a session from outside; "
              f"not asking again this run.")
        tree.emit(who, "compact_unsupported", tokens_before=tokens)
    else:
        # A tail, and a bounded one: a CLI that dumps a response body onto
        # stderr can make its "last line" megabytes long.
        tail = " | ".join(line.strip() for line in err.strip().splitlines()[-3:])
        tail = tail if len(tail) <= 500 else "…" + tail[-500:]
        print(f"\ncompaction failed (exit {code}): {tail or 'no reason given'}")
        tree.emit(who, "compact_failed", code=code, detail=tail)
    sys.stdout.flush()
    return code


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
    executor_of = executor_for(paths, config, providers)
    budgets = read_all({spec.provider: provider}, executor_of,
                       global_config_dir(), paths.config, {}, {}, use_cache=False)
    budget = budgets.get(spec.provider)
    if budget is None or budget.usable:
        return None
    when = f", resets {reset_label(budget.resets_at)}" if budget.resets_at else ""
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


def _clear_limit_pause(paths, spec) -> None:
    """Lift a limit pause when a person launches the role again.

    A spend cap is paused for hours because nothing but a human can fix it —
    which makes the human typing `multiagents run` the event it was waiting
    for. Leaving the pause up would let the session start and then refuse every
    agent it tried to spawn, for a reason already dealt with.

    Only for a person, and only for the provider named in the pause. A script
    relaunching on a timer has fixed nothing, and lifting a pause on its behalf
    would turn a stop into a loop.
    """
    if not sys.stdin.isatty():
        return                  # no human here; the pause was not for a script
    tree = Tree(paths.tree_file, paths.events_file)
    state = tree.pause_state()
    if state and spec.provider in (state.get("providers") or []):
        print(f"note         lifting the pause on {spec.provider}: "
              f"{state.get('reason', '')}")
        tree.resume("relaunched by hand")


def _provider_reset_at(paths, config, provider_name: str) -> float | None:
    """When the provider says its window reopens, as epoch seconds, or None."""
    try:
        providers = load_providers(config.providers)
        provider = providers.get(provider_name)
        if provider is None:
            return None
        budgets = read_all({provider_name: provider},
                           executor_for(paths, config, providers),
                           global_config_dir(), paths.config, {}, {},
                           use_cache=False)
        stamp = getattr(budgets.get(provider_name), "resets_at", None)
        if not stamp:
            return None
        from datetime import datetime
        when = datetime.fromisoformat(str(stamp)).timestamp()
        return when if when > time.time() else None
    except Exception:                      # a wait must never fail to happen
        return None


def _limit_stop(paths, config, spec, limit: dict, attempt: int = 1) -> int | None:
    """Act on a provider limit the CLI reported. `None` = worth trying again.

    Two limits, two answers. A window that resets is a wait: the tree is paused
    for that provider so nothing is spawned into a wall, and the caller retries
    afterwards. A spend cap does not reset — no amount of waiting adds money to
    an account — so it stops the run loudly and leaves the reason on record
    rather than burning the restart budget rediscovering it.
    """
    tree = Tree(paths.tree_file, paths.events_file)
    detail = limit.get("detail") or "the provider reported a limit"
    said = (limit.get("said") or "").strip()

    if not limit.get("resets", True):
        hours = _limit_number(config, "spend_limit_pause_hours")
        tree.pause(tree_now() + hours * 3600,
                   f"{spec.provider}: {detail}", [spec.provider])
        print(f"\n{spec.provider} has stopped: {detail}.")
        if said:
            print(f"  it said: {said}")
        print("Not retrying, and not waiting: this one does not reset on its "
              "own.\nRaise the limit or switch the orchestrator to another "
              "provider, then\n`multiagents run` picks the session back up.")
        return 3

    # Backs off across attempts: a window that has not reopened after fifteen
    # minutes is not about to, and retrying on the same clock only fills the
    # transcript with start-up-and-stop cycles.
    wait = _limit_number(config, "limit_wait_seconds") * min(attempt, 4)
    # Unless the provider says exactly when, in which case guessing is silly.
    # This is why the account is asked directly rather than through the CLI's
    # cache: a real timestamp turns a blind backoff into one wait of the right
    # length. Capped, because a bad clock or a weekly window should not park
    # the run until tomorrow without anyone deciding that.
    exact = _provider_reset_at(paths, config, spec.provider)
    if exact is not None:
        wait = max(60.0, min(exact - time.time() + 30, 6 * 3600))
        print(f"\n{spec.provider} says its window resets at "
              f"{time.strftime('%H:%M', time.localtime(exact))}.")
    tree.pause(tree_now() + wait, f"{spec.provider}: {detail}", [spec.provider])
    print(f"\n{spec.provider} has stopped: {detail}.")
    if said:
        print(f"  it said: {said}")
    print(f"Waiting {wait / 60:.0f} min for the window to reset — Ctrl-C to stop.")
    sys.stdout.flush()
    try:
        time.sleep(wait)
    except KeyboardInterrupt:
        print("\nstopped waiting.")
        return 0
    tree.resume("the usage window it was waiting for has passed")
    return None
