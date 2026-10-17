"""Verifier-only auth ingress for isolated attempts (IC-C28, K4B2.6).

No signed legacy claim or request field supplies routing authority here. The
backend boundary carries only the provider and run recorded in a host grant.
"""
from __future__ import annotations

import hashlib
import hmac
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import re
import socket
import threading

if __package__:
    from ..auth_upstream import (BODY_IDLE_TIMEOUT, MAX_BODY_SIZE, UPSTREAM,
                                 request_headers, upstream_url)
else:
    from auth_upstream import (BODY_IDLE_TIMEOUT, MAX_BODY_SIZE, UPSTREAM,
                              request_headers, upstream_url)

_BEARER = re.compile(r"Bearer (ma-auth-v1\.[0-9a-f]{64})\Z")
_VERIFIER = re.compile(r"[0-9a-f]{64}\Z")
WRITE_TIMEOUT = 15


class UpstreamBackend:
    """Fixed upstream transport, without credential projection until slice 4C."""

    def open(self, method, path, headers, body, *, provider, run_id):
        if not upstream_url(path):
            raise ValueError("invalid auth upstream path")
        # HTTPConnection does not follow redirects or consult proxy environment.
        connection = http.client.HTTPSConnection("api.anthropic.com", timeout=900)
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
        except BaseException:
            connection.close()
            raise
        return _Response(connection, response)


class _Response:
    def __init__(self, connection, response):
        self.connection, self.response = connection, response
        self.status, self.headers = response.status, response.headers

    def read(self, size):
        return self.response.read1(size)

    def close(self):
        # HTTPResponse.close alone may wait on its buffered reader's lock.
        if self.connection.sock:
            try:
                self.connection.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        self.connection.close()
        self.response.close()


def _shutdown(connection):
    try:
        connection.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    connection.close()


def _close_sessions(sessions):
    for session in sessions:
        _shutdown(session["connection"])
        if session["upstream"]:
            session["upstream"].close()


