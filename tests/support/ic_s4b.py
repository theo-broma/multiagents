"""Slice 4B.1 black-box helpers: per-attempt networks and authenticated egress.

Contract: context/specs/phase7-part2-contract.md, "Slice 4B" (D-S4B.1..5, 7;
IC-C25, IC-C26, IC-C27, IC-C29/30 egress, IC-C31).

Three worlds live here:

* `NetSched`: the scheduler on the REAL isolated Docker backend over the fake
  `docker` CLI, with a provider that declares `needs.network`. Everything is
  observed in Docker's own vocabulary (networks, members, aliases, labels) and
  in the host's public views. Fake-CLI passes are NEVER evidence of
  containment (4B work split): the real-Docker cases are in
  `test_ic_s4b_docker.py`, gated by SG_TEST_DOCKER=1.
* `Egress`: the egress gateway's REAL request handler, served on loopback and
  driven by raw sockets. Name resolution and dialling are injected controls
  (the test-side upstream and DNS the contract allows); the allowlist is the
  test's own configuration, never a production edit.
* the plain helpers for reading the guest invocation and scanning for secrets.

THE ONE ASSUMED SEAM (NEED_INFO(egress-handler-seam)): the contract names no
entry point for serving the egress handler. `Egress.start` calls

    multiagents.scheduler.gateways.serve_egress(
        listen=("127.0.0.1", 0), allowlist=[...], grants={grant_id: {"username",
        "password", "networks": [cidr, ...]}}, resolver=callable(host, port) ->
        list[str], connect=callable(address: str, port: int) -> socket,
        forbidden_networks=[cidr, ...])

returning an object with `.address` (host, port), `.revoke(grant_id)` and
`.close()`. If the implementation names it differently, change `Egress.start`
only: no test builds the handler any other way.
"""
from __future__ import annotations

import base64
import importlib
import json
import re
import socket
import threading
import time
from pathlib import Path

import fake_docker as fdk
import ic_s3 as s3
import ic_support as ic

WAIT = 6                    # a bounded wait for anything the scheduler does against the fake CLI
SHORT = 3                   # a bounded wait for a local socket
GATEWAY_IMAGE = "multiagents/workspace:latest"
ALLOW = ["example.com"]
SECRET_URL = re.compile(r"ma1-[^:@/\s\"']+:[^@/\s\"']+@")
PASSWORD = re.compile(r"^[0-9a-f]{64}$")
PROXY_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")
NO_PROXY_VALUE = "localhost,127.0.0.1,::1,ma-auth"
# base64 of "ma1-" at the start of a Basic credential: no encoding of an issued
# `ma1-<attempt>:<password>` pair may appear in durable text.
BASIC_PREFIX = "bWExL"

PUBLIC_V4 = "93.184.216.34"      # globally numbered
PUBLIC_V4_B = "93.184.216.35"
PUBLIC_V6 = "2606:4700:4700::1111"


def gateways_module():
    module = importlib.import_module("multiagents.scheduler.gateways")
    return module


def have(name: str):
    """The public name from `multiagents.scheduler.gateways`, or a failure that
    says what is missing (not an import trace)."""
    try:
        module = gateways_module()
    except ImportError as exc:
        raise AssertionError(f"IC-C25: multiagents.scheduler.gateways is missing ({exc})") from None
    value = getattr(module, name, None)
    assert value is not None, f"IC-C25: multiagents.scheduler.gateways.{name} is missing"
    return value


# ------------------------------------------------------------------ the world

