"""Legacy quota handover policy and its durable recovery protocol (C23).

An attempt advances through prepared -> transferred -> launching -> launched
-> completed, or failed. Prepared/ transferred can finish the same idempotent
transfer after a crash. Launching is a write-ahead fence: recovery follows the
recorded wrapper if there is one, and refuses to launch again if spawn cannot
be proved absent. Launched is adopted, never spawned again. Completed/failed
are terminal. The supervisor lock owns reconciliation, and the node's pending
reservation owns admission. Stop cancels the owner before changing status;
steer and merge refuse an in-flight attempt. Stop writes a terminal failure
before cancellation; transfer and launch boundaries recheck it, so recovery
cannot resurrect a stopped attempt. No transfer starts until the old wrapper
AND its session are confirmed dead.
"""
from __future__ import annotations

import asyncio
import contextlib
import copy
import hashlib
import functools
import threading
from contextvars import ContextVar
from dataclasses import asdict, replace
import os
from pathlib import Path
import re
import stat
import uuid

from . import budget, gitops, procs
from .config import AgentSpec
from .paths import global_config_dir
from .transcripts import session_transcript
from .providers import expand_env_value, resolved_profile


# Retained as an inert diagnostic seam for old observers. Admission never
# reads or sets task context: a floor grant is bound to one run and consumed.
_floor_dispatch: ContextVar[str] = ContextVar("quota_floor_dispatch", default="")


def quota_handover_settings(project: dict) -> dict:
    """Resolve project defaults and the effective reservation without runner state."""
    settings = {"enabled": True, "reserved_instance": None, "reserve_fraction": .25,
                **(project.get("quota_handover") or {})}
    if not settings["enabled"]:
        settings["reserved_instance"] = None
    return settings


async def before_transfer(attempt: dict) -> None:
    """Deterministic test gate, after prepared is durable, before transfer.

    Monkeypatch with an async gate (or a process crash). Not configuration.
    """


def copy_chunk(output, chunk: bytes) -> None:
    """Deterministic mid-copy I/O fault seam; patch to write then raise."""
    output.write(chunk)


def before_install(directory: int, staged_name: str, target_name: str) -> None:
    """Deterministic gate after staging fsync, immediately before rename."""


def _validate_components(path: Path) -> None:
    """Reject links anywhere on a store path, including missing-file parents."""
    path = path.absolute()
    for component in [*reversed(path.parents), path]:
        if component.is_symlink():
            raise ValueError("session transfer refuses symlinks")


def _directory_fd(path: Path, *, create=False) -> int:
    """Open each directory without following links, closing the check/use race.

    All install operations are relative to this descriptor, so replacing an
    ancestor with a symlink cannot redirect a staged rollout or its rename.
    """
    path = path.absolute()
    fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            if create:
                with contextlib.suppress(FileExistsError):
                    os.mkdir(part, 0o700, dir_fd=fd)
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except OSError as exc:
        os.close(fd)
        raise ValueError("session directory is unavailable or contains a symlink") from exc


def _check_session_fd(fd: int, credentials=()) -> None:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError("session transfer requires a regular file with exactly one link")
    if (info.st_dev, info.st_ino) in credentials:
        raise ValueError("session rollout aliases a profile credential")


@contextlib.contextmanager
def _source_session(path: Path, profile: Path | None = None):
    """Pin credentials and validate the one descriptor that supplies bytes.

    Capture credential identities before opening the rollout, so renaming a
    credential onto that name cannot hide its identity by removing auth.json.
    The caller keeps this descriptor across before_transfer and the copy.
    """
    # QH-R8 prevents copying credential files, not arbitrary transcript bytes a profile writer supplies.
    credentials = set()
    if profile is not None:
        directory = _directory_fd(profile)
        try:
            for name in ("auth.json", "credentials.json", ".credentials.json"):
                try:
                    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                 dir_fd=directory)
                except FileNotFoundError:
                    continue
                try:
                    info = os.fstat(fd)
                    credentials.add((info.st_dev, info.st_ino))
                finally:
                    os.close(fd)
        finally:
            os.close(directory)
    directory = _directory_fd(path.parent)
    try:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                     dir_fd=directory)
    finally:
        os.close(directory)
    with os.fdopen(fd, "rb") as source:
        _check_session_fd(source.fileno(), credentials)
        yield source, credentials


def _read_session(directory: int, name: str) -> bytes:
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    with os.fdopen(fd, "rb") as source:
        _check_session_fd(source.fileno())
        return source.read()


def _verify_install(directory: int, name: str, written_fd: int) -> None:
    # QH-R8 verifies file identity, not transcript contents a profile writer can rewrite before or after install.
    # O_PATH also lets us identify a swapped symlink without following it.
    fd = os.open(name, getattr(os, "O_PATH", os.O_RDONLY | os.O_NONBLOCK) | os.O_NOFOLLOW,
                 dir_fd=directory)
    try:
        installed, written = os.fstat(fd), os.fstat(written_fd)
        identity = (installed.st_dev, installed.st_ino)
        if identity != (written.st_dev, written.st_ino):
            # Remove only the entry still naming the inode we just observed;
            # a concurrent writer's replacement does not belong to this copy.
            current = os.stat(name, dir_fd=directory, follow_symlinks=False)
            if (current.st_dev, current.st_ino) == identity:
                os.unlink(name, dir_fd=directory)
                os.fsync(directory)
            raise ValueError("installed session identity differs from staged data")
    finally:
        os.close(fd)


def transfer_session(source: Path, target: Path, *, opened_source=None) -> None:
    """Atomically install the declared session, never credentials or links.

    R30: identical sessions are idempotent; a strict prefix fast-forwards.
    Neither direction permits divergent histories. Descriptor-relative staging,
    fsync and rename preserve the old target even when copy_chunk raises.
    """
    _validate_components(source)
    _validate_components(target)
    with contextlib.ExitStack() as stack:
        stream, credentials = opened_source or stack.enter_context(_source_session(source))
        _check_session_fd(stream.fileno(), credentials)
        source_data = stream.read()
    if source == target:
        return
    directory = _directory_fd(target.parent, create=True)
    name = ".handover-" + uuid.uuid4().hex
    def current():
        try:
            return _read_session(directory, target.name)
        except FileNotFoundError:
            return None
    try:
        old = current()
        if old is not None:
            if old.startswith(source_data):
                return
            if not source_data.startswith(old):
                raise ValueError("session histories diverge")
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=directory)
        with os.fdopen(fd, "wb") as out:
            for offset in range(0, len(source_data), 64 * 1024):
                copy_chunk(out, source_data[offset:offset + 64 * 1024])
            out.flush()
            os.fsync(out.fileno())
            if current() != old:
                raise ValueError("target session changed during transfer")
            before_install(directory, name, target.name)
            os.replace(name, target.name, src_dir_fd=directory, dst_dir_fd=directory)
            _verify_install(directory, target.name, out.fileno())
            os.fsync(directory)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(name, dir_fd=directory)
        os.close(directory)


def session_store_root(provider, executor):
    """Resolve a declared store through the same profile as the adapter.

    Docker's private_state maps that account's CODEX_HOME to its host
    backing. Locally, resolved_profile is also what _launch exports. No
    credential, global history or SQLite index is included in the store.
    """
    declaration = provider.session_store
    if getattr(executor, "kind", "local") == "docker":
        backing = list(getattr(executor, "private_state", lambda _: {})(provider.name).values())
        if len(backing) != 1:
            raise ValueError("session store needs one account-specific private profile")
        profile = Path(backing[0])
    else:
        field = declaration.get("profile_env", "")
        env = provider.credential_env or provider.env or {}
        configured = expand_env_value(env[field]) if field and env.get(field) else ""
        profile = Path(configured or resolved_profile(provider)
                       or declaration.get("profile_default", "")).expanduser()
        if not profile.is_absolute():
            raise ValueError("session store needs an absolute adapter profile")
        forbidden = declaration.get("forbidden_profile")
        if forbidden and profile.resolve() == Path(forbidden).expanduser().resolve():
            raise ValueError("session store resolves to a forbidden user profile")
    subdir = Path(declaration.get("dir", ""))
    if subdir.is_absolute() or not subdir.parts or ".." in subdir.parts:
        raise ValueError("session store must be a subdirectory of the private profile")
    root = profile / subdir
    if profile.resolve() not in root.resolve().parents:
        raise ValueError("session store escapes its private profile")
    _validate_components(root)
    return root


def stored_session(provider, executor, session_id):
    """Locate exactly one conversation in a profile store, or refuse.

    Codex resumes a UUID from its rollout and read-repairs its own state
    database. Copying the database would copy unrelated sessions and retain
    source-profile paths, so only the matching rollout is installed.
    https://github.com/openai/codex/blob/main/codex-rs/rollout/src/list.rs
    """
    if not re.fullmatch(r"[A-Za-z0-9_-]+", session_id):
        raise ValueError("invalid session id for session-store lookup")
    root = session_store_root(provider, executor)
    pattern = provider.session_store.get("glob", "")
    if (pattern.count("{session_id}") != 1 or Path(pattern).is_absolute()
            or ".." in Path(pattern).parts):
        raise ValueError("session store glob must select the session id")
    paths = sorted(root.glob(pattern.format(session_id=session_id)))
    if len(paths) > 1:
        raise ValueError("multiple session rollouts; refusing an ambiguous transfer")
    if paths and (paths[0].is_symlink() or root.resolve() not in paths[0].resolve().parents):
        raise ValueError("session rollout escapes its declared store")
    if paths:
        _validate_components(paths[0])
    return root, paths[0] if paths else None


