"""Quota and spend awareness.

Two different things share this module and must not be confused:

* **spend** — what has been used. Always knowable: every provider reports token
  usage in its event stream, and we roll it up ourselves.
* **headroom** — what is left. Only knowable where the provider tells us.

The three providers sit at three different tiers, and the adapter reports that
honestly via ``known`` rather than inventing a number:

* **claude** — real subscription state in ``~/.claude.json``
  (``cachedUsageUtilization``): percent used per bucket, reset timestamps,
  overage credits. It is a *cache*, refreshed only when Claude Code runs, so
  staleness is reported alongside it.
* **opencode** — no quota surface today (free zen tier / PAYG credits held in
  the provider account). Stubbed deliberately: see :func:`probe_opencode`.
* **agy** — has a full quota subsystem internally (``quota_manager.go``,
  ``RetrieveUserQuotaSummary``, refreshed every few minutes per its logs) but
  exposes none of it: no subcommand, no cached file. Spend-only, with
  exhaustion detected reactively from a failed run.

The point of all this is *routing*, not reporting. Quota pressure on the
orchestrator is precisely when delegating to an unrationed provider is most
valuable, which makes this the thing that earns the system its keep.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


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
STALE_AFTER = 900.0                        # 15 min; the cache only refreshes on use


def read_claude() -> Budget:
    """Read Claude Code's cached utilisation.

    Defensive throughout: this is an undocumented internal cache, so a missing
    file, a renamed key or a changed shape must degrade to ``known=False``
    rather than raise.
    """
    budget = Budget(provider="claude", known=False, source="cachedUsageUtilization")
    try:
        with CLAUDE_STATE.open() as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        budget.note = f"{CLAUDE_STATE} unreadable"
        return budget

    cached = data.get("cachedUsageUtilization") or {}
    utilization = cached.get("utilization") or {}
    if not utilization:
        budget.note = "no cachedUsageUtilization; run Claude Code once to populate it"
        return budget

    fetched_ms = cached.get("fetchedAtMs")
    if isinstance(fetched_ms, (int, float)):
        budget.stale_seconds = max(0.0, time.time() - fetched_ms / 1000.0)

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
    if extra.get("is_enabled"):
        limit, used = extra.get("monthly_limit"), extra.get("used_credits")
        if isinstance(limit, (int, float)) and isinstance(used, (int, float)):
            budget.spent["extra_credits_used"] = int(used)
            budget.spent["extra_credits_limit"] = int(limit)
        if extra.get("spend_limit_reached"):
            budget.severity = "critical"
    return budget


# --------------------------------------------------------------------------
# opencode — deliberately stubbed until the subscription exists
# --------------------------------------------------------------------------


def probe_opencode() -> dict[str, Any] | None:
    """Discover opencode's quota surface. Not yet implemented — by design.

    On this machine the ``account`` and ``control_account`` tables in
    ``~/.local/share/opencode/opencode.db`` are both empty and
    ``opencode providers list`` reports zero credentials: there is no
    subscription to read. Rather than guess at a schema, this is left blank
    until there is a real account to inspect.

    The intended discovery order, once one exists:

    1. a native subcommand, if the CLI grows one for subscribers
       (re-diff ``opencode --help`` and ``opencode providers --help`` after
       logging in — that is the cheapest signal)
    2. an authenticated request to ``account.url`` using the stored token
    3. fall back to spend-only accounting from ``opencode.db``

    Whichever works gets cached so discovery runs once, not per call.

    **Credential rule for step 2**: the token comes out of a local sqlite DB and
    must never be logged, returned through an MCP tool, or placed in a child's
    environment. It is registered as a redaction literal the moment it is read.
    """
    return None


def read_opencode(spent: dict[str, int] | None = None) -> Budget:
    probed = probe_opencode()
    if probed:                              # pragma: no cover - future path
        return Budget(provider="opencode", known=True, source="probe", **probed)
    return Budget(
        provider="opencode",
        known=False,
        source="tree accounting",
        spent=spent or {},
        note="no quota surface yet; awaiting subscription (see probe_opencode)",
    )


# --------------------------------------------------------------------------
# agy — spend only, exhaustion detected reactively
# --------------------------------------------------------------------------


def read_agy(spent: dict[str, int] | None = None) -> Budget:
    return Budget(
        provider="agy",
        known=False,
        source="stream usage",
        spent=spent or {},
        note="CLI exposes no quota surface; exhaustion is detected from failed runs",
    )


# --------------------------------------------------------------------------


_READERS = {"claude": read_claude, "opencode": read_opencode, "agy": read_agy}


def read_all(spend_by_provider: dict[str, dict[str, int]] | None = None,
             cooldowns: dict[str, dict] | None = None) -> dict[str, Budget]:
    spend_by_provider = spend_by_provider or {}
    cooldowns = cooldowns or {}
    out: dict[str, Budget] = {}
    for name, reader in _READERS.items():
        try:
            budget = reader(spend_by_provider.get(name)) if name != "claude" else reader()
        except Exception as exc:            # never let telemetry break a run
            budget = Budget(provider=name, known=False, note=f"{type(exc).__name__}: {exc}")
        entry = cooldowns.get(name)
        if entry and entry.get("until", 0) > time.time():
            budget.cooldown_until = entry["until"]
            budget.severity = "critical"
            budget.note = (budget.note + " | " if budget.note else "") + entry.get("reason", "cooling down")
        out[name] = budget
    return out


def choose_provider(
    preferred: str,
    budgets: dict[str, Budget],
    chain: list[str],
    reserve: float = 0.15,
) -> tuple[str | None, str]:
    """Pick a provider to run on. Returns ``(provider, reason)``.

    ``None`` means every candidate is exhausted or cooling down and the task
    should be deferred until something resets.
    """
    candidate = budgets.get(preferred)
    if candidate is None or candidate.usable:
        if candidate and candidate.known and candidate.headroom is not None \
                and candidate.headroom < reserve:
            pass                            # below the reserve: fall through
        else:
            return preferred, "preferred provider has headroom"

    for name in chain:
        if name == "defer":
            break
        if name == preferred:
            continue
        alternative = budgets.get(name)
        if alternative is not None and alternative.usable:
            return name, f"{preferred} is constrained; falling back to {name}"
    return None, f"{preferred} and all fallbacks are exhausted or cooling down"
