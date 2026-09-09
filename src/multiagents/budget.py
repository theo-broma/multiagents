"""Quota and spend awareness.

Two different things share this module and must not be confused:

* **spend** — what has been used. Always knowable: every provider reports token
  usage in its event stream, and we roll it up ourselves.
* **headroom** — what is left. Only knowable where the provider tells us.

The three providers sit at three different tiers, and the adapter reports that
honestly via ``known`` rather than inventing a number:

* **claude** — real subscription state, percent used per window with reset
  timestamps and overage credits. Two sources for one payload: the CLI's cache
  in ``~/.claude.json`` (``cachedUsageUtilization``) while it is fresh, and
  ``GET /api/oauth/usage`` — the request the CLI itself makes to fill that
  cache — when it is stale or gone. Gone happens: the key vanished in a vendor
  update and took every window reading in this project with it.
* **opencode** — a Go subscription is detectable (``auth.json``), but the CLI
  exposes no quota surface even with one active. Spend-only; see
  :func:`probe_opencode` for what was checked and ruled out.
* **agy** — has a full quota subsystem internally (``quota_manager.go``,
  ``RetrieveUserQuotaSummary``, refreshed every few minutes per its logs) but
  exposes none of it: no subcommand, no cached file. Spend-only, with
  exhaustion detected reactively from a failed run.

The point of all this is *routing*, not reporting. Quota pressure on the
orchestrator is precisely when delegating to an unrationed provider is most
valuable, which makes this the thing that earns the system its keep.
"""

from __future__ import annotations

import contextlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .redact import register_literal, scrub


@dataclass
class Budget:
    provider: str
    known: bool                            # is headroom knowable at all?
    headroom: float | None = None          # 0.0 exhausted .. 1.0 untouched
    severity: str = "unknown"              # normal | warning | critical | unknown
    resets_at: str | None = None
    stale_seconds: float | None = None
    source: str = ""
    spent: dict[str, int] = field(default_factory=dict)
    cooldown_until: float | None = None
    note: str = ""
    # Per-window detail where a provider reports more than one bucket — opencode
    # serves rolling/weekly/monthly. headroom is the worst of them, because the
    # fullest bucket is the one that will actually stop a run, but which bucket
    # it is changes what to do about it: a rolling window clears in hours, a
    # monthly one does not.
    windows: dict[str, Any] = field(default_factory=dict)

    @property
    def usable(self) -> bool:
        if self.cooldown_until and self.cooldown_until > time.time():
            return False
        if self.known and self.headroom is not None:
            return self.headroom > 0.02
        return True                        # unknown headroom is not "no headroom"

    def to_dict(self) -> dict[str, Any]:
        data = {
            "provider": self.provider,
            "known": self.known,
            "severity": self.severity,
            "source": self.source,
            "usable": self.usable,
        }
        if self.headroom is not None:
            data["headroom"] = round(self.headroom, 3)
            data["used_percent"] = round((1 - self.headroom) * 100, 1)
        if self.windows:
            data["windows"] = self.windows
        if self.resets_at:
            data["resets_at"] = self.resets_at
        if self.stale_seconds is not None:
            data["stale_seconds"] = round(self.stale_seconds)
        if self.spent:
            data["spent"] = self.spent
        if self.cooldown_until:
            data["cooldown_until"] = self.cooldown_until
            data["cooldown_remaining"] = max(0, round(self.cooldown_until - time.time()))
        if self.note:
            data["note"] = self.note
        return data


# --------------------------------------------------------------------------
# claude — real subscription state
# --------------------------------------------------------------------------

CLAUDE_STATE = Path.home() / ".claude.json"
CLAUDE_CREDENTIALS = Path.home() / ".claude" / ".credentials.json"
CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
STALE_AFTER = 900.0                        # 15 min; the cache only refreshes on use
FETCH_TIMEOUT = 8.0


def _claude_token() -> str | None:
    """The CLI's OAuth access token, registered as a secret on the way out.

    Read at the moment of use and never held, never printed, never passed to a
    child. Registering it as a redaction literal means that if it ever escapes
    into output by some route nobody thought of, it is masked before that
    output reaches disk.
    """
    try:
        with CLAUDE_CREDENTIALS.open() as handle:
            oauth = (json.load(handle) or {}).get("claudeAiOauth") or {}
    except (OSError, json.JSONDecodeError):
        return None
    token = oauth.get("accessToken")
    expires_ms = oauth.get("expiresAt")
    if not isinstance(token, str) or not token:
        return None
    if isinstance(expires_ms, (int, float)) and expires_ms / 1000.0 <= time.time():
        return None                       # expired; refreshing is the CLI's job
    register_literal(token)
    return token


