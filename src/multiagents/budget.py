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
* **agy** — real headroom, reached through the one surface that exposes its
  internal quota subsystem (``quota_manager.go``, ``RetrieveUserQuotaSummary``):
  the interactive ``/usage`` slash command, which print mode expands and answers
  locally for no tokens. Its script reports it; see ``providers/agy.sh``. agy
  bills two independent pools from one binary and only the Gemini one is this
  provider's, so the third-party pool is reported under ``windows`` but kept out
  of ``headroom``.

The point of all this is *routing*, not reporting. Quota pressure on the
orchestrator is precisely when delegating to an unrationed provider is most
valuable, which makes this the thing that earns the system its keep.
"""

from __future__ import annotations

import contextlib
import fnmatch
import hashlib
import json
import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any, NamedTuple

from .providers import resolved_profile
from .redact import register_literal, scrub


def reset_label(stamp: Any, now: float | None = None) -> str:
    """A reset time as the person reading it experiences it: their own clock.

    Every provider sends UTC — claude as "...+00:00", agy as "...Z" — and every
    display of it used to cut the string at 19 characters, which drops the
    offset and prints a UTC wall clock on a local clock face. Silent, because
    the result still looks exactly like a time: the monitor said a window reset
    at 00:00 while the CLI's own display said 2am, and only a person who knew
    both numbers could tell which was lying. Two hours of error here in summer,
    none in winter, which is the kind of bug that survives a whole season.

    The countdown is the half that cannot be misread at all, so it is always
    there: a timezone confusion shifts the clock time, never "in 3h12m".
    """
    if not stamp:
        return ""
    text = str(stamp)
    try:
        when = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text[:19].replace("T", " ")
    if when.tzinfo is None:
        # No offset to trust. Converting would invent an error rather than fix
        # one, so it is shown as sent and marked as unanchored.
        return when.strftime("%b %d %H:%M") + " (no timezone)"
    local = when.astimezone()
    left = when.timestamp() - (time.time() if now is None else now)
    if left <= 0:
        return f"{local:%b %d %H:%M %Z} \u00b7 due"
    days, rest = divmod(int(left), 86400)
    hours, rest = divmod(rest, 3600)
    if days:
        ago = f"{days}d{hours:02d}h"
    elif hours:
        ago = f"{hours}h{rest // 60:02d}m"
    elif rest >= 60:
        ago = f"{rest // 60}m"
    else:
        ago = "<1m"
    return f"{local:%b %d %H:%M %Z} \u00b7 in {ago}"


# reset_display's countdown grammar: "2d03h", "1h05", "7m", "<1m". Shorter
# than reset_label's on purpose — it sits inside a usage line, where the slot
# is narrow and the "(HH:MM ZONE)" after it carries the units.
def _countdown(left: float) -> str:
    days, rest = divmod(int(left), 86400)
    hours, rest = divmod(rest, 3600)
    if days:
        return f"{days}d{hours:02d}h"
    if hours:
        return f"{hours}h{rest // 60:02d}"
    if rest >= 60:
        return f"{rest // 60}m"
    return "<1m"


def reset_display(stamp: Any, now: float | None = None) -> str:
    """A reset time as it fits a usage line: countdown first, then local clock.

    Provider scripts print their own usage lines and several stamp them with
    the raw UTC ISO string the API sent (a provider script's ``ms_iso``). Q6: a
    viewer at UTC+2 read "10:57" as local time and concluded a quota that was
    still an hour away had already failed to come back. :func:`reset_label`
    already renders the generic path, but a script's line passes the monitor
    untouched — so the display layer rewrites each ISO token with this, and
    every provider benefits without each script growing its own formatter.

    Countdown leads because it is the half that cannot be misread ("resets in
    1h05"); the clock time follows with its zone ("12:57 CEST") so a countdown
    near zero can still be checked against the viewer's watch. Unparsable text
    comes back unchanged — a script may print something that merely looks like
    a timestamp, and inventing a rendering for it would be worse than the raw
    string.
    """
    if not stamp:
        return ""
    text = str(stamp)
    try:
        when = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    if when.tzinfo is None:
        # No offset to trust. Converting would invent an error rather than fix
        # one, so it is shown as sent and marked as unanchored.
        return when.strftime("%b %d %H:%M") + " (no timezone)"
    local = when.astimezone()
    left = when.timestamp() - (time.time() if now is None else now)
    if left <= 0:
        return f"reset due ({local:%H:%M %Z})"
    return f"resets in {_countdown(left)} ({local:%H:%M %Z})"


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
    # RM-R4b: when the provider's script gives no `stale_seconds` but does say
    # WHEN it took the reading (epoch seconds), the age is computed from that.
    read_at: float | None = None
    # RM-R4b: the reading's age exceeded `budget.max_reading_age_seconds`, so
    # for ROUTING it is unknown — it can no longer testify that a window is
    # full. The raw values above stay exactly as reported, for display.
    stale: bool = False

    @property
    def usable(self) -> bool:
        if self.cooldown_until and self.cooldown_until > time.time():
            return False
        if self.stale:
            # An aged-out reading is not evidence about now: routing on it
            # would spend a window that may have reset hours ago (RM-R4b).
            return True
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
            # Each window carries its own local label for the same reason the
            # top-level one does: a provider script formats this dict, and
            # three scripts each slicing an ISO string is three chances to
            # print UTC on a local clock. Computed once, here.
            data["windows"] = {
                name: ({**detail, "resets_label": reset_label(detail["resets_at"])}
                       if isinstance(detail, dict) and detail.get("resets_at")
                       else detail)
                for name, detail in self.windows.items()
            }
        if self.resets_at:
            data["resets_at"] = self.resets_at
            data["resets_label"] = reset_label(self.resets_at)
        if self.stale_seconds is not None:
            data["stale_seconds"] = round(self.stale_seconds)
        if self.read_at is not None:
            data["read_at"] = self.read_at
        if self.stale:
            data["stale"] = True
        if self.spent:
            data["spent"] = self.spent
        if self.cooldown_until:
            data["cooldown_until"] = self.cooldown_until
            data["cooldown_remaining"] = max(0, round(self.cooldown_until - time.time()))
        if self.note:
            data["note"] = self.note
        return data


def _reset_is_past(resets_at: Any, margin: float, now_: float) -> bool:
    """Is a window's own reset far enough in the past to say it has reset?

    A reading carries its own expiry: once ``now`` is at least ``margin``
    seconds past ``resets_at``, the window counts as reset even if the cache
    holding the reading still calls itself fresh. Naive (no timezone) or
    unparseable timestamps never count as past-reset — see
    ``context/specs/quota-freshness.md``.
    """
    if not resets_at:
        return False
    try:
        when = datetime.fromisoformat(str(resets_at).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return False
    if when.tzinfo is None:
        return False
    return now_ - when.timestamp() >= margin


def _effective_windows(b: Budget) -> dict[str, dict]:
    """``b.windows``, or a synthesized single-window view for a bare reading.

    A single-window budget never populates ``windows`` (a builtin reader
    only does that once there is more than one bucket), so a reading with a
    top-level ``resets_at`` and no ``windows`` is treated as one window here,
    per the spec's Decisions section.

    PS-R5: a window the reading marks ``counted: false`` never constrains
    this provider — not at fetch time and not in the recomputation here. A
    window with no flag at all counts, which keeps every reading that never
    heard of the flag exactly as it was.
    """
    if b.windows:
        return {name: detail for name, detail in b.windows.items()
                if isinstance(detail, dict) and detail.get("counted", True)}
    if b.headroom is not None and b.resets_at:
        return {"_default": {"percent": (1 - b.headroom) * 100,
                             "resets_at": b.resets_at}}
    return {}


def _apply_reset_margin(b: Budget, margin: float, now_: float) -> Budget:
    """Recompute a Budget so a window past its own reset never counts.

    Applied uniformly to every provider's reading on every retrieval — a
    cache hit or a fresh fetch alike — and never cached itself, so the
    margin is always judged against the CURRENT clock (QF-R1).
    """
    windows = _effective_windows(b)
    if not windows:
        return b
    past = {name for name, detail in windows.items()
            if _reset_is_past(detail.get("resets_at"), margin, now_)}
    if not past:
        return b
    remaining = {name: detail for name, detail in windows.items() if name not in past}
    new_windows = dict(b.windows)
    for name in past:
        if name in new_windows:
            new_windows[name] = {**new_windows[name], "percent": 0.0, "resets_at": None}
    if not remaining:
        return replace(b, headroom=1.0, resets_at=None, severity="normal",
                       windows=new_windows)
    # Usage comes from `_window_used`, so a window that reports only a
    # `headroom` is read as what it says (review ag-2f0d3e, finding 9) —
    # a bare `.get("percent") or 0` read its 0.5 headroom as 0% used and
    # returned full headroom after a recomputation.
    worst = max(remaining, key=lambda name: _window_used(remaining[name]) or 0.0)
    worst_percent = _window_used(remaining[worst]) or 0.0
    return replace(
        b, headroom=max(0.0, 1.0 - worst_percent / 100.0),
        resets_at=remaining[worst].get("resets_at"),
        severity=("critical" if worst_percent >= 90
                  else "warning" if worst_percent >= 75 else "normal"),
        windows=new_windows,
    )


# BP-R1: one parse per file per read. The config layers behind a budget read
# used to be re-read per helper per provider — `_reset_margin` and
# `_reading_age_bound_from_layers` each merged the three project.yamls on
# every call: ~17 YAML parses on every spawn and every poll. Both now read
# through `config.read_yaml_cached`, and `read_all`/`read_provider` hold a
# `config.parse_once()` view open for the call, so every helper inside it —
# and the `config.load` behind `_fetching_allowed` — sees one version of
# each file, parsed once. The next call stats the files afresh (BP-R2).
def _merged_project_layers(project_config: Path | None) -> dict:
    """The project.yaml of every layer, lowest first, deep-merged.

    What `_reset_margin` and `_reading_age_bound_from_layers` each re-read
    per call, now read once and shared between them (BP-R1): each file comes
    through `config.read_yaml_cached`, in the call's view. The same
    layers in the same order as before, so the merged result is
    byte-for-byte today's.
    """
    from .config import deep_merge, read_yaml_cached
    from .paths import global_config_dir, shipped_defaults_dir

    merged: dict = {}
    for layer in (shipped_defaults_dir(), global_config_dir(), project_config):
        if layer is None:
            continue
        merged = deep_merge(merged, read_yaml_cached(Path(layer) / "project.yaml"))
    return merged


def _reset_margin(project_config: Path | None, limits: dict | None = None) -> float:
    """``limits.quota_reset_margin_seconds``, layered like every other limit.

    ``limits`` is the caller's own already-loaded (last-good) config, when it
    has one — a `Runner` always does. Given one, this trusts it outright
    rather than re-reading `project.yaml` itself: re-reading is exactly what
    crashed `start_agent` under a broken project.yaml (P0-R5.4), since a
    caller reaching this function already survived that same file being
    unreadable and is holding the limits that survived it.

    Only a caller with no last-good config to hand (a one-shot CLI read, a
    provider script probe) falls through to reading the layers itself — once
    per file, through the shared layer cache (BP-R1) — and a parse error
    there, the one failure this project.yaml is actually prone to, skips the
    layer and falls back towards the shipped default rather than propagating,
    since there is no previous reading to prefer instead.
    """
    from .config import limit_number

    if limits is not None:
        return limit_number(limits, "quota_reset_margin_seconds")
    merged = _merged_project_layers(project_config)
    return limit_number(merged.get("limits") or {}, "quota_reset_margin_seconds")


# RM-R4b: a reading older than this is no longer evidence about now — the
# incident that named the requirement was a codex reading of "weekly 100%,
# resets Oct 4" taken from rollout history 13 000 s old, although the window
# had already been reset and the account had room.
DEFAULT_READING_AGE = 3600.0


def _number(value: Any) -> float | None:
    """A finite number, or None: person- or script-typed, so a bool, a string
    or NaN is not a value."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def reading_age_bound(project: dict | None) -> float:
    """`budget.max_reading_age_seconds` from a loaded project config.

    For callers that already hold the config (a `Runner`); a malformed value
    falls back to the shipped default rather than turning the bound off —
    off would mean trusting readings of any age, which is the failure the
    setting exists to prevent.
    """
    value = _number(((project or {}).get("budget") or {}).get("max_reading_age_seconds"))
    return value if value is not None and value > 0 else DEFAULT_READING_AGE


def _reading_age_bound_from_layers(project_config: Path | None) -> float:
    """The same setting, read from the config layers themselves.

    For a caller with no loaded config to hand (a one-shot CLI read), the
    same fallback that `_reset_margin` uses: read the layers — once per
    file, through the shared layer cache (BP-R1) — and survive a broken
    project.yaml by falling back to the shipped default.
    """
    return reading_age_bound(_merged_project_layers(project_config))


def _reading_age(b: Budget, now_: float, cached_at: float | None = None,
                 elapsed: float | None = None) -> float | None:
    """A reading's age in seconds (RM-R4b), or None when it carries no age.

    The provider's own `stale_seconds` is the authority when it gives one;
    otherwise the optional `read_at` stamp, which is absolute and so grows on
    its own. A cached reading KEEPS ageing — age is never frozen; that is
    the point.

    RM-R4d: time going backwards never makes a reading fresher.

    - A cached reading whose wall-clock stamp is now AHEAD of the current
      one — the clock stepped backwards under it (NTP correction, VM
      resume) — is expired: its age is past every bound, so routing treats
      it as unknown. Clamping the negative elapsed to 0 would freeze the age
      for as long as the clock stays behind, which is the defect; dropping
      the cache entry to re-read would be worse, for the re-read would come
      back looking fresh through the very step that makes the bookkeeping
      untrustworthy.
    - `elapsed` is the time spent in the cache, accumulated from the wall
      clock's FORWARD movement alone (review ag-f21a0c). A wall
      `now_ - cached_at` would shrink on such a step and only catch up
      afterwards, freezing the age in between; a monotonic clock would not
      see the synthetic time the tests drive through `time.time` at all.
      Forward movement has the monotonic properties RM-R4d asks for: the
      age never decreases and never stops growing while the clock moves.
      It is added ONCE, on top of the age the reading already had: for an
      absolute `read_at` the wall elapsed is already inside `now_ -
      read_at`, so adding it again counted the cached time twice (review
      ag-f21a0c).
    - A `read_at` more than 300 s in the future is a broken stamp — one sent
      in milliseconds arrives looking years ahead — and expires the reading
      too. Clamping its age to 0 would let a forged stamp stay fresh for
      ever; a little ahead (within the 300 s) is still tolerated, as a
      clock slightly fast has always been.
    """
    if b.read_at is not None and b.read_at - now_ > 300.0:
        # RM-R4d: a read_at more than 300 s ahead is a broken stamp — one
        # sent in milliseconds arrives looking years ahead — and expires the
        # reading whatever its stale_seconds says. Clamping its age to 0
        # would let a forged stamp stay fresh for ever; a little ahead
        # (within the 300 s) is still tolerated, as a clock slightly fast
        # has always been.
        return math.inf
    if cached_at is not None and now_ < cached_at:
        return math.inf
    if b.stale_seconds is not None:
        age = b.stale_seconds
    elif b.read_at is not None:
        if cached_at is None:
            return max(0.0, now_ - b.read_at)
        if elapsed is not None:
            # A cache hit: the age the reading already had when it was
            # cached, grown by the time spent in the cache.
            return max(0.0, cached_at - b.read_at) + elapsed
        return max(0.0, now_ - b.read_at)
    else:
        return None
    if elapsed is not None:
        return age + elapsed
    return age + (now_ - cached_at if cached_at is not None else 0.0)


def _apply_reading_age(b: Budget, bound: float, now_: float,
                       cached_at: float | None = None,
                       elapsed: float | None = None) -> Budget:
    """Demote a reading older than `bound` to unknown, for routing (RM-R4b).

    Applied uniformly to every provider's reading on every retrieval — a
    cache hit or a fresh fetch alike — and never cached itself, so the age is
    always judged against the CURRENT clock, exactly like the reset margin
    beside it. The raw values stay on the Budget untouched: display keeps the
    last numbers and the age, marked stale. A reading with no age information
    is never demoted. The per-window reset rule (QF-R1) runs first and stays
    authoritative: demoting the WHOLE reading could bypass another window
    that is still genuinely full, which is why RM-R4(a) was withdrawn.
    """
    age = _reading_age(b, now_, cached_at, elapsed)
    if age is None or b.stale or age <= bound:
        return b
    age_text = (f"{age / 60:.0f} min old" if math.isfinite(age)
                else "past every bound (the clock went backwards)")
    note = (f"reading is {age_text}, over the "
            f"{bound / 60:.0f} min reading age; treated as unknown for routing")
    return replace(b, stale=True,
                   note=(b.note + "; " if b.note else "") + note)


# --------------------------------------------------------------------------
# claude — real subscription state
# --------------------------------------------------------------------------

CLAUDE_STATE = Path.home() / ".claude.json"
CLAUDE_CREDENTIALS = Path.home() / ".claude" / ".credentials.json"
CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
STALE_AFTER = 900.0                        # 15 min; the cache only refreshes on use
FETCH_TIMEOUT = 8.0


def _claude_token(config_dir: Path | None = None) -> str | None:
    """The CLI's OAuth access token, registered as a secret on the way out.

    Read at the moment of use and never held, never printed, never passed to a
    child. Registering it as a redaction literal means that if it ever escapes
    into output by some route nobody thought of, it is masked before that
    output reaches disk.
    """
    path = (config_dir / ".credentials.json") if config_dir else CLAUDE_CREDENTIALS
    try:
        with path.open() as handle:
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


def fetch_claude_usage(config_dir: Path | None = None) -> tuple[dict | None, str]:
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

    token = _claude_token(config_dir)
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


def _shared_cache_file(config_dir: Path | None = None) -> Path:
    from .paths import state_root

    if config_dir is None:
        return state_root() / "usage-claude.json"
    # One file per profile. Sharing it across accounts would answer for
    # whichever asked first, which is the failure this parameter exists to stop.
    digest = hashlib.sha256(str(config_dir).encode()).hexdigest()[:12]
    return state_root() / f"usage-claude-{digest}.json"


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


def _usage_windows(utilization: dict | None) -> dict[str, dict]:
    """Every readable bucket in a utilization payload, by name."""
    windows: dict[str, Any] = {}
    for i, entry in enumerate((utilization or {}).get("limits") or []):
        percent = entry.get("percent")
        if not isinstance(percent, (int, float)):
            continue
        windows[str(entry.get("kind") or i)] = {
            "percent": float(percent), "resets_at": entry.get("resets_at")}
    if not windows:
        for key in ("five_hour", "seven_day"):
            bucket = (utilization or {}).get(key) or {}
            percent = bucket.get("utilization")
            if isinstance(percent, (int, float)):
                windows[key] = {"percent": float(percent), "resets_at": bucket.get("resets_at")}
    return windows


def _payload_past_reset(payload: dict | None, margin: float, now_: float) -> bool:
    """Does a utilization payload hold a window past its own reset?"""
    if not payload:
        return False
    return any(_reset_is_past(detail.get("resets_at"), margin, now_)
              for detail in _usage_windows(payload).values())


def _shared_usage(config_dir: Path | None = None, *, force: bool = False,
                  margin: float | None = None, now_: float | None = None,
                  reset_suspected: bool = False) -> tuple[dict | None, str]:
    """The machine's shared copy of the usage payload, refreshed by one caller.

    Normally bound by `SHARED_TTL` (jittered). But a reading's own `resets_at`
    can lapse well inside that window, so when a past reset is suspected —
    either because the caller just read one from the CLI's own cache, or
    because this shared record's own stored payload holds one — a tighter
    60s-per-machine bound applies instead (QF-R2). `force=True`
    (`refresh-quota`) skips both bounds outright, but a backoff already in
    force (`blocked_until`, from a 429/401/403) always still wins.
    """
    import fcntl
    import random

    now_ = time.time() if now_ is None else now_
    path = _shared_cache_file(config_dir)
    record = None
    try:
        record = json.loads(path.read_text())
    except (OSError, ValueError, KeyError, TypeError):
        record = None

    if record is not None:
        age = now_ - float(record.get("at", 0))
        reset_due = reset_suspected or (
            margin is not None and _payload_past_reset(record.get("payload"), margin, now_))
        # Jittered at the ordinary bound, so N machines and N restarts do not
        # settle into one exact heartbeat. Not jittered at the tight bound — a
        # suspected reset is worth asking about promptly, not irregularly.
        bound = 60.0 if reset_due else SHARED_TTL + random.uniform(0, 60)
        if not force and age < bound:
            return record.get("payload"), record.get("note", "")
        if now_ < float(record.get("blocked_until") or 0):
            stamp = time.strftime("%H:%M", time.localtime(record["blocked_until"]))
            return record.get("payload"), f"not re-read: rate-limited until {stamp}"

    if not _fetching_allowed():
        return None, ("asking the provider for usage is off "
                      "(limits.ask_provider_for_usage)")
    fetch = lambda: fetch_claude_usage(config_dir)          # noqa: E731

    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        handle = path.with_suffix(".lock").open("a+")
    except OSError:
        return fetch()                     # no lock available; correctness first
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
        payload, note = fetch()
        if payload is None and record is not None:
            # A failed refresh must not erase a stale-but-still-meaningful
            # reading — QF-R1's margin correction can still act on it, which
            # is strictly better than reporting "unknown" over a transient
            # network failure.
            payload = record.get("payload")
        refused = _refused_for(note)
        if refused:
            note += f"; not asking again for {refused / 3600:.0f}h"
        with contextlib.suppress(OSError):
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps({
                "at": now_, "payload": payload, "note": note,
                "blocked_until": now_ + refused if refused else 0}))
            tmp.replace(path)
        return payload, note


def read_claude(fetch: bool = True, config_dir: Path | str | None = None,
                project_config: Path | None = None, force: bool = False,
                limits: dict | None = None, vault_profile: bool = False) -> Budget:
    """What is left on the claude account — WHICH account depends on where.

    `config_dir` is the instance's CLAUDE_CONFIG_DIR. With two subscriptions on
    one machine, reading the default profile for both would report one
    account's headroom while the work spends the other's: the numbers would
    look right and mean nothing.

    `project_config` is unrelated: it is this project's own config directory,
    used only to look up `limits.quota_reset_margin_seconds` — unless `limits`
    (the caller's own already-loaded config) is given, in which case that is
    trusted instead and `project_config` is not re-read for it (P0-R5.4).

    Prefers the CLI's own cache — free, and no request against somebody's rate
    limit — and asks the account directly when that cache is missing or stale,
    or when the cache itself is past its own reset (QF-R2). `force=True`
    (`refresh-quota`) always asks, bypassing the shared file's freshness
    bounds — though not an active rate-limit backoff. Defensive throughout: an
    undocumented surface must degrade to ``known=False`` rather than raise.
    """
    budget = Budget(provider="claude", known=False, source="cachedUsageUtilization")
    profile = Path(config_dir).expanduser() if config_dir else None
    if vault_profile and (profile is None or _claude_token(profile) is None):
        return replace(budget, note="vault account credentials are missing or expired")
    # With CLAUDE_CONFIG_DIR set the CLI keeps both files inside it; without,
    # the config sits beside the home directory and the credential inside
    # ~/.claude. Measured, not assumed — an empty profile dir grew a
    # .claude.json of its own the moment the CLI ran against it.
    state_file = (profile / ".claude.json") if profile else CLAUDE_STATE
    data = {}
    try:
        with state_file.open() as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        budget.note = f"{state_file} unreadable"

    cached = data.get("cachedUsageUtilization") or {}
    utilization = cached.get("utilization") or {}
    fetched_ms = cached.get("fetchedAtMs")
    now_ = time.time()
    if isinstance(fetched_ms, (int, float)):
        budget.stale_seconds = max(0.0, now_ - fetched_ms / 1000.0)

    margin = _reset_margin(project_config, limits)
    reset_suspected = bool(utilization) and _payload_past_reset(utilization, margin, now_)
    if fetch and (force or not utilization or reset_suspected
                 or (budget.stale_seconds or 0) > STALE_AFTER):
        fresh, note = _shared_usage(profile, force=force, margin=margin, now_=now_,
                                    reset_suspected=reset_suspected)
        if fresh:
            utilization, budget.stale_seconds = fresh, 0.0
            budget.source = "api/oauth/usage"
        elif not utilization:
            budget.note = note
    if not utilization:
        budget.note = budget.note or "no usage data; run Claude Code once"
        return budget

    # The normalised `limits` array is the friendliest surface; fall back to the
    # individual buckets if it is absent — `_usage_windows` tries
    # both. Every readable bucket comes back keyed by whatever names it
    # (falling back to its position); the worst-of-them logic below is
    # unchanged, `windows` is purely additional bookkeeping.
    windows = _usage_windows(utilization)
    worst_percent, worst_reset = None, None
    for name, detail in windows.items():
        percent = detail.get("percent")
        if worst_percent is None or percent > worst_percent:
            worst_percent, worst_reset = percent, detail.get("resets_at")

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
    if len(windows) > 1:
        budget.windows = windows          # a single-window profile is unaffected
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
        note="quota comes from the provider script's /usage probe; this built-in "
             "is the fallback for when that script is missing or cannot answer",
    )


