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

import hashlib
import base64
import hmac
import json
import os
import re
import secrets
import socketserver
import sys
import threading
import time
import urllib.error
import urllib.request
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

UPSTREAM = "https://api.anthropic.com"
PORT = 8930
TOKEN_PREFIX = "mxa"
CLAIM_PREFIX = "mxa2"

# Long, because time-to-first-token on a large request is not fast and a read
# timeout shorter than the upstream's worst case kills healthy requests. The
# CLI has its own timeout (it sends X-Stainless-Timeout: 600) and should be the
# one to give up.
UPSTREAM_TIMEOUT = 900

# Headers that belong to the hop, not the request. `authorization` is dropped
# because replacing it is the entire job; the rest would be wrong to forward.
HOP_HEADERS = {"host", "authorization", "content-length", "connection",
               "accept-encoding", "proxy-connection", "keep-alive",
               "transfer-encoding", "upgrade", "te", "trailer"}


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


def validate_label(label: str) -> str:
    if not isinstance(label, str) or not re.fullmatch(r"[a-z0-9_-]+", label):
        raise ValueError("account label must contain lowercase letters, digits, '-' or '_'")
    return label


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

class Accounts:
    """The pool, its limits, and the pinning that keeps caches warm."""

    def __init__(self, vault: Path, pins: dict[str, str] | None = None,
                 pins_path: Path | None = None) -> None:
        self.vault = vault
        self.lock = threading.Lock()
        self.pinned: dict[str, str] = {}          # agent id -> account label
        self.limited: dict[str, float] = {}       # account label -> until
        self._cache: dict[str, tuple[str, str]] = {}
        self.rejected: dict[str, str] = {}       # label -> rejected content hash
        self.pins_path = pins_path
        self.provider_pins = self._validate_pins(pins or {})
        self.reload_pins()
        self.labels()                           # fail before admitting requests

    @staticmethod
    def _validate_pins(pins: dict[str, str]) -> dict[str, str]:
        if not isinstance(pins, dict):
            raise ValueError("proxy pins must be a provider-to-account mapping")
        return {provider: validate_label(label) for provider, label in pins.items()}

    def reload_pins(self) -> None:
        if self.pins_path is not None:
            self.provider_pins = self._validate_pins(json.loads(self.pins_path.read_text()))

    def labels(self) -> list[str]:
        """Every account with a credential, in label order.

        Read from disk every time rather than held: a login can add one while
        this is running, and an operator who has just added an account and
        watched nothing happen is owed a better answer than "restart it".
        """
        root = self.vault / "accounts"
        labels = ["default"] if (self.vault / ".credentials.json").is_file() else []
        if root.is_dir():
            if (root / "default").exists():
                raise ValueError("accounts/default is reserved; rename it to another account label")
            for path in root.iterdir():
                if not path.is_dir():
                    continue
                validate_label(path.name)
                if (path / ".credentials.json").is_file():
                    labels.append(path.name)
        return sorted(labels)

    def path(self, label: str) -> Path:
        validate_label(label)
        if label == "default":
            return self.vault / ".credentials.json"
        return self.vault / "accounts" / label / ".credentials.json"

    def token(self, label: str) -> str:
        """That account's current access token, re-read when the file changes."""
        path = self.path(label)
        try:
            raw = path.read_bytes()
        except OSError:
            return ""
        stamp = hashlib.sha256(raw).hexdigest()
        cached = self._cache.get(label)
        if cached and cached[0] == stamp:
            return cached[1]
        try:
            data = json.loads(raw)
        except (OSError, ValueError):
            return ""
        for block in data.values() if isinstance(data, dict) else []:
            if isinstance(block, dict) and isinstance(block.get("accessToken"), str) and block["accessToken"]:
                self._cache[label] = (stamp, block["accessToken"])
                return block["accessToken"]
        return ""

    def for_agent(self, agent_id: str, provider: str = "",
                  tried: list[str] | None = None, *,
                  eligible: list[str] | None = None, fixed: str | None = None) -> str:
        """Which account this agent uses. Decided once, then kept.

        Pinned for the agent's whole life even when a rested account appears,
        because the cache it has built on this one is worth more than the
        headroom on that one. See the module docstring.
        """
        with self.lock:
            eligible = self.eligible(provider) if eligible is None else eligible
            free = [label for label in eligible if label not in (tried or [])
                    and self.usable(label)]
            fixed = self.provider_pins.get(provider) if fixed is None else fixed
            if fixed:
                return fixed if fixed in free else ""
            label = self.pinned.get(agent_id)
            if label and label in free:
                return label
            if not free:
                return ""
            # Fewest agents first, so a second account is actually used rather
            # than sitting idle until the first one is exhausted.
            counts = {l: 0 for l in free}
            for pinned in self.pinned.values():
                if pinned in counts:
                    counts[pinned] += 1
            label = min(free, key=lambda l: (counts[l], l))
            self.pinned[agent_id] = label
            return label

    def eligible(self, provider: str = "") -> list[str]:
        fixed = self.provider_pins.get(provider)
        return [fixed] if fixed else [label for label in self.labels()
                                     if label not in self.provider_pins.values()]

    def usable(self, label: str) -> bool:
        self.token(label)                        # refresh the content fingerprint
        stamp = self._cache.get(label, ("", ""))[0]
        if label in self.rejected and self.rejected[label] == stamp:
            return False
        return self.limited.get(label, 0) <= time.time()

    def mark_unauthenticated(self, label: str, token: str) -> None:
        with self.lock:
            # The file may have changed while this request was upstream. Do
            # not quarantine a new login because an older token was refused.
            self.token(label)
            stamp, current = self._cache.get(label, ("", ""))
            if current == token:
                self.rejected[label] = stamp
            for agent, pinned in list(self.pinned.items()):
                if pinned == label:
                    del self.pinned[agent]

    def mark_limited(self, label: str, seconds: float) -> None:
        with self.lock:
            self.limited[label] = time.time() + max(60.0, seconds)
            # Unpin everyone on it. They will land somewhere else NEXT request,
            # which is the only moment a switch is safe: mid-stream it is not
            # possible and between streams it costs only the cache.
            for agent, pinned in list(self.pinned.items()):
                if pinned == label:
                    del self.pinned[agent]

    def release(self, agent_id: str) -> None:
        with self.lock:
            self.pinned.pop(agent_id, None)