def _error_reason(exc: Any) -> str:
    """The vendor's own reason for refusing, with nothing of ours in it.

    Dropping the body entirely was the first version and it was wrong: "HTTP
    403" hides "account suspended" and "unsupported region", which are
    operational facts somebody needs. So the two known-safe fields are read out
    of the error envelope and scrubbed, and everything else is discarded — an
    auth failure's body can otherwise echo back what was sent.
    """
    try:
        payload = json.loads(exc.read().decode()) or {}
        error = payload.get("error") or {}
        reason = " ".join(str(error.get(k, "")) for k in ("type", "message")).strip()
    except Exception:
        reason = ""
    return f": {scrub(reason)[:160]}" if reason else ""


def fetch_claude_usage() -> tuple[dict | None, str]:
    """Ask the account what is left. Returns ``(payload, note)``.

    The same request Claude Code makes for its own display, and the source the
    cache in ``~/.claude.json`` is a copy of. Going to it directly matters
    because that cache is written only while the CLI is running and has already
    vanished once under us — a vendor schema change left every window reader in
    this project blind, and an orchestrator sat at a wall for three hours while
    `status` reported it as waiting for a human.

    Cheap, but not free, and it is somebody's rate limit: callers cache, and
    :func:`_shared_usage` makes that cache one per machine rather than one per
    process.
    """
    import urllib.error
    import urllib.request

    token = _claude_token()
    if token is None:
        return None, ("claude credentials are missing or expired — run `claude` "
                      "once to refresh them")
    from . import __version__

    request = urllib.request.Request(CLAUDE_USAGE_URL, headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "anthropic-beta": "oauth-2025-04-20",
        # Says who this is. The alternative — copying the CLI's own user-agent
        # so the request is indistinguishable from it — is impersonation to
        # evade a check, which is a different thing from reading your own
        # account's usage and not a thing this project does.
        "User-Agent": f"multiagents/{__version__} (claude-code companion)",
    })
    try:
        with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT) as response:
            return json.loads(response.read().decode()), ""
    except urllib.error.HTTPError as exc:
        wait = _retry_after(exc)
        note = f"usage endpoint returned HTTP {exc.code}{_error_reason(exc)}"
        return None, f"{note} [retry-after {wait:.0f}s]" if wait else note
    except Exception as exc:              # offline, DNS, TLS, malformed JSON
        return None, f"usage endpoint unreachable: {type(exc).__name__}"


# One fetch per machine, not one per process. Every agent runs its own MCP
# server, so an in-process cache means N processes crossing the same staleness
# second and asking the same undocumented endpoint in the same millisecond —
# a thundering herd whose only possible reward is being rate-limited off the
# one surface that tells us anything. The lock is not held across the request:
# whoever gets it fetches, everyone else reads what that fetch wrote, or keeps
# the stale copy if the fetch is still in flight.

SHARED_TTL = 300.0
# How long to stop asking after the account says no. A 401 or 403 needs a human,
# and asking again on a timer until one appears is exactly the behaviour that
# would deserve being blocked.
#
# 429 is deliberately NOT in that company. It is overloaded: it can mean a
# quota, but it far more often means "too many at once", and answering a
# sixty-second concurrency limit with an hour of silence turns somebody else's
# transient into our own self-inflicted outage. So it takes the smallest
# backoff, and `Retry-After` overrides all of this when the server sends one.
REFUSED_BACKOFF = {401: 6 * 3600, 403: 6 * 3600, 429: 300}
RETRY_AFTER_MAX = 3600.0
# How long a cold-started reader waits for the one that took the lock.
COLD_START_WAIT = 3.0


def _shared_cache_file() -> Path:
    from .paths import state_root
    return state_root() / "usage-claude.json"


def _fetching_allowed() -> bool:
    """`limits.ask_provider_for_usage` — the user's call, not ours.

    Reading your own account's usage with your own token, at most once every
    five minutes, is the same request the vendor's client makes. But the thing
    at risk if a bot-detector disagrees is somebody's account, not ours, so it
    is a switch and it says so in the shipped config.
    """
    try:
        from .config import load
        return bool(load(None).limits.get("ask_provider_for_usage", True))
    except Exception:
        return True


