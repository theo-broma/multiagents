"""HF6 — the auth proxy sends the account token only to the model API.

Contract: `context/specs/findings-c1-c2-part1.md`, HF6 (and Decision 4,
which records where it came from). `authproxy` built its upstream URL as
`UPSTREAM + self.path`, so a request target that does not start with `/`
(`.attacker.invalid/x`) made the proxy call
`https://api.anthropic.com.attacker.invalid/x` with the account's bearer
token in `Authorization`.

How these tests watch the wire, without any real network:

- `UPSTREAM` is left at its real value, `https://api.anthropic.com`, so the
  check is exercised against the host it actually protects.
- `urllib.request.OpenerDirector.open` is replaced by a recorder that keeps
  every request the proxy tries to make and answers 200 with a marker body.
  Both `urllib.request.urlopen` and any `build_opener()` opener go through it.
- `socket.create_connection` and `socket.getaddrinfo` refuse, and record, any
  host but loopback. An implementation that bypasses urllib (say
  `http.client.HTTPSConnection` directly) is still caught: a leak shows up
  there, and the HF6.3 tests go red because nothing reached the recorder.
- `Accounts.token`, the store's public "read this account's token" method, is
  wrapped to record which labels were read (HF6.1: "before any account is
  chosen or any token is read").

The client side uses `http.client`, because `urllib` cannot put a request
target that does not start with `/` on the request line.
"""

import contextlib
import http.client
import io
import socket
import sys
import time
import urllib.parse
import urllib.request
import urllib.response
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))
import c1_harness as h
from multiagents import authproxy


EXPECTED_UPSTREAM = "https://api.anthropic.com"
EXPECTED_HOST = "api.anthropic.com"

# Every wait is short and explicit: a red test must fail in seconds.
SOCKET_TIMEOUT = 5.0
EVENT_WAIT = 2.0

TOKEN_A = "sk-ant-oat01-hf6-account-a-secret"
TOKEN_B = "sk-ant-oat01-hf6-account-b-secret"
UPSTREAM_MARKER = b"HF6-UPSTREAM-CONTENT-MARKER"
AGENT = "agent-hf6"

# The five spellings HF6.1 names, each with a fragment of it that must never
# be echoed back (HF6.2).
REFUSED = [
    pytest.param(".attacker.invalid/x", "attacker.invalid", id="dot-suffix"),
    pytest.param("-x.attacker.invalid/x", "attacker.invalid", id="dash-suffix"),
    pytest.param("@attacker.invalid/x", "attacker.invalid", id="userinfo"),
    pytest.param(":8443/x", "8443", id="port"),
    pytest.param("http://attacker.invalid/x", "attacker.invalid", id="absolute-form"),
]
REFUSED_TARGETS = [p.values[0] for p in REFUSED]


# ---------------------------------------------------------------------------
# The recorders
# ---------------------------------------------------------------------------

def _install_wire_recorders(monkeypatch):
    sent = []          # SimpleNamespace(url, host, headers, data) per request
    dialled = []       # non-loopback hosts anything tried to reach
    token_reads = []   # labels passed to Accounts.token

    def fake_open(self, fullurl, data=None, timeout=None, *args, **kwargs):
        req = fullurl if isinstance(fullurl, urllib.request.Request) \
            else urllib.request.Request(fullurl, data=data)
        sent.append(SimpleNamespace(
            url=req.full_url, host=req.host,
            headers={k.lower(): v for k, v in req.header_items()},
            data=req.data if data is None else data))
        headers = http.client.parse_headers(
            io.BytesIO(b"content-type: application/json\r\n\r\n"))
        body = b'{"from":"' + UPSTREAM_MARKER + b'"}'
        return urllib.response.addinfourl(io.BytesIO(body), headers,
                                          req.full_url, code=200)

    monkeypatch.setattr(urllib.request.OpenerDirector, "open", fake_open)

    loopback = {"127.0.0.1", "localhost", "::1"}
    real_create_connection = socket.create_connection
    real_getaddrinfo = socket.getaddrinfo

    def guarded_create_connection(address, *args, **kwargs):
        if str(address[0]) not in loopback:
            dialled.append(address)
            raise OSError(f"HF6 test: no real network ({address[0]!r})")
        return real_create_connection(address, *args, **kwargs)

    def guarded_getaddrinfo(host, *args, **kwargs):
        name = host.decode() if isinstance(host, bytes) else str(host)
        if host is not None and name not in loopback:
            dialled.append((name,))
            raise socket.gaierror(f"HF6 test: no real DNS ({name!r})")
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket, "create_connection", guarded_create_connection)
    monkeypatch.setattr(socket, "getaddrinfo", guarded_getaddrinfo)

    real_token = authproxy.Accounts.token

    def recording_token(self, label):
        token_reads.append(label)
        return real_token(self, label)

    monkeypatch.setattr(authproxy.Accounts, "token", recording_token)
    return sent, dialled, token_reads


