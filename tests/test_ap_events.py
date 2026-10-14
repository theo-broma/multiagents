"""AP2 (F66) and AP6 (F20): the proxy's failures are observable.

Contract: `context/specs/findings-c1-c2-part2.md`, Part 2b, AP2 and AP6.

AP2: an exception from the `on_event` callback is reported on the proxy's
stderr (kind, exception type, message through `redact.scrub`), once per
occurrence, best-effort, and the proxy keeps serving.

AP6: each upstream attempt emits at most one of `upstream_error`
`{"agent", "label", "status"}` (a status >= 400 other than 401/429/529) or
`upstream_unreachable` `{"agent", "label", "error"}` (no status received, or
the relay of a successful response failing). The agent disconnecting emits
nothing. The existing events are unchanged and not counted.

The proxy runs in this process, so "its stderr" is this process's stderr,
captured with `capfd` (file descriptor and `sys.stderr` both).
"""

import socket
import sys
import threading
from pathlib import Path

import pytest

from multiagents import authproxy, redact

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))
import ap_support as ap

AGENT = "agent-ap6"
LITERAL = "ap-two-registered-sensitive-value"
BODY_MARKER = "ap6-upstream-body-marker"
TARGET_MARKER = "ap6targetmarker"
NEW_KINDS = ("upstream_error", "upstream_unreachable")
CLASS_NAME = r"[A-Za-z_][A-Za-z0-9_]*"


@pytest.fixture(autouse=True)
def isolated_registration(monkeypatch):
    monkeypatch.setattr(redact, "_literals", set())


class CallbackBroke(Exception):
    pass


def _limited_then_ok(req):
    """acc-a answers 429, acc-b answers 200: one `switch` on the way."""
    if req.token == "tok-a":
        return ap.Reply(429, [("retry-after", "120")], b"{}")
    return ap.Reply(200, [("content-type", "application/json")], b'{"served":true}')


# ---------------------------------------------------------------------------
# AP2 — a failing callback is reported and does not stop the proxy
# ---------------------------------------------------------------------------

def test_ap2_callback_failure_is_reported_with_kind_and_type_not_the_literal(
        tmp_path, monkeypatch, capfd):
    redact.register_literal(LITERAL)
    calls = []

    def callback(kind, fields):
        calls.append(kind)
        error = CallbackBroke(f"sink refused {LITERAL}")
        raise error

    upstream = ap.Upstream(_limited_then_ok)
    with ap.upstream_server(upstream) as url, \
            ap.proxy_server(tmp_path, monkeypatch, accounts={"acc-a": "tok-a", "acc-b": "tok-b"},
                            upstream_url=url, on_event=callback) as proxy:
        status, _, body = ap.request(proxy, agent=AGENT)
        assert (status, body) == (200, b'{"served":true}'), "the request must still complete"
        again, _, _ = ap.request(proxy, agent="agent-ap2-later")
        assert again == 200, "later requests must still be served"

    err = capfd.readouterr().err
    assert calls, "the scenario must have produced at least one event"
    assert LITERAL not in err, "the exception message must go through redact.scrub"
    reports = [line for line in err.splitlines() if "CallbackBroke" in line]
    assert len(reports) == len(calls), (
        f"expected one report per failed callback ({calls}), got: {reports}")
    for kind in set(calls):
        assert any(kind in line for line in reports), f"no report names {kind!r}: {reports}"


def test_ap2_each_occurrence_is_reported(tmp_path, monkeypatch, capfd):
    calls = []

    def callback(kind, fields):
        calls.append(kind)
        raise CallbackBroke("x")

    with ap.proxy_server(tmp_path, monkeypatch, accounts={"acc": "tok"},
                         upstream_url=ap.closed_port_url(), on_event=callback) as proxy:
        import http.client
        for _ in range(3):
            # An unsigned request: the proxy's own `rejected` event, each time.
            conn = http.client.HTTPConnection("127.0.0.1", proxy.port, timeout=ap.SOCKET_TIMEOUT)
            conn.request("POST", "/v1/messages", body=b"{}")
            assert conn.getresponse().status == 401
            conn.close()

    err = capfd.readouterr().err
    reports = [line for line in err.splitlines() if "CallbackBroke" in line]
    assert len(calls) >= 3
    assert len(reports) == len(calls), f"{calls} vs {reports}"