def _retry_after(exc: Any) -> float:
    """The server's own answer to "when may I ask again", in seconds."""
    try:
        raw = (exc.headers or {}).get("Retry-After")
    except Exception:
        return 0.0
    if not raw:
        return 0.0
    try:                                   # the delta-seconds form
        return max(0.0, min(float(str(raw).strip()), RETRY_AFTER_MAX))
    except ValueError:
        pass
    try:                                   # the HTTP-date form
        from email.utils import parsedate_to_datetime
        when = parsedate_to_datetime(str(raw)).timestamp()
        return max(0.0, min(when - time.time(), RETRY_AFTER_MAX))
    except Exception:
        return 0.0


def _refused_for(note: str) -> float:
    """How long to stay quiet, given what came back. The server wins."""
    marker = "[retry-after "
    if marker in note:
        with contextlib.suppress(ValueError):
            return float(note.split(marker, 1)[1].split("s]", 1)[0])
    for code, seconds in REFUSED_BACKOFF.items():
        if f"HTTP {code}" in note:
            return seconds
    return 0.0


def _shared_usage() -> tuple[dict | None, str]:
    """The machine's shared copy of the usage payload, refreshed by one caller."""
    import fcntl
    import random

    path = _shared_cache_file()
    record = None
    try:
        record = json.loads(path.read_text())
        # Jittered, so N machines and N restarts do not settle into one exact
        # heartbeat. A perfectly periodic request is a signature; an irregular
        # one is a person using a tool.
        if time.time() - float(record["at"]) < SHARED_TTL + random.uniform(0, 60):
            return record.get("payload"), record.get("note", "")
        if time.time() < float(record.get("blocked_until") or 0):
            return record.get("payload"), record.get("note", "")
    except (OSError, ValueError, KeyError, TypeError):
        record = None

    if not _fetching_allowed():
        return None, ("asking the provider for usage is off "
                      "(limits.ask_provider_for_usage)")

    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        handle = path.with_suffix(".lock").open("a+")
    except OSError:
        return fetch_claude_usage()        # no lock available; correctness first
    with handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            # Somebody else is fetching. Their answer is seconds away and a
            # slightly stale number is worth far more than a duplicate request.
            if record:
                return record.get("payload"), record.get("note", "")
            # Cold start: there is no stale copy to fall back to, and every
            # process on the machine hits this in the same second the first
            # time anything asks. Returning "unknown" here is safe but wasteful
            # — the answer is about to exist. Wait briefly for the writer.
            for _ in range(int(COLD_START_WAIT / 0.2)):
                time.sleep(0.2)
                try:
                    fresh = json.loads(path.read_text())
                    return fresh.get("payload"), fresh.get("note", "")
                except (OSError, ValueError):
                    continue
            return None, "another process is refreshing the usage reading"
        payload, note = fetch_claude_usage()
        refused = _refused_for(note)
        if refused:
            note += f"; not asking again for {refused / 3600:.0f}h"
        with contextlib.suppress(OSError):
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps({
                "at": time.time(), "payload": payload, "note": note,
                "blocked_until": time.time() + refused if refused else 0}))
            tmp.replace(path)
        return payload, note


