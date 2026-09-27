"""What this project costs the orchestrator's own Claude subscription.

`multiagents usage` answers where the *agents'* tokens went, from our own stream
accounting. This module answers the other question, which no stream of ours can
see: running multiagents means attaching an MCP server to a Claude Code session,
and that server's tool results are the orchestrator's context. They are paid for
out of the human's subscription, on every request for the rest of the session.

Claude Code knows this and says so — its `/usage` panel reports a line like
"16% of your usage came from the MCP server multiagents". Three things make that
line hard to build on, and are why this module exists rather than a probe:

* It is **prose**. `claude -p "/usage" --output-format json` is free (num_turns 0,
  total_cost_usd 0 — it is answered locally, and `local_command: "usage"` is the
  marker that it never reached a model), but unlike agy's `/usage` it carries no
  structured payload. The figures come back rounded, and the server list is
  "Top MCP servers", truncated to an unstated N.
* Asking pollutes the answer. Each probe opens a session and writes a transcript,
  so polling `/usage` inflates the session count `/usage` reports.
* It cannot break down per model, per project or per tool, which is the level at
  which a decision about an MCP server actually gets made.

All of it is computed from local transcripts, so we compute it from those too.

How the attribution works
-------------------------
The cost of an MCP tool result is not the tokens it returned. It is those tokens
carried in the context of **every later request in that session** — which is
exactly what the panel's own advice means by "MCP tool results stay in context
for the rest of the session". So the unit of work here is one API request:

* The denominator is the request's *measured* context (input + cache read +
  cache creation). Measured, not reconstructed, because the context also holds
  the system prompt, CLAUDE.md and the tool definitions, none of which appear in
  the transcript. Reconstructing the denominator instead was the first thing
  tried and it overstated the MCP share roughly twofold.
* The numerator is the MCP content accumulated in that session *before* this
  request: tool results and the arguments of the calls that produced them,
  estimated at :data:`CHARS_PER_TOKEN`.
* That share is applied to the request's dollar cost and summed.

Applying a token share to a dollar cost assumes MCP content is cached at the
same rate as everything else in the context. It is ordinary conversation history,
so it is — but a session that never caches will attribute slightly high.

Known limits, stated rather than hidden:

* **Tool definitions are not counted.** An MCP server puts its whole tool schema
  in context before any tool is called, and the transcript never records it, so
  this is a floor on a server's true cost, not a full measure.
* **Compaction resets the numerator.** At a `compact_boundary` the context
  becomes a summary, and tool results are what compaction drops first. Carrying
  the accumulator across the boundary would bill a server for tokens that are no
  longer there.
* **Dollars are a weighting, not a bill.** Subscription usage is not billed at
  API rates. The rates below exist to make an Opus request count for more than a
  Sonnet one when computing a share; read the percentages, not the totals.
"""

from __future__ import annotations

import json
import os
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

# Public API rates, $ per million tokens, as (input, output). Cache write is
# 1.25x input at the 5-minute TTL and 2x at one hour; cache read is 0.1x. These
# weight one request against another — see the docstring on what they are not.
PRICES: dict[str, tuple[float, float]] = {
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0),
    "claude-opus-4-6": (5.0, 25.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
}
FALLBACK_PRICE = (5.0, 25.0)       # an unknown model is priced as Opus, not free

CHARS_PER_TOKEN = 4.0
BIG_CONTEXT = 150_000              # the bucket Claude Code's own panel reports


def default_root() -> Path:
    """Where Claude Code keeps its transcripts, honouring its own override."""
    configured = os.environ.get("CLAUDE_CONFIG_DIR")
    base = Path(configured).expanduser() if configured else Path.home() / ".claude"
    return base / "projects"


def _price(model: str) -> tuple[float, float]:
    if model in PRICES:
        return PRICES[model]
    # Transcripts carry dated ids ("claude-opus-4-5-20251101") and, for the
    # synthetic assistant turns Claude Code writes itself, "<synthetic>".
    for known, price in PRICES.items():
        if model.startswith(known):
            return price
    return FALLBACK_PRICE


def request_cost(model: str, usage: dict[str, Any]) -> float:
    """Dollar weight of one API request."""
    p_in, p_out = _price(model or "")
    created = usage.get("cache_creation") or {}
    write_5m = created.get("ephemeral_5m_input_tokens", 0)
    write_1h = created.get("ephemeral_1h_input_tokens", 0)
    if not (write_5m or write_1h):      # older transcripts carry only the total
        write_5m = usage.get("cache_creation_input_tokens", 0)
    return (
        usage.get("input_tokens", 0) * p_in
        + usage.get("cache_read_input_tokens", 0) * p_in * 0.10
        + write_5m * p_in * 1.25
        + write_1h * p_in * 2.0
        + usage.get("output_tokens", 0) * p_out
    ) / 1e6


