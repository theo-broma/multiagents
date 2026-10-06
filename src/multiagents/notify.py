"""Push notifications through a self-hosted ntfy server (NT).

Part 1 only: config validation (NT-R1), the one-shot sender behind the
root-only `notify` tool (NT-R2) with its content discipline (NT-R7), and the
CLI's share of NT-R8 (`notify test`, and the `pending` line of
`notify status`). The scheduler sender (NT-R3..R6, the outbox, retries, rate
limiting) is out of scope here.

In-house code on purpose: the publish is plain stdlib HTTP
(`http.client`), with no third-party package and no new dependency. A send is
"accepted" when the ntfy server answers 2xx; delivery to the phone is ntfy's
business, so every message and output says "accepted", never "delivered".

Placeholder addresses only in tracked files: examples here use
`https://<host>.<tailnet>.ts.net:8443` or `example.invalid`. A real address
lives only in the user's own, untracked `.multiagents/config/project.yaml`.
"""

from __future__ import annotations

import base64
import http.client
import os
import re
import socket
import stat
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any

# NT-R1: the events the scheduler may publish (NT-R3); the tool itself is not
# limited to them, but the config's `events:` list is.
NOTIFY_EVENTS = ("question", "held", "anomaly", "done")
DEFAULT_EVENTS = ["question", "held", "anomaly", "done"]
DEFAULT_MIN_INTERVAL_SECONDS = 30

# NT-R2: the tool returns within 10 s overall — connection, response, cleanup.
SEND_BOUND_SECONDS = 10.0

# NT-R7: a tool-sent body is free text truncated to 1000 characters (not bytes).
MAX_BODY_CHARS = 1000

PRIORITIES = ("min", "low", "default", "high", "urgent")

# NT-R2/NT-R7: failure reasons are fixed strings. Exception text never reaches
# a reason, because an exception raised while the token is in play (notably
# http.client's "Invalid header value", which quotes the offending header)
# may carry the token with it. The token file's path is safe to name: it is
# the operator's own pointer, never the secret.
_TIMEOUT_REASON = "timeout: the ntfy server never answered within 10 s"
_NETWORK_REASON = "network error: the ntfy server could not be reached"

_TOPIC_RE = re.compile(r"[A-Za-z0-9_-]+\Z")
_CRLF_RE = re.compile(r"[\r\n]+")


class NotifyConfigError(ValueError):
    """A `notify:` setting with its source file and line."""


# --------------------------------------------------------------------------
# NT-R1: validation
# --------------------------------------------------------------------------

def _at(path: Path, parts: list[str]) -> tuple[int, Any] | None:
    from .notices import _node_at

    return _node_at(path, parts)


def _refuse(path: Path, key: str, detail: str, line: int | None) -> NotifyConfigError:
    # The offending VALUE is never interpolated: an inline token is refused
    # here, and its secret must not land in the message, a log or the CLI.
    where = f"{path}:{line or 1}"
    return NotifyConfigError(f"notify.{key}: {detail} ({where})")


def _check_url(value: Any) -> str | None:
    """None when `value` is a usable base URL, else the refusal detail."""
    if not isinstance(value, str) or not value.strip():
        return "expected an http or https URL with a host, e.g. https://<host>.<tailnet>.ts.net:8443"
    try:
        parts = urllib.parse.urlsplit(value.strip())
    except ValueError:
        return "expected an http or https URL with a host, e.g. https://<host>.<tailnet>.ts.net:8443"
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return "expected an http or https URL with a host, e.g. https://<host>.<tailnet>.ts.net:8443"
    return None


def _check_topic(value: Any) -> str | None:
    if not isinstance(value, str):
        return "expected 1-64 characters from [A-Za-z0-9_-]"
    if len(value) > 64 or not _TOPIC_RE.fullmatch(value):
        return "expected 1-64 characters from [A-Za-z0-9_-]"
    return None


def _check_events(value: Any) -> str | None:
    if not isinstance(value, list):
        return f"expected a list of known events: {sorted(NOTIFY_EVENTS)}"
    for entry in value:
        if not isinstance(entry, str) or entry not in NOTIFY_EVENTS:
            return f"expected a list of known events: {sorted(NOTIFY_EVENTS)}"
    return None


def _check_interval(value: Any) -> str | None:
    import math

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "expected a positive number of seconds"
    if not math.isfinite(value) or value <= 0:
        return "expected a positive number of seconds"
    return None


def validate_layer_block(block: Any, path: Path) -> None:
    """Refuse one layer's `notify:` mapping at its source (NT-R1).

    Only the keys the layer sets are checked, so a project layer holding just
    a `topic:` still merges with a global layer holding the `ntfy_url:`. The
    required keys are checked on the merged section by `finalize_notify`.
    """
    if not isinstance(block, dict):
        at = _at(path, ["notify"])
        raise _refuse(path, "notify", "expected a mapping", at[0] if at else None)
    base = _at(path, ["notify"])
    base_line = base[0] if base else None
    if "token" in block:
        at = _at(path, ["notify", "token"])
        raise _refuse(path, "token",
                      "an inline token is not allowed; use token_file",
                      at[0] if at else base_line)
    checks = (("ntfy_url", _check_url), ("topic", _check_topic),
              ("events", _check_events), ("min_interval_seconds", _check_interval))
    for key, check in checks:
        if key in block:
            detail = check(block[key])
            if detail is not None:
                at = _at(path, ["notify", key])
                raise _refuse(path, key, detail, at[0] if at else base_line)
    # `token_file` is never a load error (a missing file must leave `notify
    # status` and a fix-and-retry working); it refuses *sending* instead.