class NetSched(s3.FakeDockerSched):
    """FakeDockerSched whose provider declares `needs.network` (and nothing else).

    `settings` are merged into `executor.isolated`; `allowlist` is the effective
    `executor.docker.egress_allowlist`.
    """

    def __init__(self, tmp_path, monkeypatch, *, network=None, allowlist=ALLOW, gateway_image=None,
                 isolated: dict | None = None, rpc=False, images=None, **kw):
        super().__init__(tmp_path, monkeypatch, images=images, **kw)
        self.provider_declares(network=True, rpc=rpc)
        self.configure(network=network, allowlist=allowlist, gateway_image=gateway_image, **(isolated or {}))
        self.fd.workload(runs=True)

    def configure(self, *, network=None, allowlist=ALLOW, gateway_image=None, **isolated) -> None:
        settings = {"backend": "docker", **isolated}
        if network is not None:
            settings["network"] = network
        if gateway_image is not None:
            settings["docker"] = dict(settings.get("docker") or {}, gateway_image=gateway_image)
        extra = {"isolated": settings}
        if allowlist is not None:
            extra["docker"] = {"egress_allowlist": list(allowlist)}
        self.executor("isolated", **extra)

    # -- launching ------------------------------------------------------------
    def launch(self, **fields) -> str:
        return self.create(**fields)["id"]

    def attempt(self, node: str, n: int = -1) -> dict:
        return self.wait_runs(node, 1, WAIT)[n]

    def running(self, node: str, timeout: float = WAIT) -> dict:
        """The attempt's container, once it is running."""
        def ready():
            view = self.get(node)
            if view["state"] in ("held", "failed", "done", "cancelled"):
                raise AssertionError(f"{node} ended {view['state']} before its container ran: "
                                     + ic.text_of(ic.causes(view))[:500])
            runs = self.runs(node)
            if not runs:
                return None
            found = self.container_of(runs[-1]["attempt_id"])
            return found if found and found["State"]["Running"] else None
        return self.wait(ready, timeout, f"{node}'s container to run")

    def container_of(self, attempt_id: str) -> dict | None:
        for c in self.fd.containers().values():
            if c["Config"]["Labels"].get("multiagents.owner") == attempt_id:
                return c
        return None

    def attempt_networks(self, attempt_id: str) -> list[dict]:
        """Networks Docker holds that carry this attempt as their owner label."""
        return [n for n in self.fd.networks().values() if n["Labels"].get("multiagents.owner") == attempt_id]

    def egress_gateways(self) -> list[dict]:
        """Containers that hold the `ma-egress` alias on some network."""
        found = {}
        for c in self.fd.containers().values():
            for net in c.get("NetworkSettings", {}).get("Networks", {}).values():
                if "ma-egress" in (net.get("Aliases") or []):
                    found[c["Id"]] = c
        return list(found.values())

    def invocation(self, container: dict) -> dict:
        mounts = {m["destination"]: m for m in fdk.mounts_of(container)}
        source = Path(mounts["/multiagents-input"]["source"])
        return json.loads((source / "invocation.json").read_text())

    def guest_env(self, node: str) -> dict:
        return self.invocation(self.running(node))["env"]

    # -- the host-only registry, as a second host process would build it --------
    def registry(self):
        from multiagents import config as config_mod
        from multiagents.paths import ProjectPaths
        cls = have("GatewayRegistry")
        paths = ProjectPaths(self.root)
        loaded = config_mod.load(paths, seed=False)
        return cls(paths, config=loaded, providers=loaded.providers)

    def rows(self) -> list[dict]:
        return ic.run(self.registry().reconcile())

    # -- everything that is public, as text ----------------------------------------
    def public_texts(self, node: str) -> dict[str, str]:
        """Every durable or public place D-S4B.7 says holds no credential."""
        out = {"docker": json.dumps(self.fd.state(), default=str),
               "calls": json.dumps(self.fd.calls()),
               "node": json.dumps(self.get(node), default=str),
               "status": json.dumps(self.rpc("scheduler_status"), default=str)}
        for i, run in enumerate(self.runs(node)):
            out[f"evidence-{i}"] = json.dumps(self.evidence(run["attempt_id"]), default=str)
            stdout, stderr = self.host_output(run["run_id"])
            out[f"stdout-{i}"] = stdout.decode(errors="replace")
            out[f"stderr-{i}"] = stderr.decode(errors="replace")
        orphans = self.cli("scheduler", "orphans", timeout=ic.WAIT)
        out["orphans"] = orphans.stdout + orphans.stderr
        return out