# --------------------------------------------------------------------------


# Built-in readers, used only when a provider's script does not implement the
# `budget` action. Claude's quota lives in an undocumented internal cache with
# several bucket shapes, staleness to account for and an overage block; parsing
# that defensively in shell would be worse code in two places. A provider with
# no built-in and no script action is simply reported as unknown — which is the
# honest answer, and is what a newly added provider gets until it implements it.
_BUILTIN = {"claude": read_claude, "opencode": read_opencode, "agy": read_agy}


def _extends_chain(name: str, provider: Any,
                   providers: dict[str, Any] | None) -> list[str]:
    """The provider names an instance reaches through `extends`, nearest first.

    The chain is walked through the loaded provider map because a `Provider`
    keeps only its immediate `extends`; `resolve_inheritance` has already
    folded every field the other way. A base that is absent from the map stops
    the walk at the name it was given, so a reader it names can still be found.
    """
    chain: list[str] = []
    seen = {name}
    current = getattr(provider, "extends", "") or ""
    while current and current not in seen:
        chain.append(current)
        seen.add(current)
        ancestor = (providers or {}).get(current)
        current = (getattr(ancestor, "extends", "") or "") if ancestor else ""
    return chain


def _builtin_for(name: str, provider: Any,
                 providers: dict[str, Any] | None) -> tuple[str, Any]:
    """The built-in reader a provider reaches, and the provider that owns it.

    Resolution is through `extends`, never `family`: a family can hold
    providers on different binaries, and one CLI's quota read as another's is
    the confusion this avoids. The provider's own name is checked first, so a
    base provider keeps its reading exactly as it was. The owner is recorded on
    the Provider at load (`budget_builtin`), so every caller — the watchdog,
    `refresh-quota`, a runner — resolves a chain of any depth the same way;
    the caller's map is only a fallback for a directly-built Provider.
    """
    if name in _BUILTIN:
        return name, _BUILTIN[name]
    owner = getattr(provider, "budget_builtin", "") or ""
    if owner in _BUILTIN:
        return owner, _BUILTIN[owner]
    for ancestor in _extends_chain(name, provider, providers):
        if ancestor in _BUILTIN:
            return ancestor, _BUILTIN[ancestor]
    return "", None