def _figure(value: Any) -> int | float:
    """One usage field, coerced to a number.

    A transcript's usage fields are normally ints, but a numeric string must
    still add rather than concatenate — three string figures summed with `+`
    silently became one five-million-token string.
    """
    if isinstance(value, bool):
        raise TypeError("bool is not a usage figure")
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        return float(value)
    raise TypeError(f"not a usage figure: {value!r}")


def context_tokens(usage: dict[str, Any]) -> int:
    """Everything the model was handed on this request."""
    return (_figure(usage.get("input_tokens", 0))
            + _figure(usage.get("cache_read_input_tokens", 0))
            + _figure(usage.get("cache_creation_input_tokens", 0)))


def server_of(tool_name: str) -> str | None:
    """``mcp__multiagents__agent_tree`` -> ``multiagents``; built-ins -> None."""
    if not tool_name.startswith("mcp__"):
        return None
    parts = tool_name.split("__")
    return parts[1] if len(parts) >= 3 and parts[1] else None


@dataclass
class Request:
    """One API request, with the MCP weight its context was carrying."""
    session: str
    project: str
    model: str
    at: float
    cost_usd: float
    context: int
    mcp_tokens: dict[str, float] = field(default_factory=dict)

    @property
    def mcp_share(self) -> float:
        """Fraction of this request's context that is MCP content, capped at 1.

        The cap matters: a request whose context was evicted and rebuilt can
        measure smaller than what we reconstructed for it, and an uncapped
        share would then bill a server for more than the whole request.
        """
        if self.context <= 0:
            return 0.0
        return min(1.0, sum(self.mcp_tokens.values()) / self.context)


def _timestamp(record: dict[str, Any]) -> float | None:
    raw = record.get("timestamp")
    if not isinstance(raw, str):
        return None
    try:
        from datetime import datetime
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def walk_session(path: Path, since: float) -> Iterator[Request]:
    """Yield the in-window requests of one transcript, in order.

    The whole file is walked even when only its tail is in window: the MCP
    weight a request is carrying is the sum of everything before it, so
    starting at the window boundary would report a long session's later
    requests as carrying almost nothing.
    """
    owner: dict[str, str] = {}          # tool_use id -> server
    carried: Counter[str] = Counter()   # server -> chars of MCP content in context
    seen: set[str] = set()
    project = path.parent.name
    session = path.stem

    try:
        handle = path.open(errors="replace")
    except OSError:
        return
    with handle:
        for line in handle:
            try:
                record = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue

            if (record.get("type") == "system"
                    and record.get("subtype") == "compact_boundary"):
                # Compaction does not empty the context, it shrinks it, and the
                # record says by how much. Scaling by what survived beats both
                # extremes: carrying the accumulator across the boundary bills a
                # server for tokens that are gone (it roughly doubled every
                # share when measured), and zeroing it credits back MCP content
                # the summary still describes. In practice the ratio is small —
                # a 967k context came back as 23k — so this lands near a reset
                # without pretending the boundary is one.
                meta = record.get("compactMetadata") or {}
                pre = meta.get("preTokens")
                post = meta.get("postTokens")
                if isinstance(pre, (int, float)) and pre > 0 and \
                        isinstance(post, (int, float)):
                    ratio = max(0.0, min(1.0, post / pre))
                    for server in list(carried):
                        carried[server] = int(carried[server] * ratio)
                else:
                    carried.clear()     # no metadata; assume nothing survived
                continue

            message = record.get("message") or {}
            usage = message.get("usage") or {}
            request_id = record.get("requestId")

            # Attribute BEFORE accumulating this record: a request is charged
            # for the context it was sent, not for the reply it produced.
            if (record.get("type") == "assistant" and usage and request_id
                    and request_id not in seen):
                seen.add(request_id)
                at = _timestamp(record)
                if at is not None and at >= since:
                    yield Request(
                        session=session, project=project,
                        model=message.get("model") or "",
                        at=at,
                        cost_usd=request_cost(message.get("model") or "", usage),
                        context=context_tokens(usage),
                        mcp_tokens={s: c / CHARS_PER_TOKEN
                                    for s, c in carried.items() if c},
                    )

            content = message.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict):
                    continue
                kind = block.get("type")
                if kind == "tool_use":
                    server = server_of(str(block.get("name") or ""))
                    if server:
                        owner[str(block.get("id"))] = server
                        # The call arguments are the server's doing too, and a
                        # large input is a large context cost.
                        carried[server] += len(json.dumps(block.get("input"),
                                                          default=str))
                elif kind == "tool_result":
                    server = owner.get(str(block.get("tool_use_id")))
                    if server:
                        carried[server] += len(json.dumps(block.get("content"),
                                                          default=str))