class _Auth:
    def __init__(self, listen, grants, backend):
        self.lock = threading.RLock()
        self.grants = dict(grants)
        self.requests = {}
        self.backend = backend
        gateway = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            timeout = WRITE_TIMEOUT

            def log_message(self, *args):
                pass

            def fail(self, status):
                self.close_connection = True
                body = json.dumps({"type": "error", "error": {"type": "multiagents",
                                   "message": "auth gateway request refused"}}).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Content-Type", "application/json")
                self.send_header("Connection", "close")
                if status == 405:
                    self.send_header("Allow", "GET, HEAD, POST")
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            def do_unsupported(self):
                self.fail(405)

            def __getattr__(self, name):
                if name.startswith("do_"):
                    return self.do_unsupported
                raise AttributeError(name)

            def do_POST(self):
                session = None
                upstream = None
                self.close_connection = True
                try:
                    with gateway.lock:
                        found = gateway.authenticate(self.headers, self.client_address[0])
                        if found is not None:
                            grant_id, grant = found
                            session = {"connection": self.connection, "upstream": None, "revoked": False}
                            gateway.requests.setdefault(grant_id, []).append(session)
                    if found is None:
                        return self.fail(401)
                    if "transfer-encoding" in self.headers:
                        return self.fail(411)
                    values = [part.strip(" \t") for length in self.headers.get_all("content-length", [])
                              for part in length.split(",")]
                    if any(not re.fullmatch(r"[0-9]+", value) for value in values):
                        return self.fail(400)
                    lengths = {value.lstrip("0") or "0" for value in values}
                    if len(lengths) > 1:
                        return self.fail(400)
                    length = lengths.pop() if lengths else "0"
                    bound = str(MAX_BODY_SIZE)
                    if len(length) > len(bound) or len(length) == len(bound) and length > bound:
                        return self.fail(413)
                    if not self.path.startswith("/") or any(ord(c) < 32 or ord(c) == 127 for c in self.path) or not upstream_url(self.path):
                        return self.fail(400)
                    self.connection.settimeout(BODY_IDLE_TIMEOUT)
                    body = bytearray()
                    remaining = int(length)
                    while remaining:
                        chunk = self.rfile.read1(min(8192, remaining))
                        if not chunk:
                            return self.fail(400)
                        body.extend(chunk)
                        remaining -= len(chunk)
                    self.connection.settimeout(WRITE_TIMEOUT)
                    headers = request_headers(self.headers, isolated=True)
                    with gateway.lock:
                        if session["revoked"]:
                            return
                    upstream = gateway.backend.open(self.command, self.path, headers, bytes(body),
                                                    provider=grant["provider"], run_id=grant["run_id"])
                    with gateway.lock:
                        if session["revoked"]:
                            return
                        session["upstream"] = upstream
                    # Revocation can close the tracked socket while a slow
                    # client blocks any header or body write (IC-C29).
                    status = upstream.status
                    if not isinstance(status, int) or not 200 <= status <= 599:
                        return self.fail(502)
                    self.send_response(status)
                    hop = {"connection", "content-length", "transfer-encoding", "keep-alive",
                           "upgrade", "te", "trailer", "authorization", "set-cookie", "www-authenticate", "location"}
                    for key, value in upstream.headers.items():
                        if key.lower() not in hop and not key.lower().startswith("proxy-") and not any(c in key + value for c in "\r\n"):
                            self.send_header(key, value)
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.wfile.flush()
                    if self.command == "HEAD" or status in (204, 304):
                        return
                    while True:
                        with gateway.lock:
                            if session["revoked"]:
                                return
                        chunk = upstream.read(8192)
                        with gateway.lock:
                            if session["revoked"] or not chunk:
                                return
                        self.wfile.write(chunk)
                        self.wfile.flush()
                except (OSError, TimeoutError):
                    if not upstream and not (session and session["revoked"]):
                        try:
                            self.fail(502)
                        except OSError:
                            pass
                except Exception:
                    if not upstream and not (session and session["revoked"]):
                        try:
                            self.fail(502)
                        except OSError:
                            pass
                finally:
                    if upstream:
                        upstream.close()
                    if session:
                        with gateway.lock:
                            sessions = gateway.requests.get(grant_id, [])
                            if session in sessions:
                                sessions.remove(session)
                            if not sessions:
                                gateway.requests.pop(grant_id, None)

            do_GET = do_HEAD = do_POST

        self.server = ThreadingHTTPServer(listen, Handler)
        self.server.daemon_threads = True
        self.address = self.server.server_address
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def authenticate(self, headers, source):
        values = headers.get_all("authorization", [])
        match = _BEARER.fullmatch(values[0]) if len(values) == 1 else None
        if not match:
            return None
        verifier = hashlib.sha256(match[1].encode("ascii")).hexdigest()
        address = ipaddress.ip_address(source)
        for grant_id, grant in self.grants.items():
            stored = grant.get("verifier", "")
            if not isinstance(stored, str) or not _VERIFIER.fullmatch(stored):
                continue
            if hmac.compare_digest(verifier, stored):
                try:
                    if any(address in ipaddress.ip_network(net, strict=False) for net in grant["networks"]):
                        return grant_id, dict(grant)
                except (ValueError, KeyError):
                    pass
        return None

    def _revoke_locked(self, grant_id):
        self.grants.pop(grant_id, None)
        sessions = list(self.requests.get(grant_id, []))
        for session in sessions:
            session["revoked"] = True
        return sessions

    def revoke(self, grant_id):
        with self.lock:
            sessions = self._revoke_locked(grant_id)
        _close_sessions(sessions)

    def update(self, grants):
        sessions = []
        with self.lock:
            for grant_id in list(self.grants):
                if grants.get(grant_id) != self.grants[grant_id]:
                    sessions.extend(self._revoke_locked(grant_id))
            self.grants = dict(grants)
        _close_sessions(sessions)

    def close(self):
        for grant_id in list(self.grants):
            self.revoke(grant_id)
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


def serve_auth(listen, grants, backend):
    return _Auth(listen, grants, backend)
