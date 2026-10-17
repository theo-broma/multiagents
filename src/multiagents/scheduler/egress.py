"""Authenticated, destination-checked HTTP egress for isolated attempts."""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import ipaddress
import re
import select
import socket
import socketserver
import threading
from urllib.parse import urlsplit

_LABEL_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")


def _patterns(allowlist):
    if __package__:
        # The installed handler uses the existing validator and matcher.
        from ..executor.docker import DockerExecutor

        legacy = DockerExecutor.__new__(DockerExecutor)
        legacy.config = {"egress_allowlist": allowlist}
        return legacy._proxy_filter().splitlines()
    # The gateway runs this file standalone. Its host supplies only entries
    # already accepted and normalized by DockerExecutor.allowlist_entries.
    # Reject malformed payloads here too; never turn one into a wildcard.
    patterns = []
    for host in allowlist:
        if (not isinstance(host, str) or host != host.encode("idna").decode("ascii").lower()
                or len(host) > 253 or not all(_LABEL_RE.fullmatch(p) for p in host.split("."))):
            raise ValueError("invalid gateway allowlist")
        try:
            ipaddress.IPv4Address(host)
        except ValueError:
            if "." not in host or re.fullmatch(r"(?:[0-9]+|0x[0-9a-f]*)", host.rsplit(".", 1)[-1]):
                raise ValueError("invalid gateway allowlist")
            patterns.append(r"^([^.]+\.)*" + re.escape(host) + "$")
        else:
            patterns.append("^" + re.escape(host) + "$")
    return patterns


class _Refused(Exception):
    def __init__(self, status):
        self.status = status


def _shutdown(conn):
    try:
        conn.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    conn.close()


def _authority(value, default_port=None):
    if not value or any(ord(c) <= 32 or ord(c) >= 127 for c in value):
        raise _Refused(400)
    if any(c in value for c in "@%\\/?#"):
        raise _Refused(400)
    if value.startswith("["):
        match = re.fullmatch(r"\[([^\]]+)\](?::([0-9]+))?", value)
        if not match:
            raise _Refused(400)
        try:
            host = str(ipaddress.IPv6Address(match[1]))
        except ValueError:
            raise _Refused(400) from None
        port_text = match[2]
    else:
        if value.count(":") > 1:
            raise _Refused(400)
        host, colon, port_text = value.partition(":")
        if not colon:
            port_text = None
        if not host or len(host) > 253 or not all(_LABEL_RE.fullmatch(p) for p in host.split(".")):
            raise _Refused(400)
        host = host.encode("idna").decode("ascii").lower()
    if port_text is None:
        if default_port is None:
            raise _Refused(400)
        port = default_port
    elif not re.fullmatch(r"[0-9]+", port_text):
        raise _Refused(400)
    else:
        try:
            port = int(port_text)
        except ValueError:
            raise _Refused(400) from None
    if not 0 < port <= 65535:
        raise _Refused(400)
    return host, port


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    block_on_close = False