class _BrokenStream:
    def write(self, *_):
        raise OSError("stderr is gone")

    def flush(self):
        raise OSError("stderr is gone")

    def writelines(self, *_):
        raise OSError("stderr is gone")


def test_ap2_a_stderr_that_raises_does_not_break_the_request(tmp_path, monkeypatch):
    def callback(kind, fields):
        raise CallbackBroke("x")

    monkeypatch.setattr(sys, "stderr", _BrokenStream())
    upstream = ap.Upstream(_limited_then_ok)
    with ap.upstream_server(upstream) as url, \
            ap.proxy_server(tmp_path, monkeypatch, accounts={"acc-a": "tok-a", "acc-b": "tok-b"},
                            upstream_url=url, on_event=callback) as proxy:
        status, _, body = ap.request(proxy, agent=AGENT)
        assert (status, body) == (200, b'{"served":true}')
        assert ap.request(proxy, agent="agent-ap2-later")[0] == 200


# ---------------------------------------------------------------------------
# AP6 — upstream_error
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("status", [400, 403, 404, 500, 503])
def test_ap6_error_status_emits_one_upstream_error(tmp_path, monkeypatch, status):
    body = ('{"type":"error","error":{"message":"%s"}}' % BODY_MARKER).encode()
    upstream = ap.Upstream(lambda req: ap.Reply(status, [("content-type", "application/json")], body))
    with ap.upstream_server(upstream) as url, \
            ap.proxy_server(tmp_path, monkeypatch, accounts={"acc": "tok"}, upstream_url=url) as proxy:
        got, _, _ = ap.request(proxy, target=f"/v1/messages?{TARGET_MARKER}=1",
                               body=f'{{"q":"{BODY_MARKER}"}}'.encode(), agent=AGENT)
        assert got == status
        assert ap.wait_for(lambda: ap.new_events(proxy.events)), \
            f"no upstream_error for a {status}: {proxy.events}"
        assert ap.new_events(proxy.events) == [
            ("upstream_error", {"agent": AGENT, "label": "acc", "status": status})]
        flat = repr(proxy.events)
        assert BODY_MARKER not in flat and TARGET_MARKER not in flat


@pytest.mark.parametrize("status", [302, 307])
def test_ap6_redirect_below_400_emits_no_new_event(tmp_path, monkeypatch, status):
    upstream = ap.Upstream(lambda req: ap.Reply(status, [("location", "/elsewhere")], b""))
    with ap.upstream_server(upstream) as url, \
            ap.proxy_server(tmp_path, monkeypatch, accounts={"acc": "tok"}, upstream_url=url) as proxy:
        got, _, _ = ap.request(proxy, agent=AGENT)
        assert got == status
        assert not ap.wait_for(lambda: ap.new_events(proxy.events), ap.NEGATIVE_GRACE)


def test_ap6_error_whose_body_read_fails_emits_upstream_error_and_gets_502(tmp_path, monkeypatch):
    upstream = ap.Upstream(lambda req: ap.Reply(
        500, [("content-type", "application/json")],
        cut_chunked=('{"partial":"%s' % BODY_MARKER).encode()))
    with ap.upstream_server(upstream) as url, \
            ap.proxy_server(tmp_path, monkeypatch, accounts={"acc": "tok"}, upstream_url=url) as proxy:
        raw, closed = ap.raw_exchange(proxy.port, (
            "POST /v1/messages HTTP/1.1\r\nHost: proxy\r\n"
            f"Authorization: Bearer {proxy.mint(AGENT)}\r\nContent-Length: 2\r\n\r\n{{}}").encode())
        status, _, rest = ap.split_response(raw)
        assert status == 502, raw[:200]
        assert BODY_MARKER.encode() not in raw, "the proxy's own 502, not upstream's bytes"
        assert closed, "the connection is closed after the 502"
        assert raw.count(b"HTTP/1.1 ") == 1
        assert ap.wait_for(lambda: ap.new_events(proxy.events))
        assert ap.new_events(proxy.events) == [
            ("upstream_error", {"agent": AGENT, "label": "acc", "status": 500})]


# ---------------------------------------------------------------------------
# AP6 — upstream_unreachable
# ---------------------------------------------------------------------------

