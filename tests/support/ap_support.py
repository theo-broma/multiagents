"""Shared seams for the AP1–AP6 tests (`context/specs/findings-c1-c2-part2.md`).

Everything here drives the real `authproxy.serve()` over real loopback
sockets. Nothing in it reaches the real network:

- `Upstream` is a local stand-in for the model API. `authproxy.UPSTREAM` is
  monkeypatched to point at it (the same seam `c1_harness.authproxy_server`
  uses). It records every request that arrives — method, target, headers,
  body — and answers through a per-test `responder`.
- `FakeClock` moves the wall clock and the monotonic clock forward together,
  through the `time` module the proxy reads (`time.time`, `time.monotonic`).
  It never sleeps: it adds an offset to the real readings, so time keeps
  flowing normally for sockets and threads and only jumps when told to.
- `raw_exchange` talks HTTP/1.1 bytes directly, for what `http.client`
  hides: HEAD bodies, chunked requests, connection close, a second response.
"""

from __future__ import annotations

import contextlib
import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from typing import Callable

from multiagents import authproxy

# Every wait is short, explicit, and named: a red test fails in seconds.
SOCKET_TIMEOUT = 5.0     # a whole exchange with the proxy
EVENT_WAIT = 1.0         # an event that should already have been emitted
CLOSE_WAIT = 2.0         # how long a closed connection may take to read EOF
NEGATIVE_GRACE = 0.3     # how long to watch for an event that must NOT come


def wait_for(predicate: Callable[[], bool], deadline_s: float = EVENT_WAIT) -> bool:
    deadline = time.monotonic() + deadline_s
    while not predicate():
        if time.monotonic() > deadline:
            return False
        threading.Event().wait(0.01)
    return True


# ---------------------------------------------------------------------------
# The clock
# ---------------------------------------------------------------------------

class FakeClock:
    """`time.time()` and `time.monotonic()` advanced by `advance(seconds)`.

    Starts at the real time, so an HTTP-date or an epoch built from `now()`
    means the same instant whichever clock the proxy compares it with."""

    def __init__(self, monkeypatch) -> None:
        self.offset = 0.0
        real_time, real_monotonic = time.time, time.monotonic
        monkeypatch.setattr(time, "time", lambda: real_time() + self.offset)
        monkeypatch.setattr(time, "monotonic", lambda: real_monotonic() + self.offset)

    def now(self) -> float:
        return time.time()

    def advance(self, seconds: float) -> None:
        self.offset += seconds


# ---------------------------------------------------------------------------
# The upstream
# ---------------------------------------------------------------------------

class Reply(SimpleNamespace):
    """What the upstream answers: status, headers (a list of pairs, so a
    header can repeat), body. `send_body=False` sends headers only, as a
    server does for HEAD. `declared_length` overrides Content-Length, and a
    body shorter than it followed by a close is a truncated body.
    `cut_chunked=b"..."` sends a chunked body of that one chunk and then
    drops the connection before the next chunk: a body read failing."""

    def __init__(self, status=200, headers=None, body=b"", send_body=True,
                 declared_length=None, before_body=None, cut_chunked=None):
        super().__init__(status=status, headers=list(headers or []), body=body,
                         send_body=send_body, declared_length=declared_length,
                         before_body=before_body, cut_chunked=cut_chunked)


class Upstream:
    def __init__(self, responder: Callable[[SimpleNamespace], Reply] | None = None):
        self.requests: list[SimpleNamespace] = []
        self.responder = responder or (lambda req: Reply(200, [("content-type", "application/json")],
                                                         b'{"ok":true}'))
        self.release = threading.Event()   # set on teardown; unblocks hung handlers
        upstream = self

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _handle(self):
                headers = {k.lower(): v for k, v in self.headers.items()}
                length = headers.get("content-length")
                body = self.rfile.read(int(length)) if length else b""
                record = SimpleNamespace(
                    method=self.command, path=self.path, headers=headers, body=body,
                    token=headers.get("authorization", "").removeprefix("Bearer "))
                upstream.requests.append(record)
                reply = upstream.responder(record)
                self.send_response(reply.status)
                for key, value in reply.headers:
                    self.send_header(key, value)
                if reply.cut_chunked is not None:
                    self.send_header("transfer-encoding", "chunked")
                    self.end_headers()
                    self.close_connection = True
                    with contextlib.suppress(OSError):
                        data = reply.cut_chunked
                        self.wfile.write(b"%x\r\n" % len(data) + data + b"\r\n")
                        self.wfile.flush()
                        self.connection.shutdown(socket.SHUT_RDWR)
                    return
                declared = reply.declared_length
                if declared is None:
                    declared = len(reply.body)
                self.send_header("content-length", str(declared))
                truncated = declared != len(reply.body)
                if truncated:
                    self.send_header("connection", "close")
                    self.close_connection = True
                self.end_headers()
                if reply.before_body is not None:
                    reply.before_body(self)
                if reply.send_body and self.command != "HEAD":
                    try:
                        self.wfile.write(reply.body)
                        self.wfile.flush()
                    except OSError:
                        pass
                if truncated:
                    with contextlib.suppress(OSError):
                        self.connection.shutdown(socket.SHUT_RDWR)

            do_GET = do_POST = do_HEAD = do_PUT = do_DELETE = _handle
            do_OPTIONS = do_PATCH = do_TRACE = do_CONNECT = _handle

        self.handler = _Handler

    def tokens(self) -> list[str]:
        return [r.token for r in self.requests]