def finalize_notify(project: dict, layer_files: list[Path]) -> dict | None:
    """Check the merged `notify:` section and fill its defaults (NT-R1).

    Returns the normalised block, or None when the feature is off. Raises
    NotifyConfigError naming the key and the `project.yaml:<line>` holding
    it, the way invalid scheduler settings are refused.
    """
    block = project.get("notify")
    if block is None:
        return None
    ordered = list(reversed(layer_files))

    def line_of(*parts: str) -> tuple[Path, int]:
        for candidate in ordered:
            at = _at(candidate, list(parts))
            if at is not None:
                return candidate, at[0]
        return ordered[-1] if ordered else Path("project.yaml"), 1

    if not isinstance(block, dict):
        path, line = line_of("notify")
        raise _refuse(path, "notify", "expected a mapping", line)
    if "token" in block:
        path, line = line_of("notify", "token")
        raise _refuse(path, "token",
                      "an inline token is not allowed; use token_file", line)
    for key, check in (("ntfy_url", _check_url), ("topic", _check_topic),
                       ("events", _check_events),
                       ("min_interval_seconds", _check_interval)):
        if key not in ("ntfy_url", "topic") and key not in block:
            continue
        if key in ("ntfy_url", "topic") and key not in block:
            path, line = line_of("notify")
            raise _refuse(path, key, "missing required key", line)
        detail = check(block[key])
        if detail is not None:
            path, line = line_of("notify", key)
            raise _refuse(path, key, detail, line)
    normalised = dict(block)
    normalised.setdefault("events", list(DEFAULT_EVENTS))
    normalised.setdefault("min_interval_seconds", DEFAULT_MIN_INTERVAL_SECONDS)
    project["notify"] = normalised
    return normalised


# --------------------------------------------------------------------------
# Token file (NT-R1 send-time refusals)
# --------------------------------------------------------------------------

def read_token(token_file: Any) -> tuple[str | None, str | None]:
    """The bearer token, or (None, reason) refusing the send (NT-R1).

    The reason never carries the token itself.
    """
    if token_file is None or token_file == "":
        return None, None
    if not isinstance(token_file, str):
        return None, "token file is not a path"
    raw = os.path.expanduser(token_file)
    path = Path(raw)
    try:
        info = path.stat()
    except OSError:
        return None, f"token file is missing or unreadable: {raw}"
    if not stat.S_ISREG(info.st_mode):
        return None, f"token file is not a regular file: {raw}"
    if info.st_mode & 0o077:
        return None, (f"token file is readable by group or others "
                      f"(it must be 0600 or stricter): {raw}")
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, ValueError, UnicodeDecodeError):
        return None, f"token file cannot be read: {raw}"
    token = content.strip()
    if not token:
        return None, f"token file is empty: {raw}"
    try:
        from .redact import register_literal

        register_literal(token)
    except Exception:
        pass
    if "\r" in token or "\n" in token:
        # Refused before any header is built: http.client would raise
        # ValueError("Invalid header value ...") quoting the Bearer header,
        # and that text must never become a reason. The reason names the
        # file, never the token.
        return None, (f"token file holds more than one line "
                      f"(a token cannot contain CR or LF): {raw}")
    return token, None


# --------------------------------------------------------------------------
# NT-R2 / NT-R7: the one-shot sender
# --------------------------------------------------------------------------

def _clean_title(title: str) -> str:
    """CR and LF never reach a header: they become spaces (NT-R2)."""
    return _CRLF_RE.sub(" ", title)


def encode_title(title: str) -> str:
    """A header-safe Title: plain ASCII stays plain, anything else is one
    RFC 2047 encoded word, so it arrives intact (NT-R2)."""
    clean = _clean_title(title)
    if clean.isascii():
        return clean
    encoded = base64.b64encode(clean.encode("utf-8")).decode("ascii")
    return f"=?utf-8?b?{encoded}?="


def _exchange(conn: http.client.HTTPConnection, target: str, body: bytes,
              headers: dict, box: dict) -> None:
    """The HTTP round trip, on a daemon worker: never raises (NT-R2).

    Reports `{"status": code}` or `{"error": "timeout" | "network"}` into
    `box`. Only the outcome kind crosses back to the calling thread — never
    exception text, which may quote the Authorization header and with it
    the token.
    """
    try:
        conn.request("POST", target, body=body, headers=headers)
        box["status"] = conn.getresponse().status
    except (socket.timeout, TimeoutError):
        box["error"] = "timeout"
    except Exception:
        box["error"] = "network"


