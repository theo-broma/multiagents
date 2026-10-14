"""AP17 — request bodies are bounded.

Contract: `context/specs/findings-c1-c2-part2.md`, Part 2b, "AP — third
adversary pass", AP17, and Decision 19 (the 64 MiB bound sits above the
upstream API's own limit; the 60 s idle bound is for the request body only).

- `Content-Length` above 64 MiB (67108864): `413`, connection closed, before
  any body byte is read and with nothing sent upstream;
- a body that ends (close or half-close) before `Content-Length` bytes
  arrived: `400`, closed, nothing upstream;
- a body during which no byte arrives for 60 s: `408`, closed, nothing
  upstream, the worker released;
- memory for a body never exceeds what was received plus a constant.

Seam assumed (named by this suite, not by the spec): the idle bound is the
module-level constant `authproxy.BODY_IDLE_TIMEOUT`, in seconds, read when a
body is being received. The tests shorten it with `monkeypatch` instead of
waiting 60 s. Memory is measured with `tracemalloc` (deterministic Python
allocations in this process, where the proxy runs), not RSS.
"""

import socket
import sys
import threading
import time
import tracemalloc
from pathlib import Path

import pytest

from multiagents import authproxy, redact

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))
import ap_support as ap

AGENT = "agent-ap17"
MAX_BODY = 64 * 1024 * 1024
# A correct proxy answers these at once; a red one blocks reading the body.
ANSWER_WAIT = 1.0
SHORT_IDLE = 0.3              # the shortened BODY_IDLE_TIMEOUT
IDLE_ANSWER_WAIT = SHORT_IDLE + 1.0
# Far below the 64 MiB a pre-allocating reader would take, far above the
# few hundred KiB a streaming reader and the test's own bookkeeping use.
MEMORY_CEILING = 8 * 1024 * 1024


@pytest.fixture(autouse=True)
def isolated_registration(monkeypatch):
    monkeypatch.setattr(redact, "_literals", set())


def head(proxy, length, method="POST", extra=""):
    return (f"{method} /v1/messages HTTP/1.1\r\nHost: proxy\r\n"
            f"Authorization: Bearer {proxy.mint(AGENT)}\r\n"
            f"Content-Length: {length}\r\n{extra}\r\n").encode()


def follower(proxy) -> bytes:
    """A complete signed request: if read after a refusal, it goes upstream."""
    return (f"POST /v1/messages HTTP/1.1\r\nHost: proxy\r\n"
            f"Authorization: Bearer {proxy.mint('agent-ap17-follower')}\r\n"
            "Content-Length: 2\r\nConnection: close\r\n\r\n{}").encode()


def read_until_closed(sock: socket.socket, wait: float) -> tuple[bytes, bool]:
    received, deadline = b"", time.monotonic() + wait
    while True:
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


def half_close_exchange(port: int, data: bytes, wait: float = ANSWER_WAIT):
    """Send, half-close (SHUT_WR), read until the proxy closes or `wait` passes."""
    sock = socket.create_connection(("127.0.0.1", port), timeout=ap.SOCKET_TIMEOUT)
    try:
        sock.sendall(data)
        sock.shutdown(socket.SHUT_WR)
        return read_until_closed(sock, wait)
    finally:
        sock.close()


@pytest.fixture
def world(tmp_path, monkeypatch):
    upstream = ap.Upstream()
    with ap.upstream_server(upstream) as url, \
            ap.proxy_server(tmp_path, monkeypatch, accounts={"acc": "tok-ap17"},
                            upstream_url=url) as proxy:
        proxy.upstream = upstream
        yield proxy


def assert_refused(raw, closed, upstream, expected):
    assert ap.split_response(raw)[0] == expected, raw[:300]
    assert raw.count(b"HTTP/1.1 ") == 1, f"more than one response: {raw!r}"
    assert closed, f"the proxy must close the connection after the {expected}"
    assert upstream.requests == [], "nothing may reach the upstream"


# ---------------------------------------------------------------------------
# 413 above 64 MiB, before any body byte is read
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("method", ["POST", "GET"])
def test_ap17_content_length_one_above_64_mib_gets_413_at_once(world, method):
    """No body byte is sent at all: the answer cannot wait for one."""
    raw, closed = ap.raw_exchange(world.port, head(world, MAX_BODY + 1, method),
                                  wait=ANSWER_WAIT)
    assert_refused(raw, closed, world.upstream, 413)


def test_ap17_bytes_after_a_413_are_never_read_as_a_request(world):
    """What follows the head is the refused body, never a request: a signed
    request placed there must not reach the upstream."""
    raw, closed = ap.raw_exchange(world.port, head(world, MAX_BODY + 1) + follower(world),
                                  wait=ANSWER_WAIT)
    assert_refused(raw, closed, world.upstream, 413)


