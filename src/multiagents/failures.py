"""TF-R1: transient-failure classification, shared by the runner and the engine.

Every attempt that ends other than ``done`` keeps a classified cause:

    {"class": ..., "provider": ..., "account": ..., "model": ...,
     "exit_code": ..., "signal": ..., "excerpt": ..., "reset_time": ...}

The class precedence is the contract's own (context/specs/transient-failures.md,
TF-R1): question, stopped, auth, refusal, spend_cap, throttle, quota, launch,
crash, transient, other — first match wins. Provider-declared ``failures:``
patterns (the interface of 2026-10-10) drive transient/quota matching and reset
extraction; the words alone classify nothing.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from .auth import looks_like_auth_failure
from .redact import scrub
from .supervisor import looks_like_rate_limit

# TF-R2: the classes one bounded automatic retry path owns. `throttle` keeps
# today's 429 path (Decision 3) and is deliberately absent.
RETRYABLE = frozenset({"launch", "crash", "transient"})

EXCERPT_CHARS = 2000

# A retry delay, as opposed to an announced reset: the words a CLI prints when
# it will retry the request itself, or a Retry-After the runner parsed.
_DELAY_WORDS = re.compile(r"\bwill be retried\b", re.IGNORECASE)

# The quota wordings that are ONLY ever quota. The rate-limit phrasings
# ("rate limit", "too many requests") are excluded: a bare 429 with no quota
# wording keeps the throttle path (Decision 3), and "rate limited - the
# request will be retried" is the CLI promising a retry, not an exhaustion.
_RATE_ONLY_MARKERS = {"rate limit", "rate_limit", "too many requests"}
_SUCCESS_STATUSES = {"SUCCESS", "OK", "COMPLETED", "DONE"}


class _PatternLists(dict):
    """Pattern lists with the provider's interpretation of naive resets."""

    reset_timezone = timezone.utc


def delay_announced(retry_after, error_text: str) -> bool:
    """TF-R1 `throttle`: a rate-limit signal WITH a retry delay.

    The z.ai exhaustion arrives as an HTTP 429 whose text announces a reset,
    not a delay — that is `quota`, never `throttle` (Decision 4).
    """
    if retry_after is not None:
        return True
    return bool(_DELAY_WORDS.search(error_text or ""))


def quota_wording(blob: str) -> bool:
    """Does this failure text say the ACCOUNT is empty, in the provider's own
    words (a usage or spend limit, exhausted credit) — the markers that are
    about capacity rather than pacing?"""
    from .supervisor import _QUOTA_MARKERS
    lowered = (blob or "").lower()
    return any(marker in lowered for marker in _QUOTA_MARKERS
               if marker not in _RATE_ONLY_MARKERS)


def pattern_lists(provider) -> dict[str, list[str]]:
    """The provider's declared `failures:` lists; absent means no match."""
    block = getattr(provider, "failures", None) if provider is not None else None
    if not isinstance(block, dict):
        return {}
    out = _PatternLists()
    zone = block.get("reset_timezone", "+00:00")
    if isinstance(zone, str) and re.fullmatch(r"[+-]\d{2}:\d{2}", zone):
        hours, minutes = map(int, zone[1:].split(":"))
        if hours < 24 and minutes < 60:
            offset = timedelta(hours=hours, minutes=minutes)
            out.reset_timezone = timezone(-offset if zone.startswith("-") else offset)
    for key in ("transient", "quota", "reset"):
        value = block.get(key)
        if isinstance(value, list):
            out[key] = [str(item) for item in value]
    return out


def matches(patterns: list[str], text: str) -> str:
    """First case-insensitive pattern that occurs in `text`, else ""."""
    for pattern in patterns or []:
        try:
            match = re.search(pattern, text or "", re.IGNORECASE)
        except re.error:
            continue
        if match:
            return match.group(0)
    return ""


def parse_reset(value, *, naive_timezone=timezone.utc) -> str:
    """A reset instant as a timezone-aware ISO string, or "".

    Structured and generic naive timestamps mean UTC. A provider's textual
    reset may supply its own timezone; every returned instant is UTC.
    """
    if value in (None, ""):
        return ""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(value, timezone.utc).isoformat()
        except (ValueError, OverflowError, OSError):
            return ""
    text = str(value).strip()
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return ""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=naive_timezone)
    return moment.astimezone(timezone.utc).isoformat()


def extract_reset(patterns: list[str], text: str, *, naive_timezone=timezone.utc) -> str:
    """The reset time a provider's `reset:` pattern announces, or ""."""
    for pattern in patterns or []:
        try:
            match = re.search(pattern, text or "", re.IGNORECASE)
        except re.error:
            continue
        if match and match.groupdict().get("reset"):
            reset = parse_reset(match.group("reset"), naive_timezone=naive_timezone)
            if reset:
                return reset
    return ""


