"""The credential never enters the sandbox.

Every other arrangement in this project starts from "an agent can read the
credential, so how bad is that" — a private profile, then a vault holding the
refresh token back, then an eight-hour projection. Each shrank the blast
radius. None of them closed it, because a file an agent can read is a file an
agent can copy out, and agents here run with approvals off.

This closes it. Agents are given a token that authenticates nothing: a signed
name-tag naming which agent is calling. They send it to this proxy, which is
the only thing they can reach that speaks to the model API at all, and it
replaces that header with a real one it reads from a vault the container has
no mount for. Steal the file out of a container now and you have stolen a
string that works on exactly one host, against exactly one process, which will
tell you which agent you are pretending to be and nothing else.

What this does NOT solve, and the reason it is not the end of the story: an
agent that cannot take the credential can still *spend* it. Every request it
sends is paid for by a real account. The proxy is where that becomes
measurable — it is the only place every agent's traffic passes through — but
measuring is not preventing, and nothing here refuses an expensive request.

## Accounts

A provider may have several. They are separate logins, each a full profile
under ``accounts/``, refreshed independently.

Switching between them is a LAST RESORT, not load balancing, and the reason is
prompt caching: a cache is per-account, one agent in this system has been
measured reading thirteen million cached tokens, and moving that agent to a
fresh account throws all of it away. The saving from a rested account is
nowhere near the cost of re-sending its context. So an agent is pinned to one
account for its entire life, and only a limit that account cannot serve moves
anything — and then only for agents that have not started yet.
"""

from __future__ import annotations

import base64
import email.utils
import hashlib
import hmac
import http.client
import json
import math
import os
import re
import secrets
import socketserver
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

from .auth_upstream import (Accounts, BODY_IDLE_TIMEOUT, HOP_HEADERS, MAX_BODY_SIZE,
                            UPSTREAM, request_headers, routing_identity,
                            routing_pool, upstream_url as _upstream_url, validate_label)
PORT = 8930
TOKEN_PREFIX = "mxa"
CLAIM_PREFIX = "mxa2"

# Long, because time-to-first-token on a large request is not fast and a read
# timeout shorter than the upstream's worst case kills healthy requests. The
# CLI has its own timeout (it sends X-Stainless-Timeout: 600) and should be the
# one to give up.
UPSTREAM_TIMEOUT = 900

# --------------------------------------------------------------------------
# The name-tag agents carry
# --------------------------------------------------------------------------

def mint_token(agent_id: str, secret: str, provider: str = "") -> str:
    """A token that authenticates NOTHING, and names who is calling.

    This is what goes into the container in place of a credential. It is not a
    secret in any useful sense — anything inside can read it, and so could
    anything that gets a copy — which is exactly the property being bought:
    there is nothing there worth stealing.

    Signed anyway, for one narrow reason. Without a signature the proxy would
    serve any string that arrived, so a container could name itself another
    agent to inherit that agent's account pinning, or invent agents endlessly
    and walk the account pool. The signature costs nothing and removes both.
    """
    if provider:
        claim = base64.urlsafe_b64encode(agent_id.encode()).decode().rstrip("=")
        value = "v2." + claim + "." + urllib.parse.quote(provider, safe="")
        signed = CLAIM_PREFIX + "_" + value
        mac = hmac.new(secret.encode(), signed.encode(), hashlib.sha256)
        return f"{signed}_{mac.hexdigest()[:32]}"
    mac = hmac.new(secret.encode(), agent_id.encode(), hashlib.sha256)
    return f"{TOKEN_PREFIX}_{agent_id}_{mac.hexdigest()[:32]}"


def read_token(value: str, secret: str) -> str:
    """The agent named by a token, or "" if it is not one of ours."""
    return read_claim(value, secret)[0]