# --------------------------------------------------------------------------
# The proxy itself
# --------------------------------------------------------------------------

def _retry_after(headers: Any, body: bytes) -> float:
    """How long the upstream says to wait. Falls back to an hour."""
    for name in ("retry-after", "anthropic-ratelimit-unified-reset"):
        raw = headers.get(name) if headers else None
        if raw:
            try:
                value = float(raw)
                # A reset may be an epoch or a duration; both appear in the wild.
                return value - time.time() if value > 1e9 else value
            except ValueError:
                pass
    del body
    return 3600.0


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    accounts: Accounts
    secret: str
    on_event: Any = None

    def log_message(self, *args) -> None:        # noqa: D102 - quiet by default
        pass

    def _fail(self, status: int, message: str) -> None:
        payload = json.dumps({"type": "error",
                              "error": {"type": "multiagents", "message": message}}
                             ).encode()
        self._error_payload(status, payload)

    def _error_payload(self, status: int, payload: bytes) -> None:
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _event(self, kind: str, **fields) -> None:
        if callable(type(self).on_event):
            try:
                type(self).on_event(kind, fields)
            except Exception:
                pass

    def do_POST(self) -> None:                   # noqa: N802 - http.server API
        agent, provider = read_claim(self.headers.get("authorization", ""), type(self).secret)
        if not agent:
            # Not one of ours. Nothing reachable here should be sending
            # anything else, so this is either a misconfigured agent or
            # something that should not be on this network at all.
            self._event("rejected", reason="unsigned token")
            return self._fail(401, "this proxy serves multiagents agents only")

        body = self.rfile.read(int(self.headers.get("content-length") or 0))
        accounts = type(self).accounts
        try:
            accounts.reload_pins()
            accounts.labels()
            eligible = accounts.eligible(provider)
            fixed = accounts.provider_pins.get(provider, "")
        except (OSError, ValueError):
            return self._fail(503, "account pin configuration is unavailable")
        identity = f"{agent}\x00{provider}" if provider else agent
        tried: list[str] = []
        unauthenticated = False
        while True:
            # Freeze admission's pool and pin for this request. Concurrent
            # logins cannot grow a retry loop, nor can a reload move a pinned
            # request onto a different account halfway through its attempts.
            label = accounts.for_agent(identity, provider, tried,
                                       eligible=eligible, fixed=fixed)
            if not label or label in tried:
                self._event("exhausted", agent=agent, tried=tried)
                denied = unauthenticated or any(l in accounts.rejected for l in eligible)
                if not fixed and unauthenticated:
                    return self._error_payload(401, self._last_auth_error)
                return self._fail(
                    401 if denied or (fixed and not accounts.token(fixed)) else 429,
                    f"account {fixed} is unusable" if fixed else
                    "no usable account remains" if denied else
                    "every account for this provider is rate limited; "
                    "the run will be retried when one resets")
            tried.append(label)
            token = type(self).accounts.token(label)
            if not token:
                type(self).accounts.mark_limited(label, 300)
                continue
            outcome = self._forward(label, token, body)
            if outcome not in ("limited", "unauthenticated"):
                return
            unauthenticated |= outcome == "unauthenticated"
            # A limit found BEFORE any of the response was written is the only
            # kind that can be retried elsewhere. `_forward` says so by
            # returning here rather than having sent anything.
            self._event("switch", agent=agent, away_from=label)

    do_GET = do_POST

    def _forward(self, label: str, token: str, body: bytes) -> str:
        headers = {k: v for k, v in self.headers.items()
                   if k.lower() not in HOP_HEADERS}
        headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(UPSTREAM + self.path, data=body,
                                         headers=headers, method="POST")
        try:
            upstream = urllib.request.urlopen(request, timeout=UPSTREAM_TIMEOUT)
        except urllib.error.HTTPError as exc:
            payload = exc.read()
            if exc.code == 401:
                type(self).accounts.mark_unauthenticated(label, token)
                self._event("unauthenticated", label=label)
                self._last_auth_error = _scrub(payload)
                return "unauthenticated"
            if exc.code in (429, 529):
                type(self).accounts.mark_limited(
                    label, _retry_after(exc.headers, payload))
                return "limited"
            # Upstream errors name the account, the organisation, sometimes the
            # email. That reply is about to be handed to a container running
            # somebody else's code with approvals off, so it is scrubbed first
            # — the same redactor that guards everything else written here.
            payload = _scrub(payload)
            self.send_response(exc.code)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return "error"
        except Exception as exc:                  # network, DNS, timeout
            self._fail(502, f"upstream unreachable: {type(exc).__name__}")
            return "error"

        # Past this line the status is committed and a retry elsewhere is no
        # longer possible: bytes have gone to the client. A limit that arrives
        # mid-stream therefore has to be a hard failure, which the runner
        # already knows how to read — it detects the limit in the agent's own
        # output and puts the provider on a cooldown.
        with upstream:
            self.send_response(upstream.status)
            for key, value in upstream.headers.items():
                if key.lower() not in ("transfer-encoding", "connection",
                                       "content-length"):
                    self.send_header(key, value)
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            while True:
                # Raw bytes, unaltered. Server-sent events are framed by the
                # upstream and re-framing them here — merging chunks, splitting
                # a line — corrupts a stream the client parses incrementally.
                chunk = upstream.read(8192)
                if not chunk:
                    break
                try:
                    self.wfile.write(b"%x\r\n" % len(chunk) + chunk + b"\r\n")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    return "error"           # the agent gave up; so do we
            self.wfile.write(b"0\r\n\r\n")
        return "ok"


def _scrub(payload: bytes) -> bytes:
    try:
        from .redact import scrub
    except ImportError:                       # running as a bare script
        return payload
    try:
        return json.dumps(scrub(json.loads(payload))).encode()
    except Exception:
        return payload


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