@contextlib.contextmanager
def upstream_server(upstream: Upstream):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), upstream.handler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05},
                              daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}"
    finally:
        upstream.release.set()
        httpd.shutdown()
        httpd.server_close()


def closed_port_url() -> str:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return f"http://127.0.0.1:{port}"


# ---------------------------------------------------------------------------
# The proxy
# ---------------------------------------------------------------------------

def seed_account(vault, label: str, token: str) -> None:
    d = vault / "accounts" / label
    d.mkdir(parents=True, exist_ok=True)
    (d / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {"accessToken": token}}))


@contextlib.contextmanager
def proxy_server(tmp_path, monkeypatch, *, accounts: dict[str, str] | None = None,
                 upstream_url: str | None = None, pins: dict[str, str] | None = None,
                 pins_path=None, record_events: bool = True, on_event=None):
    """The real proxy on an ephemeral port. Yields a namespace with `port`,
    `mint(agent, provider="")`, `events` (kind, fields) and `server`."""
    vault = tmp_path / "vault"
    vault.mkdir(parents=True, exist_ok=True)
    for label, token in (accounts or {}).items():
        seed_account(vault, label, token)
    if upstream_url is not None:
        monkeypatch.setattr(authproxy, "UPSTREAM", upstream_url)
    events: list[tuple[str, dict]] = []
    callback = on_event
    if callback is None and record_events:
        def callback(kind, fields):
            events.append((kind, dict(fields)))
    server = authproxy.serve(vault, host="127.0.0.1", port=0, on_event=callback,
                             pins=pins, pins_path=pins_path)
    secret = authproxy.Handler.secret
    try:
        yield SimpleNamespace(
            port=server.server_address[1], vault=vault, events=events, server=server,
            mint=lambda agent, provider="": authproxy.mint_token(agent, secret, provider))
    finally:
        server.shutdown()
        server.server_close()


# ---------------------------------------------------------------------------
# The client
# ---------------------------------------------------------------------------

def request(proxy, method="POST", target="/v1/messages", body: bytes | None = b"{}",
            agent="agent-ap", provider="", headers: dict | None = None):
    """One exchange through `http.client`. Returns (status, headers, body) with
    header names lowercased; a repeated header keeps its last value."""
    import http.client

    conn = http.client.HTTPConnection("127.0.0.1", proxy.port, timeout=SOCKET_TIMEOUT)
    try:
        sent = {"Authorization": "Bearer " + proxy.mint(agent, provider)}
        sent.update(headers or {})
        conn.request(method, target, body=body, headers=sent)
        resp = conn.getresponse()
        payload = resp.read()
        return resp.status, {k.lower(): v for k, v in resp.getheaders()}, payload
    finally:
        conn.close()


def raw_exchange(port: int, data: bytes, wait: float = CLOSE_WAIT) -> tuple[bytes, bool]:
    """Send raw bytes, read until the proxy closes or `wait` passes.
    Returns (everything received, whether the connection was closed)."""
    sock = socket.create_connection(("127.0.0.1", port), timeout=SOCKET_TIMEOUT)
    received = b""
    closed = False
    try:
        sock.sendall(data)
        deadline = time.monotonic() + wait
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                break
            sock.settimeout(left)
            try:
                chunk = sock.recv(65536)
            except socket.timeout:
                break
            except ConnectionResetError:
                closed = True
                break
            if not chunk:
                closed = True
                break
            received += chunk
    finally:
        sock.close()
    return received, closed


def split_response(raw: bytes) -> tuple[int, dict[str, str], bytes]:
    """The first response's status, headers (lowercased names) and every byte
    after its header block."""
    head, _, rest = raw.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    status = int(lines[0].split(" ")[1]) if lines and lines[0].startswith("HTTP/") else 0
    headers = {}
    for line in lines[1:]:
        name, _, value = line.partition(":")
        headers[name.strip().lower()] = value.strip()
    return status, headers, rest


def new_events(events, kinds=("upstream_error", "upstream_unreachable")):
    return [(k, f) for k, f in events if k in kinds]