def read_claude(fetch: bool = True) -> Budget:
    """What is left on the claude account.

    Prefers the CLI's own cache — free, and no request against somebody's rate
    limit — and asks the account directly when that cache is missing or stale.
    Defensive throughout: an undocumented surface must degrade to
    ``known=False`` rather than raise.
    """
    budget = Budget(provider="claude", known=False, source="cachedUsageUtilization")
    data = {}
    try:
        with CLAUDE_STATE.open() as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        budget.note = f"{CLAUDE_STATE} unreadable"

    cached = data.get("cachedUsageUtilization") or {}
    utilization = cached.get("utilization") or {}
    fetched_ms = cached.get("fetchedAtMs")
    if isinstance(fetched_ms, (int, float)):
        budget.stale_seconds = max(0.0, time.time() - fetched_ms / 1000.0)

    if fetch and (not utilization or (budget.stale_seconds or 0) > STALE_AFTER):
        fresh, note = _shared_usage()
        if fresh:
            utilization, budget.stale_seconds = fresh, 0.0
            budget.source = "api/oauth/usage"
        elif not utilization:
            budget.note = note
    if not utilization:
        budget.note = budget.note or "no usage data; run Claude Code once"
        return budget

    # The normalised `limits` array is the friendliest surface; fall back to the
    # individual buckets if it is absent.
    worst_percent, worst_reset = None, None
    for entry in utilization.get("limits") or []:
        percent = entry.get("percent")
        if isinstance(percent, (int, float)) and (worst_percent is None or percent > worst_percent):
            worst_percent, worst_reset = float(percent), entry.get("resets_at")

    if worst_percent is None:
        for key in ("five_hour", "seven_day"):
            bucket = utilization.get(key) or {}
            percent = bucket.get("utilization")
            if isinstance(percent, (int, float)) and (worst_percent is None or percent > worst_percent):
                worst_percent, worst_reset = float(percent), bucket.get("resets_at")

    if worst_percent is None:
        budget.note = "utilisation present but no readable bucket"
        return budget

    budget.known = True
    budget.headroom = max(0.0, 1.0 - worst_percent / 100.0)
    budget.resets_at = worst_reset
    budget.severity = (
        "critical" if worst_percent >= 90
        else "warning" if worst_percent >= 75
        else "normal"
    )
    if budget.stale_seconds and budget.stale_seconds > STALE_AFTER:
        budget.note = (
            f"cache is {budget.stale_seconds / 60:.0f} min old; treat as advisory "
            f"and prefer locally accumulated spend"
        )

    extra = utilization.get("extra_usage") or {}
    limit, used = extra.get("monthly_limit"), extra.get("used_credits")
    if isinstance(limit, (int, float)) and isinstance(used, (int, float)):
        budget.spent["extra_credits_used"] = int(used)
        budget.spent["extra_credits_limit"] = int(limit)
    if extra.get("spend_limit_reached"):
        # Not the wall itself — this pool is what would have carried the session
        # PAST the wall. Exhausted, the next full five-hour window stops work
        # dead, and the CLI announces that as "you've hit your monthly spend
        # limit", which is the sentence that sent us looking for a spend cap
        # that had not been reached. Recorded here so the confusion is
        # answerable from data instead of from the wording.
        budget.note = (budget.note + "; " if budget.note else "") + (
            "extra-usage credits are spent, so nothing carries a session past "
            "the window limit")
        if extra.get("is_enabled"):
            budget.severity = "critical"
    return budget


# --------------------------------------------------------------------------
# opencode — subscription detectable, headroom not
# --------------------------------------------------------------------------


def detect_opencode_subscription() -> list[str]:
    """Which opencode providers hold a stored credential.

    The credential lives in ``~/.local/share/opencode/auth.json`` keyed by
    provider name — an active Go subscription shows as ``opencode-go`` with
    ``type: api``. Note this is NOT the ``account`` table in opencode.db, which
    stays empty for an api-key credential.

    Only key *names* are returned. The secret itself is registered as a
    redaction literal on the way past, so that if it ever reaches output by
    another route it is masked before anything is written to disk.
    """
    path = Path.home() / ".local" / "share" / "opencode" / "auth.json"
    try:
        with path.open() as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, dict):
        return []
    for entry in data.values():
        if isinstance(entry, dict):
            for field_name in ("key", "access", "refresh", "access_token", "refresh_token"):
                value = entry.get(field_name)
                if isinstance(value, str):
                    register_literal(value)
    return sorted(data)


def probe_opencode() -> dict[str, Any] | None:
    """Discover opencode's quota surface. Still returns None — verified, not lazy.

    Checked with an active Go subscription:

    * **no native subcommand** — ``opencode --help`` and
      ``opencode providers --help`` gained nothing after subscribing
    * **no new local state** — opencode.db has the same 20 tables, and the
      ``account`` / ``control_account`` tables remain empty because the
      subscription is an api-key credential rather than an OAuth login
    * **no spend signal** — ``opencode stats`` reports ``Total Cost $0.00``,
      since subscription models are not billed per token

    That leaves only an authenticated call to an undocumented endpoint, which
    would mean sending the user's subscription key to a URL guessed rather than
    known. Not worth it for a routing hint: opencode stays ``known: False`` and
    exhaustion is detected reactively from a failed run, exactly as with agy.

    Re-check after an opencode upgrade — a ``usage`` or ``balance`` subcommand
    is the cheapest thing to watch for, and would make this a two-line function.
    """
    return None


def read_opencode(spent: dict[str, int] | None = None) -> Budget:
    probed = probe_opencode()
    if probed:                              # pragma: no cover - future path
        return Budget(provider="opencode", known=True, source="probe", **probed)

    providers = detect_opencode_subscription()
    subscribed = [p for p in providers if p != "opencode"]
    note = (
        f"subscription active ({', '.join(subscribed)}) but the CLI exposes no "
        f"quota surface; exhaustion is detected from failed runs"
        if subscribed else
        "no subscription credential found; free tier only"
    )
    return Budget(
        provider="opencode",
        known=False,
        source="auth.json + tree accounting",
        spent=spent or {},
        note=note,
    )


