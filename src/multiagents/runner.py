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
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import budget as budget_mod
from . import gitops
from .config import AgentSpec, Config
from .executor import build_env, get_executor, prepare_home
from .executor.base import Handle
from .paths import ProjectPaths, global_config_dir
from .providers import Event, Provider, load_providers
from .redact import scrub
from .auth import looks_like_auth_failure
from .supervisor import Supervisor, looks_like_quota_failure
from .tree import Node, Tree, new_id, now

MAX_SUMMARY_CHARS = 6000

# How often stream progress is flushed to the shared tree. The watchdogs read
# the in-process Supervisor, not the tree, so this bounds disk writes without
# affecting supervision; it only delays what another process sees in
# check_agent by at most this long.
TREE_FLUSH_SECONDS = 2.0

# Matched against an agent's TEXT only, never tool arguments — an agent reading
# a file that mentions the marker must not park itself.
NEED_DECISION = re.compile(r"NEED_DECISION\(([^)]{0,80})\)\s*:\s*(.+)")
PROPOSED_DEFAULT = re.compile(r"(?im)^\s*DEFAULT\s*:\s*(.+)$")


PREAMBLE = """\
You are an autonomous subagent in a delegated agent tree. This block is
generated — it tells you where you stand.

- Your id: {agent_id} ({agent_name}), running on {provider}/{model}
- Parent: {parent}
- Depth: {depth} of a maximum {max_depth}
- Working directory: {workdir}
{branch_line}{spawn_line}
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
- Work only inside your working directory.
- Do not merge, rebase, push, or switch branches. Your parent owns that.

---
"""


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
    final_status: str = ""
    stop_requested: bool = False      # distinguishes an explicit stop from teardown
    awaiting: dict | None = None      # a NEED_DECISION seen mid-stream
    done: asyncio.Event = field(default_factory=asyncio.Event)