def _builtin_budget(owner: str, name: str, reader: Any, provider: Any,
                    project_config: Path | None, force: bool,
                    limits: dict | None,
                    account_profiles: dict[str, Path] | None = None) -> Budget:
    """Run a built-in reader for `name`, on the right account.

    A provider reading under its own name gets the plain reader, unchanged. An
    instance that reaches the reader through `extends` NEVER gets the base
    account's reading: it is pointed at ITS OWN credentials — the directory its
    inherited `budget_profile_env` variable holds — and where that cannot be
    resolved it is reported unknown, naming what is missing.
    """
    if reader is None:
        return Budget(provider=name, known=False, source="none",
                      note="no budget action and no built-in reader")
    if account_profiles is not None:
        if not reader_takes_profile(reader):
            return Budget(provider=name, known=False, source="vault",
                          note="built-in reader cannot read vault account profiles")
        readings = {label: _call_reader(
            reader, config_dir=path, project_config=project_config,
            force=force, limits=limits, vault_profile=True)
            for label, path in account_profiles.items()}
        usable = {label: reading for label, reading in readings.items()
                  if reading.known and not reading.stale and reading.headroom is not None}
        if not usable:
            return Budget(provider=name, known=False, source="vault",
                          note="no readable vault account" +
                          (": " + "; ".join(f"{label}: {b.note}" for label, b in readings.items())
                           if readings else ""))
        best = max(usable, key=lambda label: usable[label].headroom)
        windows = {}
        for label, reading in usable.items():
            detail = reading.windows or {"quota": {
                "percent": (1 - reading.headroom) * 100,
                "resets_at": reading.resets_at}}
            for key, window in detail.items():
                if isinstance(window, dict):
                    windows[f"{label}/{key}"] = {
                        **window, "account": label,
                        "counted": label == best and window.get("counted", True)}
        return replace(usable[best], provider=name, windows=windows,
                       note=f"vault account {best}")
    if owner == name:
        return _call_reader(reader, project_config=project_config,
                            force=force, limits=limits)
    if not reader_takes_profile(reader):
        # The instance inherits this reader but the reader has no profile to
        # point at, so there is no way to read the INSTANCE's account. Running
        # it would read the base account and relabel the result.
        return Budget(
            provider=name, known=False, source="none",
            note=(f"extends {owner}, whose built-in reader cannot read a "
                  f"per-instance profile; refusing to report the base "
                  f"account's quota as {name}'s"))
    profile_env = getattr(provider, "budget_profile_env", "") or ""
    if not profile_env:
        return Budget(
            provider=name, known=False, source="none",
            note=(f"extends {owner}, whose built-in reader takes an account "
                  f"profile, but no budget_profile_env is declared; refusing to "
                  f"report the base account's quota as {name}'s"))
    credential_env = getattr(provider, "credential_env", None) \
        or getattr(provider, "env", None) or {}
    if not credential_env.get(profile_env):
        return Budget(
            provider=name, known=False, source="none",
            note=(f"extends {owner} but sets no {profile_env}; reading the "
                  f"base account would report the wrong quota"))
    reading = _call_reader(reader, config_dir=Path(resolved_profile(provider)),
                           project_config=project_config, force=force,
                           limits=limits)
    # The instance's own reading, under the instance's name: the reader hard-
    # codes the base provider it belongs to, and a dependent of `budget_from`
    # is relabelled the same way.
    return replace(reading, provider=name)