@dataclass
class Report:
    window_hours: float
    requests: int = 0
    sessions: int = 0
    cost_usd: float = 0.0
    by_server: dict[str, float] = field(default_factory=dict)
    by_model: dict[str, float] = field(default_factory=dict)
    big_context_usd: float = 0.0
    projects: dict[str, float] = field(default_factory=dict)

    def share(self, amount: float) -> float:
        return (amount / self.cost_usd) if self.cost_usd else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "window_hours": self.window_hours,
            "requests": self.requests,
            "sessions": self.sessions,
            "cost_usd": round(self.cost_usd, 4),
            "mcp_servers": {
                name: {"cost_usd": round(cost, 4),
                       "share": round(self.share(cost), 4)}
                for name, cost in sorted(self.by_server.items(),
                                         key=lambda kv: -kv[1])
            },
            "by_model": {
                name: {"cost_usd": round(cost, 4),
                       "share": round(self.share(cost), 4)}
                for name, cost in sorted(self.by_model.items(),
                                         key=lambda kv: -kv[1])
            },
            "by_project": {
                name: {"cost_usd": round(cost, 4),
                       "share": round(self.share(cost), 4)}
                for name, cost in sorted(self.projects.items(),
                                         key=lambda kv: -kv[1])
            },
            f"share_above_{BIG_CONTEXT // 1000}k_context":
                round(self.share(self.big_context_usd), 4),
            "note": ("dollars weight models against each other; subscription "
                     "usage is not billed at API rates. MCP tool definitions "
                     "are not counted, so a server's share is a floor."),
        }


def analyse(window_hours: float = 24.0, root: Path | None = None,
            now: float | None = None) -> Report:
    """Attribute the window's Claude Code usage across MCP servers."""
    root = root or default_root()
    now = time.time() if now is None else now
    since = now - window_hours * 3600.0
    report = Report(window_hours=window_hours)
    sessions: set[str] = set()

    if not root.is_dir():
        return report
    for path in sorted(root.glob("*/*.jsonl")):
        # A transcript last written before the window opened cannot hold a
        # request inside it, and skipping those is what keeps this cheap on a
        # transcript directory that grows without bound.
        try:
            if path.stat().st_mtime < since:
                continue
        except OSError:
            continue
        for req in walk_session(path, since):
            report.requests += 1
            report.cost_usd += req.cost_usd
            sessions.add(f"{req.project}/{req.session}")
            report.by_model[req.model or "(unknown)"] = (
                report.by_model.get(req.model or "(unknown)", 0.0) + req.cost_usd)
            report.projects[req.project] = (
                report.projects.get(req.project, 0.0) + req.cost_usd)
            if req.context > BIG_CONTEXT:
                report.big_context_usd += req.cost_usd
            share = req.mcp_share
            if share:
                total = sum(req.mcp_tokens.values())
                for server, tokens in req.mcp_tokens.items():
                    # Split the capped share between servers in proportion to
                    # what each is carrying, so two servers never sum to more
                    # than the request.
                    part = req.cost_usd * share * (tokens / total)
                    report.by_server[server] = (
                        report.by_server.get(server, 0.0) + part)
    report.sessions = len(sessions)
    return report


# --------------------------------------------------------------------------
# One session's context size, for a launched role watching its own window
# --------------------------------------------------------------------------

# path -> (inode, mtime_ns, size, offset, check, reading). `offset` is where the
# last complete line ended; `check` is the bytes just before it, so a file
# rewritten in place to something longer is not mistaken for one that grew.
_readings: dict[str, tuple[int, int, int, int, bytes, int | None]] = {}
_CHUNK = 1 << 16
_CHECK = 64


def _usage_of(line: bytes) -> int | None:
    if b'"usage"' not in line:
        return None                          # cheap reject before parsing
    try:
        record = json.loads(line)
    except ValueError:
        return None
    message = record.get("message") if isinstance(record, dict) else None
    usage = message.get("usage") if isinstance(message, dict) else None
    if not isinstance(usage, dict) or not usage:
        return None
    try:
        return int(context_tokens(usage))
    except (TypeError, ValueError, OverflowError):
        # `int(float("inf"))` raises OverflowError, not caught above; `Infinity`
        # is valid to Python's json, and a raising read here is never cached,
        # so every later tool call would re-read and raise again.
        return None