def read_claim(value: str, secret: str) -> tuple[str, str]:
    """Verify the whole routing claim before interpreting its provider.

    This enforces routing identity, not isolation between agents sharing a
    container: any of them can read another's signed name-tag.
    """
    if not value:
        return "", ""
    value = value.split(" ")[-1].strip()          # tolerate "Bearer <token>"
    versioned = value.startswith(CLAIM_PREFIX + "_")
    prefix = CLAIM_PREFIX if versioned else TOKEN_PREFIX
    if not value.startswith(prefix + "_"):
        return "", ""
    # From the RIGHT. The name is a project slug as often as an agent id, and a
    # slug is a directory name with the separators replaced — it contains
    # underscores. Splitting on all of them read the name as its first
    # fragment, the signature never matched, and every request from a project
    # whose directory had an underscore in it was refused as forged.
    agent_id, _, signature = value[len(prefix) + 1:].rpartition("_")
    if not agent_id or not signature:
        return "", ""
    signed = prefix + "_" + agent_id if versioned else agent_id
    expected = hmac.new(secret.encode(), signed.encode(), hashlib.sha256).hexdigest()[:32]
    # Constant time: the comparison is against a value the caller controls.
    if not hmac.compare_digest(signature, expected):
        return "", ""
    if versioned:
        if not agent_id.startswith("v2."):
            return "", ""
        try:
            encoded, separator, provider = agent_id[3:].partition(".")
            agent = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode()
            if agent and separator and provider:
                return agent, urllib.parse.unquote(provider)
        except (ValueError, UnicodeError):
            pass
        return "", ""
    return agent_id, ""


def load_secret(vault: Path) -> str:
    """The signing secret, created on first use. Host-side only."""
    path = vault / "proxy-secret"
    try:
        value = path.read_text().strip()
        if value:
            return value
    except OSError:
        pass
    value = secrets.token_hex(32)
    vault.mkdir(parents=True, exist_ok=True)
    path.write_text(value)
    os.chmod(path, 0o600)
    return value


# --------------------------------------------------------------------------
# The accounts, and which agent is on which
# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# The proxy itself
# --------------------------------------------------------------------------

def upstream_url(path: str) -> str:
    return _upstream_url(path, UPSTREAM)