def reader_takes_profile(reader: Any) -> bool:
    """Can this built-in reader read an INSTANCE's own profile directory?

    Decided by signature, never by name: a reader that declares `config_dir`
    (or swallows keyword arguments) can be pointed at a second account, and the
    ones that take only the caller's spend cannot. The core names no CLI, so a
    newly registered reader answers this automatically.
    """
    import inspect

    parameters = inspect.signature(reader).parameters
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values()):
        return True
    return "config_dir" in parameters


def _call_reader(reader: Any, *, spent: dict[str, int] | None = None,
                 config_dir: Path | None = None,
                 project_config: Path | None = None, force: bool = False,
                 limits: dict | None = None, vault_profile: bool = False) -> Budget:
    """Call a registered built-in reader with only what it accepts.

    The registry holds a reader per CLI and their signatures differ — one takes
    the caller's spend, another a profile directory and the config lookup — and
    the core must not know which is which. Every argument is offered and the
    ones the reader does not declare are dropped; a reader taking ``**kwargs``
    receives them all.
    """
    import inspect

    parameters = inspect.signature(reader).parameters
    offered = {"spent": spent, "config_dir": config_dir,
               "project_config": project_config, "force": force, "limits": limits}
    if vault_profile:
        offered["vault_profile"] = True
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values()):
        return reader(**offered)
    return reader(**{key: value for key, value in offered.items()
                     if key in parameters})


