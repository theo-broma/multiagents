"""AP9 — refusals never leave unread bytes on the connection.

Contract: `context/specs/findings-c1-c2-part2.md`, Part 2b, "AP — adversary
findings", AP9 (with AP4 and HF6).

- every response the proxy generates before reading the request body (the
  401 for a missing or forged token, 405, 411, HF6's 400) closes the
  connection;
- the method and `Transfer-Encoding` checks come before the token check: a
  chunked request gets 411 whatever its token, an unsupported method gets
  405 even with `Transfer-Encoding`;
- a malformed, negative or repeated-and-conflicting `Content-Length` gets
  400 and the connection is closed;
- nothing after a refusal on that connection is read as a request.

Each refusal is followed on the same connection by a complete, validly
signed request. If the proxy read on, that request would reach the upstream
with an account token, and a second response would come back.
"""

import sys
from pathlib import Path

import pytest

from multiagents import redact

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))
import ap_support as ap

AGENT = "agent-ap9"


@pytest.fixture(autouse=True)
def isolated_registration(monkeypatch):
    monkeypatch.setattr(redact, "_literals", set())


def build(method="POST", target="/v1/messages", token="", headers=(), body=b""):
    lines = [f"{method} {target} HTTP/1.1", "Host: proxy"]
    if token is not None:
        lines.append(f"Authorization: Bearer {token}")
    lines += list(headers)
    return ("\r\n".join(lines) + "\r\n\r\n").encode() + body


def follower(proxy) -> bytes:
    """A valid signed request that must never be served."""
    return build(token=proxy.mint("agent-ap9-follower"), headers=["Content-Length: 2"],
                 body=b"{}")


def run(tmp_path, monkeypatch, make_first):
    """Send `make_first(proxy)` then a valid request on one connection.
    Returns (status, headers, raw, closed, upstream requests)."""
    upstream = ap.Upstream()
    with ap.upstream_server(upstream) as url, \
            ap.proxy_server(tmp_path, monkeypatch, accounts={"acc": "tok"},
                            upstream_url=url) as proxy:
        raw, closed = ap.raw_exchange(proxy.port, make_first(proxy) + follower(proxy))
        requests = list(upstream.requests)
    status, headers, _ = ap.split_response(raw)
    return status, headers, raw, closed, requests


def assert_refused_once_and_closed(raw, closed, requests):
    assert raw.count(b"HTTP/1.1 ") == 1, f"bytes after the refusal were served: {raw!r}"
    assert b"HTTP/1.0 " not in raw and b"<html" not in raw.lower(), raw
    assert closed, "the proxy must close the connection after the refusal"
    assert requests == [], "nothing may reach the upstream"


CHUNKED = b"5\r\nhello\r\n0\r\n\r\n"


# ---------------------------------------------------------------------------
# Ordering: 405, then 411, then the token
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("method,token,te,expected", [
    ("PUT", "forged", True, 405),
    ("DELETE", None, True, 405),
    ("OPTIONS", None, False, 405),
    ("PATCH", "forged", False, 405),
    ("POST", "forged", True, 411),
    ("POST", None, True, 411),
    ("GET", "forged", True, 411),
    ("HEAD", None, True, 411),
    ("POST", "forged", False, 401),
    ("GET", None, False, 401),
], ids=["put-forged-te", "delete-none-te", "options-none", "patch-forged", "post-forged-te",
        "post-none-te", "get-forged-te", "head-none-te", "post-forged", "get-none"])
def test_ap9_method_then_transfer_encoding_then_token(tmp_path, monkeypatch,
                                                      method, token, te, expected):
    headers = ["Transfer-Encoding: chunked"] if te else ["Content-Length: 5"]
    body = CHUNKED if te else b"hello"
    status, response_headers, raw, closed, requests = run(
        tmp_path, monkeypatch, lambda proxy: build(method, token=token, headers=headers,
                                                   body=body))
    assert status == expected, raw[:300]
    if expected == 405:
        assert response_headers.get("allow") == "GET, HEAD, POST"
    assert_refused_once_and_closed(raw, closed, requests)