def leaks(texts: dict[str, str], *secrets: str) -> dict[str, str]:
    """Which public text carries an issued credential: any literal secret given,
    any `ma1-<id>:<secret>@` URL, or any Basic encoding of an issued pair."""
    found = {}
    for where, text in texts.items():
        for secret in secrets:
            if secret and secret in text:
                found[where] = "literal"
        if SECRET_URL.search(text):
            found[where] = "credential-bearing URL"
        if re.search(r"(?<![A-Za-z0-9+/])" + BASIC_PREFIX, text):
            found[where] = "Basic encoding"
    return found


def proxy_url(env: dict) -> tuple[str, str]:
    """(username, password) from the guest's HTTP_PROXY."""
    match = re.fullmatch(r"http://(ma1-[^:@/\s]+):([^@/\s]*)@ma-egress:8888", env.get("HTTP_PROXY", ""))
    assert match, f"IC-C26: HTTP_PROXY is not the egress credential URL: {env.get('HTTP_PROXY')!r}"
    return match.group(1), match.group(2)


def wait_done(s, node, timeout=WAIT):
    return s3.wait_done(s, node, timeout)


def finish(s, container: dict, code: int = 0):
    s.fd.finish(container["Id"], code)


# ------------------------------------------------------- the egress handler world

def basic(username: str, password: str) -> str:
    return "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()


class Upstream:
    """A local controlled upstream: records what it received, answers like a web
    server, and echoes tunnel bytes. It stands behind the injected `connect`."""

    def __init__(self, body: bytes = b"UPSTREAM-OK", status: str = "200 OK", extra: str = ""):
        self.body, self.status, self.extra = body, status, extra
        self.requests: list[bytes] = []
        self.tunnels: list[socket.socket] = []
        self.closed = threading.Event()
        self.server = socket.socket()
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(16)
        self.port = self.server.getsockname()[1]
        self.stop = False
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while not self.stop:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        conn.settimeout(10)
        try:
            data = conn.recv(65536)
        except OSError:
            data = b""
        if data.startswith((b"GET ", b"POST ", b"HEAD ", b"PUT ", b"DELETE ", b"OPTIONS ")):
            try:
                while b"\r\n\r\n" not in data:
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    data += chunk
            except OSError:
                pass
            self.requests.append(data)
            conn.settimeout(SHORT)
            head = (f"HTTP/1.1 {self.status}\r\nContent-Length: {len(self.body)}\r\nConnection: close\r\n"
                    f"{self.extra}\r\n").encode()
            try:
                conn.sendall(head + self.body)
            except OSError:
                pass
            conn.close()
            return
        # A tunnel: echo what arrives, and note when the far end closes.
        self.tunnels.append(conn)
        conn.settimeout(None)
        try:
            if data:
                conn.sendall(data)
            while data:
                data = conn.recv(65536)
                if not data:
                    break
                conn.sendall(data)
        except OSError:
            pass
        self.closed.set()
        conn.close()

    def close(self):
        self.stop = True
        self.server.close()
        for t in self.tunnels:
            try:
                t.close()
            except OSError:
                pass