def transfer_paths(source_provider, target_provider, worktree, session_id, source_executor, target_executor):
    if source_provider.session_store or target_provider.session_store:
        if not source_provider.session_store or not target_provider.session_store:
            raise ValueError("both siblings must declare compatible session stores")
        source_root, source = stored_session(source_provider, source_executor, session_id)
        target_root, target = stored_session(target_provider, target_executor, session_id)
        if source is None:
            raise ValueError("source session rollout is unavailable")
        target = target or target_root / source.relative_to(source_root)
        _validate_components(target)
        if target_root.resolve() not in target.resolve().parents:
            raise ValueError("target session escapes its declared store")
        return source, target
    source = session_transcript(source_provider, worktree, session_id, source_executor)
    target = session_transcript(target_provider, worktree, session_id, target_executor)
    if source is None or target is None:
        raise ValueError("provider declares no transferable session store")
    return source, target


def promotion_merge_guard(fn):
    """Reserve metadata only; git and tree I/O happen after this lock."""
    @functools.wraps(fn)
    def guarded(self, agent_id, *args, **kwargs):
        with self._qh_mutex:
            merging = self.__dict__.setdefault("_qh_merging", set())
            if agent_id in self.__dict__.get("_qh_busy", set()) or agent_id in merging:
                return {"agent_id": agent_id, "merged": False, "error": "quota handover or merge is in progress"}
            merging.add(agent_id)
        try:
            return fn(self, agent_id, *args, **kwargs)
        finally:
            with self._qh_mutex:
                merging.discard(agent_id)
    return guarded