def read_agy(spent: dict[str, int] | None = None) -> Budget:
    return Budget(
        provider="agy",
        known=False,
        source="stream usage",
        spent=spent or {},
        note="CLI exposes no quota surface; exhaustion is detected from failed runs",
    )


# --------------------------------------------------------------------------


# Built-in readers, used only when a provider's script does not implement the
# `budget` action. Claude's quota lives in an undocumented internal cache with
# several bucket shapes, staleness to account for and an overage block; parsing
# that defensively in shell would be worse code in two places. A provider with
# no built-in and no script action is simply reported as unknown — which is the
# honest answer, and is what a newly added provider gets until it implements it.
_BUILTIN = {"claude": read_claude, "opencode": read_opencode, "agy": read_agy}

# Budget is consulted on every spawn for routing. Without a cache that means
# three subprocesses per agent start, on the event loop.
_CACHE_TTL = 60.0
_cache: dict[str, tuple[float, Budget]] = {}


def invalidate_cache() -> None:
    _cache.clear()


def _from_script(name: str, provider: Any, executor: Any, config_dir: Path,
                 project_config: Path | None) -> Budget | None:
    """Ask the provider's script. None means "it did not answer"."""
    from . import scripts as _scripts

    code, out, err = _scripts.run_action(
        name, provider, executor, "budget", config_dir, project_config, timeout=10,
    )
    if code == _scripts.UNIMPLEMENTED or code == 127:
        return None                       # unimplemented, or no script at all
    if code != 0:
        return Budget(provider=name, known=False, source="script",
                      note=(err or out).strip()[:200] or f"budget action exit {code}")
    try:
        data = json.loads(out.strip() or "{}")
    except json.JSONDecodeError:
        return Budget(provider=name, known=False, source="script",
                      note="budget action did not print valid JSON")
    if not isinstance(data, dict):
        return Budget(provider=name, known=False, source="script",
                      note="budget action printed a non-object")
    # A script that reports headroom but no severity was being read as
    # "normal" at any level, so a provider 86% through its weekly window looked
    # as calm as an untouched one — and the monitor's alert banner keys on
    # exactly this field. Derived from the number, on the same thresholds the
    # built-in reader uses, unless the script says otherwise itself.
    headroom = data.get("headroom")
    severity = data.get("severity")
    if not severity and data.get("known") and isinstance(headroom, (int, float)):
        used = (1 - float(headroom)) * 100
        severity = "critical" if used >= 90 else "warning" if used >= 75 else "normal"

    return Budget(
        provider=name,
        known=bool(data.get("known", False)),
        headroom=headroom,
        severity=str(severity or ("normal" if data.get("known") else "unknown")),
        resets_at=data.get("resets_at"),
        source=str(data.get("source") or "script"),
        note=str(data.get("note") or ""),
        windows=data.get("windows") if isinstance(data.get("windows"), dict) else {},
    )


def read_provider(name: str, provider: Any, executor: Any, config_dir: Path,
                  project_config: Path | None = None,
                  spent: dict[str, int] | None = None,
                  use_cache: bool = True) -> Budget:
    now_ = time.time()
    if use_cache:
        cached = _cache.get(name)
        if cached and now_ - cached[0] < _CACHE_TTL:
            budget = cached[1]
            budget.spent = spent or budget.spent
            return budget
    try:
        budget = _from_script(name, provider, executor, config_dir, project_config)
        if budget is None:
            builtin = _BUILTIN.get(name)
            budget = builtin() if builtin is read_claude else (
                builtin(spent) if builtin else
                Budget(provider=name, known=False, source="none",
                       note="no budget action and no built-in reader")
            )
    except Exception as exc:              # telemetry must never break a run
        budget = Budget(provider=name, known=False,
                        note=f"{type(exc).__name__}: {exc}")
    if spent:
        budget.spent = {**budget.spent, **spent}
    _cache[name] = (now_, budget)
    return budget