class Runner:
    def __init__(self, paths: ProjectPaths, config: Config):
        self.paths = paths
        self.config = config
        self.providers = load_providers(config.providers)
        self.tree = Tree(paths.tree_file, paths.events_file)
        self.runs: dict[str, Run] = {}

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

    # ------------------------------------------------------------- identity --

    def self_id(self) -> str | None:
        """Which node *this* server is running as, if it was spawned by us."""
        return os.environ.get("MULTIAGENTS_AGENT_ID") or None

    def self_depth(self) -> int:
        try:
            return int(os.environ.get("MULTIAGENTS_DEPTH", "0"))
        except ValueError:
            return 0

    def can_spawn(self) -> bool:
        if self.self_id() is None:
            return True                       # the root orchestrator always may
        return os.environ.get("MULTIAGENTS_CAN_SPAWN", "0") == "1"

    # ------------------------------------------------------------- guardrails --

    def _preflight(self, spec: AgentSpec) -> None:
        limits = self.config.limits
        if spec.launch:
            raise PermissionError(
                f"Agent {spec.name!r} is the orchestrator: it is launched by "
                f"`multiagents run`, not spawned as a subagent. Spawning it "
                f"would give you an orchestrator inside an orchestrator."
            )
        if not self.can_spawn():
            raise PermissionError(
                "This agent was not granted permission to spawn subagents "
                "(can_spawn is false in its config)."
            )

        depth = self.self_depth() + 1
        max_depth = int(limits.get("max_depth", 3))
        if depth > max_depth:
            raise PermissionError(f"Depth limit reached: {depth} > max_depth={max_depth}")

        active = self.tree.active()
        max_concurrent = int(limits.get("max_concurrent", 4))
        if len(active) >= max_concurrent:
            raise RuntimeError(
                f"{len(active)} agents already running (max_concurrent={max_concurrent}). "
                f"Wait for one to finish or stop it."
            )

        parent = self.self_id()
        if parent:
            siblings = [c for c in self.tree.children_of(parent) if c.status in {"pending", "running", "stuck"}]
            cap = spec.max_children or int(limits.get("max_children", 2))
            if len(siblings) >= cap:
                raise RuntimeError(f"This agent already has {len(siblings)} active children (max {cap}).")

        ceiling = int(limits.get("budget_tokens", 0) or 0)
        if ceiling:
            used = self.tree.rollup_usage().get("total", 0)
            if used >= ceiling:
                raise RuntimeError(f"Tree token budget exhausted: {used:,} >= {ceiling:,}")

        provider = self.providers.get(spec.provider)
        if provider is None:
            raise KeyError(f"Agent {spec.name!r} names unknown provider {spec.provider!r}")
        if not provider.enabled:
            raise PermissionError(
                f"Provider {provider.name!r} is disabled in providers.yaml "
                f"(needed by agent {spec.name!r}). Set `enabled: true` to use it."
            )
        if not provider.available():
            raise FileNotFoundError(f"{provider.bin!r} is not on PATH (needed by agent {spec.name!r})")
        if not (provider.spawn or {}).get("args"):
            raise PermissionError(
                f"Provider {provider.name!r} declares no spawn args, so it cannot run "
                f"delegates (agent {spec.name!r}). Without this check it would exec the "
                f"bare binary with no stdin and hang or fail obscurely."
            )

    # ---------------------------------------------------------------- prompt --

    def compose_prompt(self, spec: AgentSpec, task: str, node: Node, workdir: Path) -> str:
        limits = self.config.limits
        branch_line = f"- Branch: {node.branch} (yours alone; commit freely)\n" if node.branch else ""
        spawn_line = (
            f"- You may spawn subagents (up to {spec.max_children}).\n"
            if spec.can_spawn else "- You may not spawn subagents.\n"
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
        )
        instructions = self.config.instructions_for(spec)
        parts = [preamble]
        if instructions.strip():
            parts.append(instructions.strip() + "\n\n---\n")
        parts.append(f"## Task\n\n{task.strip()}\n")
        return "\n".join(parts)

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
    ) -> Run:
        """Build the environment and command for one turn and start the process.

        Shared by every path that runs an agent — a fresh task, a steer, and a
        turn of a standing conversation — so identity injection and credential
        handling cannot drift between them.
        """
        home = None
        if self.config.home_policy == "per-agent":
            home = prepare_home(self.paths.home(node_id), provider.home_links,
                                "per-agent", agent=spec.name,
                                copies=provider.home_copy)
        env = build_env(
            passthrough=self.config.env_passthrough,
            blocked=self.config.env_block,
            home=home,
            identity={
                "MULTIAGENTS_AGENT_ID": node_id,
                "MULTIAGENTS_PARENT_ID": parent or "",
                "MULTIAGENTS_DEPTH": str(depth),
                "MULTIAGENTS_BRANCH": branch,
                "MULTIAGENTS_CAN_SPAWN": "1" if spec.can_spawn else "0",
                "MULTIAGENTS_ROOT": str(self.paths.data),
                "MULTIAGENTS_PROJECT": str(self.paths.root),
            },
        )
        options = {"effort": spec.effort,
                   **{k: v for k, v in spec.extra.items() if isinstance(v, (str, int))}}
        argv = provider.build_command(
            prompt=prompt, model=spec.model, workdir=str(workdir),
            permission=spec.permission, session_id=session_id, options=options,
        )

        run_dir = self.paths.run_dir(node_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        turn = len(list(run_dir.glob("prompt*.md")))
        (run_dir / (f"prompt.{turn}.md" if turn else "prompt.md")).write_text(prompt)
        # Environment KEYS only — values may be secret and this file is on disk.
        (run_dir / "command.json").write_text(json.dumps(scrub({
            "argv": argv, "cwd": str(workdir), "env_keys": sorted(env),
            "provider": provider.name, "model": spec.model,
            "permission": spec.permission, "resumed": bool(session_id),
        }), indent=2))

        executor = self.executor(spec)
        problems = executor.preflight()
        if problems:
            raise RuntimeError("; ".join(problems))

        handle = await executor.start(argv, workdir, env)
        run = Run(
            node_id=node_id, provider=provider, spec=spec, handle=handle,
            supervisor=Supervisor(
                silence_timeout=spec.silence_timeout,
                wall_timeout=timeout or spec.timeout,
                max_steps=spec.max_steps,
                loop_repeats=int(self.config.limits.get("doom_loop_repeats", 3)),
            ),
        )
        self.runs[node_id] = run
        self.tree.update(node_id, pid=handle.pid)
        self.tree.set_status(node_id, "running")
        run.task = asyncio.create_task(self._consume(run))
        return run

    # ----------------------------------------------------------------- start --

    async def start(
        self,
        agent_name: str,
        task: str,
        *,
        workdir: str | None = None,
        timeout: int | None = None,
        model: str | None = None,
    ) -> dict[str, Any]:
        spec = self.config.agent(agent_name)
        if model:
            spec = AgentSpec(**{**spec.__dict__, "model": model})
        self._preflight(spec)
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
        )
        chosen, why = budget_mod.choose_provider(
            spec.provider, budgets,
            list(self.config.project.get("budget", {}).get("fallback_chain", [])),
            float(self.config.project.get("budget", {}).get("reserve_headroom", 0.15)),
        )
        if chosen is None:
            retry_at = now() + float(
                self.config.project.get("budget", {}).get("blind_cooldown_seconds", 900)
            )
            self.tree.defer({"agent": agent_name, "task": task}, retry_at, why)
            return {"deferred": True, "reason": why, "retry_after": retry_at}
        if chosen != spec.provider:
            # The model id belongs to the original provider's namespace, so it
            # is meaningless to the new one — failing over without remapping
            # would run `agy --model opencode-go/glm-5.3-flash`. Only fail over
            # if this agent names a model for the fallback provider too.
            alternative = (spec.extra.get("models") or {}).get(chosen)
            if not alternative:
                why = (f"{spec.provider} is constrained, but {spec.name!r} names no "
                       f"model for {chosen}; add one under `models:` to allow failover")
                chosen = spec.provider
            else:
                provider = self.providers[chosen]
                spec = AgentSpec(**{**spec.__dict__, "model": alternative})

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
        if not workdir and gitops.is_repo(repo):
            base = self.config.base_branch or gitops.current_branch(repo)
            desired = f"{self.config.branch_prefix}/{agent_name}/{node_id.removeprefix('ag-')}"
            worktree_path = self.paths.worktree(node_id)
            branch = gitops.create_worktree(repo, worktree_path, desired, base)

        node = Node(
            id=node_id, agent=agent_name, provider=provider.name, model=spec.model,
            parent=parent, depth=depth, task=task[:500], branch=branch,
            worktree=str(worktree_path), status="pending",
        )
        self.tree.add(node)

        prompt = self.compose_prompt(spec, task, node, worktree_path)
        try:
            run = await self._launch(
                node_id=node_id, spec=spec, provider=provider, prompt=prompt,
                workdir=worktree_path, branch=branch, parent=parent, depth=depth,
                timeout=timeout,
            )
        except RuntimeError as exc:
            self.tree.set_status(node_id, "failed", str(exc))
            return {"agent_id": node_id, "status": "failed", "error": str(exc)}

        return {
            "agent_id": node_id,
            "agent": agent_name,
            "provider": provider.name,
            "model": spec.model,
            "branch": branch or None,
            "workdir": str(worktree_path),
            "status": "running",
            "routing": why,
            "log": str(self.paths.run_dir(node_id)),
            "pid": run.handle.pid if run.handle else None,
        }

    # --------------------------------------------------------------- consume --

    async def _consume(self, run: Run) -> None:
        """Read the event stream, log it, supervise it, record the outcome."""
        node_id, provider, handle = run.node_id, run.provider, run.handle
        assert handle is not None and run.supervisor is not None
        run_dir = self.paths.run_dir(node_id)
        stream_log = (run_dir / "stream.jsonl").open("a")
        stderr_task = asyncio.create_task(handle.drain_stderr())
        watchdog = asyncio.create_task(self._watch_timers(run))
        usage: dict[str, Any] = {}
        cost_total = 0.0
        session_id = ""
        flush = _FlushGate()

        try:
            async for line in handle.lines():
                event = provider.parse_line(line)
                if event is None:
                    continue

                record = scrub({
                    "t": now(), "kind": event.kind, "name": event.name,
                    "args": event.args, "state": event.state, "status": event.status,
                    "step": event.step, "text": event.text[:2000],
                })
                stream_log.write(json.dumps(record) + "\n")
                stream_log.flush()
                run.events.append(record)

                if event.text:
                    run.text_parts.append(event.text)
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
                    run.final_status = event.status

                # Batched: see TREE_FLUSH_SECONDS. A newly captured session id
                # is flushed immediately regardless, because steer() and
                # answer_question() cannot resume an agent without it.
                batch = flush.add(urgent=captured_session)
                if batch:
                    self.tree.note_event(
                        node_id, steps=run.supervisor.steps or None,
                        usage=usage or None, session_id=session_id or None,
                        events=batch,
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

                trip = run.supervisor.observe(event)
                if trip:
                    self.tree.set_status(node_id, "stuck", f"{trip.reason}: {trip.detail}")
                    self.tree.emit(node_id, "stuck", reason=trip.reason, detail=trip.detail)

            code = await handle.wait()
        except asyncio.CancelledError:
            await handle.stop()
            # Both an explicit stop_agent() and the event loop shutting down
            # arrive here as a CancelledError, but they mean different things
            # and only one of them is anybody's decision. Recording both as
            # "cancelled by parent" makes a session ending look like a
            # deliberate kill, which is genuinely misleading when reading back
            # a log later.
            reason = ("stopped by parent" if run.stop_requested
                      else "interrupted: the server exited while this agent was running")
            self.tree.set_status(node_id, "cancelled", reason)
            raise
        except Exception as exc:
            self.tree.set_status(node_id, "failed", f"{type(exc).__name__}: {exc}")
            code = -1
        finally:
            watchdog.cancel()
            stderr_task.cancel()
            stream_log.close()

        remainder = flush.drain()
        if remainder:
            self.tree.note_event(node_id, steps=run.supervisor.steps or None,
                                 usage=usage or None, session_id=session_id or None,
                                 events=remainder)

        text = "\n".join(run.text_parts).strip()
        stderr = handle.stderr_tail
        # Checked first and unconditionally. Stopping the process makes wait()
        # return a signal code, which _classify would read as "failed"; and a
        # silence trip in the window before exit would otherwise leave the node
        # `stuck` with the question invisible.
        status = "awaiting_user" if run.awaiting else self._classify(run, code, text, stderr)

        # Commit anything the agent left uncommitted so no work is stranded on
        # an unreferenced worktree. Skipped while parked on a question: the
        # agent is mid-thought and will resume in the same worktree, and a
        # commit per question would both add noise and change what
        # _drop_if_empty decides for every later run.
        node = self.tree.get(node_id)
        if node and node.branch and Path(node.worktree).is_dir() and not run.awaiting:
            gitops.commit_all(Path(node.worktree), f"{node.agent}: work in progress ({node_id})")

        summary = text[-MAX_SUMMARY_CHARS:] if text else ""
        (run_dir / "result.json").write_text(json.dumps(scrub({
            "status": status, "exit_code": code, "session_id": session_id,
            "usage": usage, "text": text, "stderr_tail": stderr,
        }), indent=2))

        self.tree.update(node_id, usage=usage, session_id=session_id, summary=summary[:2000])
        if run.awaiting:
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
                f"{run.provider.name} is not authenticated — "
                f"run: multiagents auth login {run.provider.name}",
            )
            self.tree.emit(node_id, "unauthenticated", provider=run.provider.name)
        elif status == "quota":
            cooldown = now() + float(
                self.config.project.get("budget", {}).get("blind_cooldown_seconds", 900)
            )
            self.tree.set_cooldown(run.provider.name, cooldown, "quota failure during run")
            self.tree.set_status(node_id, "failed", "quota exhausted")
        elif self.tree.get(node_id) and self.tree.get(node_id).status == "stuck":
            pass                              # keep the trip reason visible
        elif run.spec.conversational and status == "done":
            # A conversation is not finished just because a turn is. Park it as
            # idle so the session stays resumable for the next question.
            self.tree.set_status(node_id, "idle")
        else:
            self.tree.set_status(node_id, status)

        # Auto-merge this agent's own children upward: their work is still
        # quarantined on this agent's branch, so nothing real has changed yet.
        if status == "done":
            # Children first: this agent's branch should carry their work when
            # it is itself merged upward, rather than stranding it.
            await self._merge_pending_children(node_id)
            await self._maybe_merge_into_parent(node_id)

        if not run.awaiting:
            # A parked agent still owns its worktree and will resume in it.
            self._drop_if_empty(node_id, run.spec)
        run.done.set()

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
        base = self.config.base_branch or gitops.current_branch(self.paths.root)
        if gitops.commits_on(self.paths.root, node.branch, base) == 0:
            self._cleanup(node)
            self.tree.update(node_id, branch="", worktree="")
        else:
            self.tree.emit(
                node_id, "unexpected_commits",
                branch=node.branch,
                detail="agent is configured writes:false but committed; branch kept",
            )

    async def _watch_timers(self, run: Run) -> None:
        """Detect the conditions no event will announce: silence and wall clock."""
        assert run.supervisor is not None
        while True:
            await asyncio.sleep(5)
            trip = run.supervisor.check_timers()
            if trip:
                self.tree.set_status(run.node_id, "stuck", f"{trip.reason}: {trip.detail}")
                self.tree.emit(run.node_id, "stuck", reason=trip.reason, detail=trip.detail)
                return

    def _classify(self, run: Run, code: int, text: str, stderr: str) -> str:
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
            # A headless agent that produced nothing almost always hit an
            # auto-denied permission rather than finishing successfully.
            return "failed"
        return "done"

    async def _merge_pending_children(self, node_id: str) -> None:
        """Merge children that finished while this agent was still working.

        Called once this agent's own run has ended and its worktree has been
        committed, so nothing lands under it mid-task.
        """
        for child in self.tree.children_of(node_id):
            if child.status == "done" and child.branch:
                await self._maybe_merge_into_parent(child.id)

    async def _maybe_merge_into_parent(self, node_id: str) -> None:
        node = self.tree.get(node_id)
        if not node or not node.branch or not node.parent:
            return                            # depth-1 lands via explicit merge
        policy = self.config.project.get("git", {}).get("merge", {})
        if policy.get("inside_tree", "auto") != "auto":
            return
        parent = self.tree.get(node.parent)

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
        status, detail = gitops.merge(
            target, node.branch, f"{node.agent}: {node.task[:72]}",
            policy.get("style", "squash"),
        )
        self.tree.emit(node_id, "merge", result=status, detail=detail[:400], into=str(target))
        if status == "merged":
            self.tree.set_status(node_id, "merged")
            self._cleanup(node)
        elif status == "conflict":
            self.tree.set_status(node_id, "done", "merge conflict; branch kept for parent")

    def _cleanup(self, node: Node) -> None:
        """Remove a finished agent's worktree and branch.

        Only ever called after a successful merge or an explicit discard, so
        force-deleting the branch is safe: its commits are already elsewhere.
        """
        if node.worktree and Path(node.worktree).is_dir():
            gitops.remove_worktree(self.paths.root, Path(node.worktree), force=True)
        if node.branch:
            gitops.delete_branch(self.paths.root, node.branch, force=True)

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
        return result

    def _read_stream(self, agent_id: str) -> list[dict]:
        path = self.paths.run_dir(agent_id) / "stream.jsonl"
        if not path.is_file():
            return []
        out = []
        with path.open() as handle:
            for line in handle:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return out

    def collect(self, agent_id: str, mode: str = "summary") -> dict[str, Any]:
        node = self.tree.get(agent_id)
        if node is None:
            raise KeyError(f"Unknown agent {agent_id!r}")
        run_dir = self.paths.run_dir(agent_id)
        result_file = run_dir / "result.json"
        data: dict[str, Any] = {}
        if result_file.is_file():
            try:
                data = json.loads(result_file.read_text())
            except json.JSONDecodeError:
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
            payload["commits"] = gitops.commits_on(self.paths.root, node.branch, base)
            payload["diff_stat"] = gitops.diff_stat(self.paths.root, node.branch, base)[:2000]
        return payload

    # ---------------------------------------------------------------- control --

    async def stop(self, agent_id: str) -> dict[str, Any]:
        run = self.runs.get(agent_id)
        if run is not None:
            run.stop_requested = True     # recorded before the cancel lands
        if run and run.task and not run.task.done():
            run.task.cancel()
            try:
                await run.task
            except (asyncio.CancelledError, Exception):
                pass
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
                executor = self.executor()
                killer = getattr(executor, "kill_detached", None)
                if killer is not None:
                    killer(agent_id)
                if node.pid:
                    try:
                        os.killpg(os.getpgid(node.pid), 15)
                    except (ProcessLookupError, PermissionError, OSError):
                        pass
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
        if not node.session_id:
            return {
                "agent_id": agent_id, "steered": False,
                "error": "no session id captured yet; the agent has not produced "
                         "enough of its stream to be resumable. Try again shortly, "
                         "or stop it and start a fresh run.",
            }
        await self.stop(agent_id)
        spec = self.config.agent(node.agent)
        provider = self.providers[node.provider]
        try:
            await self._launch(
                node_id=agent_id, spec=spec, provider=provider, prompt=message,
                workdir=Path(node.worktree), branch=node.branch,
                parent=node.parent, depth=node.depth, session_id=node.session_id,
            )
        except RuntimeError as exc:
            self.tree.set_status(agent_id, "failed", str(exc))
            return {"agent_id": agent_id, "steered": False, "error": str(exc)}
        self.tree.set_status(agent_id, "running", "steered")
        self.tree.emit(agent_id, "steered", message=message[:400])
        return {"agent_id": agent_id, "steered": True, "status": "running"}

    # ---------------------------------------------------------- conversation --

    def _find_conversation(self, agent_name: str) -> Node | None:
        """The standing conversation node for this agent, if one exists.

        Found by scanning the shared tree rather than an in-process map, so a
        conversation survives a server restart and is visible to nested agents.
        """
        best: Node | None = None
        for raw in self.tree.read()["nodes"].values():
            if raw.get("agent") != agent_name or not raw.get("conversation"):
                continue
            if raw.get("status") in {"idle", "running", "stuck"} and raw.get("session_id"):
                node = Node(**raw)
                if best is None or node.created_at > best.created_at:
                    best = node
        return best

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
        provider = self.providers.get(spec.provider)
        if provider is None or not provider.available():
            raise FileNotFoundError(f"provider {spec.provider!r} is unavailable")

        node = self._find_conversation(agent_name)
        turn = 1

        if node is None:
            self._preflight(spec)
            parent = self.self_id()
            depth = self.self_depth() + 1
            node_id = new_id()
            worktree_path, branch = self.paths.root, ""
            if gitops.is_repo(self.paths.root):
                base = self.config.base_branch or gitops.current_branch(self.paths.root)
                worktree_path = self.paths.worktree(node_id)
                branch = gitops.create_worktree(
                    self.paths.root, worktree_path,
                    f"{self.config.branch_prefix}/{agent_name}/{node_id.removeprefix('ag-')}",
                    base,
                )
            node = Node(
                id=node_id, agent=agent_name, provider=provider.name, model=spec.model,
                parent=parent, depth=depth, task=message[:500], branch=branch,
                worktree=str(worktree_path), status="pending", conversation=True,
            )
            self.tree.add(node)
            prompt = self.compose_prompt(spec, message, node, worktree_path)
            session_id = None
        else:
            node_id = node.id
            turn = node.turns + 1
            worktree_path = Path(node.worktree)
            prompt = message
            session_id = node.session_id

        self.tree.update(node_id, turns=turn)
        try:
            run = await self._launch(
                node_id=node_id, spec=spec, provider=provider, prompt=prompt,
                workdir=worktree_path, branch=node.branch, parent=node.parent,
                depth=node.depth, session_id=session_id, timeout=timeout,
            )
        except RuntimeError as exc:
            self.tree.set_status(node_id, "failed", str(exc))
            return {"agent_id": node_id, "error": str(exc)}

        limit = timeout or spec.timeout
        try:
            await asyncio.wait_for(run.done.wait(), timeout=limit + 30)
        except (asyncio.TimeoutError, TimeoutError):
            await self.stop(node_id)
            return {
                "agent_id": node_id, "turn": turn, "timed_out": True,
                "error": f"no reply within {limit}s",
            }

        final = self.tree.get(node_id)
        reply = "\n".join(run.text_parts).strip()
        if run.awaiting:
            # The advisor stopped to ask, not to answer. Returning its partial
            # text would read as a considered reply.
            return {
                "agent_id": node_id, "agent": agent_name, "turn": turn,
                "status": "awaiting_user",
                "asked": run.awaiting["question"],
                "topic": run.awaiting["topic"],
                "proposed_default": run.awaiting["proposed"],
                "partial_reply": reply[-2000:],
                "note": "this agent asked a question instead of answering; "
                        "resolve it with answer_question before relying on this",
            }
        return {
            "agent_id": node_id,
            "agent": agent_name,
            "turn": turn,
            "status": final.status if final else "unknown",
            "reply": reply[-MAX_SUMMARY_CHARS:],
            "usage": final.usage if final else {},
            "note": "advisory only — you decide whether to act on this",
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

    async def wait_for_any(self, agent_ids: list[str] | None, timeout: float) -> dict[str, Any]:
        """Block until any of the given agents leaves the running state.

        Polls the shared tree rather than only in-process events, so an
        orchestrator can also wait on agents started by a nested server.
        """
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
        if not watched:
            return {"changed": [], "reason": "no active agents"}

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
            if node.status in {"pending", "running"}:
                pending.append(agent_id)
            else:
                already.append({
                    "agent_id": agent_id, "agent": node.agent,
                    "status": node.status, "reason": node.reason,
                })

        if not pending:
            return {"changed": already, "all_finished": True,
                    "note": "every agent you named had already finished"}

        while time.monotonic() < deadline:
            changed = []
            for agent_id in pending:
                node = self.tree.get(agent_id)
                if node is not None and node.status not in {"pending", "running"}:
                    changed.append({
                        "agent_id": agent_id, "agent": node.agent,
                        "status": node.status, "reason": node.reason,
                    })
            if changed:
                return {
                    "changed": changed,
                    "already_finished": already,
                    "still_running": [i for i in pending if i not in {c["agent_id"] for c in changed}],
                    "waited_seconds": round(timeout - (deadline - time.monotonic())),
                }
            await asyncio.sleep(1.0)

        return {
            "changed": [],
            "timed_out": True,
            "still_running": [
                {"agent_id": n.id, "agent": n.agent, "status": n.status,
                 "elapsed_seconds": round(n.elapsed())}
                for n in self.tree.active() if n.id in watched
            ],
        }

    # ------------------------------------------------------------------- git --

    def merge_agent(self, agent_id: str, into: str | None = None) -> dict[str, Any]:
        node = self.tree.get(agent_id)
        if node is None:
            raise KeyError(f"Unknown agent {agent_id!r}")
        if not node.branch:
            return {"agent_id": agent_id, "merged": False, "error": "agent has no branch (writes: false)"}
        target = Path(into).expanduser() if into else self.paths.root
        policy = self.config.project.get("git", {}).get("merge", {})
        status, detail = gitops.merge(
            target, node.branch, f"{node.agent}: {node.task[:72]}", policy.get("style", "squash")
        )
        self.tree.emit(agent_id, "merge", result=status, detail=detail[:400], into=str(target))
        if status == "merged":
            self.tree.set_status(agent_id, "merged")
            self._cleanup(node)
        return {"agent_id": agent_id, "result": status, "detail": detail[:1000], "branch": node.branch}

    def discard_agent(self, agent_id: str, force: bool = False) -> dict[str, Any]:
        node = self.tree.get(agent_id)
        if node is None:
            raise KeyError(f"Unknown agent {agent_id!r}")
        base = self.config.base_branch or gitops.current_branch(self.paths.root)
        unmerged = gitops.commits_on(self.paths.root, node.branch, base) if node.branch else 0
        if unmerged and not force:
            return {
                "agent_id": agent_id, "discarded": False, "unmerged_commits": unmerged,
                "error": f"branch has {unmerged} unmerged commit(s). Pass force=true to "
                         f"delete this work permanently.",
            }
        self._cleanup(node)
        self.tree.set_status(agent_id, "discarded", "discarded by parent")
        return {"agent_id": agent_id, "discarded": True, "branch": node.branch}

    def push_branch(self, agent_id: str | None, remote: str | None = None) -> dict[str, Any]:
        target_remote = remote or self.config.remote
        if not target_remote:
            return {"pushed": False, "error": "no remote configured (git.remote is empty in project.yaml)"}
        if agent_id:
            node = self.tree.get(agent_id)
            if node is None or not node.branch:
                return {"pushed": False, "error": f"no branch for {agent_id!r}"}
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
