"""Slice 4B.2 black-box helpers: the auth gateway for isolated attempts.

Contract: context/specs/phase7-part2-contract.md, "Slice 4B.2 decisions"
(K4B2.1..K4B2.7), IC-C28, and IC-C29/IC-C30 for the auth gateway.

Worlds:

* `AuthSched` (fake CLI): the 4B.1 `NetSched` whose provider also declares
  `gateway_auth: anthropic`. Observed in Docker's vocabulary and the host's
  public views. Fake-CLI passes are never evidence of containment.
* `Auth`: the auth gateway's REAL request handler on loopback, driven by raw
  sockets, with the synthetic upstream of `ic_s4b2_backend` behind the seam.

SEAMS (K4B2.8):

* `multiagents.scheduler.gateways.serve_auth(listen, grants, backend)` returns an
  object with `.address`, `.revoke(grant_id)` and `.close()`. `grants` is
  {grant_id: {"verifier", "networks", "provider", "run_id"}}; `verifier` is the
  lowercase hex SHA-256 of the full bearer string. The gateway never sees a
  plaintext bearer; the tests compute the verifier. `backend` is the adapter.
* `GatewayRegistry(..., service="egress"|"auth")`, default "egress"; the
  container label is `multiagents.gateway=<service>`.
* The auth receipt has the egress receipt's keys, recorded under the service name.
"""
from __future__ import annotations

import base64
import hashlib
import re
import socket
import time

import ic_s4b as nb
import ic_s4b2_backend as backend
import ic_support as ic

BEARER = re.compile(r"^ma-auth-v1\.[0-9a-f]{64}$")
BEARER_ANYWHERE = re.compile(r"ma-auth-v1\.[0-9a-f]{8,}")
AUTH_URL = "http://ma-auth:8930"
BACKEND_ENV = "MULTIAGENTS_TEST_GATEWAY_BACKEND"
SHORT = nb.SHORT
WAIT = nb.WAIT
START = 15                 # a container's start on a shared, loaded machine; only a missing start waits it out


def hexes(n=64, ch="a"):
    return (ch * n)[:n]


def verifier_of(bearer: str) -> str:
    """The gateway's view of a bearer: lowercase hex SHA-256 of the full string."""
    return hashlib.sha256(bearer.encode()).hexdigest()


def bearer_of(env: dict) -> str:
    value = env.get("ANTHROPIC_AUTH_TOKEN", "")
    assert BEARER.match(value), f"IC-C28: ANTHROPIC_AUTH_TOKEN is not an ma-auth-v1 bearer: {value!r}"
    return value


# ------------------------------------------------------------ the fake-CLI world