def test_ap17_eight_gigabytes_declared_gets_413_at_once_without_memory_growth(world):
    started = not tracemalloc.is_tracing()
    if started:
        tracemalloc.start()
    try:
        tracemalloc.reset_peak()
        raw, closed = ap.raw_exchange(world.port, head(world, 8_000_000_000) + b"{}",
                                      wait=ANSWER_WAIT)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        if started:
            tracemalloc.stop()
    assert_refused(raw, closed, world.upstream, 413)
    assert peak < MEMORY_CEILING, f"{peak} bytes allocated for a 2-byte body"


# ---------------------------------------------------------------------------
# 400 for a body that ends early
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("declared,sent", [(100, b"{}"), (2, b""), (MAX_BODY, b"{}")],
                         ids=["100-then-2", "2-then-none", "exactly-64-mib-then-2"])
def test_ap17_a_body_ending_at_half_close_gets_400_with_nothing_upstream(world, declared, sent):
    """The 64 MiB case is the boundary: at the bound, not above it, so 400
    for the short body and not 413."""
    raw, closed = half_close_exchange(world.port, head(world, declared) + sent)
    assert_refused(raw, closed, world.upstream, 400)


def test_ap17_a_body_ending_at_close_sends_nothing_upstream(world):
    with socket.create_connection(("127.0.0.1", world.port), timeout=ap.SOCKET_TIMEOUT) as sock:
        sock.sendall(head(world, 100) + b"{}")
    assert not ap.wait_for(lambda: world.upstream.requests, ap.NEGATIVE_GRACE), \
        f"a truncated body was forwarded: {world.upstream.requests[0].body!r}"
    # The proxy still serves.
    status, _, _ = ap.request(world)
    assert status == 200


def test_ap17_memory_for_a_body_follows_what_was_received(world):
    """64 MiB declared (allowed), 2 bytes sent: no 64 MiB buffer."""
    started = not tracemalloc.is_tracing()
    if started:
        tracemalloc.start()
    try:
        tracemalloc.reset_peak()
        raw, closed = half_close_exchange(world.port, head(world, MAX_BODY) + b"{}")
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        if started:
            tracemalloc.stop()
    assert peak < MEMORY_CEILING, f"{peak} bytes allocated for a 2-byte body"
    assert ap.split_response(raw)[0] == 400, raw[:300]


def test_ap17_a_complete_body_of_the_declared_length_is_still_forwarded(world):
    """Guard: the bounds refuse short bodies, not whole ones."""
    payload = b'{"k":"' + b"x" * 70000 + b'"}'
    raw, _ = half_close_exchange(world.port, head(world, len(payload)) + payload)
    assert ap.split_response(raw)[0] == 200, raw[:300]
    assert [r.body for r in world.upstream.requests] == [payload]


# ---------------------------------------------------------------------------
# 408 for an idle body
# ---------------------------------------------------------------------------

def test_ap17_the_idle_bound_is_60_seconds():
    """The seam this suite assumes, at the contract's value."""
    assert getattr(authproxy, "BODY_IDLE_TIMEOUT", None) == 60


def test_ap17_a_stalled_body_gets_408_and_the_worker_is_released(world, monkeypatch):
    monkeypatch.setattr(authproxy, "BODY_IDLE_TIMEOUT", SHORT_IDLE, raising=False)
    baseline = threading.active_count()
    sock = socket.create_connection(("127.0.0.1", world.port), timeout=ap.SOCKET_TIMEOUT)
    try:
        sock.sendall(head(world, 100) + b"{}")
        raw, closed = read_until_closed(sock, IDLE_ANSWER_WAIT)
        # The agent still holds its end open: the worker must not need it closed.
        released = ap.wait_for(lambda: threading.active_count() <= baseline, ap.EVENT_WAIT)
    finally:
        sock.close()
    assert_refused(raw, closed, world.upstream, 408)
    assert released, "the proxy's worker for the stalled request is still alive"
    status, _, _ = ap.request(world)
    assert status == 200, "the proxy still serves after a stalled body"


def test_ap17_the_idle_bound_is_per_byte_not_a_total_deadline(world, monkeypatch):
    """A body trickling in, each byte inside the bound, the whole well past
    it, is received and forwarded."""
    monkeypatch.setattr(authproxy, "BODY_IDLE_TIMEOUT", 0.4, raising=False)
    payload = b'{"k":1}'
    sock = socket.create_connection(("127.0.0.1", world.port), timeout=ap.SOCKET_TIMEOUT)
    try:
        sock.sendall(head(world, len(payload), extra="Connection: close\r\n"))
        for byte in payload:
            threading.Event().wait(0.1)
            sock.sendall(bytes([byte]))
        raw, _ = read_until_closed(sock, ANSWER_WAIT)
    finally:
        sock.close()
    assert ap.split_response(raw)[0] == 200, raw[:300]
    assert [r.body for r in world.upstream.requests] == [payload]
