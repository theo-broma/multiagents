"""AP13 — framing of bodiless responses and of a request too large to read.

Contract: `context/specs/findings-c1-c2-part2.md`, Part 2b, "AP — second
adversary pass", AP13 (with AP9).

- a 204 or 304 relayed to the agent carries no body bytes and no chunked
  terminator, so a second request on the same connection gets its own
  response intact;
- a `Content-Length` too large to read is refused and the connection is
  closed; nothing after it is read as a request. AP13 said 400; AP17 (third
  adversary pass) supersedes it: a well-formed length above 64 MiB gets 413.

The client side is raw sockets: `http.client` would skip stray bytes after
a 204 and hide exactly what is being tested.
"""

import socket
import sys
import time
from pathlib import Path

import pytest

from multiagents import redact

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))
import ap_support as ap

AGENT = "agent-ap13"
SECOND_BODY = b'{"id":"msg_ap13_second","ok":true}'


@pytest.fixture(autouse=True)
def isolated_registration(monkeypatch):
    monkeypatch.setattr(redact, "_literals", set())


def build(proxy, method, target, headers=(), body=b"", close=False, token=None):
    lines = [f"{method} {target} HTTP/1.1", "Host: proxy",
             f"Authorization: Bearer {token or proxy.mint(AGENT)}", *headers]
    if body or method == "POST":
        lines.append(f"Content-Length: {len(body)}")
    if close:
        lines.append("Connection: close")
    return ("\r\n".join(lines) + "\r\n\r\n").encode() + body


def recv_until(sock, predicate, wait=ap.CLOSE_WAIT) -> tuple[bytes, bool]:
    """Read until `predicate(received)` holds, the peer closes, or `wait`
    passes. Returns (received, closed)."""
    received = b""
    deadline = time.monotonic() + wait
    while not predicate(received):
        left = deadline - time.monotonic()
        if left <= 0:
            return received, False
        sock.settimeout(left)
        try:
            chunk = sock.recv(65536)
        except socket.timeout:
            return received, False
        except ConnectionResetError:
            return received, True
        if not chunk:
            return received, True
        received += chunk
    return received, False


def body_of(headers: dict, rest: bytes) -> bytes:
    """The body as its own framing delimits it; a byte left over fails."""
    if headers.get("transfer-encoding", "").lower() == "chunked":
        body = b""
        while True:
            size_line, _, rest = rest.partition(b"\r\n")
            size = int(size_line.split(b";")[0], 16)
            if size == 0:
                assert rest == b"\r\n", f"bytes after the last chunk: {rest!r}"
                return body
            body += rest[:size]
            rest = rest[size + 2:]
    assert len(rest) == int(headers["content-length"]), rest
    return rest


def two_on_one_connection(tmp_path, monkeypatch, first_reply, method="POST", headers=()):
    """Send a request answered by `first_reply`, read its header block, then
    send a second request on the same socket. Returns (first status, first
    headers, every byte after the first header block, upstream requests)."""
    def responder(req):
        if req.path == "/v1/first":
            return first_reply
        return ap.Reply(200, [("content-type", "application/json")], SECOND_BODY)

    upstream = ap.Upstream(responder)
    with ap.upstream_server(upstream) as url, \
            ap.proxy_server(tmp_path, monkeypatch, accounts={"acc": "tok-ap13"},
                            upstream_url=url) as proxy:
        sock = socket.create_connection(("127.0.0.1", proxy.port), timeout=ap.SOCKET_TIMEOUT)
        try:
            body = b"{}" if method == "POST" else b""
            sock.sendall(build(proxy, method, "/v1/first", headers, body))
            first, closed = recv_until(sock, lambda got: b"\r\n\r\n" in got)
            assert b"\r\n\r\n" in first, f"no complete first response: {first!r}"
            assert not closed, "the connection must stay open for the second request"
            sock.sendall(build(proxy, "POST", "/v1/messages", body=b"{}", close=True))
            rest, _ = recv_until(sock, lambda got: False)
        finally:
            sock.close()
        requests = list(upstream.requests)
    raw = first + rest
    status, first_headers, after = ap.split_response(raw)
    return status, first_headers, after, requests


