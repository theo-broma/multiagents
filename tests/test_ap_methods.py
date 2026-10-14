"""AP4 — F23: the proxy serves GET, HEAD and POST, and nothing else.

Contract: `context/specs/findings-c1-c2-part2.md`, Part 2b, AP4, with
Decision 10.

- GET, HEAD and POST go upstream with the same method and HF6's target check;
  a request with a Content-Length (even 0) is forwarded with that body, one
  without is forwarded with none;
- a Transfer-Encoding header gets 411, nothing upstream;
- any other method gets 405 with `Allow: GET, HEAD, POST`, nothing upstream
  (a deliberate change from today's 501);
- after a 405 or 411 for a request that may carry a body, the connection is
  closed (`Connection: close`);
- a HEAD response carries no body bytes, whoever produced it;
- redirects are never followed: the 3xx is passed back, scrubbed and
  filtered (no Location).

The wire is read raw where `http.client` would hide the property under test
(HEAD bodies, chunked requests, whether the connection closes).
"""

import http.client
import json
import sys
from pathlib import Path

import pytest

from multiagents import redact

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))
import ap_support as ap

AGENT = "agent-ap4"
LITERAL = "ap-four-registered-sensitive-value"


@pytest.fixture(autouse=True)
def isolated_registration(monkeypatch):
    monkeypatch.setattr(redact, "_literals", set())


def raw_request(proxy, method, target="/v1/messages", headers=(), body=b"", close=True,
                signed=True):
    lines = [f"{method} {target} HTTP/1.1", "Host: proxy"]
    if signed:
        lines.append(f"Authorization: Bearer {proxy.mint(AGENT)}")
    lines += list(headers)
    if close:
        lines.append("Connection: close")
    return ("\r\n".join(lines) + "\r\n\r\n").encode() + body


# ---------------------------------------------------------------------------
# Served methods arrive upstream unchanged
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("method", ["GET", "HEAD", "POST"])
@pytest.mark.parametrize("body", ["with-body", "empty-body", "no-body"])
def test_ap4_served_method_arrives_upstream_with_the_same_method_and_body(
        tmp_path, monkeypatch, method, body):
    upstream = ap.Upstream()
    with ap.upstream_server(upstream) as url, \
            ap.proxy_server(tmp_path, monkeypatch, accounts={"acc": "tok"}, upstream_url=url) as proxy:
        headers = {"with-body": ["Content-Length: 11"], "empty-body": ["Content-Length: 0"],
                   "no-body": []}[body]
        payload = b'{"msg":"x"}' if body == "with-body" else b""
        raw, _ = ap.raw_exchange(proxy.port, raw_request(proxy, method, "/v1/messages?beta=true",
                                                          headers, payload))
        status, _, _ = ap.split_response(raw)
        assert status == 200, raw[:200]
        assert len(upstream.requests) == 1
        got = upstream.requests[0]
        assert (got.method, got.path, got.token) == (method, "/v1/messages?beta=true", "tok")
        assert got.body == payload
        assert "transfer-encoding" not in got.headers
        if body == "with-body":
            assert got.headers.get("content-length") == "11"
        elif body == "empty-body":
            assert got.headers.get("content-length") == "0", \
                "a Content-Length of 0 is forwarded as that (empty) body"
        elif method in ("GET", "HEAD"):
            assert "content-length" not in got.headers, \
                "a request without a body is forwarded with none"


@pytest.mark.parametrize("method", ["GET", "HEAD"])
@pytest.mark.parametrize("target", [".attacker.invalid/x", "@attacker.invalid/x",
                                    "http://attacker.invalid/x"])
def test_ap4_refused_target_gets_hf6s_400_for_every_served_method(
        tmp_path, monkeypatch, method, target):
    upstream = ap.Upstream()
    with ap.upstream_server(upstream) as url, \
            ap.proxy_server(tmp_path, monkeypatch, accounts={"acc": "tok"}, upstream_url=url) as proxy:
        raw, _ = ap.raw_exchange(proxy.port, raw_request(proxy, method, target))
        status, _, rest = ap.split_response(raw)
        assert status == 400
        assert upstream.requests == []
        if method == "HEAD":
            assert rest == b""