class Egress:
    """The egress gateway's real handler plus its controlled world."""

    def __init__(self, allowlist=("example.com",), answers=None, forbidden=(), grants=None, upstream=None):
        self.upstream = upstream or Upstream()
        self.answers = dict(answers or {})
        self.resolved: list[tuple[str, int]] = []
        self.dialled: list[tuple[str, int]] = []
        self.dial_error: Exception | None = None
        self.resolve_error: Exception | None = None
        self.sequence: dict[str, list[list[str]]] = {}
        self.grants = grants or {"g1": {"username": "ma1-att_0001", "password": "a1" * 32, "networks": ["127.0.1.0/24"]},
                                 "g2": {"username": "ma1-att_0002", "password": "b2" * 32, "networks": ["127.0.2.0/24"]}}
        self.handle = None
        self.allowlist, self.forbidden = list(allowlist), list(forbidden)

    # the injected controls ------------------------------------------------
    def resolver(self, host: str, port: int = 0):
        self.resolved.append((host, port))
        if self.resolve_error:
            raise self.resolve_error
        if host in self.sequence and self.sequence[host]:
            queue = self.sequence[host]
            return list(queue.pop(0) if len(queue) > 1 else queue[0])
        return list(self.answers.get(host.lower(), []))

    def connect(self, address: str, port: int):
        self.dialled.append((address, port))
        if self.dial_error:
            raise self.dial_error
        return socket.create_connection(("127.0.0.1", self.upstream.port), timeout=SHORT)

    def start(self):
        serve = have("serve_egress")
        self.handle = serve(listen=("127.0.0.1", 0), allowlist=self.allowlist, grants=self.grants,
                            resolver=self.resolver, connect=self.connect, forbidden_networks=self.forbidden)
        return self

    @property
    def address(self):
        return tuple(self.handle.address)

    def close(self):
        try:
            if self.handle:
                self.handle.close()
        finally:
            self.upstream.close()

    # a client ------------------------------------------------------------------
    def open(self, source="127.0.1.5") -> socket.socket:
        c = socket.socket()
        c.settimeout(SHORT)
        c.bind((source, 0))
        c.connect(self.address)
        return c

    def request(self, raw: bytes, source="127.0.1.5") -> tuple[int, dict[str, str], bytes]:
        c = self.open(source)
        try:
            c.sendall(raw)
            return read_response(c)
        finally:
            c.close()

    def get(self, url: str, *, grant="g1", source="127.0.1.5", headers=(), host=None, auth=True, method="GET"):
        parsed_host = host or re.sub(r"^https?://", "", url).split("/", 1)[0]
        lines = [f"{method} {url} HTTP/1.1", f"Host: {parsed_host}"]
        if auth:
            g = self.grants[grant]
            lines.append("Proxy-Authorization: " + basic(g["username"], g["password"]))
        lines += list(headers)
        return self.request(("\r\n".join(lines) + "\r\n\r\n").encode(), source)

    def connect_raw(self, target: str, *, grant="g1", source="127.0.1.5", auth=True, headers=()):
        lines = [f"CONNECT {target} HTTP/1.1", f"Host: {target}"]
        if auth:
            g = self.grants[grant]
            lines.append("Proxy-Authorization: " + basic(g["username"], g["password"]))
        lines += list(headers)
        c = self.open(source)
        c.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
        return c

    def untouched(self) -> bool:
        return not self.resolved and not self.dialled


def read_response(c: socket.socket) -> tuple[int, dict[str, str], bytes]:
    data = b""
    try:
        while b"\r\n\r\n" not in data:
            chunk = c.recv(65536)
            if not chunk:
                break
            data += chunk
    except (socket.timeout, ConnectionError):
        pass
    head, _, rest = data.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    status = int(lines[0].split()[1]) if lines and len(lines[0].split()) > 1 else 0
    headers = {}
    for line in lines[1:]:
        k, _, v = line.partition(":")
        headers[k.strip().lower()] = v.strip()
    want = int(headers.get("content-length", "0") or 0)
    body = rest
    try:
        while len(body) < want:
            chunk = c.recv(65536)
            if not chunk:
                break
            body += chunk
    except (socket.timeout, ConnectionError):
        pass
    return status, headers, body


def tunnel_alive(c: socket.socket, payload: bytes = b"ping") -> bool:
    """True when the bytes we send come back; False when the tunnel is closed."""
    try:
        c.sendall(payload)
        got = b""
        deadline = time.monotonic() + SHORT
        while len(got) < len(payload) and time.monotonic() < deadline:
            chunk = c.recv(4096)
            if not chunk:
                return False
            got += chunk
        return got == payload
    except (OSError, socket.timeout):
        return False