def assert_second_response_intact(after: bytes, requests) -> None:
    assert after.startswith(b"HTTP/1.1 "), (
        f"bytes between the first response and the second: {after[:80]!r}")
    status, headers, rest = ap.split_response(after)
    assert status == 200, after[:300]
    assert body_of(headers, rest) == SECOND_BODY
    assert [r.path for r in requests] == ["/v1/first", "/v1/messages"]


# ---------------------------------------------------------------------------
# 204 and 304 carry no body
# ---------------------------------------------------------------------------

def test_ap13_guard_a_200_then_a_second_request_on_one_connection(tmp_path, monkeypatch):
    """Guard: the connection is reusable at all, so the tests below fail on
    the bodiless framing and not on keep-alive."""
    status, headers, after, requests = two_on_one_connection(tmp_path, monkeypatch, ap.Reply(
        200, [("content-type", "application/json")], b'{"first":true}'))
    assert status == 200
    # `after` holds the first body, then the second response.
    first_body, second, rest = after.partition(b"HTTP/1.1 ")
    assert body_of(headers, first_body) == b'{"first":true}'
    assert_second_response_intact(second + rest, requests)


@pytest.mark.parametrize("method", ["POST", "GET"])
def test_ap13_a_204_carries_no_body_and_the_next_response_is_intact(tmp_path, monkeypatch,
                                                                    method):
    status, _, after, requests = two_on_one_connection(
        tmp_path, monkeypatch, ap.Reply(204, [("request-id", "req_ap13_204")], b""), method)
    assert status == 204
    assert_second_response_intact(after, requests)


@pytest.mark.parametrize("declared", [None, 42], ids=["no-length", "representation-length"])
def test_ap13_a_304_carries_no_body_and_the_next_response_is_intact(tmp_path, monkeypatch,
                                                                    declared):
    """A 304 to a conditional GET; upstream may state the representation's
    length (RFC 9110 8.6), which is still no body."""
    reply = ap.Reply(304, [("etag", '"ap13-etag"')], b"", declared_length=declared)
    status, _, after, requests = two_on_one_connection(
        tmp_path, monkeypatch, reply, "GET", headers=['If-None-Match: "ap13-etag"'])
    assert status == 304
    assert_second_response_intact(after, requests)


# ---------------------------------------------------------------------------
# A Content-Length too large to read
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("length", ["99999999999999999999999", str(2 ** 64)],
                         ids=["spec-value", "two-to-the-64"])
@pytest.mark.parametrize("method", ["POST", "GET"])
def test_ap13_ap17_a_content_length_too_large_to_read_gets_413_and_closes(
        tmp_path, monkeypatch, method, length):
    """Followed by a complete signed request: it must not be served.

    Reversed deliberately for AP17: was 400 under AP13; a well-formed length
    above 64 MiB is now 413."""
    upstream = ap.Upstream()
    with ap.upstream_server(upstream) as url, \
            ap.proxy_server(tmp_path, monkeypatch, accounts={"acc": "tok-ap13"},
                            upstream_url=url) as proxy:
        first = (f"{method} /v1/messages HTTP/1.1\r\nHost: proxy\r\n"
                 f"Authorization: Bearer {proxy.mint(AGENT)}\r\n"
                 f"Content-Length: {length}\r\n\r\n{{}}").encode()
        follower = build(proxy, "POST", "/v1/messages", body=b"{}",
                         token=proxy.mint("agent-ap13-follower"))
        raw, closed = ap.raw_exchange(proxy.port, first + follower)
        requests = list(upstream.requests)
    assert ap.split_response(raw)[0] == 413, raw[:300]
    assert raw.count(b"HTTP/1.1 ") == 1, f"bytes after the refusal were served: {raw!r}"
    assert closed, "the proxy must close the connection after the 413"
    assert requests == [], "nothing may reach the upstream"