class AuthSched(nb.NetSched):
    """NetSched whose provider declares network + `gateway_auth: anthropic`."""

    def __init__(self, tmp_path, monkeypatch, *, gateway_auth="anthropic", backend_spec=backend.SPEC, **kw):
        super().__init__(tmp_path, monkeypatch, **kw)
        if backend_spec:
            monkeypatch.setenv(BACKEND_ENV, backend_spec)
        else:
            monkeypatch.delenv(BACKEND_ENV, raising=False)
        self.set_gateway_auth(gateway_auth)

    def set_gateway_auth(self, value) -> None:
        if value is None:
            self.fake.entry.pop("gateway_auth", None)
        else:
            self.fake.entry["gateway_auth"] = value
        self.p.write()
        self._reset_server()

    def rpc(self, op, *args, **kwargs):
        """A read that the loaded scheduler's socket reset is read again; a write never is."""
        for attempt in range(5):
            try:
                return super().rpc(op, *args, **kwargs)
            except ConnectionError:
                if not (op.startswith(("get_", "list_")) or op == "scheduler_status") or attempt == 4:
                    raise
                time.sleep(0.2)

    def running(self, node: str, timeout: float = START) -> dict:
        return super().running(node, timeout)

    def gateways(self, service: str) -> list[dict]:
        """Containers holding the `ma-<service>` alias on some network."""
        found = {}
        for c in self.fd.containers().values():
            for net in c.get("NetworkSettings", {}).get("Networks", {}).values():
                if f"ma-{service}" in (net.get("Aliases") or []):
                    found[c["Id"]] = c
        return list(found.values())

    def gateway(self, service: str) -> dict:
        found = self.gateways(service)
        assert len(found) == 1, f"expected exactly one {service} gateway, found {len(found)}"
        return found[0]

    def registry(self, service: str | None = None):
        if service in (None, "egress"):
            return super().registry()          # the default service: no keyword is needed
        import importlib
        import os
        from multiagents import config as config_mod
        from multiagents.paths import ProjectPaths
        cls = nb.have("GatewayRegistry")
        paths = ProjectPaths(self.root)
        loaded = config_mod.load(paths, seed=False)
        kwargs = {}
        if os.environ.get(nb.PROBE_ENV):
            module, _, name = os.environ[nb.PROBE_ENV].partition(":")
            kwargs["readiness_probe"] = getattr(importlib.import_module(module), name)()
        try:
            return cls(paths, config=loaded, providers=loaded.providers, service=service, **kwargs)
        except TypeError as exc:
            raise AssertionError(f"K4B2.1: GatewayRegistry(..., service={service!r}) is not accepted ({exc})") from None

    def rows_of(self, service: str) -> list[dict]:
        return ic.run(self.registry(service).reconcile())

    def state_of(self, service: str, attempt_id: str):
        rows = [r for r in self.rows_of(service) if r["attempt_id"] == attempt_id]
        return rows[0]["state"] if rows else None

    def pair(self):
        """One running network+auth attempt: (node, run, container, network)."""
        self.start()
        node = self.launch()
        container = self.running(node)
        run = self.runs(node)[-1]
        (network,) = self.attempt_networks(run["attempt_id"])
        return node, run, container, network

    def has_gateway(self, service: str, network: dict) -> bool:
        gw = self.gateways(service)
        return bool(gw) and all(g["State"]["Running"] and g["Id"] in self.fd.members(network["Id"]) for g in gw)

    def all_text(self, node: str) -> dict[str, str]:
        return self.public_texts(node)


def bearer_leaks(texts: dict[str, str], *secrets: str) -> dict[str, str]:
    """Which public text carries an issued bearer: a literal secret, or anything
    shaped like an `ma-auth-v1.<hex>` value (this includes its `Bearer ` form)."""
    found = {}
    for where, text in texts.items():
        for secret in secrets:
            if secret and secret in text:
                found[where] = "literal"
        if BEARER_ANYWHERE.search(text):
            found[where] = "bearer-shaped value"
    return found


# ----------------------------------------------------------- the handler world

def bearer_header(value: str) -> str:
    return "Bearer " + value