# Budget is consulted on every spawn for routing. Without a cache that means
# three subprocesses per agent start, on the event loop.
_CACHE_TTL = 60.0
_cache: dict[str, _CacheEntry] = {}

# What a cached entry was actually read from. A cache hit used to be judged on
# provider name alone, so a second caller sharing a name with an unrelated
# first one — genuinely different config_dir, provider, whatever actually
# varies the answer — silently got the first one's Budget back without its
# own script ever running (F100). Kept as a side table rather than folded
# into `_cache`'s value tuple, so the two things a cache hit is judged against
# — freshness and identity — stay independently checkable.
_cache_source: dict[str, str] = {}

# RM-R4e: serialises a cache entry's hit updates, so concurrent hits cannot
# move `seen` backwards or count the same interval twice.
_cache_lock = threading.Lock()


class _CacheEntry:
    """One cache slot: the raw reading and its own ageing bookkeeping.

    RM-R4d/RM-R4e (reviews ag-f21a0c, ag-53986b): the cache time a hit adds
    to a reading's age belongs to THIS entry. An age floor keyed by name
    survived a replacement reading and wrote an old entry's age over a fresh
    one; held inside the entry it dies with it — a fresh read builds a new
    one, so the bookkeeping is always synchronised with replacement.

    `elapsed` is accumulated from the wall clock's forward movement only
    (`seen` is the last wall time a hit was judged at): a plain
    `now - wall` shrinks when the clock steps back and only catches up
    afterwards, which freezes the age in between — the thing RM-R4d
    forbids. Forward movement never goes back and never stops growing
    while the clock moves, and a hit that observes the clock behind `seen`
    has caught a backward step: under RM-R4e the entry is permanently
    expired and dropped.
    """

    __slots__ = ("wall", "budget", "elapsed", "seen", "executor")

    def __init__(self, wall: float, budget: Budget,
                 executor: tuple[str, str] | None = None) -> None:
        self.wall = wall                # the wall stamp the entry was cached under
        self.budget = budget            # the reader's own RAW budget (R17)
        self.elapsed = 0.0              # cache time, forward wall movement only
        self.seen = wall                # last wall time a hit was judged at
        self.executor = executor       # None for legacy, unscoped entries

    def __iter__(self):
        """Unpack as the `(wall, budget)` pair the entry replaced."""
        return iter((self.wall, self.budget))


# PS-R5: one fetch per budget source at a time (see `_source_reading`). The
# generation counts a source's completed fetches, so a caller that queued on
# the lock can tell a payload published while it waited from one it had
# already judged too old.
_fetch_locks: dict[str, threading.Lock] = {}
_fetch_locks_guard = threading.Lock()
_fetch_generation: dict[str, int] = {}


def _fetch_lock(name: str) -> threading.Lock:
    with _fetch_locks_guard:
        return _fetch_locks.setdefault(name, threading.Lock())


def invalidate_cache() -> None:
    _cache.clear()
    _cache_source.clear()


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
        stale_seconds=_script_stale_seconds(data.get("stale_seconds")),
        read_at=_script_read_at(data.get("read_at")),
    )


def _script_stale_seconds(value: Any) -> float | None:
    """CX-C15: a budget script's `stale_seconds`, the age of its reading.

    A finite number, clamped at 0 as the built-in readers clamp it — a clock
    a little ahead is not a reading from the future. Anything else is
    ignored: a malformed age must not spoil the rest of the reading.
    """
    value = _number(value)
    return None if value is None else max(0.0, value)


def _script_read_at(value: Any) -> float | None:
    """RM-R4b: a budget script's `read_at`, epoch seconds, the fallback age
    source when the script gives no `stale_seconds`.

    A finite number is taken as sent — a clock a little ahead gives a
    negative age, which clamps to 0 at use, the same tolerance the
    `stale_seconds` clamp shows. Anything else is ignored.
    """
    return _number(value)


# --------------------------------------------------------------------------
# PS-R5: a shared quota source, projected per provider
# --------------------------------------------------------------------------

def _window_used(detail: dict) -> float | None:
    """A window's percent USED, or None when it does not say one.

    `percent` is what every window carries; a window that gives only a
    `headroom` is converted, so either spelling counts.
    """
    percent = _number(detail.get("percent"))
    if percent is not None:
        return percent
    headroom = _number(detail.get("headroom"))
    if headroom is not None:
        return (1.0 - headroom) * 100.0
    return None


def _project_reading(name: str, base: Budget, provider: Any) -> Budget:
    """Read the shared payload as THIS provider's, through its own selector.

    PS-R5. The base is the budget source's own reading. A provider with no
    `budget_from` and no `budget_windows` of its own is returned untouched —
    the unchanged path. A dependent without a selector keeps the payload's
    own counted flags and headroom, as today, but drops the source's note:
    it describes the source's pool, not this provider's.

    With a selector, every window it matches counts — overriding the
    payload's flags (PS-R5a) — and headroom, severity, constraining window
    and reset are recomputed from the counted windows only. Windows it does
    not match stay in the reading for display, marked `counted: false`, and
    never constrain — `_effective_windows` sees to the second half, so the
    reset-margin recomputation cannot smuggle one back in. A selector that
    matches no valid window makes the reading unknown; it never falls back
    to the source's aggregate, whose constraint is somebody else's.
    """
    owner_name = getattr(provider, "budget_from", "") or ""
    selector = getattr(provider, "budget_windows", None)
    if selector is None and not owner_name:
        return base
    windows = base.windows if isinstance(base.windows, dict) else {}
    if selector is None:
        return replace(base, provider=name, spent={},
                       note=("" if owner_name else base.note))
    selected = {key: detail for key, detail in windows.items()
                if isinstance(detail, dict)
                and _window_used(detail) is not None
                and any(fnmatch.fnmatch(key, pattern) for pattern in selector)}
    marked: dict[str, Any] = {}
    for key, detail in windows.items():
        if isinstance(detail, dict):
            marked[key] = {**detail, "counted": key in selected}
        else:
            # Not a window at all — kept for display, never counted.
            marked[key] = detail
    # Spend is per provider: a dependent never inherits the source's.
    projected = replace(base, provider=name, windows=marked,
                        spent={} if owner_name else dict(base.spent))
    if not selected:
        # A failed source read has no windows to select. Keep its diagnostic
        # (including the login instruction) instead of blaming the selector.
        note = base.note if not base.known and base.note else (
            "no window in the shared budget reading matches "
            "this provider's budget_windows "
            f"({', '.join(selector)})")
        return replace(projected, known=False, headroom=None,
                       severity="unknown", resets_at=None, note=note)
    worst = max(selected, key=lambda key: _window_used(selected[key]) or 0.0)
    used = _window_used(selected[worst]) or 0.0
    return replace(
        projected,
        known=True,
        headroom=max(0.0, 1.0 - used / 100.0),
        severity=("critical" if used >= 90 else "warning" if used >= 75
                  else "normal"),
        resets_at=selected[worst].get("resets_at"),
        note=("" if owner_name else base.note),
    )