class QuotaHandover:
    @property
    def _qh_mutex(self):
        return self.__dict__.setdefault("_qh_metadata_lock", threading.Lock())

    def _qh_now(self):
        # The existing runner clock is the public deterministic clock seam.
        from .runner import now
        return now()

    def _qh_settings(self, spec=None):
        return {**quota_handover_settings(self.config.project),
                **(spec.quota_handover if spec else {})}

    def _qh_enabled(self, spec=None, node=None):
        # Scheduler admission of a legacy start does not make it managed.
        # Only the persisted node_id/activation identity excludes a run.
        return (self._qh_settings()["enabled"]
                and (spec is None or spec.handover)
                and (node is None or (not node.node_id and not node.role)))

    def _qh_reserved(self):
        return self._qh_settings()["reserved_instance"]

    def _qh_above_floor(self, entry):
        if entry is None or not entry.known or entry.headroom is None:
            return True
        # Select short, counted windows rather than a weekly limiting bucket.
        windows = [(float(w.get("span_minutes") or 0), w) for w in entry.windows.values()
                   if isinstance(w, dict) and w.get("counted", True)
                   and w.get("span_minutes") and w.get("percent") is not None]
        headroom = entry.headroom
        if windows:
            shortest = min(span for span, _ in windows)
            headroom = min(1 - float(w["percent"]) / 100
                           for span, w in windows if span == shortest)
        return headroom > self._qh_settings()["reserve_fraction"]

    def _qh_names(self, spec):
        if spec.priorities is not None:
            from .config import priority_entries
            return [name for names, _ in priority_entries(spec, self.providers) for name in names]
        return list(dict.fromkeys([spec.provider, *(spec.models or {})]))

    def _qh_priority_names(self, spec, readings=None):
        if spec.priorities is None:
            return self._qh_names(spec)
        from .config import priority_entries
        load, last_used = self._instance_load()
        result = []
        for names, _ in priority_entries(spec, self.providers):
            pool = [n for n in names if n != self._qh_reserved()]
            while pool:
                chosen = budget.pick_instance(pool, readings or {}, 0, set(), load, last_used,
                                              **self._instance_strategy(pool[0]))
                chosen = chosen or pool[0]
                result.append(chosen)
                pool.remove(chosen)
            result.extend(n for n in names if n == self._qh_reserved())
        return result

    def _qh_position(self, node):
        if node is None:
            return {"rank": None, "pinned": False}
        return {"rank": node.rank, "pinned": node.pinned}


    async def _qh_budgets(self, spec=None):
        return await asyncio.to_thread(
            budget.read_all, self.providers, lambda _: self.executor(spec),
            global_config_dir(), self.paths.config, None,
            self.tree.read().get("cooldowns", {}), limits=self.config.limits)

    async def _qh_usable(self, name, spec, readings, node=None, *, floor=False):
        provider = self.providers.get(name)
        if (not provider or not provider.enabled or not spec.model
                or self._model_refusal(name, spec.model)
                or self._transport_refusal(provider) or self.startup.availability(name)
                or self._cap_refusal(name, spec.model)):
            return False
        if node and node.quota_stops.get(name, 0) > self._qh_now():
            return False
        cooldown = self.tree.read().get("cooldowns", {}).get(name) or {}
        until = cooldown.get("until", 0) if isinstance(cooldown, dict) else cooldown
        if until and until > self._qh_now():
            return False
        entry = readings.get(name)
        if entry and entry.known and entry.headroom is not None and entry.headroom <= .02:
            return False
        if entry and entry.cooldown_until and entry.cooldown_until > self._qh_now():
            return False
        if name == self._qh_reserved() and not floor and not self._qh_above_floor(entry):
            return False
        if self._pc_admit(self.tree.read(), name, node.id if node else "") is not None:
            return False
        return await asyncio.to_thread(self._auth_ok, name, self.executor(spec)) is not False

    def _qh_choose_new(self, spec, readings, choose, *, managed=False, floor=False):
        """QH-R6/R13: apply reservation around C22, retaining its strategy."""
        self.__dict__["_qh_readings"] = readings
        reserved = self._qh_reserved()
        if spec.priorities is not None and not managed:
            for name in self._qh_priority_names(spec, readings):
                provider = self.providers[name]
                routed = self._usable_spec(spec, name)
                entry = readings.get(name)
                if (not provider.enabled or not routed or not routed.model
                        or self._model_refusal(name, routed.model) or self._transport_refusal(provider)
                        or self.startup.availability(name)):
                    continue
                if entry and (entry.cooldown_until and entry.cooldown_until > self._qh_now()
                              or entry.known and entry.headroom is not None and entry.headroom <= .02):
                    continue
                if name == reserved and not floor and not self._qh_above_floor(entry):
                    continue
                return name, "agent priority list"
            return None, "no usable instance in agent priority list"
        if managed or not reserved or reserved not in self._qh_names(spec):
            return choose(readings)
        alternative = dict(readings)
        old = readings.get(reserved) or budget.Budget(reserved, known=False)
        alternative[reserved] = replace(old, cooldown_until=self._qh_now() + 10**9)
        chosen, reason = choose(alternative)
        if chosen is not None:
            return chosen, reason
        if floor or self._qh_above_floor(old):
            return choose(readings)
        return None, "reserved instance is at its quota floor"

    def _qh_event(self, node, kind, attempt, reason="", **extra):
        fields = {
            "agent_id": node.id if node else "",
            "session_id": attempt.get("session_id", ""),
            "from": attempt.get("from", ""), "to": attempt.get("to", ""),
            "tier": attempt.get("tier", 4), "segment": attempt.get("segment", 1),
            "attempt": attempt.get("attempt", 1), "reason": reason or attempt.get("reason", "quota"),
            "at": self._qh_now(), **extra}
        if kind.startswith("promote_"):
            fields.pop("tier")
            fields.update(from_rank=attempt["from_rank"], to_rank=attempt["to_rank"])
        self.tree.emit_checked(node.id if node else "", kind, **fields)

    def _qh_cancel(self, node_id):
        task = self.__dict__.get("_qh_promotions", {}).get(node_id)
        if task and task is not asyncio.current_task():
            task.cancel()
        node = self.tree.get(node_id)
        if node and node.promotion:
            self._qh_event(node, "promote_failed", node.promotion, "stopped by parent")
            self.tree.update(node_id, promotion=None)
        attempt = node.handover_attempt if node else None
        if attempt and attempt.get("state") in ("prepared", "transferred", "launching", "launched"):
            failed = dict(attempt, state="failed", error="stopped by parent")
            self.tree.update(node_id, handover_attempt=failed)
            self._qh_event(node, "handover_failed", failed, failed["error"])
        pending = ("queued", "requested", "allowed")
        if any(r.get("node_id") == node_id and r.get("state") in pending
               for r in self.tree.read().get("quota_reserve", [])):
            with self.tree.transaction() as data:
                for request in data.get("quota_reserve", []):
                    if request.get("node_id") == node_id and request.get("state") in pending:
                        request["state"] = "cancelled"

    def _qh_check_switch(self, node_id, attempt_id=None):
        node = self.tree.get(node_id)
        if (not node or node.status == "cancelled"
                or (node.handover_attempt or {}).get("state") == "failed"
                or (attempt_id is not None and (node.handover_attempt or {}).get("attempt") != attempt_id)):
            raise RuntimeError("handover was stopped")

    def _qh_account(self, provider, spec):
        # A pinned vault account is known. An unpinned auth proxy may choose
        # an account per request, so guessing from the provider name is wrong.
        executor = self.executor(spec)
        if getattr(executor, "kind", "local") == "docker":
            pins = getattr(executor, "account_pins", lambda: {})()
            return pins.get(provider.name)
        return None

    def _qh_segment(self, provider, spec, session_id=""):
        return {"provider": provider.name, "account": self._qh_account(provider, spec),
                "model": spec.model, "session_id": session_id,
                "started_at": self._qh_now(), "ended_at": None, "end_reason": "", "usage": {}}

    def _qh_confirm(self, run, session_id):
        node = self.tree.get(run.node_id)
        attempt = node.handover_attempt if node else None
        if (not attempt or attempt.get("state") not in ("launching", "launched")
                or attempt.get("to") != run.provider.name or not session_id
                or (attempt.get("resume") and session_id != attempt.get("target_session_id", attempt.get("session_id")))):
            return
        # A provider's session event verifies the handover. The remainder of
        # a live turn is steerable; it is no longer an in-flight transfer.
        completed = dict(attempt, state="completed")
        self.tree.update(node.id, handover_attempt=completed)
        self._qh_event(node, "handover_completed", completed)
        self._qh_promoted(node, completed)

    def _qh_promoted(self, node, attempt):
        promotion = attempt.get("promotion")
        if not promotion or not node.promotion:
            return
        self._qh_event(node, "promote_completed", promotion)
        self.tree.update(node.id, promotion=None, on_reserve_floor=False)
        if node.reserve_request:
            self._qh_request_state(node.reserve_request, state="finished")

    def _qh_restore_launch(self, node_id, attempt):
        self.launch_limits.record_spec(node_id, attempt["source_spec"],
            attempt.get("source_launched_at") or self.launch_limits.launch_time(node_id) or self._qh_now(),
            attempt.get("source_prompt_file", self.launch_limits.prompt_file(node_id)))

    def _qh_launched(self, node_id, spec, provider, session_id):
        node = self.tree.get(node_id)
        if not self._qh_enabled(node=node):
            return
        segments = copy.deepcopy(node.segments)
        if not segments:
            segments.append(self._qh_segment(provider, spec, session_id or ""))
        base = copy.deepcopy(segments[-1].get("usage") or {})
        segments[-1].update(ended_at=None, end_reason="")
        roster = self.config.agents.get(node.agent)
        names = self._qh_priority_names(roster, self.__dict__.get("_qh_readings", {})) if roster else []
        self.tree.update(node_id, rank=names.index(provider.name) if provider.name in names else None,
                         home_provider=node.home_provider or provider.name,
                         segments=segments, provider=provider.name, model=spec.model,
                         effort=spec.effort or "", segment_usage_base=base)
        attempt = node.handover_attempt or {}
        if attempt.get("state") == "failed" and attempt.get("from") == provider.name:
            # A new source turn is a new quota-stop episode. Failed candidates
            # were tried once for the old stop, not banned for the whole run.
            self.tree.update(node_id, handover_attempt=dict(attempt, tried=[]))

    def _qh_segment_end(self, run, usage, session_id, reason):
        node = self.tree.get(run.node_id)
        if not node or not node.segments:
            return
        segments = copy.deepcopy(node.segments)
        segment = segments[-1]
        from .tree import sum_usage
        cumulative = (sum_usage([{"usage": node.segment_usage_base}, {"usage": usage}])
                      if node.segment_usage_base else usage)
        segment.update(session_id=session_id or segment["session_id"],
                       ended_at=self._qh_now(), end_reason=reason, usage=cumulative)
        self.tree.update(node.id, segments=segments)

    def _qh_total_usage(self, node_id, usage):
        from .tree import sum_usage
        node = self.tree.get(node_id)
        if not node or not node.segments:
            return usage
        if len(node.segments) == 1:
            return node.segments[0].get("usage") or usage
        return sum_usage(node.segments)

    async def _qh_candidates(self, node, spec, readings):
        roster = self.config.agent(node.agent)
        reserved = self._qh_reserved()
        candidates = []
        for name in self._qh_priority_names(roster, readings):
            if name == node.provider:
                continue
            prior = node.handover_attempt or {}
            if (prior.get("from") == node.provider
                    and prior.get("segment") == len(node.segments) + 1
                    and name in prior.get("tried", [])):
                continue
            routed = self._usable_spec(roster, name)
            if routed is None:
                continue
            sibling = self._family_of(name) == self._family_of(node.provider)
            mode = self.providers[name].handover_mode.get(self.executor(spec).kind, "none")
            if sibling and routed.model != spec.model:
                self.tree.emit(node.id, "handover_policy_gap", provider=name,
                               reason="same-family candidate supports only a different model; migration policy is unresolved")
                continue
            resume = sibling and mode != "none"
            tier = (2 if name == reserved else 1) if resume else 3
            if await self._qh_usable(name, routed, readings, node):
                candidates.append((tier, name, resume))
        if roster.priorities is not None:
            return candidates
        result = []
        for tier in (1, 2, 3):
            pool = [c for c in candidates if c[0] == tier]
            if tier in (1, 2):
                load, last_used = self._instance_load()
                while pool:
                    chosen = budget.pick_instance([c[1] for c in pool], readings, 0,
                                                  set(), load, last_used,
                                                  **self._instance_strategy(node.provider))
                    candidate = next((c for c in pool if c[1] == chosen), pool[0])
                    result.append(candidate)
                    pool.remove(candidate)
            else:
                result.extend(pool)
        # QH-R13: use a reserved sibling only after every nonreserved route.
        return [c for c in result if c[1] != reserved] + [c for c in result if c[1] == reserved]

    def _qh_prompt(self, node, message, *, resume, promotion=False):
        if resume:
            return message or self._retry_prompt(node.id, "prompt.md")
        run_dir = self.paths.run_dir(node.id)
        original = (run_dir / "prompt.md").read_text()
        base = self.config.base_branch or gitops.current_branch(self.paths.root)
        commits = gitops.run(self.paths.root, "log", "--format=%h %s",
                             f"{base}..{node.branch}").out if node.branch else ""
        cause = "moved for priority" if promotion else "cut by quota"
        return (original + f"\n\nContinuation: the previous segment was {cause}. "
                f"Read its run log at .multiagents/runs/{node.id}/. "
                f"Commits since launch: {commits}. The worktree may contain uncommitted "
                "work from that segment.\n" + message)

    async def _qh_switch(self, node, spec, target, tier, resume, *, run=None, message="", attempt=None,
                         promotion=None, target_session_id=None, target_spec=None, promotion_rollback=None):
        # Same-session migration preserves the complete frozen launch spec;
        # a continuation remaps only destination options, never ownership.
        if resume:
            routed = spec.replace(provider=target)
        else:
            roster = self.config.agent(node.agent)
            destination = self._usable_spec(roster, target) or roster.routed(target)
            routed = spec.replace(provider=target, model=destination.model,
                                  effort=destination.effort, extra=destination.extra)
        if target_spec is not None:
            routed = target_spec.replace(provider=target)
        if attempt is not None:
            promotion = attempt.get("promotion")
            target_session_id = attempt.get("target_session_id", target_session_id)
        launch_session = target_session_id if target_session_id is not None else node.session_id
        provider = self.providers[target]
        if attempt is None:
            prior = node.handover_attempt or {}
            tried = prior.get("tried", []) if (
                prior.get("from") == node.provider
                and prior.get("segment") == len(node.segments) + 1) else []
            attempt = {"from": node.provider, "to": target, "tier": tier,
                       "segment": len(node.segments) + (target != node.provider),
                       "new_segment": target != node.provider,
                       "attempt": int(prior.get("attempt", 0)) + 1,
                       "session_id": node.session_id, "resume": resume,
                       "state": "prepared", "reason": "priority" if promotion else "quota" if not message else "explicit steer",
                       "source_spec": asdict(spec), "source_pid": node.pid,
                       "target_exec_identity": self._new_hold(node.id, target, "", self.executor(routed)).record["executor"],
                       "source_limits": self.launch_limits.lookup(node.id),
                       "source_launched_at": self.launch_limits.launch_time(node.id),
                       "source_prompt_file": self.launch_limits.prompt_file(node.id),
                       "message": message, "owner_pid": os.getpid(),
                       "owner_start": procs.start_time(os.getpid()),
                       "tried": [*tried, target]}
            if promotion_rollback:
                attempt["promotion_rollback"] = promotion_rollback
            if promotion:
                attempt.update(promotion=promotion, target_session_id=launch_session,
                               target_spec=asdict(routed))
                attempt["target_spec"]["set_fields"] = sorted(routed.set_fields or ())
            attempt["source_spec"]["set_fields"] = sorted(spec.set_fields or ())
            self.tree.update(node.id, handover_attempt=attempt)
            self._qh_event(node, "handover_started", attempt)
        if attempt.get("target_spec"):
            routed = AgentSpec(**attempt["target_spec"])
            launch_session = attempt["target_session_id"]
        prior_status = node.status
        try:
            self._pc_reserve_resume(routed, node, target, "")
            predecessor = self._steer_predecessor(node.id)
            if not await self._steer_predecessor_dead(predecessor):
                raise RuntimeError("predecessor wrapper or session is not confirmed dead")
            with contextlib.ExitStack() as transfer_files:
                opened_source = None
                if (resume and self._family_of(attempt["from"]) == self._family_of(target)
                        and provider.handover_mode.get(self.executor(spec).kind, "none") == "copy"):
                    source_provider = self.providers[attempt["from"]]
                    source, target_path = transfer_paths(source_provider, provider,
                        Path(node.worktree), attempt["session_id"], self.executor(spec), self.executor(routed))
                    profile = None
                    if source_provider.session_store:
                        profile = session_store_root(source_provider, self.executor(spec))
                        for _ in Path(source_provider.session_store["dir"]).parts:
                            profile = profile.parent
                    opened_source = transfer_files.enter_context(_source_session(source, profile))
                await before_transfer(copy.deepcopy(attempt))
                self._qh_check_switch(node.id)
                if opened_source is not None:
                    transfer = asyncio.create_task(asyncio.to_thread(
                        transfer_session, source, target_path, opened_source=opened_source))
                    try:
                        await asyncio.shield(transfer)
                    except asyncio.CancelledError:
                        # The thread owns the source fd until it finishes.
                        # Cancellation cannot close and recycle it mid-read.
                        with contextlib.suppress(Exception):
                            await self._await_cleanup(transfer)
                        raise
            self._qh_check_switch(node.id)
            attempt = dict(attempt, state="transferred")
            self.tree.update(node.id, handover_attempt=attempt)
            prompt = self._qh_prompt(node, message, resume=resume, promotion=bool(attempt.get("promotion")))
            segments = copy.deepcopy(node.segments)
            if segments and not resume and (node.handover_attempt or {}).get("state") != "failed":
                fingerprint = await asyncio.to_thread(self._qh_session_fingerprint, node.provider, spec,
                                                     Path(node.worktree), node.session_id)
                segments[-1]["session_fingerprint"] = fingerprint
                segments[-1]["spec"] = asdict(spec)
                segments[-1]["spec"]["set_fields"] = sorted(spec.set_fields or ())
            if attempt.get("new_segment", True):
                segments.append(self._qh_segment(provider, routed, launch_session if resume else ""))
            # The new provider and segment are recorded before the wrapper;
            # adoption therefore follows the right account after any crash.
            run_dir = self.paths.run_dir(node.id)
            def size(path):
                return path.stat().st_size if path.exists() else 0
            offset = size(run_dir / "output.ndjson")
            attempt = dict(attempt, state="launching", target_follow={
                "turn": offset, "offset": offset, "log": size(run_dir / "stream.jsonl")})
            self.tree.update(node.id, provider=target, model=routed.model,
                             session_id=launch_session if resume else "", segments=segments, segment_usage_base={},
                             promotion_dwell_at=self._qh_now(), handover_attempt=attempt)
            with await self.gate.enter_when_open():
                self._qh_check_switch(node.id)
                launched = await self._launch(node_id=node.id, spec=routed, provider=provider,
                                             prompt=prompt, workdir=Path(node.worktree), branch=node.branch,
                                             parent=node.parent, depth=node.depth,
                                             session_id=launch_session if resume else None,
                                             done=run.done if run else None,
                                             release_lock=False,
                                             preserved_limits=attempt["source_limits"],
                                             handover_attempt=attempt["attempt"])
            # Stop may land while start() or container registration yields.
            # Never replace its durable failed fence with a successful launch.
            try:
                self._qh_check_switch(node.id, attempt["attempt"])
                if launched.stop_requested:
                    raise RuntimeError("handover was stopped")
            except Exception:
                await self.stop(node.id)
                raise
            current = self.tree.get(node.id).handover_attempt
            if current.get("state") != "completed":
                attempt = dict(current, state="launched")
                self.tree.update(node.id, handover_attempt=attempt)
            # _launch only schedules its consumer. Mark the write-ahead fence
            # before yielding; that consumer reconciles on first session data.
            return True
        except Exception as exc:
            if self._held(node.id):
                return True   # cleanup owns done and the still-uncertain process; never launch another
            attempt = dict(attempt, state="failed", error=str(exc))
            current = self.tree.get(node.id)
            segments = copy.deepcopy(current.segments)
            if attempt.get("new_segment", True) and len(segments) >= attempt["segment"]:
                segments.pop()
            self.tree.update(node.id, provider=attempt["from"], model=spec.model,
                             session_id=attempt["session_id"], segments=segments,
                             rank=promotion["from_rank"] if promotion else node.rank, handover_attempt=attempt)
            self._qh_restore_launch(node.id, attempt)
            self.tree.set_status(node.id, "cancelled" if current.status == "cancelled" else prior_status)
            self._qh_event(node, "handover_failed", attempt, str(exc))
            return False

    async def _qh_after(self, run, status, usage, session_id, limited):
        node = self.tree.get(run.node_id)
        if not node or not self._qh_enabled(run.spec, node):
            return False
        attempt = node.handover_attempt or {}
        promotion = attempt.get("promotion")
        if (promotion and attempt.get("state") in ("launching", "launched")
                and attempt.get("to") == run.provider.name):
            requested = attempt.get("target_session_id", attempt["session_id"])
            if (status in ("unauthenticated", "refused", "failed", "session_lost")
                    or run.requested_session or attempt.get("resume") and session_id != requested):
                source_spec = AgentSpec(**attempt["source_spec"])
                segments = copy.deepcopy(node.segments)
                if attempt.get("new_segment", True) and len(segments) >= attempt["segment"]:
                    segments.pop()
                failed = dict(attempt, state="failed", error="target rejected priority migration")
                self.tree.update(node.id, provider=attempt["from"], model=source_spec.model,
                                 session_id=attempt["session_id"], segments=segments,
                                 rank=promotion["from_rank"], handover_attempt=failed)
                self._qh_restore_launch(node.id, attempt)
                return await self._qh_promotion_failed(node.id, promotion, source_spec, run,
                                                       failed["error"])
        if attempt.get("state") in ("launching", "launched", "completed") and attempt.get("to") == run.provider.name:
            if (attempt.get("resume") and (not session_id
                    or session_id != attempt.get("target_session_id", attempt["session_id"])
                    or run.requested_session or status == "unauthenticated")):
                # No fresh old-id session: return to source bookkeeping and
                # walk remaining candidates with the same run identity.
                source_spec = AgentSpec(**attempt["source_spec"])
                failed = dict(attempt, state="failed", error="target rejected session")
                segments = copy.deepcopy(node.segments)
                if attempt.get("new_segment", True) and len(segments) >= attempt["segment"]:
                    segments.pop()
                self.tree.update(node.id, provider=attempt["from"], model=source_spec.model,
                                 session_id=attempt["session_id"], segments=segments,
                                 handover_attempt=failed)
                self._qh_restore_launch(node.id, attempt)
                self._qh_event(node, "handover_failed", failed, "target rejected session")
                node = self.tree.get(node.id)
                readings = await self._qh_budgets(source_spec)
                for tier, target, resume in await self._qh_candidates(node, source_spec, readings):
                    if target != attempt["to"] and await self._qh_switch(
                            node, source_spec, target, tier, resume, run=run):
                        return True
                # Normal steer uses the in-memory Run when it survives. Keep
                # it and the host-owned frozen spec on the retained source,
                # as well as the node; otherwise a later steer picks the
                # rejected target again despite the source bookkeeping.
                run.spec, run.provider = source_spec, self.providers[attempt["from"]]
                return {"session_id": attempt["session_id"],
                        "limited": {"reason": "target resume rejected; source session is preserved",
                                    "until": node.quota_stops.get(node.provider) or self._qh_now()},
                        "usage": node.segments[-1].get("usage", {}) if node.segments else {}}
            if session_id and attempt.get("state") != "completed":
                completed = dict(attempt, state="completed")
                self.tree.update(node.id, handover_attempt=completed)
                self._qh_event(node, "handover_completed", completed)
                self._qh_promoted(node, completed)
        self._qh_segment_end(run, usage, session_id, status)
        if run.cap_stop is not None or (status != "quota" and not (status == "limited" and limited)):
            return False
        if run.stop_requested or node.on_reserve_floor:
            return False
        # QH-R22: a provider with no successor follows its original finalizer
        # immediately. Reading every provider's quota here would delay slot
        # release after a killed run even though no switch can be attempted.
        roster = self.config.agent(node.agent)
        if not any(name != node.provider for name in self._qh_names(roster)):
            return False
        stop_time = self._qh_now()
        busy = self.__dict__.setdefault("_qh_busy", set())
        if node.id in busy:
            # Exhaustion takes precedence over an optional safe-point wait.
            # Cancel that wait and let the normal quota handover own this stop.
            if not await self._qh_abort_pending_promotion(
                    node.id, "source exhausted while awaiting a safe point"):
                return False
            node = self.tree.get(node.id)
        # The abort awaits: claim under the mutex, and leave a run that a
        # recovery, steer or merge took meanwhile to its owner.
        with self._qh_mutex:
            if node.id in busy or node.id in self.__dict__.get("_qh_merging", set()):
                return False
            busy.add(node.id)
        try:
            stops = dict(node.quota_stops)
            stops[node.provider] = (limited or {}).get("until") or stop_time + float(
                self.config.project.get("budget", {}).get("blind_cooldown_seconds", 900))
            self.tree.update(node.id, quota_stops=stops, session_id=session_id)
            node = self.tree.get(node.id)
            readings = await self._qh_budgets(run.spec)
            stamps = dict(node.quota_stop_readings)
            stamp = readings[node.provider].read_at if node.provider in readings else None
            # budget._from_script preserves the provider script's clock.
            # Compare its stamps with each other; only an unstamped stop uses
            # our local stop time, marked as such (QH-R30.5, _qh_refreshed).
            stamps[node.provider] = stamp if stamp is not None else {"local": stop_time}
            self.tree.update(node.id, quota_stop_readings=stamps)
            for tier, target, resume in await self._qh_candidates(node, run.spec, readings):
                if self.tree.get(node.id).status == "cancelled":
                    return False
                if await self._qh_switch(node, run.spec, target, tier, resume, run=run):
                    return True
                node = self.tree.get(node.id)
            reserved = self._qh_reserved()
            roster = self.config.agent(node.agent)
            target_spec = self._usable_spec(roster, reserved) if reserved else None
            if (target_spec and not self._qh_above_floor(readings.get(reserved))
                    and await self._qh_usable(reserved, target_spec, readings, node, floor=True)):
                self._qh_request(node.agent, node.task, node=node)
        finally:
            busy.discard(node.id)
        return False

    async def _qh_manual(self, node, message, target):
        if node.node_id:
            return {"agent_id": node.id, "error": "scheduler-managed runs have a frozen provider binding"}
        spec, _ = self._spec_of(node)
        if not self._qh_enabled(spec, node):
            return {"agent_id": node.id, "error": "quota handover is disabled for this run"}
        node = self.authoritative(node, "steer")
        if not node or not self.unrecorded_branch_ok(node, "steer"):
            return {"error": "run authority or branch validation failed"}
        roster = self.config.agent(node.agent)
        readings = await self._qh_budgets(spec)
        if target == "auto":
            target = None
            for name in self._qh_priority_names(roster, readings):
                alternative = self._usable_spec(roster, name)
                if alternative and await self._qh_usable(name, alternative, readings, node):
                    target = name
                    break
            if target is None:
                return {"agent_id": node.id, "error": "no usable instance in priority list"}
        if target not in self._qh_names(roster):
            return {"agent_id": node.id, "error": "target is not in the agent's allowed provider list"}
        routed = self._usable_spec(roster, target)
        if routed is None:
            return {"agent_id": node.id, "error": "target has no configured model"}
        readings = await self._qh_budgets(spec)
        if target == self._qh_reserved() and not self._qh_above_floor(readings.get(target)):
            return {"agent_id": node.id, "error": "reserved instance is at its quota floor"}
        if target == self._qh_reserved():
            for name in self._qh_names(roster):
                alternative = self._usable_spec(roster, name)
                if name != target and alternative and await self._qh_usable(name, alternative, readings, node):
                    return {"agent_id": node.id,
                            "error": f"reserved instance is protected while {name} is usable"}
        if not await self._qh_usable(target, routed, readings, node):
            return {"agent_id": node.id, "error": "target is not usable: quota, cooldown, auth, budget or concurrency"}
        resume = self._family_of(target) == self._family_of(node.provider)
        if resume and routed.model != spec.model:
            return {"agent_id": node.id, "error": "same-family different-model migration policy is unresolved; refusing session migration"}
        if resume and self.providers[target].handover_mode.get(self.executor(spec).kind, "none") == "none":
            return {"agent_id": node.id, "error": "target handover mode cannot resume a sibling session"}
        if not node.session_id or not Path(node.worktree).is_dir():
            return {"agent_id": node.id, "error": "session or worktree is unavailable"}
        # Stop and steer share the supervision lock and cancellation path.
        # Busy covers the awaits before stop too; merge cannot consume the
        # branch while we transfer. A concurrent explicit stop wins. The
        # validation above awaits, so a recovery may have claimed the run
        # since steer() checked; refuse without releasing its reservation.
        busy = self.__dict__.setdefault("_qh_busy", set())
        with self._qh_mutex:
            if node.id in busy or node.id in self.__dict__.get("_qh_merging", set()):
                return {"agent_id": node.id, "error": "quota handover or merge is in progress"}
            busy.add(node.id)
        run = self.runs.get(node.id)
        try:
            current = self.tree.get(node.id)
            if (current.provider != node.provider or (current.handover_attempt or {}).get("state") in
                    ("prepared", "transferred", "launching", "launched")):
                return {"agent_id": node.id, "error": "run changed instance during steer validation; retry the steer"}
            predecessor = self._steer_predecessor(node.id)
            await self.stop(node.id, internal=True)
            if not await self._steer_predecessor_dead(predecessor):
                return {"agent_id": node.id, "error": "predecessor is not confirmed dead"}
            if self.tree.get(node.id).status == "cancelled":
                return {"agent_id": node.id, "error": "run was stopped"}
            node = self.tree.get(node.id)
            # note_event already holds the segment's cumulative usage. Using
            # node.usage here would charge earlier instances to this segment.
            if node.segments:
                segments = copy.deepcopy(node.segments)
                segments[-1].update(ended_at=self._qh_now(), end_reason="steered")
                self.tree.update(node.id, segments=segments)
                node = self.tree.get(node.id)
            success = await self._qh_switch(node, spec, target, 1 if resume else 3,
                                            resume, run=run, message=message)
            return {"agent_id": node.id, "steered": success,
                    **({} if success else {"error": "handover failed; source session is preserved"})}
        finally:
            busy.discard(node.id)

    def _qh_short_headroom(self, entry):
        if entry is None or not entry.known or entry.headroom is None:
            return None
        windows = [(float(w.get("span_minutes") or 0), w) for w in entry.windows.values()
                   if isinstance(w, dict) and w.get("counted", True)
                   and w.get("span_minutes") and w.get("percent") is not None]
        if not windows:
            return entry.headroom
        shortest = min(span for span, _ in windows)
        return min(1 - float(w["percent"]) / 100 for span, w in windows if span == shortest)

    def _qh_observe(self, run, event):
        # Results may be raw or step events in older stream declarations.
        # Call identities keep concurrent tools from ending each other's pause.
        raw = event.raw or {}
        kind = str(raw.get("type") or raw.get("event") or "")
        step = raw.get("step_update")
        step = step if isinstance(step, dict) else {}
        part = raw.get("part")
        part = part if isinstance(part, dict) else {}
        identity = str(raw.get("tool_id") or raw.get("tool_use_id") or raw.get("call_id")
                       or raw.get("id") or part.get("id") or step.get("step_index") or "tool")
        message = raw.get("message")
        message = message if isinstance(message, dict) else {}
        blocks = message.get("content", raw.get("content", []))
        tool_blocks = False
        if isinstance(blocks, list):
            for block in blocks:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use":
                    run.active_tools.add(str(block.get("id") or "tool"))
                    tool_blocks = True
                elif block.get("type") == "tool_result":
                    run.active_tools.discard(str(block.get("tool_use_id") or "tool"))
                    tool_blocks = True
        terminal = (str(event.state or "").lower() in ("completed", "complete", "done", "finished", "success",
                                             "succeeded", "failed", "error", "cancelled")
                    or kind in ("tool_result", "tool_end", "function_call_output", "tool.completed", "item.completed"))
        if terminal:
            run.active_tools.discard(identity)
        elif event.kind == "tool" and not tool_blocks:
            run.active_tools.add(identity)
        if event.kind == "result":
            run.active_tools.clear()

    def _qh_session_fingerprint(self, name, spec, worktree, session_id):
        if not session_id:
            return None
        provider = self.providers[name]
        if provider.handover_mode.get(self.executor(spec).kind, "none") == "none":
            return None
        try:
            path = (stored_session(provider, self.executor(spec), session_id)[1]
                    if provider.session_store else
                    session_transcript(provider, worktree, session_id, self.executor(spec)))
            if path is None:
                return None
            profile = None
            if provider.session_store:
                profile = session_store_root(provider, self.executor(spec))
                for _ in Path(provider.session_store["dir"]).parts:
                    profile = profile.parent
            with _source_session(path, profile) as (source, _):
                return hashlib.sha256(source.read()).hexdigest()
        except (OSError, ValueError):
            return None

    def _qh_promotion_eligible(self, node, *, recovery=False):
        roster = self.config.agents.get(node.agent) if node else None
        if (not roster or self.gate.closed or self.__dict__.get("_pc_shutting_down")
                or not self._qh_enabled(roster, node)
                or node.id not in self.runs or node.id not in self._locks
                or node.status not in ("running", "stuck", "detached") or node.pinned):
            return False
        # Recover an already-started migration before considering exceptions
        # to starting a new one: its steering marker may outlive its owner.
        return recovery or (not node.promotion
                            and node.id not in self.__dict__.get("_qh_busy", set())
                            and node.id not in self.__dict__.get("_steering_nodes", set())
                            and node.id not in self.__dict__.get("_qh_merging", set())
                            and (node.handover_attempt or {}).get("state") not in
                            ("prepared", "transferred", "launching"))

    async def _qh_promote_check(self):
        if self.gate.closed or not self._qh_enabled() or self.__dict__.get("_pc_shutting_down"):
            return
        # QH-R23..R27: only our supervised runs with a configured agent
        # can be promoted. Adoption is a separate lifecycle pass: invoking it
        # here can fail unrelated, synthetic or externally owned tree nodes.
        nodes = [n for n in self._qh_nodes() if self._qh_promotion_eligible(n, recovery=True)]
        if not nodes:
            return
        readings = await self._qh_budgets()
        self.__dict__["_qh_readings"] = readings
        for node in nodes:
            node = self.tree.get(node.id)
            if not self._qh_promotion_eligible(node, recovery=True):
                continue
            roster = self.config.agents[node.agent]
            if (node.promotion and node.id not in self.__dict__.get("_qh_promotions", {})
                    and not procs.alive(node.promotion.get("owner_pid", 0), node.promotion.get("owner_start", ""))
                    and not (node.handover_attempt or {}).get("promotion")):
                # Adoption owns the source's supervisor flock. Continue a
                # dead owner's safe-point wait without a second start event.
                self.__dict__.setdefault("_qh_busy", set()).add(node.id)
                task = asyncio.create_task(self._qh_promote(node.id, node.promotion, self._qh_settings(roster)))
                self.__dict__.setdefault("_qh_promotions", {})[node.id] = task
                continue
            if not self._qh_promotion_eligible(node):
                continue
            names = self._qh_priority_names(roster, readings)
            if node.provider not in names:
                continue
            rank = names.index(node.provider)
            self.tree.update(node.id, rank=rank)
            if rank == 0:
                continue
            settings = self._qh_settings(roster)
            since = node.segments[-1]["started_at"] if node.segments else node.started_at
            since = max(since, node.promotion_dwell_at or since)
            if self._qh_now() - since < settings.get("promote_min_dwell_seconds", 300):
                continue
            for target in names[:rank]:
                entry = readings.get(target)
                stamp = entry.read_at if entry else None
                if target in node.promotion_refusals:
                    old = node.promotion_refusals[target]
                    if stamp is None or old is not None and stamp <= old:
                        continue
                # Only a fresh quota reading ends the stop episode. Expiry of
                # its blind cooldown is not a refresh: a wedged script would
                # otherwise resurrect the exhausted instance (QH-R30.5).
                stops = dict(node.quota_stops)
                if target in stops and not self._qh_refreshed(node, target, stamp):
                    continue
                headroom = self._qh_short_headroom(entry)
                if headroom is None or headroom + 1e-12 < settings.get("promote_min_headroom", .10):
                    continue
                if target in stops:
                    stops.pop(target)
                    cooldown = self.tree.read().get("cooldowns", {}).get(target) or {}
                    if isinstance(cooldown, dict) and cooldown.get("cause") == "quota":
                        self.tree.clear_quota({target})
                        if entry.cooldown_until == cooldown.get("until"):
                            readings[target] = replace(entry, cooldown_until=0)
                usable_node = replace(node, quota_stops=stops)
                routed = self._usable_spec(roster, target)
                source_spec, _ = self._spec_of(node)
                if (routed and self._family_of(target) == self._family_of(node.provider)
                        and routed.model != source_spec.model):
                    gap = (node.id, node.provider, source_spec.model, target, routed.model)
                    gaps = self.__dict__.setdefault("_qh_promotion_gaps", set())
                    if gap not in gaps:
                        gaps.add(gap)
                        self.tree.emit(node.id, "handover_policy_gap", provider=target,
                                       reason="same-family different-model migration policy is unresolved")
                    continue
                if not routed:
                    continue
                usable = await self._qh_usable(target, routed, readings, usable_node)
                current = self.tree.get(node.id)
                if (not self._qh_promotion_eligible(current)
                        or current.provider != node.provider or current.agent != node.agent
                        or self.config.agents.get(node.agent) != roster):
                    break
                if not usable:
                    continue
                promotion = {"from": node.provider, "to": target, "session_id": node.session_id,
                             "from_rank": rank, "to_rank": names.index(target),
                             "segment": len(node.segments) + 1,
                             "attempt": int((node.handover_attempt or {}).get("attempt", 0)) + 1,
                             "reason": "moved to a higher priority instance", "read_at": stamp,
                             "triggered_at": self._qh_now(), "phase": "waiting", "owner_pid": os.getpid(),
                             "owner_start": procs.start_time(os.getpid())}
                with self._qh_mutex:
                    busy = self.__dict__.setdefault("_qh_busy", set())
                    if node.id in busy or node.id in self.__dict__.get("_qh_merging", set()):
                        break
                    busy.add(node.id)
                self.tree.update(node.id, promotion=promotion, quota_stops=stops,
                                 promotion_dwell_at=promotion["triggered_at"])
                self._qh_event(node, "promote_started", promotion)
                task = asyncio.create_task(self._qh_promote(node.id, promotion, settings))
                self.__dict__.setdefault("_qh_promotions", {})[node.id] = task
                break

    def _qh_refreshed(self, node, name, stamp):
        """QH-R30.5: is a reading stamped `stamp` newer than the quota stop's?

        A provider baseline compares the provider's own stamps. A stop whose
        reading was unstamped has only our local stop time: a stamp after it
        counts, and the first stamp seen after the stop is kept as a provider
        baseline too, so a provider clock behind ours refreshes on its next
        reading rather than never. Unstamped re-reads cannot show they are new.
        """
        baseline = node.quota_stop_readings.get(name)
        if stamp is None:
            # Accepted: a never-stamping provider is not promoted back to this run.
            return False
        if not isinstance(baseline, dict):
            return baseline is None or stamp > baseline
        if stamp > baseline["local"] or stamp > baseline.get("anchor", stamp):
            return True
        if "anchor" not in baseline:
            stamps = dict(node.quota_stop_readings)
            stamps[name] = dict(baseline, anchor=stamp)
            self.tree.update(node.id, quota_stop_readings=stamps)
        return False

    async def _qh_promote(self, node_id, promotion, settings):
        try:
            while True:
                node = self.tree.get(node_id)
                if (node.status not in ("running", "stuck", "detached") or node.pinned
                        or not node.promotion or self.gate.closed):
                    return
                run = self.runs.get(node_id)
                if run and run.awaiting:
                    return
                if run and run.adopted and getattr(run.handle, "offset", 0) < run.replay_to:
                    await asyncio.sleep(.05)
                    continue
                if (not run or not run.active_tools or
                        self._qh_now() >= promotion["triggered_at"] + settings.get("promote_grace_seconds", 120)):
                    break
                await asyncio.sleep(.05)
            node = self.authoritative(node, "steer")
            if not node or not self.unrecorded_branch_ok(node, "steer"):
                raise RuntimeError("run authority or branch validation failed")
            spec, _ = self._spec_of(node)
            source_run = self.runs.get(node_id)
            predecessor = self._steer_predecessor(node_id)
            self.tree.update(node_id, promotion=dict(promotion, phase="switching"))
            await self.stop(node_id, internal=True)
            if not await self._steer_predecessor_dead(predecessor):
                raise RuntimeError("predecessor is not confirmed dead")
            node = self.tree.get(node_id)
            if node.status == "cancelled":
                return
            if node.segments:
                segments = copy.deepcopy(node.segments)
                segments[-1].update(ended_at=self._qh_now(), end_reason="promoted")
                self.tree.update(node_id, segments=segments)
                node = self.tree.get(node_id)
            target = promotion["to"]
            sibling = self._family_of(target) == self._family_of(node.provider)
            mode = self.providers[target].handover_mode.get(self.executor(spec).kind, "none")
            routed = self._usable_spec(self.config.agent(node.agent), target)
            resume = bool(node.session_id) and sibling and mode != "none" and routed.model == spec.model
            saved_session, saved_spec = None, None
            if not sibling and mode != "none":
                for segment in reversed(node.segments):
                    if self._family_of(segment["provider"]) != self._family_of(target):
                        continue
                    frozen = AgentSpec(**segment["spec"]) if segment.get("spec") else routed
                    fingerprint = await asyncio.to_thread(self._qh_session_fingerprint, target, frozen,
                                                         Path(node.worktree), segment["session_id"])
                    if fingerprint and fingerprint == segment.get("session_fingerprint"):
                        resume, saved_session, saved_spec = True, segment["session_id"], frozen
                    break
            message = "Moved to a higher priority instance because its quota is available. Continue the assigned work."
            if not await self._qh_switch(node, spec, target, 1 if resume else 3, resume,
                                         run=source_run, message=message, promotion=promotion,
                                         target_session_id=saved_session, target_spec=saved_spec):
                await self._qh_promotion_failed(node_id, promotion, spec, source_run,
                                                "target launch refused")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            node = self.tree.get(node_id)
            if node and node.status != "cancelled" and node.promotion:
                self._qh_event(node, "promote_failed", promotion, str(exc))
                self.tree.update(node_id, promotion=None)
        finally:
            self.__dict__.setdefault("_qh_busy", set()).discard(node_id)
            self.__dict__.setdefault("_qh_promotions", {}).pop(node_id, None)
            node = self.tree.get(node_id)
            if node and node.promotion and not (node.handover_attempt or {}).get("promotion"):
                self.tree.update(node_id, promotion=None)

    async def _qh_abort_pending_promotion(self, node_id, reason, *, pinned=None):
        node = self.tree.get(node_id)
        task = self.__dict__.get("_qh_promotions", {}).get(node_id)
        if (not node or not node.promotion
                or node.promotion.get("phase", "waiting") != "waiting" or not task):
            return False
        promotion = node.promotion
        changes = {"promotion": None}
        if pinned is not None:
            changes["pinned"] = pinned
        self.tree.update(node_id, **changes)
        self._qh_event(node, "promote_failed", promotion, reason)
        steering = self.__dict__.setdefault("_steering_nodes", set())
        owned = node_id not in steering
        steering.add(node_id)
        task.cancel()
        try:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        finally:
            self.__dict__.setdefault("_qh_busy", set()).discard(node_id)
            if self.__dict__.get("_qh_promotions", {}).get(node_id) is task:
                self._qh_promotions.pop(node_id)
            if owned:
                steering.discard(node_id)
        return True

    def _qh_fail_promotion(self, node_id, promotion, reason):
        node = self.tree.get(node_id)
        if not node or node.status == "cancelled":
            return False
        refusals = dict(node.promotion_refusals)
        reported = (not node.promotion and promotion["to"] in refusals
                    and refusals[promotion["to"]] == promotion["read_at"])
        refusals[promotion["to"]] = promotion["read_at"]
        self.tree.update(node_id, promotion=None, promotion_refusals=refusals,
                         promotion_dwell_at=max(node.promotion_dwell_at or 0,
                                                promotion.get("triggered_at", 0)))
        if not reported:
            self._qh_event(node, "promote_failed", promotion, reason)
        return True

    async def _qh_promotion_failed(self, node_id, promotion, source_spec, run, reason):
        if not self._qh_fail_promotion(node_id, promotion, reason):
            return False
        node = self.tree.get(node_id)
        # The original instance still holds the stopped source session.
        return await self._qh_switch(node, source_spec, promotion["from"], 1, True,
                                     run=run, message="Continue after the priority migration was refused.",
                                     promotion_rollback=promotion)

    async def _qh_shutdown(self):
        for task in list(self.__dict__.get("_qh_promotions", {}).values()):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    def _qh_idle(self, exclude=""):
        return not any(n.id != exclude and not n.role and n.status in (
            "running", "pending", "stuck", "awaiting_user", "detached") for n in self._qh_nodes())

    def _qh_nodes(self):
        return [node for key in self.tree.read()["nodes"] if (node := self.tree.get(key)) is not None]

    def _qh_request(self, agent, task, *, node=None, kwargs=None):
        with self.tree.transaction() as data:
            queue = data.setdefault("quota_reserve", [])
            request = {"id": "reserve-" + uuid.uuid4().hex, "agent": agent, "task": task,
                       "node_id": node.id if node else "", "kwargs": kwargs or {},
                       "state": "queued", "queued_at": self._qh_now(),
                       "deferred_by": self.self_id(), "readings": {}}
            queue.append(request)
        self._qh_propose()
        return {"deferred": True, "deferred_id": request["id"], "reason": "reserved instance quota floor",
                "paused": False, **({"agent_id": node.id} if node else {})}

    def _qh_propose(self):
        requests = self.tree.read().get("quota_reserve", [])
        if any(r["state"] in ("requested", "allowed", "running") for r in requests):
            return
        request = next((r for r in requests if r["state"] == "queued"), None)
        if not request or not self._qh_idle(request["node_id"]):
            return
        with self.tree.transaction() as data:
            current = next(r for r in data["quota_reserve"] if r["id"] == request["id"])
            if current["state"] != "queued":
                return
            current.update(state="requested", deadline=self._qh_now() + self._qh_settings().get("veto_window_seconds", 120))
        node = self.tree.get(request["node_id"]) if request["node_id"] else None
        self._qh_event(node, "reserve_request", {"from": node.provider if node else "",
                       "to": self._qh_reserved(), "session_id": node.session_id if node else ""},
                       "idle; no other instance is usable", request_id=request["id"],
                       agent=request["agent"], task=request["task"])

    async def reserve_answer(self, request_id, *, veto=False, reason=""):
        # Read before the transaction: a veto clears only on a later reset.
        readings = await self._qh_budgets() if veto else {}
        request = refused = None
        with self.tree.transaction() as data:
            for r in data.get("quota_reserve", []):
                if r["id"] != request_id:
                    continue
                fingerprints = self._qh_reading_keys(self.config.agent(r["agent"]), readings) if veto else {}
                # QH-R16: a veto wins until the run is on the floor, including
                # an approval re-queued after a late admission refusal.
                if r["state"] == "requested" or (veto and r["state"] == "allowed"):
                    r["state"] = "vetoed" if veto else "allowed"
                    r.pop("veto_pending", None)
                    if veto:
                        r["readings"] = fingerprints
                    request = copy.deepcopy(r)
                elif veto and r["state"] == "running":
                    if self._qh_floor_started(r, data["nodes"]):
                        refused = "reserve request is already running on the floor; stop its agent to end it"
                    else:
                        # The claim is taken and start() is in flight. Should
                        # it fail to start, the re-queue applies this veto.
                        r["veto_pending"] = reason or "orchestrator vetoed reserve"
                        r["readings"] = fingerprints
                        refused = ("reserve request is being dispatched to the floor; the veto applies only "
                                   "if this dispatch fails to start, otherwise stop its agent")
                break
        if refused:
            return {"error": refused, "request_id": request_id}
        if request is None:
            return {"error": "reserve request is unknown or already answered"}
        node = self.tree.get(request["node_id"]) if request["node_id"] else None
        self._qh_event(node, "reserve_vetoed" if veto else "reserve_allowed",
                       {"to": self._qh_reserved()}, reason or "orchestrator allowed reserve",
                       request_id=request_id)
        return {"request_id": request_id, "vetoed": veto, "allowed": not veto}

    def _qh_reading_keys(self, spec, readings):
        # A veto clears on a quota reset, not merely another polling pass.
        return {n: (readings[n].headroom, readings[n].windows)
                for n in self._qh_names(spec) if n in readings}

    def _qh_floor_started(self, request, nodes):
        if request.get("started"):
            return True
        # start() and _qh_switch launch before the drain records the result.
        # Raw nodes: this runs inside a tree transaction.
        node = nodes.get(request.get("dispatched_id") or "") or {}
        return (node.get("reserve_request") == request["id"]
                and node.get("provider") == self._qh_reserved()
                and node.get("status") in ("running", "stuck", "detached", "awaiting_user"))

    def _qh_requeue(self, request_id):
        # A floor dispatch that did not start keeps its approval, unless a
        # veto arrived meanwhile. The approval was never used, so a veto wins.
        vetoed = None
        with self.tree.transaction() as data:
            for r in data.get("quota_reserve", []):
                if r["id"] == request_id and r["state"] == "running":
                    vetoed = r.pop("veto_pending", None)
                    r.pop("started", None)
                    r["state"] = "vetoed" if vetoed is not None else "allowed"
                    break
        if vetoed is not None:
            request = next(r for r in self.tree.read()["quota_reserve"] if r["id"] == request_id)
            node = self.tree.get(request["node_id"]) if request["node_id"] else None
            self._qh_event(node, "reserve_vetoed", {"to": self._qh_reserved()}, vetoed,
                           request_id=request_id)

    def _qh_request_state(self, request_id, **changes):
        with self.tree.transaction() as data:
            for r in data.get("quota_reserve", []):
                if r["id"] == request_id:
                    r.update(changes)
                    return

    async def _qh_drain_reserve(self):
        if not self._qh_reserved() or self.gate.closed:
            return []
        readings = await self._qh_budgets()
        restarted = []
        for request in self.tree.read().get("quota_reserve", []):
            state = request["state"]
            if state == "running":
                node = self.tree.get(request.get("dispatched_id") or request.get("node_id", ""))
                if node is None:
                    node = next((n for n in self._qh_nodes() if n.reserve_request == request["id"]), None)
                # A crash may precede dispatch's final bookkeeping. The node
                # records reserve_request before spawn, so finding it prevents
                # a second launch and lets a completed task release the floor.
                if node is None and not procs.alive(request.get("owner_pid", 0), request.get("owner_start", "")):
                    self._qh_requeue(request["id"])
                if node and node.status not in ("running", "pending", "stuck", "detached"):
                    self._qh_request_state(request["id"], state="finished")
                continue
            if state not in ("queued", "requested", "allowed", "vetoed"):
                continue
            roster = self.config.agents.get(request["agent"])
            node = self.tree.get(request["node_id"]) if request["node_id"] else None
            if roster is None or (node and (node.node_id or node.status in ("cancelled", "merged"))):
                self._qh_request_state(request["id"], state="cancelled")
                continue
            if state == "vetoed":
                fresh = self._qh_reading_keys(roster, readings)
                old = request["readings"]
                if not any(n in old and fresh[n][0] is not None and
                           (old[n][0] is None or fresh[n][0] > old[n][0]) for n in fresh):
                    continue
                self._qh_request_state(request["id"], state="queued")
                state = "queued"
            other = None
            for name in self._qh_names(roster):
                routed = self._usable_spec(roster, name)
                if name != self._qh_reserved() and routed and await self._qh_usable(name, routed, readings, node):
                    other = name
                    break
            if other:
                if node:
                    spec, _ = self._spec_of(node)
                    success = await self._qh_switch(node, spec, other, 3,
                                                   self._family_of(other) == self._family_of(node.provider))
                    result = {"agent_id": node.id} if success else {"error": "resume failed"}
                else:
                    result = await self.start(request["agent"], request["task"], **request["kwargs"])
                if result.get("agent_id") and not result.get("deferred"):
                    self._qh_request_state(request["id"], state="finished")
                    restarted.append(result)
                continue
            if not self._qh_idle(node.id if node else ""):
                continue
            if state == "queued":
                self._qh_propose()
                continue
            if state == "requested":
                if self._qh_now() < request["deadline"]:
                    continue
                await self.reserve_answer(request["id"])
            if any(r["state"] == "running" for r in self.tree.read().get("quota_reserve", [])):
                continue
            target = self._qh_reserved()
            routed = self._usable_spec(roster, target)
            if routed is None or not await self._qh_usable(target, routed, readings, node, floor=True):
                continue
            # Claim before launch. A competing drain sees running, not allowed.
            with self.tree.transaction() as data:
                current = next(r for r in data["quota_reserve"] if r["id"] == request["id"])
                if current["state"] != "allowed":
                    continue
                current.update(state="running", owner_pid=os.getpid(),
                               owner_start=procs.start_time(os.getpid()),
                               dispatched_id=node.id if node else "")
            # A single-use grant belongs to this exact run id; no task-local
            # authority is inherited by supervision or later queue drains.
            grant = uuid.uuid4().hex
            dispatched_id = node.id if node else self._qh_new_floor_id()
            self._qh_request_state(request["id"], dispatched_id=dispatched_id)
            grants = self.__dict__.setdefault("_qh_floor_grants", {})
            grants[grant] = (dispatched_id, request["id"])
            try:
                if node:
                    self.tree.update(node.id, on_reserve_floor=True, reserve_request=request["id"])
                    spec, _ = self._spec_of(node)
                    success = await self._qh_switch(node, spec, target, 4,
                                                   self._family_of(target) == self._family_of(node.provider))
                    result = {"agent_id": node.id} if success else {"error": "floor resume failed"}
                else:
                    result = await self.start(request["agent"], request["task"],
                                              _qh_floor_grant=grant, **request["kwargs"])
                if result.get("agent_id") and not result.get("deferred"):
                    self._qh_request_state(request["id"], dispatched_id=result["agent_id"], started=True,
                                           veto_pending=None)
                    self.tree.update(result["agent_id"], on_reserve_floor=True, reserve_request=request["id"])
                    restarted.append(result)
                else:
                    self._qh_requeue(request["id"])
            finally:
                grants.pop(grant, None)
        self._qh_propose()
        return restarted

    def _qh_new_floor_id(self):
        from .runner import new_id
        return new_id()

    def _qh_take_floor_grant(self, grant):
        bound = self.__dict__.setdefault("_qh_floor_grants", {}).pop(grant, None)
        if not bound:
            raise RuntimeError("floor admission grant is absent or already consumed")
        node_id, request_id = bound
        request = next((r for r in self.tree.read().get("quota_reserve", [])
                        if r["id"] == request_id), {})
        if request.get("state") != "running" or request.get("dispatched_id") != node_id:
            raise RuntimeError("floor admission grant no longer owns its run")
        return node_id, request_id

    async def _qh_recover_promotion_failed(self, node, attempt, reason):
        promotion = attempt.get("promotion") or attempt.get("promotion_rollback")
        if not promotion:
            return
        busy = self.__dict__.setdefault("_qh_busy", set())
        with self._qh_mutex:
            if node.id in busy or node.id in self.__dict__.get("_qh_merging", set()):
                return
            busy.add(node.id)
        try:
            if not self._claim(node.id) or self._held(node.id):
                return   # failed-launch cleanup must confirm death first
            predecessor = self._steer_predecessor(node.id)
            identity = attempt.get("target_exec_identity") or node.exec_identity or {}
            predecessor = replace(predecessor, probe_raw=self._identity_probe(identity, node.id))
            if not await self._steer_predecessor_dead(predecessor):
                return
            if attempt.get("state") in ("launching", "launched") and identity.get("kind") == "local":
                from .runner import _recorded_wrapper
                recorded = _recorded_wrapper(self.paths.run_dir(node.id))
                if recorded and recorded != attempt.get("source_pid"):
                    return   # a late wrapper record belongs to target adoption
            current = self.tree.get(node.id)
            if (current.handover_attempt or {}).get("attempt") != attempt["attempt"]:
                return
            # An explicit stop during the death probe still wins, but only
            # after the source binding is restored: a later resume must not
            # find the run stranded on the target that never launched.
            stopped = current.status == "cancelled"
            spec = AgentSpec(**attempt["source_spec"])
            segments = copy.deepcopy(current.segments)
            if attempt.get("new_segment", True) and len(segments) >= attempt["segment"]:
                segments.pop()
            failed = dict(attempt, state="failed", error=reason)
            # A target segment is write-ahead data, not a successful move.
            # Restore the frozen source before the ordinary failed-promotion
            # path prepares a durable same-instance rollback attempt.
            self.tree.update(node.id, provider=promotion["from"], model=spec.model,
                             session_id=attempt["session_id"], rank=promotion["from_rank"],
                             segments=segments, handover_attempt=failed,
                             **({"promotion": None} if stopped else {}))
            self._qh_restore_launch(node.id, attempt)
            if attempt.get("state") != "failed":
                self._qh_event(node, "handover_failed", failed, reason)
            if stopped:
                return
            await self._qh_promotion_failed(node.id, promotion, spec, None, reason)
        finally:
            busy.discard(node.id)
            if node.id not in self.runs and not self._held(node.id):
                self._release(node.id)

    async def _qh_reconcile(self):
        for node in self._qh_nodes():
            attempt = node.handover_attempt or {}
            if (node.status == "cancelled" or not self._qh_enabled(node=node)
                    or node.id in self.runs
                    or procs.alive(attempt.get("owner_pid", 0), attempt.get("owner_start", ""))):
                continue
            if (attempt.get("state") == "failed" and
                    (attempt.get("promotion") or attempt.get("promotion_rollback"))):
                await self._qh_recover_promotion_failed(
                    node, attempt, attempt.get("error", "promotion recovery failed"))
                continue
            if attempt.get("state") not in ("prepared", "transferred", "launching", "launched"):
                continue
            if attempt["state"] in ("launching", "launched"):
                pid, started = node.pid, node.pid_start
                target_identity = attempt.get("target_exec_identity") or (
                    (node.cleanup_hold or {}).get("executor") or node.exec_identity or {})
                if pid == attempt.get("source_pid") and target_identity.get("kind") == "local":
                    # The local wrapper records itself before running the CLI.
                    # Its old record is removed before spawn, so a different
                    # pid proves this attempt launched even if the server died
                    # before _record_launched. Never use a container pid here.
                    from .runner import _recorded_wrapper
                    recorded = _recorded_wrapper(self.paths.run_dir(node.id))
                    if recorded and recorded != attempt.get("source_pid"):
                        pid, started = recorded, procs.start_time(recorded) or ""
                if pid != attempt.get("source_pid"):
                    if not self._claim(node.id):
                        continue
                    held = node.cleanup_hold
                    if held and (held.get("then") or held.get("steer_cleanup")):
                        self._release(node.id)
                        continue   # genuine failed-launch cleanup still owns it
                    # A handover's write-ahead fence distinguishes a successful
                    # spawn interrupted before supervision from an ordinary
                    # failed launch. Remove only that dead owner's reservation
                    # under the supervision flock, then adopt the target.
                    with self.tree.transaction() as data:
                        current = data["nodes"][node.id]
                        if (current.get("status") == "cancelled" or
                                (current.get("handover_attempt") or {}).get("state") == "failed"):
                            self._release(node.id)
                            continue
                        current.update(pid=pid, pid_start=started, status="running", cleanup_hold=None,
                                       exec_identity=target_identity)
                        if attempt["state"] == "launching" and attempt.get("target_follow"):
                            current["follow"] = attempt["target_follow"]
                    try:
                        if not await self._adopt_one(self.tree.get(node.id)):
                            self._release(node.id)
                    except Exception as exc:
                        await self._unadoptable(self.tree.get(node.id), exc)
                    continue
                if attempt.get("promotion") or attempt.get("promotion_rollback"):
                    await self._qh_recover_promotion_failed(
                        node, attempt, "target did not launch before its owner exited")
                    continue
                failed = dict(attempt, state="failed", error="spawn outcome unknown after crash")
                self.tree.update(node.id, handover_attempt=failed)
                self.tree.set_status(node.id, "limited", failed["error"])
                self._qh_event(node, "handover_failed", failed, failed["error"])
                continue
            if not self._claim(node.id):
                continue
            spec = AgentSpec(**attempt["source_spec"])
            try:
                switched = await self._qh_switch(node, spec, attempt["to"], attempt["tier"],
                                                attempt["resume"], attempt=attempt,
                                                message=attempt.get("message", ""))
                if not switched and attempt.get("promotion"):
                    await self._qh_promotion_failed(node.id, attempt["promotion"], spec, None,
                                                    "target recovery refused")
            finally:
                if node.id not in self.runs:
                    self._release(node.id)