class Auth:
    """The auth gateway's real handler plus its synthetic upstream."""

    def __init__(self, tmp_path, monkeypatch, grants=None):
        self.root = tmp_path / "auth-root"
        self.root.mkdir()
        monkeypatch.setenv(BACKEND_ENV, backend.SPEC)
        self.adapter = backend.Adapter(self.root, "proj")
        backend.ADAPTERS[str(self.root)] = self.adapter
        self.grants = grants or {
            "g1": {"bearer": "ma-auth-v1." + hexes(64, "a1"), "networks": ["127.0.1.0/24"],
                   "provider": "acme", "run_id": "run_0001"},
            "g2": {"bearer": "ma-auth-v1." + hexes(64, "b2"), "networks": ["127.0.2.0/24"],
                   "provider": "other", "run_id": "run_0002"},
        }
        self.handle = None

    def start(self):
        serve = nb.have("serve_auth")
        wire = {gid: {"verifier": verifier_of(g["bearer"]), "networks": g["networks"],
                      "provider": g["provider"], "run_id": g["run_id"]} for gid, g in self.grants.items()}
        self.handle = serve(listen=("127.0.0.1", 0), grants=wire, backend=self.adapter)
        return self

    @property
    def address(self):
        return tuple(self.handle.address)

    def close(self):
        if self.handle:
            self.handle.close()
        for r in self.adapter.responses:
            r.close()
        backend.ADAPTERS.pop(str(self.root), None)

    def open(self, source="127.0.1.5") -> socket.socket:
        c = socket.socket()
        c.settimeout(SHORT)
        c.bind((source, 0))
        c.connect(self.address)
        return c

    def raw(self, data: bytes, source="127.0.1.5", head=False):
        c = self.open(source)
        try:
            c.sendall(data)
            return response(c, head=head)
        finally:
            c.close()

    def send(self, method="POST", path="/v1/messages", *, grant="g1", token=None, authorization=None,
             headers=(), body: bytes = b'{"m":1}', source="127.0.1.5", host="ma-auth:8930"):
        """One request. `token` sets `Authorization: Bearer <token>`; `authorization`
        sets the header verbatim; neither gives the grant's own bearer."""
        if authorization is None:
            authorization = bearer_header(self.grants[grant]["bearer"] if token is None else token)
        lines = [f"{method} {path} HTTP/1.1", f"Host: {host}"]
        if authorization:
            lines.append("Authorization: " + authorization)
        if method in ("POST", "PUT", "PATCH"):
            lines.append(f"Content-Length: {len(body)}")
        lines += list(headers)
        raw = ("\r\n".join(lines) + "\r\n\r\n").encode() + (body if method in ("POST", "PUT", "PATCH") else b"")
        return self.raw(raw, source, head=method == "HEAD")

    def stream(self, *, grant="g1", source="127.0.1.5", path="/v1/messages"):
        """A POST whose response the test controls: the client socket, with the
        status line and headers read."""
        c = self.open(source)
        body = b"{}"
        c.sendall((f"POST {path} HTTP/1.1\r\nHost: ma-auth:8930\r\n"
                   f"Authorization: Bearer {self.grants[grant]['bearer']}\r\nContent-Length: {len(body)}\r\n\r\n").encode() + body)
        return c

    def untouched(self) -> bool:
        return self.adapter.opened == []


def read_until(c: socket.socket, marker: bytes, timeout=SHORT) -> bytes:
    data = b""
    deadline = time.monotonic() + timeout
    c.settimeout(0.2)
    while marker not in data and time.monotonic() < deadline:
        try:
            chunk = c.recv(65536)
        except socket.timeout:
            continue
        except OSError:
            break
        if not chunk:
            break
        data += chunk
    return data


def closes(c: socket.socket, timeout=SHORT) -> bool:
    """True when the connection ends (EOF or reset) within the bound."""
    deadline = time.monotonic() + timeout
    c.settimeout(0.2)
    while time.monotonic() < deadline:
        try:
            if not c.recv(65536):
                return True
        except socket.timeout:
            continue
        except OSError:
            return True
    return False


def basic(user: str, password: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


def response(c: socket.socket, *, head: bool = False, timeout=SHORT):
    """Read one HTTP response to its end: (status, lowercase headers, raw body,
    still-open). Chunked bodies are returned as their wire bytes; a response with
    neither length nor chunking is read to EOF."""
    deadline = time.monotonic() + timeout
    c.settimeout(0.2)
    data = b""

    def more() -> bool:
        while time.monotonic() < deadline:
            try:
                chunk = c.recv(65536)
            except socket.timeout:
                continue
            except OSError:
                return False
            if not chunk:
                return False
            nonlocal data
            data += chunk
            return True
        return False

    while b"\r\n\r\n" not in data and more():
        pass
    top, _, rest = data.partition(b"\r\n\r\n")
    lines = top.decode("latin-1").split("\r\n")
    parts = lines[0].split()
    status = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
    headers = {}
    for line in lines[1:]:
        k, _, v = line.partition(":")
        headers[k.strip().lower()] = v.strip()
    data = rest
    if head or status in (204, 304) or 100 <= status < 200:
        return status, headers, data
    if "content-length" in headers and headers["content-length"].isdigit():
        while len(data) < int(headers["content-length"]) and more():
            pass
    elif "chunked" in headers.get("transfer-encoding", "").lower():
        while not data.endswith(b"0\r\n\r\n") and more():
            pass
    else:
        while more():
            pass
    return status, headers, data