def read_all(providers: dict[str, Any] | None = None,
             executor_for: Any = None,
             config_dir: Path | None = None,
             project_config: Path | None = None,
             spend_by_provider: dict[str, dict[str, int]] | None = None,
             cooldowns: dict[str, dict] | None = None,
             use_cache: bool = True) -> dict[str, Budget]:
    """Read every provider's budget, driven by the loaded providers map.

    Previously a hardcoded three-name table that never consulted the providers
    at all, so a newly added provider could never appear and a non-claude
    orchestrator's quota could never be read.
    """
    from .paths import global_config_dir

    cooldowns = cooldowns or {}
    spend_by_provider = spend_by_provider or {}
    config_dir = config_dir or global_config_dir()

    if providers is None:                  # legacy call sites: built-ins only
        providers = {name: None for name in _BUILTIN}

    class _NullExecutor:
        kind = "local"

    out: dict[str, Budget] = {}
    for name, provider in providers.items():
        if provider is not None and not getattr(provider, "enabled", True):
            continue
        executor = executor_for(name) if callable(executor_for) else _NullExecutor()
        budget = read_provider(name, provider, executor, config_dir, project_config,
                               spend_by_provider.get(name), use_cache)
        entry = cooldowns.get(name)
        if entry and entry.get("until", 0) > time.time():
            budget.cooldown_until = entry["until"]
            budget.severity = "critical"
            budget.note = (budget.note + " | " if budget.note else "") + entry.get("reason", "cooling down")
        out[name] = budget
    return out


def reserved_providers(project: dict, providers: Any,
                       orchestrator: str = "") -> set[str]:
    """Which providers the headroom reserve applies to.

    The reserve exists for one stated reason — never spend the orchestrator's
    last slice on delegation bookkeeping — and applying it to every provider
    turned that into something else: a worker provider whose WEEKLY window was
    86% full was skipped entirely for the rest of the week, while its five-hour
    window sat empty, and every agent silently ran on a fallback.

    So it is now two switches. `reserve` extends it to every provider, off by
    default: use what you are paying for until it actually runs out.
    `reserve_orchestrator` keeps the original guarantee, on by default.
    """
    budget = (project or {}).get("budget", {})
    if budget.get("reserve", False):
        return set(providers or ())
    if budget.get("reserve_orchestrator", True) and orchestrator:
        return {orchestrator}
    return set()


def _has_room(candidate: Budget | None, reserve: float, reserved: bool) -> bool:
    """Can work go here? Unknown headroom is not no headroom."""
    if candidate is None:
        return True
    if not candidate.usable:
        return False
    if reserved and candidate.known and candidate.headroom is not None:
        return candidate.headroom >= reserve
    return True


def choose_provider(
    preferred: str,
    budgets: dict[str, Budget],
    chain: list[str],
    reserve: float = 0.15,
    reserved: Any = None,
    allowed: Any = None,
) -> tuple[str | None, str]:
    """Pick a provider to run on. Returns ``(provider, reason)``.

    ``None`` means no candidate can take the work and it should be deferred
    until something resets.

    ``reserved`` names the providers the headroom reserve applies to — see
    :func:`reserved_providers`.

    ``allowed`` names the providers THIS agent can actually run on: its own,
    plus every provider it names a model for. It matters because a model id
    belongs to its provider's namespace, so an agent cannot simply be moved.

    That argument exists because of a live failure. The chain was
    `[opencode, agy]`, claude was cooling down after a revoked token, and the
    agent named a model for agy only. This returned the FIRST usable chain
    entry — opencode — the caller found no model for it, and instead of asking
    for the next candidate the caller gave up and ran on claude anyway: five
    more runs into an authentication wall, with the agent's configured fallback
    sitting one place further down the chain, unused. Filtering here means the
    answer is always one the caller can act on.
    """
    reserved = set(budgets) if reserved is None else set(reserved)
    allowed = None if allowed is None else set(allowed)

    if _has_room(budgets.get(preferred), reserve, preferred in reserved):
        return preferred, "preferred provider has headroom"

    skipped = []
    for name in chain:
        if name == "defer":
            break
        if name == preferred:
            continue
        if allowed is not None and name not in allowed:
            skipped.append(name)
            continue                    # no model for it; not a candidate
        # The reserve applies to a fallback too. Otherwise work diverted off a
        # constrained provider lands on the orchestrator's own and eats exactly
        # the slice the reserve exists to keep.
        if budgets.get(name) is not None \
                and _has_room(budgets.get(name), reserve, name in reserved):
            return name, f"{preferred} is constrained; falling back to {name}"

    why = f"{preferred} and all fallbacks are exhausted or cooling down"
    if skipped:
        why += (f" (no model named for {', '.join(skipped)} — add one under "
                f"`models:` to allow failover there)")
    return None, why