def _abandon(conn: http.client.HTTPConnection) -> None:
    """Unblock a worker stuck in the exchange, then release its socket.

    Shutting the socket down wakes a thread blocked in `recv` (closing the
    file descriptor from another thread would not reliably do so); closing
    afterwards releases it. Either way the caller has already moved on.
    """
    try:
        sock = getattr(conn, "sock", None)
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _check_args(title: Any, message: Any, priority: Any,
                tags: Any) -> str | None:
    if not isinstance(title, str) or not isinstance(message, str):
        return "invalid title/message: both must be strings"
    if not isinstance(priority, str) or priority not in PRIORITIES:
        return (f"invalid priority: expected one of "
                f"{', '.join(PRIORITIES)}")
    if tags is None:
        return None
    if not isinstance(tags, list):
        return "invalid tags: expected a list of names from [A-Za-z0-9_-]"
    for tag in tags:
        if (not isinstance(tag, str) or len(tag) > 64
                or not _TOPIC_RE.fullmatch(tag)):
            return "invalid tags: expected a list of names from [A-Za-z0-9_-]"
    return None


def publish(base_url: str, topic: str, title: str, message: str,
            priority: str = "default", tags: list[str] | None = None,
            token: str | None = None,
            bound: float = SEND_BOUND_SECONDS) -> dict:
    """Publish one message; never raises, never follows redirects (NT-R2).

    Returns `{"ok": True, ...}` when the server accepted the message, else
    `{"ok": False, "reason": ...}` with a distinct reason per failure kind.
    The whole attempt — connection, response and cleanup — fits in `bound`.
    """
    started = time.monotonic()
    deadline = started + bound

    def remaining() -> float:
        return deadline - time.monotonic()

    try:
        parts = urllib.parse.urlsplit(base_url.strip())
    except ValueError:
        return {"ok": False,
                "reason": "invalid ntfy_url: expected an http or https URL with a host"}
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return {"ok": False,
                "reason": "invalid ntfy_url: expected an http or https URL with a host"}
    base_path = (parts.path or "").rstrip("/")
    target = f"{base_path}/{topic}"
    headers = {
        "Title": encode_title(title),
        "Priority": priority,
        "Content-Type": "text/plain; charset=utf-8",
    }
    if tags:
        headers["Tags"] = ", ".join(tags)
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    # NT-R7: free text, truncated to 1000 characters (not bytes).
    body = message[:MAX_BODY_CHARS].encode("utf-8")
    conn: http.client.HTTPConnection | None = None
    try:
        host = parts.hostname
        port = parts.port or (443 if parts.scheme == "https" else 80)
        wait = remaining()
        if wait <= 0:
            return {"ok": False, "reason": _TIMEOUT_REASON}
        conn_cls = (http.client.HTTPSConnection if parts.scheme == "https"
                    else http.client.HTTPConnection)
        # The connection object itself does no I/O, so building it here is
        # safe; everything that can block — connect, send, response headers
        # and body — runs on the worker below.
        conn = conn_cls(host, port, timeout=max(0.1, wait))
        box: dict = {}
        worker = threading.Thread(target=_exchange,
                                  args=(conn, target, body, headers, box),
                                  daemon=True)
        worker.start()
        # The one true deadline (NT-R2): whatever the server does — refuse
        # the connection, answer at once, or trickle one byte per second —
        # the join ends the call at `bound`.
        worker.join(max(0.0, remaining()))
        if worker.is_alive():
            _abandon(conn)
            conn = None
            return {"ok": False, "reason": _TIMEOUT_REASON}
        if "status" in box:
            status = box["status"]
        elif box.get("error") == "timeout":
            return {"ok": False, "reason": _TIMEOUT_REASON}
        else:
            return {"ok": False, "reason": _NETWORK_REASON}
    finally:
        try:
            if conn is not None:
                conn.close()
        except Exception:
            pass
    if 300 <= status < 400:
        return {"ok": False,
                "reason": (f"redirect (http {status}): redirects are never "
                           f"followed, so the message was not sent")}
    if 200 <= status < 300:
        return {"ok": True, "note": "accepted by the ntfy server"}
    return {"ok": False,
            "reason": f"ntfy server refused the message: http {status}"}


def send(notify_cfg: Any, title: Any, message: Any,
         priority: Any = "default", tags: Any = None) -> dict:
    """The `notify` tool's core: validate, read the token, publish (NT-R2).

    `notify_cfg` is the merged `notify:` section (None when off). Never
    raises; every refusal carries a non-empty, kind-distinct `reason`, and
    the token appears in no result. The tool itself is not rate limited and
    repeats are sent again (NT-R2).
    """
    if not isinstance(notify_cfg, dict) or not notify_cfg.get("ntfy_url") \
            or not notify_cfg.get("topic"):
        return {"ok": False,
                "reason": "notify is not configured: no notify: section in project.yaml"}
    problem = _check_args(title, message, priority, tags)
    if problem is not None:
        return {"ok": False, "reason": problem}
    token, token_error = read_token(notify_cfg.get("token_file"))
    if token_error is not None:
        return {"ok": False, "reason": token_error}
    return publish(str(notify_cfg["ntfy_url"]), str(notify_cfg["topic"]),
                   title, message, priority, list(tags or []), token)