def _retry_after(headers: Any, body: bytes) -> float:
    """Interpret hints against wall time; cooldowns use monotonic time."""
    del body
    now = time.time()
    for name in ("retry-after", "anthropic-ratelimit-unified-reset"):
        raw = headers.get(name) if headers else None
        if raw is None:
            continue
        try:
            value = float(raw)
            if name != "retry-after":
                value -= now
        except (ValueError, TypeError):
            try:
                stamp = (email.utils.parsedate_to_datetime(raw) if name == "retry-after"
                         else datetime.fromisoformat(raw))
                if stamp.tzinfo is None:
                    stamp = stamp.replace(tzinfo=timezone.utc)
                value = stamp.timestamp() - now
            except (ValueError, TypeError, OverflowError):
                continue
        if math.isfinite(value) and value >= 0:
            return value
    return 3600.0


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    accounts: Accounts
    secret: str
    on_event: Any = None

    def log_message(self, *args) -> None:        # noqa: D102 - quiet by default
        pass

    def _fail(self, status: int, message: str, headers: Any = None) -> None:
        payload = json.dumps({"type": "error",
                              "error": {"type": "multiagents", "message": message}}
                             ).encode()
        self._error_payload(status, payload, headers=headers)

    def _error_payload(self, status: int, payload: bytes, *,
                       content_type: str = "application/json; charset=utf-8",
                       headers: Any = None) -> None:
        self.send_response(status)
        bodyless = status in (204, 304)
        if not bodyless:
            self.send_header("content-type", content_type)
            self.send_header("content-length", str(len(payload)))
        for key, value in headers or []:
            self.send_header(key, value)
        if self.close_connection:
            self.send_header("connection", "close")
        self.end_headers()
        if self.command != "HEAD" and not bodyless:
            try:
                self.wfile.write(payload)
            except OSError:                    # the agent disconnected
                self.close_connection = True

    def _event(self, kind: str, **fields) -> None:
        if callable(type(self).on_event):
            try:
                type(self).on_event(kind, fields)
            except BaseException as exc:
                try:
                    from .redact import scrub
                    message = scrub(str(exc)).replace("\n", "\\n").replace("\r", "\\r")
                    print(f"authproxy event {kind}: {type(exc).__name__}: {message}",
                          file=sys.stderr, flush=True)
                except BaseException:          # reporting must not stop serving
                    pass

    def do_unsupported(self) -> None:
        self.close_connection = True
        self._fail(405, "method not allowed", [("allow", "GET, HEAD, POST")])

    def __getattr__(self, name: str):
        # BaseHTTPRequestHandler otherwise generates 501 for unknown methods.
        if name.startswith("do_"):
            return self.do_unsupported
        raise AttributeError(name)

    def do_POST(self) -> None:                   # noqa: N802 - http.server API
        if "transfer-encoding" in self.headers:
            self.close_connection = True
            return self._fail(411, "Content-Length is required")
        agent, provider = read_claim(self.headers.get("authorization", ""), type(self).secret)
        if not agent:
            # Not one of ours. Nothing reachable here should be sending
            # anything else, so this is either a misconfigured agent or
            # something that should not be on this network at all.
            self.close_connection = True
            self._event("rejected", reason="unsigned token")
            return self._fail(401, "this proxy serves multiagents agents only")

        self.agent = agent
        lengths = self.headers.get_all("content-length", [])
        try:
            values = [value.strip(" \t") for length in lengths for value in length.split(",")]
            if any(not re.fullmatch(r"[0-9]+", value) for value in values):
                raise ValueError("invalid length")
            sizes = {value.lstrip("0") or "0" for value in values}
            if len(sizes) > 1:
                raise ValueError("conflicting lengths")
            length = sizes.pop() if sizes else "0"
        except ValueError:
            self.close_connection = True
            return self._fail(400, "invalid Content-Length")
        # Compare decimal strings before conversion, including lengths too
        # long for Python's integer parser. Every valid oversized length is 413.
        bound = str(MAX_BODY_SIZE)
        if len(length) > len(bound) or (len(length) == len(bound) and length > bound):
            self.close_connection = True
            return self._fail(413, "request body exceeds 64 MiB")
        size = int(length)
        # Before any account is chosen: a target that moves the host would
        # carry the token there. The target itself is not repeated anywhere —
        # it is the caller's text, and the event names who sent it.
        url = upstream_url(self.path)
        if not url:
            self.close_connection = True
            self._event("rejected", agent=agent, reason="request target leaves the upstream")
            return self._fail(400, "request target must be an origin-form path")
        try:
            body = self._read_body(size) if lengths else None
        except TimeoutError:
            self.close_connection = True
            return self._fail(408, "request body timed out")
        except (EOFError, OSError):
            self.close_connection = True
            return self._fail(400, "incomplete request body")
        accounts = type(self).accounts
        try:
            eligible, fixed = routing_pool(accounts, provider)
        except (OSError, ValueError):
            return self._fail(503, "account pin configuration is unavailable")
        identity = routing_identity(agent, provider)
        tried: list[str] = []
        unauthenticated = False
        while True:
            # Freeze admission's pool and pin for this request. Concurrent
            # logins cannot grow a retry loop, nor can a reload move a pinned
            # request onto a different account halfway through its attempts.
            label, retry, rejected = accounts.admit(identity, provider, tried,
                                                    eligible=eligible, fixed=fixed)
            if not label or label in tried:
                self._event("exhausted", agent=agent, tried=tried)
                denied = (unauthenticated or rejected) and retry is None
                if not fixed and unauthenticated and retry is None:
                    return self._error_payload(401, **self._last_auth_error)
                status = 401 if denied or (fixed and not accounts.token(fixed)) else 429
                return self._fail(
                    status,
                    f"account {fixed} is unusable" if fixed else
                    "no usable account remains" if denied else
                    "every account for this provider is rate limited; "
                    "the run will be retried when one resets",
                    [("retry-after", str(retry))] if status == 429 and retry is not None else None)
            tried.append(label)
            token = type(self).accounts.token(label)
            if not token:
                type(self).accounts.mark_limited(label, 300)
                continue
            outcome = self._forward(url, label, token, body)
            if outcome not in ("limited", "unauthenticated"):
                return
            unauthenticated |= outcome == "unauthenticated"
            # A limit found BEFORE any of the response was written is the only
            # kind that can be retried elsewhere. `_forward` says so by
            # returning here rather than having sent anything.
            self._event("switch", agent=agent, away_from=label)

    do_GET = do_HEAD = do_POST

    def _read_body(self, size: int) -> bytes:
        timeout = self.connection.gettimeout()
        self.connection.settimeout(BODY_IDLE_TIMEOUT)
        try:
            # Only buffer bytes already received. Spill larger bodies to disk
            # so building the final bytes object needs no second body buffer.
            with tempfile.SpooledTemporaryFile(max_size=1024 * 1024) as body:
                remaining = size
                while remaining:
                    # read1 performs at most one socket read: every arrival
                    # resets the idle bound, even on a slow trickle.
                    chunk = self.rfile.read1(min(8192, remaining))
                    if not chunk:
                        raise EOFError("incomplete request body")
                    body.write(chunk)
                    remaining -= len(chunk)
                body.seek(0)
                return body.read()
        finally:
            self.connection.settimeout(timeout)

    def _forward(self, url: str, label: str, token: str, body: bytes | None) -> str:
        headers = request_headers(self.headers)
        headers["Authorization"] = f"Bearer {token}"
        try:
            request = urllib.request.Request(url, data=body,
                                             headers=headers, method=self.command)
            upstream = urllib.request.build_opener(_NoRedirect()).open(
                request, timeout=UPSTREAM_TIMEOUT)
        except urllib.error.HTTPError as exc:
            upstream = exc
        except Exception as exc:                # no upstream status was received
            self._event("upstream_unreachable", agent=self.agent, label=label,
                        error=type(exc).__name__)
            self.close_connection = True
            self._fail(502, f"upstream unreachable: {type(exc).__name__}")
            return "error"

        with upstream:
            if 100 <= upstream.status < 200 and upstream.status != 100:
                self._event("upstream_error", agent=self.agent, label=label,
                            status=upstream.status)
                self.close_connection = True
                self._fail(502, "unexpected upstream interim response")
                return "error"
            if not 200 <= upstream.status < 300:
                status = upstream.status
                if status >= 400 and status not in (401, 429, 529):
                    self._event("upstream_error", agent=self.agent, label=label, status=status)
                try:
                    payload = upstream.read()
                except Exception as exc:
                    self.close_connection = True
                    self._fail(502, f"upstream response unreadable: {type(exc).__name__}")
                    return "error"
                if status == 401:
                    type(self).accounts.mark_unauthenticated(label, token)
                    self._event("unauthenticated", label=label)
                    self._last_auth_error = _error_response(payload, upstream.headers)
                    return "unauthenticated"
                if status in (429, 529):
                    type(self).accounts.mark_limited(
                        label, _retry_after(upstream.headers, payload))
                    return "limited"
                self._error_payload(status, **_error_response(payload, upstream.headers))
                return "error"

            # After headers reach the agent, a relay failure only closes this
            # response. Separate reads and writes distinguish upstream failures
            # from an agent that has disconnected.
            try:
                self.send_response(upstream.status)
                for key, value in _response_headers(upstream.headers, success=True):
                    self.send_header(key, value)
                if upstream.status == 204:
                    pass                       # no body or framing on a 204
                elif self.command == "HEAD":
                    length = upstream.headers.get("content-length")
                    if length is not None and re.fullmatch(r"[0-9]+", length):
                        self.send_header("content-length", str(int(length)))
                else:
                    self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
            except OSError:
                self.close_connection = True
                return "error"
            if self.command == "HEAD" or upstream.status == 204:
                return "ok"
            while True:
                try:
                    chunk = upstream.read(8192)
                    if not chunk and getattr(upstream, "length", None):
                        raise http.client.IncompleteRead(b"")
                except Exception as exc:
                    self._event("upstream_unreachable", agent=self.agent, label=label,
                                error=type(exc).__name__)
                    self.close_connection = True
                    return "error"
                try:
                    if not chunk:
                        self.wfile.write(b"0\r\n\r\n")
                        break
                    self.wfile.write(b"%x\r\n" % len(chunk) + chunk + b"\r\n")
                    self.wfile.flush()
                except OSError:
                    self.close_connection = True
                    return "error"
        return "ok"


