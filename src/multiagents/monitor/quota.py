"""Quota details and ephemeral account identities. Nothing here writes to disk."""

from __future__ import annotations

import copy
import json
import re
import threading
import time
from pathlib import Path

from .. import scripts
from ..paths import global_config_dir
from ..providers import credential_owner, resolved_profile
from ..tree import Tree
from . import snapshot

IDENTITY_TTL = 30.0
IDENTITY_WAIT = 12.0
_IDENTITY_SLOTS = threading.BoundedSemaphore(2)
_CACHE: dict[tuple, tuple[float, str | None]] = {}
_PENDING: dict[tuple, threading.Event] = {}
_LOCK = threading.Lock()


class IdentityBusy(Exception):
    """The bounded reveal could not obtain a completed identity reading."""


def _safe_identity(out: str) -> str | None:
    """Accept the action's scalar claim only; never return raw diagnostics."""
    try:
        data = json.loads(out)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict) or data.get("kind") not in ("email", "account", "org"):
        return None
    value = data.get("identity")
    if (not isinstance(value, str) or not value.strip() or len(value) > 254
            or any(ord(c) < 32 or ord(c) == 127 for c in value)
            or re.search(r"(?i)(?:sk-|rt-|bearer\s|eyJ[A-Za-z0-9_-]*\.)", value)):
        return None
    if data["kind"] == "email" and not re.fullmatch(r"[^@\s]+@[^@\s]+", value):
        return None
    return value


def _accounts(provider, executor, name, row) -> list[str | None]:
    pin = (getattr(provider, "container_account", "") or
           getattr(getattr(provider, "auth_owner", None), "container_account", ""))
    if pin:
        return [pin]
    # The executor's eligibility rules are authoritative even without a quota
    # reading. They enumerate paths, never the contents of credential files.
    accounts = getattr(executor, "budget_accounts", lambda _n: None)(name)
    if accounts is not None:
        return sorted(accounts)
    labels = {w["account"] for w in (row["budget"].get("windows") or {}).values()
              if isinstance(w, dict) and isinstance(w.get("account"), str)}
    return sorted(labels) if labels else [None]


def _target(paths, providers, executor_of, name, account):
    provider = providers[name]
    executor = executor_of(name)
    owner = credential_owner(name, providers)
    owner = getattr(executor, "container_credential_owner", lambda _n: owner)(name)
    action_provider = providers.get(owner, provider)
    action_name = owner
    # A selected pooled account must survive build_env's authoritative pin
    # handling. Copying preserves the current configuration for other callers.
    action_provider = copy.copy(action_provider)
    if account is not None:
        action_provider.container_account = account
    vault = getattr(executor, "vault_state", lambda _n: {})(name) or {}
    backing = getattr(executor, "private_state", lambda _n: {})(name) or {}
    profile = resolved_profile(action_provider)
    kind = getattr(executor, "kind", "local")
    if kind == "docker" and vault:
        source = ("vault", str(Path(next(iter(vault.values()))).absolute()))
    elif kind == "docker" and backing:
        source = ("backing", str(Path(next(iter(backing.values()))).absolute()))
    elif profile:
        source = ("profile", str(Path(profile).absolute()))
    else:
        source = ("owner", owner, str(Path.home()))
    # Config scope prevents project overrides from contaminating another
    # monitor, while shared owners/profiles/accounts reuse a single result.
    script = scripts.resolve(action_name, action_provider, global_config_dir(), paths.config)
    key = (str(paths.config), kind, str(script), source, account)
    return key, action_name, action_provider, executor


def _identity(paths, target, *, wait=False) -> str | None:
    key, name, provider, executor = target
    deadline = time.monotonic() + IDENTITY_WAIT

    def fresh(cached):
        return cached is not None and time.monotonic() - cached[0] < IDENTITY_TTL

    with _LOCK:
        now = time.monotonic()
        # Keep the last reading during a refresh so availability does not
        # flicker. Old inactive entries are pruned without retaining them forever.
        for expired in [k for k, v in _CACHE.items()
                        if k != key and k not in _PENDING and now - v[0] >= 2 * IDENTITY_TTL]:
            del _CACHE[expired]
        cached = _CACHE.get(key)
        if fresh(cached):
            return cached[1]
        pending = _PENDING.get(key)
    if pending is None:
        # Identity reads cannot starve behind C18's extras and install probes.
        # Polls never wait; reveals share one bound for capacity and completion.
        slots = _IDENTITY_SLOTS
        acquired = (slots.acquire(timeout=max(0, deadline - time.monotonic()))
                    if wait else slots.acquire(blocking=False))
        if not acquired:
            if wait:
                raise IdentityBusy
            return cached[1] if cached else None
        with _LOCK:
            cached = _CACHE.get(key)
            pending = _PENDING.get(key)
            cache_fresh = fresh(cached)
            launch = not cache_fresh and pending is None
            if launch:
                pending = threading.Event()
                _PENDING[key] = pending
        if not launch:
            slots.release()
            if cache_fresh:
                return cached[1]
        else:
            def fetch():
                value = None
                try:
                    account = key[-1]
                    code, out, _err = scripts.run_action(
                        name, provider, executor, "identity", global_config_dir(),
                        paths.config, timeout=10,
                        extra_env={"MULTIAGENTS_CONTAINER_ACCOUNT": account} if account else {})
                    if code == 0:
                        value = _safe_identity(out)
                except Exception:
                    pass
                finally:
                    with _LOCK:
                        _CACHE[key] = (time.monotonic(), value)
                        _PENDING.pop(key, None)
                        pending.set()
                    slots.release()

            try:
                threading.Thread(target=fetch, daemon=True,
                                 name="monitor-identity").start()
            except Exception:
                with _LOCK:
                    _PENDING.pop(key, None)
                    pending.set()
                slots.release()
                if wait:
                    raise IdentityBusy
                return cached[1] if cached else None
    if wait:
        if not pending.wait(max(0, deadline - time.monotonic())):
            raise IdentityBusy
        with _LOCK:
            cached = _CACHE.get(key)
            if not fresh(cached):
                raise IdentityBusy
            return cached[1]
    return cached[1] if cached else None


def _model(paths, config):
    from ..executor import executor_for

    # Resolve through snapshot at call time, just like its quota panel does.
    providers = snapshot.load_providers(config.providers)
    executor_of = executor_for(paths, config, providers)
    tree = Tree(paths.tree_file, paths.events_file)
    rows = snapshot.providers_view(paths, config, tree)
    targets = {}
    for row in rows:
        name = row["name"]
        for account in _accounts(providers[name], executor_of(name), name, row):
            targets[name, account] = _target(paths, providers, executor_of, name, account)
    return rows, targets


def view(paths, config) -> list[dict]:
    rows, targets = _model(paths, config)
    for row in rows:
        row["identities"] = [
            {"account": account, "identity": "*****",
             "identity_available": _identity(paths, target) is not None}
            for (name, account), target in targets.items() if name == row["name"]]
    return rows


def reveal(paths, config, name, account) -> str | None:
    if not isinstance(name, str) or not name or not (account is None or isinstance(account, str)):
        raise ValueError("invalid provider/account")
    _rows, targets = _model(paths, config)
    target = targets.get((name, account))
    if target is None:
        raise ValueError("invalid provider/account")
    return _identity(paths, target, wait=True)