def _last_reading(handle, start: int, end: int) -> int | None:
    """The last line in [start, end) that carried usage, read from the end.

    Reads backward in fixed-size chunks. A block that carries no boundary is
    just appended to a list of pieces of the still-open line — not
    concatenated onto a growing `carry` on every chunk it spans, which is what
    made one huge line (a big tool result) cost time quadratic in its length.
    The pieces are only ever joined once, when the line's start is finally
    found, so the whole scan stays linear in the bytes read.
    """
    pos = end
    open_line: list[bytes] = []   # pieces of the not-yet-bounded line
    while pos > start:
        step = min(_CHUNK, pos - start)
        pos -= step
        handle.seek(pos)
        block = handle.read(step)
        parts = block.split(b"\n")
        open_line.append(parts[-1])
        if len(parts) == 1:
            continue                          # no boundary in this block yet
        found = _usage_of(b"".join(reversed(open_line)))
        if found is not None:
            return found
        for piece in reversed(parts[1:-1]):
            found = _usage_of(piece)
            if found is not None:
                return found
        open_line = [parts[0]]                # continues into the next block
    if open_line:
        found = _usage_of(b"".join(reversed(open_line)))
        if found is not None:
            return found
    return None


def session_transcript(provider: Any, cwd: Path, session_id: str,
                       executor: Any = None) -> Path | None:
    """The file the provider declares for this session in `cwd`, or None.

    None when there is no session id or the provider declares no transcript;
    the file itself may not exist yet. Given the executor the session ran
    under, the path is where the host finds it (SP-R2); without one, the
    executor of the project `cwd` belongs to. Of the slugs `cwd` may have
    been recorded under, the first that holds the file, else the first.
    """
    from .watchdog import transcript_sources

    if not session_id:
        return None
    found = None
    for directory, pattern in transcript_sources(provider, Path(cwd), executor):
        name = pattern.replace("*", session_id, 1) if "*" in pattern else session_id
        path = directory / name
        if path.is_file():
            return path
        found = found or path
    return found


def session_context(provider: Any, cwd: Path, session_id: str,
                    executor: Any = None) -> int | None:
    """Tokens the most recent request of this session carried, or None.

    Reads the transcript the provider declares for `cwd`, the file named by
    `session_id` and no other: a directory is shared by every role launched
    from it, and the newest file there may be somebody else's session. None —
    never 0 — when there is no reading at all, so that "unknown" cannot pass
    for "plenty of room". A compaction needs no handling of its own: the
    request after it already reports the smaller context.

    Read where the executor keeps it, as `session_transcript` finds it
    (SP-R2): a docker agent's transcript is not in the host's own profile.

    Cheap to call on every tool call: an unchanged file is not opened, and one
    that grew has only its new tail read.
    """
    path = session_transcript(provider, cwd, session_id, executor)
    if path is None:
        return None
    key = str(path)
    try:
        st = path.stat()
    except OSError:
        _readings.pop(key, None)
        return None
    cached = _readings.get(key)
    if cached and cached[:3] == (st.st_ino, st.st_mtime_ns, st.st_size):
        return cached[5]
    try:
        with path.open("rb") as handle:
            size = st.st_size
            start, previous = 0, None
            if cached and cached[0] == st.st_ino and cached[3] <= size:
                offset, check = cached[3], cached[4]
                handle.seek(offset - len(check))
                if handle.read(len(check)) == check:
                    start, previous = offset, cached[5]
            # Only complete lines: the last one may be mid-write.
            end = size
            if end > start:
                handle.seek(max(start, end - 1))
                if handle.read(1) != b"\n":
                    end = _line_start(handle, start, end)
            found = _last_reading(handle, start, end) if end > start else None
            reading = found if found is not None else previous
            handle.seek(max(0, end - _CHECK))
            check = handle.read(end - max(0, end - _CHECK))
    except OSError:
        _readings.pop(key, None)
        return None
    _readings[key] = (st.st_ino, st.st_mtime_ns, st.st_size, end, check, reading)
    return reading


def _line_start(handle, start: int, end: int) -> int:
    """Where the unterminated last line in [start, end) begins."""
    pos = end
    while pos > start:
        step = min(_CHUNK, pos - start)
        pos -= step
        handle.seek(pos)
        at = handle.read(step).rfind(b"\n")
        if at >= 0:
            return pos + at + 1
    return start