def _assert_one_unreachable(proxy, forbidden=()):
    assert ap.wait_for(lambda: ap.new_events(proxy.events)), f"no new event: {proxy.events}"
    events = ap.new_events(proxy.events)
    assert len(events) == 1, events
    kind, fields = events[0]
    assert kind == "upstream_unreachable"
    assert set(fields) == {"agent", "label", "error"}, fields
    assert (fields["agent"], fields["label"]) == (AGENT, "acc")
    import re
    assert re.fullmatch(CLASS_NAME, fields["error"]), \
        f"`error` must be the exception's class name only: {fields['error']!r}"
    for text in forbidden:
        assert text not in repr(events)


def test_ap6_refused_connection_emits_upstream_unreachable(tmp_path, monkeypatch):
    url = ap.closed_port_url()
    port = url.rsplit(":", 1)[1]
    with ap.proxy_server(tmp_path, monkeypatch, accounts={"acc": "tok"}, upstream_url=url) as proxy:
        got, _, _ = ap.request(proxy, target=f"/v1/messages?{TARGET_MARKER}=1", agent=AGENT)
        assert got == 502
        _assert_one_unreachable(proxy, forbidden=(TARGET_MARKER, port))


def test_ap6_dns_failure_emits_upstream_unreachable(tmp_path, monkeypatch):
    real_getaddrinfo = socket.getaddrinfo

    def no_dns(host, *args, **kwargs):
        if host in ("127.0.0.1", "localhost", b"127.0.0.1"):
            return real_getaddrinfo(host, *args, **kwargs)
        raise socket.gaierror(f"AP6 test: no DNS for {host!r}")

    with ap.proxy_server(tmp_path, monkeypatch, accounts={"acc": "tok"},
                         upstream_url="http://upstream.ap6.invalid") as proxy:
        monkeypatch.setattr(socket, "getaddrinfo", no_dns)
        got, _, _ = ap.request(proxy, agent=AGENT)
        assert got == 502
        _assert_one_unreachable(proxy, forbidden=("ap6.invalid",))


def test_ap6_timeout_before_a_status_emits_upstream_unreachable(tmp_path, monkeypatch):
    hold = threading.Event()

    def hang(req):
        hold.wait(ap.SOCKET_TIMEOUT)
        return ap.Reply(200, [], b"{}")

    upstream = ap.Upstream(hang)
    monkeypatch.setattr(authproxy, "UPSTREAM_TIMEOUT", 0.5)
    try:
        with ap.upstream_server(upstream) as url, \
                ap.proxy_server(tmp_path, monkeypatch, accounts={"acc": "tok"},
                                upstream_url=url) as proxy:
            got, _, _ = ap.request(proxy, agent=AGENT)
            assert got == 502
            _assert_one_unreachable(proxy)
    finally:
        hold.set()


def test_ap6_relay_failing_mid_body_emits_upstream_unreachable_and_one_response(
        tmp_path, monkeypatch):
    upstream = ap.Upstream(lambda req: ap.Reply(
        200, [("content-type", "text/event-stream")], cut_chunked=b"event: ping\n\n"))
    with ap.upstream_server(upstream) as url, \
            ap.proxy_server(tmp_path, monkeypatch, accounts={"acc": "tok"}, upstream_url=url) as proxy:
        raw, closed = ap.raw_exchange(proxy.port, (
            "POST /v1/messages HTTP/1.1\r\nHost: proxy\r\n"
            f"Authorization: Bearer {proxy.mint(AGENT)}\r\nContent-Length: 2\r\n\r\n{{}}").encode())
        assert raw.startswith(b"HTTP/1.1 200"), raw[:200]
        assert raw.count(b"HTTP/1.1 ") == 1, "a second response was started"
        assert closed, "the connection must be closed after a failed relay"
        _assert_one_unreachable(proxy)


FIRST_WRITE = b"data: first\n\n" + b"x" * (1 << 17)