class _SourceReading(NamedTuple):
    """What `_source_reading` hands back: the raw payload, its age
    bookkeeping (RM-R4b/R4d), and the clock it was judged at."""
    raw: Budget
    wall: float          # the wall stamp the payload was cached under
    elapsed: float       # cache time, forward wall movement only
    now: float           # the clock this read was judged at


def _source_reading(name: str, provider: Any, executor: Any, config_dir: Path,
                    project_config: Path | None, use_cache: bool = True,
                    force: bool = False, limits: dict | None = None,
                    providers: dict[str, Any] | None = None,
                    _pass: set[str] | None = None) -> _SourceReading:
    """The RAW budget payload behind `name` — its own script's answer, or,
    PS-R5, its budget source's — with the cache entry's age bookkeeping.

    PS-R5: what is cached is the budget SOURCE's raw payload, under the
    source's name, and nothing else. No provider's PROJECTION is ever
    cached: every read projects the payload afresh (`read_provider`), so
    refreshing the source leaves no stale dependent reading behind and a
    selector-less dependent sees the raw aggregate, not the source's own
    projection.

    One fetch serves everybody reading the same source:
    - within a caller's pass (`_pass`, the sources already read in it) — a
      `read_all` that bypasses the cache, forced or not, fetches a shared
      source once, not once per provider;
    - across concurrent callers — a miss fetches under the source's own
      lock (`_fetch_lock`; provider I/O runs outside `_cache_lock`, which
      alone could not deduplicate it), and a caller that queued on it takes
      the payload published since its generation snapshot instead of
      fetching again. The snapshot is taken BEFORE the cache is looked at,
      and everything is judged again under the lock.

    RM-R4e/R4f (batch M): every look at an entry happens under `_cache_lock`
    — the identity check first, then the backward-step check (a hit that
    sees the clock behind the entry's `seen` drops it and re-reads; the
    dropped budget is never returned), then the forward-only `elapsed`
    accumulation. A replacement and its source identity publish together
    under the same lock, and every look reads its clock under that lock
    too, so a payload published concurrently is never taken for a backward
    step.

    The source's Provider object is carried on the dependent at load
    (`budget_owner`), so a lone dependent — read without its owner in the
    map — still reaches the owner's script.
    """
    _pass = set() if _pass is None else _pass
    owner_name = getattr(provider, "budget_from", "") or ""
    if owner_name:
        owner_provider = getattr(provider, "budget_owner", None) \
            or (providers or {}).get(owner_name)
        if owner_provider is None:
            moment = time.time()
            return _SourceReading(
                Budget(provider=name, known=False,
                       note=f"budget source {owner_name!r} is not a declared "
                            f"provider"), moment, 0.0, moment)
        # One level (PS-R5 load rules): the owner reads its own source.
        return _source_reading(owner_name, owner_provider, executor,
                               config_dir, project_config, use_cache, force,
                               limits, providers, _pass)
    source = str(config_dir)
    # The account profile is part of the identity: the MCP server reloads its
    # config on every call, so the same provider name can mean a different
    # account within one TTL, and neither the name nor the config dir changes
    # with it. The RESOLVED profile is used, so two spellings of one directory
    # are one identity. Only a profile-scoped provider gets the suffix; every
    # other provider keeps the exact identity it had, so an entry seeded by
    # name and config dir alone still matches.
    profile = resolved_profile(provider)
    if profile:
        source = f"{source}\x00{profile}"
    # A container's account differs from the host keyring even with the same
    # HOME. Reloading the executor must not reuse the other account's quota.
    kind = getattr(executor, "kind", "") or ""
    context = (kind, getattr(executor, "container", "") or "")
    account_profiles = None
    if kind == "docker":
        try:
            account_profiles = getattr(executor, "budget_accounts", lambda _name: None)(name)
            if account_profiles is not None:
                source += "\x00vault:" + json.dumps(
                    {label: str(path) for label, path in account_profiles.items()}, sort_keys=True)
        except (OSError, ValueError) as exc:
            moment = time.time()
            return _SourceReading(Budget(provider=name, known=False,
                                          note=f"invalid vault accounts: {exc}"), moment, 0.0, moment)

    def take(generation: int | None) -> _SourceReading | None:
        """The cached entry, aged to now, when this caller may use it; None
        for a miss. Call under `_cache_lock`: the clock is read HERE, under
        the lock (review ag-e6b702), so no entry can be published between
        the reading and the look — a publication that slipped in between
        left `seen` ahead of the sample, and the fresh entry was destroyed
        as a backward step (RM-R4e)."""
        moment = time.time()
        stored = _cache.get(name)
        if isinstance(stored, tuple):
            # A bare (wall, budget) pair — an old caller or a test that
            # backdated the entry by writing the shape it knew. It carries no
            # bookkeeping of its own, so it ages by the wall clock alone.
            entry = _CacheEntry(stored[0], stored[1])
        else:
            entry = stored
        if entry is None or _cache_source.get(name) != source:
            return None
        if entry.executor is None:
            # Older entries had no executor tag. Preserve their local or
            # unscoped reader behavior, but never reuse them for a container
            # or an explicitly unresolved executor.
            if executor is not None and kind != "local":
                return None
        elif entry.executor != context:
            return None
        if not (name in _pass
                or (use_cache and not force and moment - entry.wall < _CACHE_TTL)
                or (generation is not None
                    and _fetch_generation.get(name, 0) != generation)):
            return None
        if moment < entry.seen:
            # RM-R4f: a backward step. Invalidated, and the ordinary fetch
            # path runs in this same call; the dropped budget is never
            # returned. (A step that lands and recovers entirely between two
            # reads is unobservable — the accepted limit.)
            if _cache.get(name) is stored:
                _cache.pop(name, None)
            return None
        entry.elapsed += moment - entry.seen          # moment >= seen
        entry.seen = moment
        return _SourceReading(entry.budget, entry.wall, entry.elapsed, moment)

    generation = _fetch_generation.get(name, 0)
    with _cache_lock:
        hit = take(None)
    if hit is not None:
        return hit
    with _fetch_lock(name):
        with _cache_lock:
            hit = take(generation)
            generation = _fetch_generation.get(name, 0)
            # Read before the fetch, so the entry's age covers it; any later
            # look reads its clock under the lock, after this publication.
            moment = time.time()
        if hit is not None:
            _pass.add(name)
            return hit
        try:
            budget = _from_script(name, provider, executor, config_dir, project_config)
            if budget is None:
                owner, builtin = _builtin_for(name, provider, providers)
                if account_profiles is not None and builtin is None:
                    credential_provider = getattr(provider, "auth_owner", None)
                    if credential_provider is not None:
                        owner, builtin = _builtin_for(
                            credential_provider.name, credential_provider, providers)
                budget = _builtin_budget(owner, name, builtin, provider,
                                         project_config, force, limits, account_profiles)
        except Exception as exc:              # telemetry must never break a run
            budget = Budget(provider=name, known=False,
                            note=f"{type(exc).__name__}: {exc}")
        # The reader's own RAW budget — before any projection, margin or the
        # caller's spent, all of which are per-read overlays (R17, QF-R1).
        # RM-R4f: it publishes with its source identity, under the lock.
        with _cache_lock:
            _cache[name] = _CacheEntry(moment, budget, context)
            _cache_source[name] = source
            _fetch_generation[name] = generation + 1
    _pass.add(name)
    return _SourceReading(budget, moment, 0.0, moment)