@contextlib.contextmanager
def proxy_world(tmp_path, monkeypatch, accounts=None):
    """The real proxy, its real `UPSTREAM`, and every wire recorded."""
    sent, dialled, token_reads = _install_wire_recorders(monkeypatch)
    events = []
    with h.authproxy_server(tmp_path, monkeypatch,
                            accounts=accounts or {"acc-a": TOKEN_A},
                            on_event=lambda k, f: events.append((k, f))) as proxy:
        yield SimpleNamespace(proxy=proxy, sent=sent, dialled=dialled,
                              token_reads=token_reads, events=events,
                              port=proxy.server.server_address[1])


def send(world, target, agent=AGENT, method="POST", body=b'{"msg":"hi"}'):
    """One request with this exact request target, on a fresh connection.

    Returns (status, headers as one lowercased string, body bytes)."""
    conn = http.client.HTTPConnection("127.0.0.1", world.port, timeout=SOCKET_TIMEOUT)
    try:
        conn.request(method, target, body=body,
                     headers={"Authorization": "Bearer " + world.proxy.mint(agent),
                              "Content-Type": "application/json"})
        resp = conn.getresponse()
        payload = resp.read()
        return resp.status, str(resp.headers).lower(), payload
    finally:
        conn.close()


def _flatten(value) -> str:
    if isinstance(value, dict):
        return " ".join(f"{k} {_flatten(v)}" for k, v in value.items())
    if isinstance(value, (list, tuple, set)):
        return " ".join(_flatten(v) for v in value)
    return str(value)


def _names_agent(fields, agent) -> bool:
    def walk(v):
        if isinstance(v, dict):
            return any(walk(x) for x in v.values())
        if isinstance(v, (list, tuple, set)):
            return any(walk(x) for x in v)
        return str(v) == agent
    return walk(fields)


def _wait_for(predicate, deadline_s=EVENT_WAIT):
    deadline = time.monotonic() + deadline_s
    while not predicate():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.02)
    return True


# ---------------------------------------------------------------------------
# HF6.1 — no upstream request and no token for a target off UPSTREAM's host
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("target,fragment", REFUSED)
def test_hf6_1_refused_target_makes_no_upstream_request_and_reads_no_token(
        tmp_path, monkeypatch, target, fragment):
    with proxy_world(tmp_path, monkeypatch) as world:
        status, headers, body = send(world, target)

        assert world.sent == [], (
            f"target {target!r} caused upstream request(s) "
            f"{[(r.url, r.headers.get('authorization')) for r in world.sent]}")
        assert world.dialled == [], f"target {target!r} dialled {world.dialled}"
        assert world.token_reads == [], (
            f"target {target!r} read account token(s) for {world.token_reads}; "
            "HF6.1 checks the URL before any token is read")
        assert TOKEN_A not in body.decode("utf-8", "replace")
        assert TOKEN_A.lower() not in headers


def test_hf6_1_absolute_form_get_is_refused_too(tmp_path, monkeypatch):
    """`do_GET` is the same handler; a forward-proxy style GET is the most
    natural way to aim this proxy at another host."""
    with proxy_world(tmp_path, monkeypatch) as world:
        send(world, "http://attacker.invalid/x", method="GET", body=None)
        assert world.sent == []
        assert world.dialled == []
        assert world.token_reads == []


def test_hf6_1_refused_target_chooses_no_account(tmp_path, monkeypatch):
    """"Before any account is chosen": a refusal leaves no account pinned.

    Observed through the documented fewest-agents-first assignment (the
    module docstring and `test_c1_authproxy_characterization.py`): with two
    unused accounts the first agent served lands on `acc-a`. If the refused
    request had chosen `acc-a` for its agent, the next agent would be sent
    to `acc-b` instead."""
    with proxy_world(tmp_path, monkeypatch,
                     accounts={"acc-a": TOKEN_A, "acc-b": TOKEN_B}) as world:
        send(world, ".attacker.invalid/x", agent="agent-refused")
        status, _, _ = send(world, "/v1/messages", agent="agent-normal")

        assert status == 200
        assert [r.headers.get("authorization") for r in world.sent] == \
            [f"Bearer {TOKEN_A}"], (
                "the refused request either went upstream or took an account: "
                f"{[(r.url, r.headers.get('authorization')) for r in world.sent]}")