# ---------------------------------------------------------------------------
# Other methods: 405, Allow, nothing upstream, connection closed
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("method", ["PUT", "DELETE", "OPTIONS", "PATCH", "TRACE"])
def test_ap4_other_method_gets_405_with_allow_and_nothing_upstream(tmp_path, monkeypatch, method):
    upstream = ap.Upstream()
    with ap.upstream_server(upstream) as url, \
            ap.proxy_server(tmp_path, monkeypatch, accounts={"acc": "tok"}, upstream_url=url) as proxy:
        raw, closed = ap.raw_exchange(proxy.port, raw_request(
            proxy, method, "/v1/files/file_abc", ["Content-Length: 2"], b"{}", close=False))
        status, headers, _ = ap.split_response(raw)
        assert status == 405, raw[:200]
        assert {m.strip() for m in headers.get("allow", "").split(",")} == {"GET", "HEAD", "POST"}
        assert headers.get("connection", "").lower() == "close"
        assert closed, "the proxy must close the connection after a 405"
        assert upstream.requests == []


def test_ap4_bytes_after_a_405_are_never_read_as_a_request(tmp_path, monkeypatch):
    """A body that is itself a request: if the proxy read on after the 405,
    that request would go upstream with an account token."""
    upstream = ap.Upstream()
    with ap.upstream_server(upstream) as url, \
            ap.proxy_server(tmp_path, monkeypatch, accounts={"acc": "tok"}, upstream_url=url) as proxy:
        smuggled = raw_request(proxy, "POST", headers=["Content-Length: 2"], body=b"{}",
                               close=False)
        raw, closed = ap.raw_exchange(proxy.port, raw_request(
            proxy, "PUT", headers=[f"Content-Length: {len(smuggled)}"], body=smuggled,
            close=False) + smuggled)
        assert ap.split_response(raw)[0] == 405
        assert raw.count(b"HTTP/1.1 ") == 1, "a second response means bytes were read on"
        assert closed
        assert upstream.requests == []


def test_ap4_unsigned_other_method_still_sends_nothing_upstream(tmp_path, monkeypatch):
    upstream = ap.Upstream()
    with ap.upstream_server(upstream) as url, \
            ap.proxy_server(tmp_path, monkeypatch, accounts={"acc": "tok"}, upstream_url=url) as proxy:
        raw, _ = ap.raw_exchange(proxy.port, raw_request(proxy, "DELETE", signed=False))
        assert ap.split_response(raw)[0] in (401, 405)
        assert upstream.requests == []


# ---------------------------------------------------------------------------
# Transfer-Encoding: 411
# ---------------------------------------------------------------------------

CHUNKED_BODY = b"b\r\n{\"msg\":\"x\"}\r\n0\r\n\r\n"


@pytest.mark.parametrize("method", ["POST", "GET"])
@pytest.mark.parametrize("headers", [
    ["Transfer-Encoding: chunked"],
    ["Transfer-Encoding: chunked", "Content-Length: 2"],
    ["Transfer-Encoding: gzip, chunked"],
], ids=["chunked", "chunked-and-length", "gzip-chunked"])
def test_ap4_transfer_encoding_gets_411_and_nothing_upstream(tmp_path, monkeypatch, method, headers):
    upstream = ap.Upstream()
    with ap.upstream_server(upstream) as url, \
            ap.proxy_server(tmp_path, monkeypatch, accounts={"acc": "tok"}, upstream_url=url) as proxy:
        raw, closed = ap.raw_exchange(proxy.port, raw_request(
            proxy, method, headers=headers, body=CHUNKED_BODY, close=False))
        status, response_headers, _ = ap.split_response(raw)
        assert status == 411, raw[:200]
        assert response_headers.get("connection", "").lower() == "close"
        assert closed, "the proxy must close the connection after a 411"
        assert raw.count(b"HTTP/1.1 ") == 1, "the chunk bytes were read as a second request"
        assert upstream.requests == []


# ---------------------------------------------------------------------------
# HEAD carries no body bytes, whoever produced the response
# ---------------------------------------------------------------------------

def _head(proxy, target="/v1/messages", headers=(), signed=True):
    raw, closed = ap.raw_exchange(proxy.port, raw_request(proxy, "HEAD", target, headers,
                                                          signed=signed))
    status, response_headers, rest = ap.split_response(raw)
    return status, response_headers, rest