def _error_response(payload: bytes, headers: Any) -> dict[str, Any]:
    """Scrub a non-2xx body and keep only the upstream's public diagnostics."""
    clean = _scrub(payload)
    upstream_type = _unfold_header(headers.get("content-type", "text/plain; charset=utf-8"))
    if _header_sensitive(upstream_type):
        content_type = "application/octet-stream"
    elif clean == b"Upstream response withheld.":
        content_type = "text/plain; charset=utf-8"
    else:
        try:
            json.loads(clean)
            content_type = "application/json; charset=utf-8"
        except (ValueError, UnicodeError):
            content_type = upstream_type
    return {"payload": clean, "content_type": content_type,
            "headers": _response_headers(headers, success=False)}


def _unfold_header(value: str) -> str:
    return re.sub(r"[ \t]*\r?\n[ \t]+", " ", value)


def _header_sensitive(value: str, *, name: bool = False) -> bool:
    from .redact import _literal_snapshot

    readings = [value]
    try:
        # http.client decodes the wire as Latin-1. Keep that reading too;
        # invalid UTF-8 bytes must not hide a valid literal beside them.
        readings.append(value.encode("latin-1").decode("utf-8", errors="surrogateescape"))
    except UnicodeEncodeError:
        pass
    literals = _literal_snapshot()
    if name:
        # Header names ignore ASCII case; values retain their literal case.
        lower = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")
        readings = [reading.translate(lower) for reading in readings]
        literals = [literal.translate(lower) for literal in literals]
    return any(any(literal in reading for literal in literals) or _EMAIL.search(reading)
               for reading in readings)


