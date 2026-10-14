"""Adversary (ag-23957f) against AP4 and AP2: the wire and the callback.

Each test FAILS on c01dc05e.
"""

import sys
from pathlib import Path

import pytest

from multiagents import authproxy  # noqa: F401  (the proxy under test)

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))
import ap_support as ap


def _responses(raw: bytes) -> int:
    return raw.count(b"HTTP/1.1 ")


# AP4: the 401 for an unsigned token (authproxy.py:457-462) returns before the
# Transfer-Encoding check and before the body is read, and leaves the
# connection open. The chunk bytes are then parsed as a second request.
# AP9 decided the order: the Transfer-Encoding check comes before the token,
# so this is a 411, and the connection is closed (tester ag-d64682).
def test_ap4_unsigned_chunked_request_does_not_leave_chunks_on_the_connection(tmp_path, monkeypatch):
    upstream = ap.Upstream()
    with ap.upstream_server(upstream) as url, \
            ap.proxy_server(tmp_path, monkeypatch, accounts={"a": "tok-a"},
                            upstream_url=url) as proxy:
        raw, closed = ap.raw_exchange(proxy.port, (
            b"POST /v1/messages HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer forged\r\n"
            b"Transfer-Encoding: chunked\r\n\r\n5\r\nhello\r\n0\r\n\r\n"))
    assert not upstream.requests
    status, headers, rest = ap.split_response(raw)
    assert status == 411
    assert closed
    # Nothing after the response's own body: today the chunk line "5" is read as a
    # request line and answered with a second (HTTP/0.9 style, HTML) error.
    assert rest[int(headers["content-length"]):] == b"", raw


# Same path with a Content-Length body: the unread body is served as the next
# request on the connection, and here it is forwarded upstream with an
# account token.
def test_ap4_unsigned_request_body_is_never_served_as_a_request(tmp_path, monkeypatch):
    upstream = ap.Upstream()
    with ap.upstream_server(upstream) as url, \
            ap.proxy_server(tmp_path, monkeypatch, accounts={"a": "tok-a"},
                            upstream_url=url) as proxy:
        inner = (f"POST /v1/messages HTTP/1.1\r\nHost: x\r\n"
                 f"Authorization: Bearer {proxy.mint('agent-inner')}\r\n"
                 f"Content-Length: 2\r\n\r\n{{}}").encode()
        raw, closed = ap.raw_exchange(proxy.port, (
            b"POST /v1/messages HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer forged\r\n"
            b"Content-Length: " + str(len(inner)).encode() + b"\r\n\r\n" + inner))
    assert _responses(raw) == 1, raw
    assert upstream.requests == []
    assert closed  # AP9: the 401 closes the connection


# AP2: `_event` catches `Exception` only (authproxy.py:436). A callback raising
# a BaseException (SystemExit, KeyboardInterrupt) kills the handler thread and
# the agent gets no response at all.
# AP10 extends AP2 to any exception: SystemExit and KeyboardInterrupt alike.
@pytest.mark.parametrize("exc_type", [SystemExit, KeyboardInterrupt])
def test_ap2_a_callback_raising_systemexit_does_not_break_the_request(tmp_path, monkeypatch,
                                                                      exc_type):
    def callback(kind, fields):
        raise exc_type("callback gave up")

    upstream = ap.Upstream()
    with ap.upstream_server(upstream) as url, \
            ap.proxy_server(tmp_path, monkeypatch, accounts={"a": "tok-a"},
                            upstream_url=url, on_event=callback) as proxy:
        status, _, _ = ap.request(proxy, target=".attacker.invalid/x")
    assert status == 400