@pytest.mark.parametrize("method", ["PUT", "DELETE", "OPTIONS"])
def test_ap9_signed_unsupported_method_with_transfer_encoding_gets_405(tmp_path, monkeypatch,
                                                                        method):
    status, _, raw, closed, requests = run(tmp_path, monkeypatch, lambda proxy: build(
        method, token=proxy.mint(AGENT), headers=["Transfer-Encoding: chunked"], body=CHUNKED))
    assert status == 405
    assert_refused_once_and_closed(raw, closed, requests)


# ---------------------------------------------------------------------------
# The 401 and HF6's 400 close the connection
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("token", ["forged", None, "", "x.y.z"],
                         ids=["forged", "missing-header", "empty", "malformed"])
def test_ap9_a_401_closes_the_connection(tmp_path, monkeypatch, token):
    status, _, raw, closed, requests = run(tmp_path, monkeypatch, lambda proxy: build(
        token=token, headers=["Content-Length: 2"], body=b"{}"))
    assert status == 401
    assert_refused_once_and_closed(raw, closed, requests)


def test_ap9_a_forged_token_whose_body_is_a_signed_request_gets_one_401(tmp_path, monkeypatch):
    """The body is exactly a complete signed request; read as the next
    request, it would go upstream."""
    def first(proxy):
        inner = follower(proxy)
        return build(token="forged", headers=[f"Content-Length: {len(inner)}"], body=inner)

    upstream = ap.Upstream()
    with ap.upstream_server(upstream) as url, \
            ap.proxy_server(tmp_path, monkeypatch, accounts={"acc": "tok"},
                            upstream_url=url) as proxy:
        raw, closed = ap.raw_exchange(proxy.port, first(proxy))
        requests = list(upstream.requests)
    assert ap.split_response(raw)[0] == 401
    assert_refused_once_and_closed(raw, closed, requests)


@pytest.mark.parametrize("target", [".attacker.invalid/x", ":8443/x", "@attacker.invalid/x"])
def test_ap9_hf6_refused_target_closes_the_connection(tmp_path, monkeypatch, target):
    status, _, raw, closed, requests = run(tmp_path, monkeypatch, lambda proxy: build(
        target=target, token=proxy.mint(AGENT), headers=["Content-Length: 2"], body=b"{}"))
    assert status == 400
    assert_refused_once_and_closed(raw, closed, requests)


# ---------------------------------------------------------------------------
# Content-Length the proxy cannot trust
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("lengths", [
    ["abc"], ["1.5"], ["0x10"], ["+2"], ["2 3"], ["-1"], ["-100"],
    ["2", "3"], ["2, 3"], ["3", "2"],
], ids=["word", "decimal", "hex", "plus-sign", "two-numbers", "minus-one", "negative",
        "repeated-conflicting", "list-conflicting", "repeated-conflicting-reversed"])
@pytest.mark.parametrize("method", ["POST", "GET"])
def test_ap9_untrustworthy_content_length_gets_400_and_closes(tmp_path, monkeypatch,
                                                              method, lengths):
    status, _, raw, closed, requests = run(tmp_path, monkeypatch, lambda proxy: build(
        method, token=proxy.mint(AGENT),
        headers=[f"Content-Length: {value}" for value in lengths], body=b"{}"))
    assert status == 400, raw[:300]
    assert_refused_once_and_closed(raw, closed, requests)


def test_ap9_a_valid_request_on_its_own_connection_is_still_served(tmp_path, monkeypatch):
    """Guard: the refusals above are about the bad request, not about a
    pipelined follower being refused for itself."""
    upstream = ap.Upstream()
    with ap.upstream_server(upstream) as url, \
            ap.proxy_server(tmp_path, monkeypatch, accounts={"acc": "tok"},
                            upstream_url=url) as proxy:
        raw, _ = ap.raw_exchange(proxy.port, build(
            token=proxy.mint(AGENT), headers=["Content-Length: 2", "Connection: close"],
            body=b"{}"))
        assert ap.split_response(raw)[0] == 200
        assert [r.method for r in upstream.requests] == ["POST"]