def _response_headers(headers: Any, *, success: bool) -> list[tuple[str, str]]:
    """Apply the same privacy and hop rules before either relay path writes."""
    hop = {"connection", "keep-alive", "upgrade", "te", "trailer",
           "transfer-encoding", "content-length"}
    unfolded = [(_unfold_header(key), _unfold_header(value)) for key, value in headers.items()]
    for key, value in unfolded:
        if key.lower() == "connection":
            hop.update(name.strip().lower() for name in value.split(","))
    allowed = []
    for key, value in unfolded:
        if _header_sensitive(key, name=True):
            continue
        name = key.lower()
        if (name in hop or name in {"set-cookie", "authorization", "www-authenticate", "location"}
                or name.startswith("proxy-")
                or any(part in name for part in ("organization", "org-", "workspace", "user-profile"))):
            continue
        if not success and not (name in ("retry-after", "request-id", "x-request-id")
                                or name.startswith("anthropic-ratelimit-")):
            continue
        if _header_sensitive(value):
            if name != "content-type":
                continue
            value = "application/octet-stream"
        allowed.append((key, value))
    return allowed


# Identity masking belongs only to upstream error bodies, never agent output.
_EMAIL = re.compile(
    r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@"
    r"(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}(?![A-Za-z0-9-])"
)
_IDENTITY_KEYS = {"organization", "organization_id", "organization_name", "org",
                  "org_id", "org_name", "email", "user_email"}


def _scrub(payload: bytes) -> bytes:
    try:
        from .redact import MASK, _literal_snapshot, scrub
    except ImportError:                       # running as a bare script
        return payload
    try:
        literals = _literal_snapshot()

        def text(value: str) -> str:
            for literal in literals:
                value = value.replace(literal, MASK)
            return _EMAIL.sub(MASK, value)

        def limit(value: Any, depth: int = 0) -> Any:
            if depth > 24:
                return MASK
            if isinstance(value, dict):
                return {key: MASK if key.lower() in _IDENTITY_KEYS else limit(item, depth + 1)
                        for key, item in value.items()}
            if isinstance(value, list):
                return [limit(item, depth + 1) for item in value]
            if isinstance(value, str):
                return text(value)
            return value

        def identity(value: Any) -> Any:
            if isinstance(value, str):
                return value
            if isinstance(value, dict):
                return {scrub(text(key)): identity(item) for key, item in value.items()}
            if isinstance(value, list):
                return [identity(item) for item in value]
            return value

        try:
            value = json.loads(payload)
        except (ValueError, UnicodeError):
            value = payload.decode("utf-8", errors="surrogateescape")
            return scrub(text(value)).encode("utf-8", errors="surrogateescape")
        return json.dumps(identity(scrub(limit(value)))).encode()
    except Exception:
        return b"Upstream response withheld."


class _Server(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True
    # An agent that hangs up mid-request must not take the proxy with it.
    allow_reuse_address = True


def serve(vault: Path, host: str = "127.0.0.1", port: int = PORT,
          on_event: Any = None, pins: dict[str, str] | None = None,
          pins_path: Path | None = None) -> _Server:
    """Start the proxy. Returns the server, already serving in a thread."""
    Handler.accounts = Accounts(vault, pins, pins_path)
    Handler.secret = load_secret(vault)
    Handler.on_event = staticmethod(on_event) if on_event else None
    server = _Server((host, port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def main(argv: list[str]) -> int:
    """`python3 -m multiagents.authproxy <vault> [host] [port]`.

    Runs in a sidecar container beside the egress proxy, on the agents'
    internal network — the same shape, for the same reason: it is the only
    thing on that network with a route out, so agents reach the model API
    through something that decides what they may send with, or not at all.
    """
    if not argv:
        print(__doc__.strip().splitlines()[0], file=sys.stderr)
        print("usage: authproxy <vault-dir> [host] [port]", file=sys.stderr)
        return 64
    vault = Path(argv[0])
    host = argv[1] if len(argv) > 1 else "0.0.0.0"
    port = int(argv[2]) if len(argv) > 2 else PORT
    server = serve(vault, host, port,
                   pins_path=Path(argv[3]) if len(argv) > 3 else None,
                   on_event=lambda kind, fields: print(
                       json.dumps({"t": time.time(), "kind": kind, **fields}),
                       flush=True))
    labels = Handler.accounts.labels()
    print(json.dumps({"t": time.time(), "kind": "listening", "host": host,
                      "port": port, "accounts": labels}), flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        server.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