def read_provider(name: str, provider: Any, executor: Any, config_dir: Path,
                  project_config: Path | None = None,
                  spent: dict[str, int] | None = None,
                  use_cache: bool = True, force: bool = False,
                  limits: dict | None = None,
                  max_reading_age: float | None = None,
                  providers: dict[str, Any] | None = None,
                  _pass: set[str] | None = None) -> Budget:
    """One provider's budget, margin- and age-corrected (see `_read_provider`).

    BP-R1: one read parses each config file at most once. Called inside a
    `read_all` it shares that call's `parse_once` view; called alone it holds
    one for its own duration, so the margin and the age bound below always
    judge the same version of a file even if it moves between them.
    """
    from .config import parse_once

    problems = getattr(executor, "configuration_problems", lambda: [])()
    if problems:
        return Budget(provider=name, known=False, note="; ".join(problems))
    with parse_once():
        return _read_provider(name, provider, executor, config_dir, project_config,
                              spent, use_cache, force, limits, max_reading_age,
                              providers, _pass)


def _read_provider(name: str, provider: Any, executor: Any, config_dir: Path,
                   project_config: Path | None = None,
                   spent: dict[str, int] | None = None,
                   use_cache: bool = True, force: bool = False,
                   limits: dict | None = None,
                   max_reading_age: float | None = None,
                   providers: dict[str, Any] | None = None,
                   _pass: set[str] | None = None) -> Budget:
    margin = _reset_margin(project_config, limits)
    # RM-R4b: the caller's loaded config wins (`reading_age_bound`); a caller
    # with none falls back to reading the layers itself, as the margin does.
    bound = max_reading_age if max_reading_age is not None \
        else _reading_age_bound_from_layers(project_config)
    # PS-R5: EVERY read — cache hit or fresh, the source's own or a
    # dependent's — passes through projection → reset margin → age bound →
    # provider-local spend. The cache holds the budget SOURCE's raw payload
    # only; an early return on a hit would hand the source its raw aggregate
    # back, counting windows its selector excludes. The age is the cache
    # entry's own (RM-R4b/R4d): deriving a projection does not reset it.
    read = _source_reading(name, provider, executor, config_dir,
                           project_config, use_cache, force, limits,
                           providers, _pass)
    now_ = read.now
    budget = _project_reading(name, read.raw, provider)
    budget = _apply_reset_margin(budget, margin, now_)
    budget = _apply_reading_age(budget, bound, now_, cached_at=read.wall,
                                elapsed=read.elapsed)
    # R16: return a copy so the fresh-read caller cannot poison the cache
    # either (Amendment 2 — F171).  Merge the caller's spent onto the copy.
    merged = {**budget.spent, **spent} if spent else dict(budget.spent)
    return replace(budget, spent=merged, windows=dict(budget.windows))


def read_all(providers: dict[str, Any] | None = None,
             executor_for: Any = None,
             config_dir: Path | None = None,
             project_config: Path | None = None,
             spend_by_provider: dict[str, dict[str, int]] | None = None,
             cooldowns: dict[str, dict] | None = None,
             use_cache: bool = True,
             force: bool = False,
             limits: dict | None = None,
             max_reading_age: float | None = None) -> dict[str, Budget]:
    """Read every provider's budget, driven by the loaded providers map.

    Previously a hardcoded three-name table that never consulted the providers
    at all, so a newly added provider could never appear and a non-claude
    orchestrator's quota could never be read.

    `limits` is the caller's own already-loaded (last-good) config, when it has
    one — see `_reset_margin`. A caller with no config of its own (a one-shot
    CLI read) can leave it unset; `quota_reset_margin_seconds` is then read
    from `project_config` itself, same as before. `max_reading_age` (RM-R4b,
    `budget.max_reading_age_seconds`) layers the same way: a loaded value
    wins, otherwise it is read from the config layers.
    """
    from .paths import global_config_dir

    cooldowns = cooldowns or {}
    spend_by_provider = spend_by_provider or {}
    config_dir = config_dir or global_config_dir()

    if providers is None:                  # legacy call sites: built-ins only
        providers = {name: None for name in _BUILTIN}

    class _NullExecutor:
        kind = "local"

    # FQ-R1: independent sources run in parallel; dependents stay in the
    # source's group, so even a forced read fetches it once per call.
    groups: dict[str, list[tuple[str, Any]]] = {}
    for name, provider in providers.items():
        if provider is not None and not getattr(provider, "enabled", True):
            continue
        source = getattr(provider, "budget_from", "") or name
        groups.setdefault(source, []).append((name, provider))

    from .config import parse_once, share_parse_once

    with parse_once() as view:
        def read_group(items):
            out = {}
            seen: set[str] = set()
            with share_parse_once(view):
                for name, provider in items:
                    executor = executor_for(name) if callable(executor_for) else _NullExecutor()
                    budget = read_provider(
                        name, provider, executor, config_dir, project_config,
                        spend_by_provider.get(name), use_cache, force,
                        limits=limits, max_reading_age=max_reading_age,
                        providers=providers, _pass=seen)
                    entry = cooldowns.get(name)
                    if entry and entry.get("until", 0) > time.time():
                        budget.cooldown_until = entry["until"]
                        budget.severity = "critical"
                        budget.note = (budget.note + " | " if budget.note else "") + entry.get("reason", "cooling down")
                    out[name] = budget
            return out

        with ThreadPoolExecutor(max_workers=8, thread_name_prefix="budget") as pool:
            results = list(pool.map(read_group, groups.values()))
    readings = {name: reading for result in results for name, reading in result.items()}
    return {name: readings[name] for name in providers if name in readings}


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


def _why_not(candidate: Budget | None, reserve: float, reserved: bool) -> str:
    """The one phrase that says why a candidate was passed over."""
    if candidate is None:
        return "no budget reading"
    if candidate.cooldown_until and candidate.cooldown_until > time.time():
        left = (candidate.cooldown_until - time.time()) / 60
        return f"cooling down for {left:.0f}m"
    if candidate.known and candidate.headroom is not None and candidate.headroom <= 0.02:
        return "window empty"
    if reserved and candidate.known and candidate.headroom is not None \
            and candidate.headroom < reserve:
        return (f"below the {reserve:.0%} reserve kept for the orchestrator "
                f"({candidate.headroom:.0%} left)")
    return "no room"


def _has_room(candidate: Budget | None, reserve: float, reserved: bool) -> bool:
    """Can work go here? Unknown headroom is not no headroom."""
    if candidate is None:
        return True
    if not candidate.usable:
        return False
    if reserved and candidate.known and candidate.headroom is not None:
        return candidate.headroom >= reserve
    return True


def resets_soon(candidate: Budget | None, within: float) -> bool:
    """Will this come back on its own shortly?

    The difference between a five-hour window filling and a monthly cap being
    reached, and it decides whether work should WAIT or move. Moving a swarm of
    workers onto the orchestrator's account to avoid a twenty-minute wait is how
    the orchestrator starves — an advisor's point, and the reason this exists.
    """
    if candidate is None or candidate.usable:
        return False
    when = candidate.cooldown_until
    if not when and candidate.resets_at:
        try:
            from datetime import datetime
            when = datetime.fromisoformat(str(candidate.resets_at)).timestamp()
        except (ValueError, TypeError):
            when = None
    if not when:
        return False
    return 0 < (when - time.time()) <= within


def _known_reading(budget: Budget | None) -> bool:
    """Is this a reading that says something usable about headroom?

    RM-R3a's "known": the provider reported a number. A reading that is
    merely not-refused (``known=False``, or no reading at all) is unknown,
    not good — and so is one that has aged past the reading bound (RM-R4b):
    a stale reading ranks as unknown inside its tier, never ahead of a
    known one.
    """
    return budget is not None and budget.known and not budget.stale