@pytest.mark.parametrize("target", [
    *REFUSED_TARGETS,
    "%2eattacker.invalid/x",        # urllib unquotes the host it dials
    "\\attacker.invalid/x",
    "//attacker.invalid/x",         # http.server folds this to one `/`
    "/v1/messages?next=@attacker.invalid:8443/x",
    "?@attacker.invalid/x",
], ids=["dot-suffix", "dash-suffix", "userinfo", "port", "absolute-form",
        "pct-dot", "backslash", "double-slash", "query-at", "bare-query-at"])
def test_hf6_1_token_only_ever_goes_to_the_upstream_host(tmp_path, monkeypatch, target):
    """Whatever the proxy does with a target — refuse it or forward it — any
    request carrying a token goes to https://api.anthropic.com on its default
    port with no userinfo, and nothing else is dialled."""
    with proxy_world(tmp_path, monkeypatch) as world:
        send(world, target)

        assert world.dialled == [], f"{target!r} dialled {world.dialled}"
        for r in world.sent:
            parts = urllib.parse.urlsplit(r.url)
            assert (parts.scheme, parts.hostname, parts.port,
                    parts.username, parts.password) == \
                ("https", EXPECTED_HOST, None, None, None), \
                f"{target!r} sent a request to {r.url!r}"
            assert r.host == EXPECTED_HOST, f"{target!r} dialled host {r.host!r}"


# ---------------------------------------------------------------------------
# HF6.2 — 400, short fixed body, an event naming the agent, still serving
# ---------------------------------------------------------------------------

def test_hf6_2_refused_target_gets_400_with_one_short_fixed_body(tmp_path, monkeypatch):
    with proxy_world(tmp_path, monkeypatch) as world:
        replies = {t: send(world, t) for t in REFUSED_TARGETS}

    statuses = {t: r[0] for t, r in replies.items()}
    assert statuses == {t: 400 for t in REFUSED_TARGETS}

    bodies = {r[2] for r in replies.values()}
    assert len(bodies) == 1, f"the refusal body varies with the target: {bodies}"
    body = bodies.pop()
    assert 0 < len(body) <= 1024, f"refusal body is not short: {len(body)} bytes"
    text = body.decode("utf-8", "replace")
    assert UPSTREAM_MARKER.decode() not in text
    assert TOKEN_A not in text
    for target, fragment in (p.values for p in REFUSED):
        assert target not in text
        assert fragment not in text


@pytest.mark.parametrize("target,fragment", REFUSED)
def test_hf6_2_refusal_is_an_event_naming_the_agent_not_the_target(
        tmp_path, monkeypatch, target, fragment):
    with proxy_world(tmp_path, monkeypatch) as world:
        status, _, _ = send(world, target)
        assert status == 400

        assert _wait_for(lambda: any(_names_agent(f, AGENT) for _, f in world.events)), (
            f"no proxy event names {AGENT!r} after refusing {target!r}: {world.events}")
        recorded = _flatten(world.events)
        assert target not in recorded, f"event repeats the target verbatim: {world.events}"
        assert TOKEN_A not in recorded


def test_hf6_2_proxy_keeps_serving_after_a_refusal(tmp_path, monkeypatch):
    with proxy_world(tmp_path, monkeypatch) as world:
        refused, _, _ = send(world, ".attacker.invalid/v1/messages")
        status, _, body = send(world, "/v1/messages", body=b'{"after":"refusal"}')

        assert refused == 400
        assert status == 200
        assert UPSTREAM_MARKER in body
        assert [(r.url, r.headers.get("authorization"), r.data) for r in world.sent] == [
            (EXPECTED_UPSTREAM + "/v1/messages", f"Bearer {TOKEN_A}",
             b'{"after":"refusal"}')]


# ---------------------------------------------------------------------------
# HF6.3 — origin-form targets are forwarded exactly as before
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("target", [
    "/v1/messages",
    "/v1/messages?beta=true",
    "/v1/messages/count_tokens?beta=true",
    # An `@` or `:` after the path is not part of the host of the URL built.
    "/v1/messages?note=user@example.invalid:8443",
])
def test_hf6_3_origin_form_target_is_forwarded_to_upstream_unchanged(
        tmp_path, monkeypatch, target):
    with proxy_world(tmp_path, monkeypatch) as world:
        status, _, body = send(world, target, body=b'{"msg":"hi"}')

        assert status == 200
        assert UPSTREAM_MARKER in body
        assert [(r.url, r.headers.get("authorization"), r.data) for r in world.sent] == [
            (EXPECTED_UPSTREAM + target, f"Bearer {TOKEN_A}", b'{"msg":"hi"}')]
        assert world.dialled == []