@pytest.mark.parametrize("upstream_status,expected", [
    (200, 200), (500, 500), (400, 400), (429, 429), (529, 429), (401, 401)],
    ids=["upstream-200", "upstream-500", "upstream-400", "exhausted-429", "exhausted-529",
         "saved-401"])
def test_ap4_head_response_has_no_body_bytes(tmp_path, monkeypatch, upstream_status, expected):
    upstream = ap.Upstream(lambda req: ap.Reply(
        upstream_status, [("content-type", "application/json")], b'{"would":"be a body"}'))
    with ap.upstream_server(upstream) as url, \
            ap.proxy_server(tmp_path, monkeypatch, accounts={"acc": "tok"}, upstream_url=url) as proxy:
        status, _, rest = _head(proxy)
        assert status == expected
        assert rest == b"", f"HEAD {status} carried body bytes: {rest[:80]!r}"
        assert [r.method for r in upstream.requests] == ["HEAD"]


def test_ap4_head_on_the_proxys_own_responses_has_no_body_bytes(tmp_path, monkeypatch):
    with ap.proxy_server(tmp_path, monkeypatch, accounts={"acc": "tok"},
                         upstream_url=ap.closed_port_url()) as proxy:
        cases = {
            "unreachable-502": _head(proxy),
            "refused-target-400": _head(proxy, ".attacker.invalid/x"),
            "unsigned-401": _head(proxy, signed=False),
            "transfer-encoding-411": _head(proxy, headers=["Transfer-Encoding: chunked"]),
        }
    assert {name: c[0] for name, c in cases.items()} == {
        "unreachable-502": 502, "refused-target-400": 400, "unsigned-401": 401,
        "transfer-encoding-411": 411}
    assert {name: c[2] for name, c in cases.items()} == {name: b"" for name in cases}


def test_ap4_head_on_the_proxys_own_503_has_no_body_bytes(tmp_path, monkeypatch):
    pins = tmp_path / "pins.json"
    pins.write_text("{}")
    with ap.proxy_server(tmp_path, monkeypatch, accounts={"acc": "tok"},
                         upstream_url=ap.closed_port_url(), pins_path=pins) as proxy:
        pins.write_text("{not json")
        status, _, rest = _head(proxy)
    assert status == 503
    assert rest == b""


def test_ap4_head_with_no_accounts_has_no_body_bytes(tmp_path, monkeypatch):
    with ap.proxy_server(tmp_path, monkeypatch, accounts={},
                         upstream_url=ap.closed_port_url()) as proxy:
        status, _, rest = _head(proxy)
    assert status == 429
    assert rest == b""


# ---------------------------------------------------------------------------
# Redirects are never followed
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("method,status", [("POST", 302), ("GET", 302), ("POST", 303),
                                           ("GET", 307), ("HEAD", 301)])
def test_ap4_redirect_is_passed_back_not_followed(tmp_path, monkeypatch, method, status):
    redact.register_literal(LITERAL)

    def responder(req):
        if req.path.startswith("/followed"):
            return ap.Reply(200, [("content-type", "application/json")], b'{"followed":true}')
        return ap.Reply(status, [("location", "/followed"), ("content-type", "application/json")],
                        json.dumps({"moved": LITERAL}).encode())

    upstream = ap.Upstream(responder)
    with ap.upstream_server(upstream) as url, \
            ap.proxy_server(tmp_path, monkeypatch, accounts={"acc": "tok"}, upstream_url=url) as proxy:
        conn = http.client.HTTPConnection("127.0.0.1", proxy.port, timeout=ap.SOCKET_TIMEOUT)
        try:
            conn.request(method, "/v1/messages", body=b"{}" if method == "POST" else None,
                         headers={"Authorization": "Bearer " + proxy.mint(AGENT)})
            resp = conn.getresponse()
            body = resp.read()
            got_status, location = resp.status, resp.getheader("location")
        finally:
            conn.close()
    assert [r.path for r in upstream.requests] == ["/v1/messages"], "the redirect was followed"
    assert got_status == status
    assert location is None, "Location is not forwarded (AP5)"
    assert LITERAL.encode() not in body
    if method != "HEAD":
        assert json.loads(body) == {"moved": redact.MASK}