def pick_instance(names: list[str], budgets: dict[str, Budget], reserve: float,
                  reserved: set[str], load: dict[str, int] | None = None,
                  last_used: dict[str, float] | None = None) -> str | None:
    """Which of several interchangeable accounts should take this work.

    NOT the one with the most headroom. Headroom is a percentage refreshed at
    most every few minutes and shared by every concurrent agent, so sorting on
    it pins ten spawns to whichever instance was ahead at the last reading and
    annihilates it before the next — an advisor's objection, and correct.
    Headroom is a filter here, never a ranking.

    The ranking is load: fewest agents running on it, then longest since it was
    last used. Both are read from the tree, so every MCP server process on the
    machine ranks them the same way.

    RM-R3b: an instance whose reading is KNOWN and roomy is preferred to one
    whose headroom is unknown BEFORE load and last use are compared — an
    unknown sibling may itself be exhausted, so knownness outranks load.
    An unknown reading stays eligible and wins when nothing known can take
    the work.
    """
    load = load or {}
    last_used = last_used or {}
    free = [name for name in names
            if _has_room(budgets.get(name), reserve, name in reserved)]
    if not free:
        return None
    # Prefer instances not held for the orchestrator; fall back to those only
    # when nothing else can take it, and even then only above the reserve.
    workers = [name for name in free if name not in reserved]
    pool = workers or free
    known = [name for name in pool if _known_reading(budgets.get(name))]
    return min(known or pool,
               key=lambda name: (load.get(name, 0), last_used.get(name, 0.0), name))


def choose_provider(
    preferred: str,
    budgets: dict[str, Budget],
    chain: list[str],
    reserve: float = 0.15,
    reserved: Any = None,
    allowed: Any = None,
    family: Any = None,
    load: dict[str, int] | None = None,
    last_used: dict[str, float] | None = None,
    wait_for_reset_within: float = 0.0,
    routes: list[str] | None = None,
) -> tuple[str | None, str]:
    """Pick a provider to run on. Returns ``(provider, reason)``.

    ``None`` means no candidate can take the work and it should be deferred
    until something resets.

    ``reserved`` names the providers the headroom reserve applies to — see
    :func:`reserved_providers`.

    ``allowed`` names the providers THIS agent can actually run on: its own,
    plus every provider it names a model for. It matters because a model id
    belongs to its provider's namespace, so an agent cannot simply be moved.
    A mapping is accepted as well as a set, so a future rule that cares about
    *which* model it would land on has it to hand rather than needing a new
    argument.

    That argument exists because of a live failure. The chain was
    `[opencode, agy]`, claude was cooling down after a revoked token, and the
    agent named a model for agy only. This returned the FIRST usable chain
    entry — opencode — the caller found no model for it, and instead of asking
    for the next candidate the caller gave up and ran on claude anyway: five
    more runs into an authentication wall, with the agent's configured fallback
    sitting one place further down the chain, unused. Filtering here means the
    answer is always one the caller can act on.

    ``routes`` (RM-R2a) is Tier B: the agent's own ``models:`` routes, each
    followed by its family siblings, in the order the agent wrote them.
    ``chain`` is then Tier C: the project ``fallback_chain`` entries not
    already listed, with ``defer`` still ending the walk after both. Without
    ``routes`` (older callers) the chain is walked as the one tier it was.
    """
    reserved = set(budgets) if reserved is None else set(reserved)
    allowed = None if allowed is None else set(allowed)
    siblings = [name for name in (family or []) if name != preferred]
    # FS-R2: the family pool never widens the candidate set. An instance the
    # caller did not allow — one the agent never named — is not a sibling for
    # routing, whatever the roster says shares its family.
    if allowed is not None:
        siblings = [name for name in siblings if name in allowed]
    routes = list(dict.fromkeys(routes or []))

    # With more than one account on this CLI, the agent's pin chooses the
    # FAMILY and the router chooses the instance. That is the point of a second
    # subscription: an agent pinned to the account the orchestrator is using
    # should run on the other one while it can, so the orchestrator keeps a
    # window to read the results in — and it should do that while the
    # orchestrator's account still looks healthy, not once it is already in
    # trouble.
    #
    # RM-R2a: preferred plus same-family instances are ONE pool (Tier A),
    # chosen by reservation, load and last use; they are never split into two
    # preference tiers.
    if siblings:
        chosen = pick_instance([preferred, *siblings], budgets, reserve,
                               reserved, load, last_used)
        if chosen == preferred:
            return preferred, "preferred provider has headroom"
        if chosen:
            if not _has_room(budgets.get(preferred), reserve, preferred in reserved):
                return chosen, f"{preferred} is constrained; using {chosen}"
            if preferred in reserved and chosen not in reserved:
                return chosen, f"{preferred} is held for the orchestrator; using {chosen}"
            if not _known_reading(budgets.get(preferred)) and _known_reading(budgets.get(chosen)):
                return chosen, f"{preferred} has no quota reading; using {chosen}"
            if (load or {}).get(chosen, 0) < (load or {}).get(preferred, 0):
                return chosen, f"sharing accounts: {chosen} has fewer running agents"
            if (last_used or {}).get(chosen, 0.0) < (last_used or {}).get(preferred, 0.0):
                return chosen, f"sharing accounts: {chosen} was used less recently"
            return chosen, f"sharing accounts: {chosen} wins the account name tie-break"
        # Nothing in the family can take it. If one of them is merely waiting
        # out a short window, waiting is cheaper than moving the work to
        # another vendor's model — and far cheaper than spending the
        # orchestrator's account on it.
        if wait_for_reset_within and any(
                resets_soon(budgets.get(name), wait_for_reset_within)
                for name in [preferred, *siblings]):
            return None, (f"{preferred} and its other accounts are full, and one "
                          f"resets shortly — waiting rather than moving the work")
    elif _has_room(budgets.get(preferred), reserve, preferred in reserved):
        return preferred, "preferred provider has headroom"

    skipped, no_room = [], []
    stop = False

    def try_tier(tier: list[str]) -> str | None:
        """Walk one preference tier, twice (RM-R3a): candidates whose reading
        is known AND roomy first, then the unknown ones, each pass in written
        order. The ranking never moves a candidate across tiers.
        """
        nonlocal stop
        order = [name for name in tier if name != preferred]
        if "defer" in order:
            order = order[:order.index("defer")]
            stop = True                    # defer ends the walk, after this tier
        roomy = [name for name in order
                 if _known_reading(budgets.get(name))
                 and _has_room(budgets.get(name), reserve, name in reserved)]
        for names in (roomy, [name for name in order if name not in roomy]):
            for name in names:
                if allowed is not None and name not in allowed:
                    skipped.append(name)
                    continue               # no model for it; not a candidate
                # The reserve applies to a fallback too. Otherwise work diverted
                # off a constrained provider lands on the orchestrator's own and
                # eats exactly the slice the reserve exists to keep.
                if budgets.get(name) is not None \
                        and _has_room(budgets.get(name), reserve, name in reserved):
                    return name
                no_room.append(f"{name} ({_why_not(budgets.get(name), reserve, name in reserved)})")
        return None

    chosen = try_tier(routes)
    if chosen:
        # RM-R2: the message says the route came from the agent's own list.
        return chosen, (f"{preferred} is constrained; using {chosen} from "
                        f"the agent's own models list")
    if not stop:
        # Tier C: the project chain entries neither Tier A nor Tier B listed.
        listed = {preferred, *siblings, *routes}
        chosen = try_tier([name for name in chain if name not in listed])
        if chosen:
            if routes:
                # RM-R2: with the agent's own routes in play, say this one
                # came from the project chain instead.
                return chosen, (f"{preferred} is constrained; falling back to "
                                f"{chosen} from the project fallback_chain")
            return chosen, f"{preferred} is constrained; falling back to {chosen}"

    # Both halves, and the second one is the half that matters. Naming only the
    # providers skipped for lack of a MODEL made the message actively
    # misleading: an agent whose only fallback was cooling down was told "no
    # model named for opencode", the one provider it had never asked for, and
    # nothing at all about the one that had failed it. Reported as bug-583360 by
    # somebody who reasonably concluded the router was looking at the wrong
    # provider entirely.
    why = f"{preferred} and all fallbacks are exhausted or cooling down"
    detail = []
    if no_room:
        detail.append("; ".join(no_room))
    if skipped:
        detail.append(f"no model named for {', '.join(skipped)} — add one under "
                      f"`models:` to allow failover there")
    if detail:
        why += f" ({'; '.join(detail)})"
    return None, why