def classify(*, status: str, exit_code=None, error_text: str = "",
             patterns: dict[str, list[str]] | None = None, provider: str = "",
             account: str = "", model: str = "", markers=None, refusal=False,
             cap_stop=False, retry_after=None, resets_at=None, launched=True,
             final_status: str = "") -> dict:
    """The TF-R1 cause of a finished attempt, class precedence first-wins.

    `status` is the run's terminal status; `error_text` its failure channels
    (stderr and the provider's own error payload) — never the assistant's
    output, which only ever discusses these words.
    """
    patterns = patterns if patterns is not None else {}
    text = error_text or ""
    # Successful result tags are metadata, not provider error prose. A
    # process can announce SUCCESS and then crash without another error.
    blob = "\n".join(line for line in f"{final_status}\n{text}".splitlines()
                     if line.strip().upper() not in _SUCCESS_STATUSES).strip()
    quota = (status in {"quota", "limited"}
             or matches(patterns.get("quota", []), blob)
             or quota_wording(blob))
    if markers or status == "awaiting_user":
        kind = "question"
    elif status == "cancelled":
        kind = "stopped"
    elif status == "unauthenticated" or looks_like_auth_failure(final_status, text):
        kind = "auth"
    elif status == "refused" or refusal:
        kind = "refusal"
    elif cap_stop or status == "spend_cap":
        kind = "spend_cap"
    elif ((status == "rate_limited" or looks_like_rate_limit(final_status, text))
          and (delay_announced(retry_after, blob) or not quota)):
        # Decision 3: the 429 path keeps its own behaviour. Decision 4: a 429
        # announcing a usage limit is quota, never throttle.
        kind = "throttle"
    elif quota:
        kind = "quota"
    elif not launched:
        kind = "launch"
    elif status == "session_lost" or any(line.strip().upper() == "SESSION_LOST"
                                         for line in blob.splitlines()):
        kind = "transient"
    elif _signal_of(exit_code) is not None or (exit_code not in (None, 0)):
        kind = "crash" if not blob else (
            "transient" if matches(patterns.get("transient", []), blob) else "other")
    elif matches(patterns.get("transient", []), blob):
        kind = "transient"
    elif status in {"failed", "timeout", "truncated", "session_lost"}:
        kind = "crash" if not blob else "other"
    else:
        kind = "other"
    cause = {"class": kind, "provider": provider, "account": account, "model": model,
             "exit_code": exit_code if type(exit_code) is int else None,
             "signal": _signal_of(exit_code),
             "excerpt": scrub(text)[:EXCERPT_CHARS]}
    reset = parse_reset(resets_at) if resets_at else ""
    if not reset:
        zone = getattr(patterns, "reset_timezone", timezone.utc)
        reset = extract_reset(patterns.get("reset", []), text, naive_timezone=zone)
    if reset:
        cause["reset_time"] = reset
    else:
        cause["reset_time"] = None
    return cause


# TF-R4: the wrapper's own words when the provider process never came up —
# a broken interpreter, a removed binary. The wrapper caught the exec error
# and recorded it as exit 127 with this line on stderr; the failure is a
# launch failure, and the recorded errno is an exec failure of the file the
# host resolved, so it is reported as the exception it is.
LAUNCH_MARKER = "agentwrap: could not start"


def spawn_failure(stderr: str) -> str:
    """The exception text of a provider process that could not be started,
    or "" when stderr shows no launch failure."""
    match = re.search(r"(?m)^" + re.escape(LAUNCH_MARKER) + r"\s+([^\n]+)", stderr or "")
    if not match:
        return ""
    detail = match.group(1).rstrip()
    errno = re.search(r"\[Errno (\d+)\]", detail)
    kind = ({2: "FileNotFoundError", 1: "PermissionError", 13: "PermissionError"}
            .get(int(errno.group(1)), "OSError") if errno else "OSError")
    if "embedded null byte" in detail:
        kind = "ValueError"
    return f"{kind}: {detail}"


def cause_from_result(message, markers=None, provider=None, *, run_provider="",
                      account="", model="") -> dict:
    """The TF-R1 cause of a finished attempt, from its recorded result.

    Used where the Run object is gone (the engine reading result.json): the
    same precedence and the same fields `classify` produces, so a cause read
    from a public view equals the one recorded at capture.
    """
    text = spawn_failure(message.get("stderr_tail", "") or "")
    if text:
        return classify(status="failed", launched=False, error_text=text,
                        patterns=pattern_lists(provider), provider=run_provider,
                        account=account, model=model, markers=markers)
    return classify(status=message.get("status", ""), exit_code=message.get("exit_code"),
                    error_text=f"{message.get('final_status', '')}\n{message.get('stderr_tail', '')}",
                    patterns=pattern_lists(provider), provider=run_provider,
                    account=account, model=model, markers=markers,
                    retry_after=message.get("retry_after"),
                    resets_at=message.get("resets_at"))


def _signal_of(exit_code):
    if isinstance(exit_code, int) and not isinstance(exit_code, bool) and exit_code < 0:
        return -exit_code
    return None


def retry_policy(scheduler: dict) -> tuple[int, list[float]]:
    """TF-R2's `scheduler.retries` interface: (budget, backoff seconds).

    The budget defaults to 3 retries per activation; the backoff list
    defaults to [30, 120, 600] and its last value repeats.
    """
    retries = (scheduler or {}).get("retries") or {}
    try:
        budget = int(retries.get("budget", 3))
    except (TypeError, ValueError):
        budget = 3
    raw = retries.get("backoff_seconds")
    backoff = [float(value) for value in raw] if isinstance(raw, list) and raw else [30.0, 120.0, 600.0]
    return max(0, budget), backoff


def backoff_for(backoff: list[float], retry_number: int) -> float:
    """The delay before retry `retry_number` (1-based); the last value repeats."""
    if not backoff:
        return 0.0
    return backoff[min(retry_number - 1, len(backoff) - 1)]