def test_ap6_agent_disconnecting_emits_nothing(tmp_path, monkeypatch):
    client_gone = threading.Event()
    upstream_done = threading.Event()

    def stream_after_disconnect(handler):
        try:
            # Larger than any read buffer, so the proxy relays it at once.
            handler.wfile.write(FIRST_WRITE)
            handler.wfile.flush()
            client_gone.wait(ap.SOCKET_TIMEOUT)
            handler.connection.settimeout(1.0)
            for _ in range(16):
                handler.wfile.write(b"x" * 65536)
            handler.wfile.flush()
        except OSError:
            pass
        finally:
            upstream_done.set()

    upstream = ap.Upstream(lambda req: ap.Reply(
        200, [("content-type", "text/event-stream")], b"", send_body=False,
        declared_length=len(FIRST_WRITE) + 16 * 65536, before_body=stream_after_disconnect))
    with ap.upstream_server(upstream) as url, \
            ap.proxy_server(tmp_path, monkeypatch, accounts={"acc": "tok"}, upstream_url=url) as proxy:
        sock = socket.create_connection(("127.0.0.1", proxy.port), timeout=ap.SOCKET_TIMEOUT)
        sock.sendall((
            "POST /v1/messages HTTP/1.1\r\nHost: proxy\r\n"
            f"Authorization: Bearer {proxy.mint(AGENT)}\r\nContent-Length: 2\r\n\r\n{{}}").encode())
        seen = b""
        while b"first" not in seen:
            chunk = sock.recv(4096)
            assert chunk, "proxy closed before relaying anything"
            seen += chunk
        sock.close()
        client_gone.set()
        assert upstream_done.wait(ap.SOCKET_TIMEOUT)
        assert not ap.wait_for(lambda: ap.new_events(proxy.events), ap.NEGATIVE_GRACE), \
            f"the agent hanging up is not an upstream failure: {proxy.events}"


# ---------------------------------------------------------------------------
# AP6 — the existing events are unchanged (green today, stays green)
# ---------------------------------------------------------------------------

def test_ap6_401_keeps_its_events_and_adds_no_new_one(tmp_path, monkeypatch):
    upstream = ap.Upstream(lambda req: ap.Reply(401, [("content-type", "application/json")],
                                                b'{"error":"expired"}'))
    with ap.upstream_server(upstream) as url, \
            ap.proxy_server(tmp_path, monkeypatch, accounts={"acc": "tok"}, upstream_url=url) as proxy:
        got, _, _ = ap.request(proxy, agent=AGENT)
        assert got == 401
        assert proxy.events == [
            ("unauthenticated", {"label": "acc"}),
            ("switch", {"agent": AGENT, "away_from": "acc"}),
            ("exhausted", {"agent": AGENT, "tried": ["acc"]}),
        ]


@pytest.mark.parametrize("status", [429, 529])
def test_ap6_limits_keep_their_events_and_add_no_new_one(tmp_path, monkeypatch, status):
    upstream = ap.Upstream(lambda req: ap.Reply(status, [("retry-after", "120")], b"{}"))
    with ap.upstream_server(upstream) as url, \
            ap.proxy_server(tmp_path, monkeypatch, accounts={"acc": "tok"}, upstream_url=url) as proxy:
        got, _, _ = ap.request(proxy, agent=AGENT)
        assert got == 429
        assert proxy.events == [
            ("switch", {"agent": AGENT, "away_from": "acc"}),
            ("exhausted", {"agent": AGENT, "tried": ["acc"]}),
        ]


def test_ap6_success_emits_no_new_event(tmp_path, monkeypatch):
    with ap.upstream_server(ap.Upstream()) as url, \
            ap.proxy_server(tmp_path, monkeypatch, accounts={"acc": "tok"}, upstream_url=url) as proxy:
        assert ap.request(proxy, agent=AGENT)[0] == 200
        assert not ap.wait_for(lambda: ap.new_events(proxy.events), ap.NEGATIVE_GRACE)


def test_ap6_each_attempt_emits_at_most_one_new_event(tmp_path, monkeypatch):
    """A limit on acc-a, then a 500 on acc-b: one switch, then exactly one
    upstream_error, for acc-b."""
    def responder(req):
        if req.token == "tok-a":
            return ap.Reply(429, [("retry-after", "120")], b"{}")
        return ap.Reply(500, [("content-type", "application/json")], b"{}")

    with ap.upstream_server(ap.Upstream(responder)) as url, \
            ap.proxy_server(tmp_path, monkeypatch, accounts={"acc-a": "tok-a", "acc-b": "tok-b"},
                            upstream_url=url) as proxy:
        assert ap.request(proxy, agent=AGENT)[0] == 500
        assert ap.wait_for(lambda: ap.new_events(proxy.events))
        assert ap.new_events(proxy.events) == [
            ("upstream_error", {"agent": AGENT, "label": "acc-b", "status": 500})]