class _Egress:
    def __init__(self, listen, allowlist, grants, resolver, connect, forbidden_networks):
        self.resolver, self.connect = resolver, connect
        self.lock = threading.RLock()
        self.revoked = set()
        self.closed = False
        self.sockets = {}
        self.connections = set()
        self.grants = {}
        self.update(allowlist, grants, forbidden_networks)
        owner = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                owner._handle(self.request, self.client_address[0])

        self.server = _Server(listen, Handler)
        self.address = self.server.server_address
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .05}, daemon=True)
        self.thread.start()

    def update(self, allowlist, grants, forbidden_networks):
        """Apply a policy in place: only withdrawn or changed grants lose their streams."""
        patterns = [re.compile(p, re.I) for p in _patterns(allowlist) if p]
        forbidden = tuple(ipaddress.ip_network(n) for n in forbidden_networks)
        replacement = {}
        for grant_id, grant in grants.items():
            verifier = hashlib.sha256((grant["username"] + ":" + grant["password"]).encode()).digest()
            replacement[grant_id] = (verifier, tuple(ipaddress.ip_network(n) for n in grant["networks"]))
        with self.lock:
            self.patterns, self.forbidden = patterns, forbidden
            for grant_id, (verifier, networks) in self.grants.items():
                new = replacement.get(grant_id)
                if (new is None or not hmac.compare_digest(new[0], verifier)
                        or not all(any(n.subnet_of(m) for m in new[1] if m.version == n.version) for n in networks)):
                    for conn in tuple(self.sockets.get(grant_id, ())):
                        _shutdown(conn)
            self.grants = replacement
            for grant_id in replacement:
                self.sockets.setdefault(grant_id, set())

    def revoke(self, grant_id):
        with self.lock:
            self.revoked.add(grant_id)
            for conn in tuple(self.sockets.get(grant_id, ())):
                _shutdown(conn)

    def close(self):
        with self.lock:
            self.closed = True
            for conn in tuple(self.connections):
                _shutdown(conn)
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def _active(self, grant_id):
        if self.closed or grant_id in self.revoked or grant_id not in self.grants:
            raise _Refused(407)

    def _authenticate(self, headers, source, conn):
        values = [v for k, v in headers if k.lower() == "proxy-authorization"]
        if len(values) != 1:
            raise _Refused(407)
        parts = values[0].split()
        if len(parts) != 2 or parts[0].lower() != "basic":
            raise _Refused(407)
        try:
            credential = base64.b64decode(parts[1], validate=True)
        except (ValueError, binascii.Error):
            raise _Refused(407) from None
        verifier = hashlib.sha256(credential).digest()
        source = ipaddress.ip_address(source)
        with self.lock:
            for grant_id, (expected, networks) in self.grants.items():
                if hmac.compare_digest(verifier, expected) and any(source in n for n in networks):
                    self._active(grant_id)
                    self.sockets[grant_id].add(conn)
                    return grant_id
        raise _Refused(407)

    def _destination(self, host, port):
        if not any(p.search(host) for p in self.patterns):
            raise _Refused(403)
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            try:
                answers = list(self.resolver(host, port))
            except Exception:
                raise _Refused(502) from None
        else:
            answers = [str(literal)]
        if not answers:
            raise _Refused(502)
        checked = []
        for answer in answers:
            try:
                address = ipaddress.ip_address(answer)
            except ValueError:
                raise _Refused(502) from None
            mapped = getattr(address, "ipv4_mapped", None)
            effective = mapped or address
            if (not effective.is_global or effective.is_multicast or effective.is_reserved
                    or any(address in n or (mapped is not None and mapped in n) for n in self.forbidden)):
                raise _Refused(403)
            checked.append(str(address))
        return checked[0]

    def _send(self, conn, data, grant_id):
        pending = memoryview(data)
        while pending:
            with self.lock:
                self._active(grant_id)
            _, ready, _ = select.select([], [conn], [], .2)
            if not ready:
                continue
            with self.lock:
                self._active(grant_id)
                try:
                    count = conn.send(pending, socket.MSG_DONTWAIT)
                except BlockingIOError:
                    continue
                if not count:
                    raise OSError("closed connection")
                pending = pending[count:]

    def _relay(self, client, upstream, grant_id, tunnel):
        readers = [client, upstream] if tunnel else [upstream]
        while True:
            ready, _, _ = select.select(readers, [], [], .2)
            with self.lock:
                self._active(grant_id)
            for conn in ready:
                data = conn.recv(65536)
                if not data:
                    return
                self._send(upstream if conn is client else client, data, grant_id)

    @staticmethod
    def _framing(headers):
        lengths = [v for k, v in headers if k.lower() == "content-length"]
        encodings = [v for k, v in headers if k.lower() == "transfer-encoding"]
        if len(lengths) > 1 or len(encodings) > 1 or (lengths and encodings):
            raise _Refused(400)
        if lengths and (not re.fullmatch(r"[0-9]+", lengths[0]) or len(lengths[0]) > 20):
            raise _Refused(400)
        if encodings and encodings[0].lower() != "chunked":
            raise _Refused(400)
        return lengths, encodings

    def _body(self, client, upstream, remainder, headers, grant_id):
        lengths, encodings = self._framing(headers)
        if encodings:
            # Decode framing ourselves so a pipelined request never becomes
            # an unauthenticated second upstream request.
            pending = bytearray(remainder)

            def read_line():
                while b"\r\n" not in pending:
                    if len(pending) > 8192:
                        raise _Refused(400)
                    data = client.recv(8192)
                    if not data:
                        raise _Refused(400)
                    pending.extend(data)
                line, _, rest = pending.partition(b"\r\n")
                pending[:] = rest
                return bytes(line)

            while True:
                line = read_line()
                if not re.fullmatch(rb"[0-9a-fA-F]+(?:;[^\r\n]*)?", line):
                    raise _Refused(400)
                size = int(line.split(b";", 1)[0], 16)
                self._send(upstream, line + b"\r\n", grant_id)
                if not size:
                    # Trailers are unnecessary for forwarding and can carry
                    # proxy credentials; consume and discard them.
                    total = 0
                    while True:
                        trailer = read_line()
                        total += len(trailer) + 2
                        if total > 65536:
                            raise _Refused(400)
                        if not trailer:
                            break
                    self._send(upstream, b"\r\n", grant_id)
                    return
                remaining = size
                while remaining:
                    if not pending:
                        pending.extend(client.recv(min(65536, remaining)))
                        if not pending:
                            raise _Refused(400)
                    count = min(remaining, len(pending))
                    self._send(upstream, bytes(pending[:count]), grant_id)
                    del pending[:count]
                    remaining -= count
                if read_line():
                    raise _Refused(400)
                self._send(upstream, b"\r\n", grant_id)
        elif lengths:
            remaining = int(lengths[0])
            data = remainder[:remaining]
            while remaining:
                if not data:
                    data = client.recv(min(65536, remaining))
                    if not data:
                        raise _Refused(400)
                self._send(upstream, data, grant_id)
                remaining -= len(data)
                data = b""

    def _handle(self, client, source):
        upstream = None
        grant_id = None
        response_started = False
        with self.lock:
            if self.closed:
                return
            self.connections.add(client)
        client.settimeout(15)
        try:
            raw = b""
            while b"\r\n\r\n" not in raw:
                data = client.recv(8192)
                if not data:
                    return
                raw += data
                if len(raw.partition(b"\r\n\r\n")[0]) > 65536:
                    raise _Refused(431)
            head, _, remainder = raw.partition(b"\r\n\r\n")
            lines = head.decode("latin-1").split("\r\n")
            parts = lines[0].split(" ")
            if len(parts) != 3 or not re.fullmatch(r"[A-Z]+", parts[0]) or parts[2] not in ("HTTP/1.0", "HTTP/1.1"):
                raise _Refused(400)
            method, target, version = parts
            if any(ord(c) <= 32 or ord(c) == 127 for c in target):
                raise _Refused(400)
            headers = []
            for line in lines[1:]:
                name, colon, value = line.partition(":")
                if (not colon or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name)
                        or any(ord(c) < 32 and c != "\t" or ord(c) == 127 for c in value)):
                    raise _Refused(400)
                headers.append((name, value.strip(" \t")))
            grant_id = self._authenticate(headers, source, client)
            if method == "CONNECT":
                host, port = _authority(target)
                path = None
            else:
                try:
                    parsed = urlsplit(target)
                except ValueError:
                    raise _Refused(400) from None
                if parsed.scheme.lower() != "http" or not parsed.netloc:
                    raise _Refused(400)
                host, port = _authority(parsed.netloc, 80)
                path = (parsed.path or "/") + ("?" + parsed.query if parsed.query else "")
            hosts = [v for k, v in headers if k.lower() == "host"]
            host_default = port if method == "CONNECT" else 80
            if len(hosts) > 1 or (hosts and _authority(hosts[0], host_default) != (host, port)):
                raise _Refused(400)
            if method == "CONNECT" and port != 443:
                raise _Refused(403)
            self._framing(headers)
            connection_tokens = {v.strip().lower() for k, value in headers if k.lower() == "connection" for v in value.split(",")}
            if connection_tokens & {"host", "content-length", "transfer-encoding", "trailer"}:
                raise _Refused(400)
            address = self._destination(host, port)
            with self.lock:
                self._active(grant_id)
            # Dial without the shared lock: one attempt's slow destination
            # must not stall other attempts or revoke (IC-C29).
            try:
                dialled = self.connect(address, port)
            except Exception:
                raise _Refused(502) from None
            with self.lock:
                try:
                    self._active(grant_id)
                except _Refused:
                    _shutdown(dialled)
                    raise
                upstream = dialled
                upstream.settimeout(15)
                self.connections.add(upstream)
                self.sockets[grant_id].add(upstream)
            if method == "CONNECT":
                self._send(client, b"HTTP/1.1 200 Connection Established\r\n\r\n", grant_id)
                response_started = True
                if remainder:
                    self._send(upstream, remainder, grant_id)
                self._relay(client, upstream, grant_id, True)
            else:
                stripped = {"proxy-authorization", "proxy-connection", "connection", "keep-alive", "upgrade", "expect", "trailer"} | connection_tokens
                forwarded = [(k, v) for k, v in headers if k.lower() not in stripped]
                request = f"{method} {path} {version}\r\n" + "".join(f"{k}: {v}\r\n" for k, v in forwarded) + "Connection: close\r\n\r\n"
                self._send(upstream, request.encode("latin-1"), grant_id)
                if any(k.lower() == "expect" for k, _ in headers):
                    self._send(client, b"HTTP/1.1 100 Continue\r\n\r\n", grant_id)
                self._body(client, upstream, remainder, headers, grant_id)
                response_started = True
                self._relay(client, upstream, grant_id, False)
        except _Refused as exc:
            if not response_started:
                self._error(client, exc.status)
        except (OSError, ValueError, UnicodeError):
            if not response_started:
                self._error(client, 502)
        finally:
            with self.lock:
                for conn in (client, upstream):
                    if conn is not None:
                        self.connections.discard(conn)
                        if grant_id is not None:
                            self.sockets[grant_id].discard(conn)
                        _shutdown(conn)

    @staticmethod
    def _error(client, status):
        reason = {400: "Bad Request", 403: "Forbidden", 407: "Proxy Authentication Required", 431: "Request Header Fields Too Large", 502: "Bad Gateway"}[status]
        body = (reason + "\n").encode()
        challenge = 'Proxy-Authenticate: basic realm="egress"\r\n' if status == 407 else ""
        try:
            client.sendall((f"HTTP/1.1 {status} {reason}\r\n{challenge}Content-Length: {len(body)}\r\nConnection: close\r\n\r\n").encode() + body)
        except OSError:
            pass


def serve_egress(listen, allowlist, grants, resolver, connect, forbidden_networks):
    """Serve IC-C26/C27 egress; revoke closes both ends of a grant's streams."""
    return _Egress(listen, allowlist, grants, resolver, connect, forbidden_networks)
