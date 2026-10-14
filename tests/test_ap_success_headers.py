"""AP7 — F22 on every response: which upstream headers reach an agent.

Contract: `context/specs/findings-c1-c2-part2.md`, Part 2b, "AP — adversary
findings", AP7 (with AP5).

- AP5's forbidden list applies to every relayed response, 2xx and HEAD
  included: never `set-cookie`, `authorization`, `www-authenticate`,
  `location`, `proxy-*`, nor a name containing `organization`, `org-`,
  `workspace` or `user-profile`;
- on a 2xx every other upstream header is still forwarded; on a non-2xx
  AP5's allowlist applies as written;
- hop-by-hop headers are removed and the framing headers (`content-length`,
  `transfer-encoding`) are the proxy's;
- a forwarded header whose value contains a registered literal or an e-mail
  address is dropped, except `content-type`, which becomes
  `application/octet-stream`.

Header names are case-insensitive, so every rule is checked against names
in mixed and upper case as well. The wire is read raw so that a repeated
header and the framing are visible.
"""

import json
import sys
from pathlib import Path

import pytest

from multiagents import redact

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))
import ap_support as ap

AGENT = "agent-ap7"
LITERAL = "ap-seven-registered-sensitive-value"
EMAIL = "Bob.Smith+ap7@Mail.Example.org"
BODY = b'{"id":"msg_ap7","ok":true}'


@pytest.fixture(autouse=True)
def isolated_registration(monkeypatch):
    monkeypatch.setattr(redact, "_literals", set())


# Each value is a unique marker, so a header forwarded under any name is seen.
FORBIDDEN = [
    ("Set-Cookie", "sess=ap7-forbidden-1"),
    ("SET-COOKIE", "sess=ap7-forbidden-2"),
    ("Authorization", "Bearer ap7-forbidden-3"),
    ("WWW-Authenticate", "Bearer realm=ap7-forbidden-4"),
    ("Location", "https://elsewhere.invalid/ap7-forbidden-5"),
    ("Proxy-Authenticate", "Basic ap7-forbidden-6"),
    ("PROXY-STATUS", "ap7-forbidden-7"),
    ("anthropic-organization-id", "ap7-forbidden-8"),
    ("Anthropic-Organization-Id", "ap7-forbidden-9"),
    ("X-Organization-Name", "ap7-forbidden-10"),
    ("x-org-id", "ap7-forbidden-11"),
    ("anthropic-workspace-id", "ap7-forbidden-12"),
    ("Anthropic-Workspace-Id", "ap7-forbidden-13"),
    ("x-workspace-name", "ap7-forbidden-14"),
    ("x-user-profile-id", "ap7-forbidden-15"),
    ("X-User-Profile-Id", "ap7-forbidden-16"),
    ("anthropic-ratelimit-organization-id", "ap7-forbidden-17"),
    ("anthropic-ratelimit-workspace-tokens-limit", "ap7-forbidden-18"),
    ("anthropic-ratelimit-user-profile-requests", "ap7-forbidden-19"),
]

# Not forbidden: forwarded on a 2xx.
KEPT = [
    ("request-id", "req_ap7_kept"),
    ("anthropic-ratelimit-requests-remaining", "49"),
    ("Anthropic-RateLimit-Tokens-Reset", "2026-10-13T12:00:00Z"),
    ("x-ap7-custom", "ap7-custom-kept"),
    ("cache-control", "no-cache"),
]


def forbidden_name(name: str) -> bool:
    name = name.lower()
    return (name in ("set-cookie", "authorization", "www-authenticate", "location")
            or name.startswith("proxy-")
            or any(part in name for part in ("organization", "org-", "workspace",
                                             "user-profile")))


def raw_request(proxy, method, target="/v1/messages"):
    lines = [f"{method} {target} HTTP/1.1", "Host: proxy", "Connection: close",
             f"Authorization: Bearer {proxy.mint(AGENT)}"]
    body = b""
    if method == "POST":
        lines.append("Content-Length: 2")
        body = b"{}"
    return ("\r\n".join(lines) + "\r\n\r\n").encode() + body


def exchange(tmp_path, monkeypatch, reply, method="POST"):
    """One request through the proxy against an upstream answering `reply`.
    Returns (status, headers as a list of lowercased-name pairs, rest)."""
    upstream = ap.Upstream(lambda req: reply)
    with ap.upstream_server(upstream) as url, \
            ap.proxy_server(tmp_path, monkeypatch, accounts={"acc": "tok"},
                            upstream_url=url) as proxy:
        raw, _ = ap.raw_exchange(proxy.port, raw_request(proxy, method))
    head, _, rest = raw.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    status = int(lines[0].split(" ")[1]) if lines[0].startswith("HTTP/") else 0
    headers = [(n.strip().lower(), v.strip())
               for n, _, v in (line.partition(":") for line in lines[1:])]
    return status, headers, rest


def unframe(headers, rest: bytes) -> bytes:
    """The body as the response's own framing delimits it; anything left
    over after it fails the test."""
    names = [n for n, _ in headers]
    assert names.count("content-length") <= 1, headers
    assert names.count("transfer-encoding") <= 1, headers
    assert not ("content-length" in names and "transfer-encoding" in names), headers
    values = dict(headers)
    if values.get("transfer-encoding", "").lower() == "chunked":
        body = b""
        while True:
            size_line, _, rest = rest.partition(b"\r\n")
            size = int(size_line.split(b";")[0], 16)
            if size == 0:
                assert rest == b"\r\n", f"bytes after the last chunk: {rest!r}"
                return body
            body += rest[:size]
            assert rest[size:size + 2] == b"\r\n"
            rest = rest[size + 2:]
    assert "transfer-encoding" not in values, headers
    length = int(values["content-length"])
    assert len(rest) == length, f"declared {length}, sent {len(rest)}"
    return rest


def ok_reply(extra, body=BODY, status=200, content_type="application/json"):
    return ap.Reply(status, [("content-type", content_type), *extra], body)


# ---------------------------------------------------------------------------
# The forbidden list on a 2xx, HEAD included
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("method", ["POST", "GET", "HEAD"])
@pytest.mark.parametrize("status", [200, 201])
def test_ap7_forbidden_headers_never_reach_the_agent_on_a_2xx(tmp_path, monkeypatch,
                                                              method, status):
    got, headers, _ = exchange(tmp_path, monkeypatch,
                               ok_reply(FORBIDDEN, status=status), method)
    assert got == status
    leaked = [(n, v) for n, v in headers if forbidden_name(n)]
    assert leaked == [], f"{method} {status} forwarded {leaked}"
    for name, value in FORBIDDEN:
        assert all(value not in v for _, v in headers), f"{name}'s value was forwarded"


@pytest.mark.parametrize("method", ["POST", "GET", "HEAD"])
def test_ap7_other_upstream_headers_are_still_forwarded_on_a_2xx(tmp_path, monkeypatch, method):
    got, headers, _ = exchange(tmp_path, monkeypatch, ok_reply([*KEPT, *FORBIDDEN]), method)
    assert got == 200
    for name, value in KEPT:
        assert (name.lower(), value) in headers, f"{name} was not forwarded: {headers}"
    assert ("content-type", "application/json") in headers


def test_ap7_forbidden_names_on_a_non_2xx_are_still_not_forwarded(tmp_path, monkeypatch):
    """The new `workspace` and `user-profile` rules apply under AP5's
    allowlist too: an `anthropic-ratelimit-` name carrying them is dropped."""
    allowed = [("request-id", "req_ap7_err"), ("anthropic-ratelimit-requests-limit", "50")]
    got, headers, _ = exchange(tmp_path, monkeypatch, ok_reply(
        [*allowed, *FORBIDDEN, ("x-ap7-custom", "ap7-custom-err")], body=b'{"error":"x"}',
        status=500))
    assert got == 500
    for pair in allowed:
        assert pair in headers, f"{pair} was not forwarded: {headers}"
    assert [n for n, _ in headers if forbidden_name(n)] == []
    assert all("ap7-custom-err" not in v for _, v in headers), "AP5's allowlist holds on a 500"


# ---------------------------------------------------------------------------
# Hop-by-hop and framing stay the proxy's
# ---------------------------------------------------------------------------

HOP = [("Connection", "keep-alive, x-ap7-hop"), ("Keep-Alive", "timeout=777, max=7"),
       ("Upgrade", "ap7-upgrade/1"), ("Proxy-Connection", "ap7-proxy-connection")]


@pytest.mark.parametrize("method", ["POST", "GET"])
def test_ap7_hop_by_hop_headers_are_not_forwarded_on_a_2xx(tmp_path, monkeypatch, method):
    got, headers, rest = exchange(tmp_path, monkeypatch, ok_reply(HOP), method)
    assert got == 200
    for name, value in HOP:
        assert all(value not in v for _, v in headers), f"upstream's {name} was forwarded"
    assert "keep-alive" not in dict(headers)
    assert "upgrade" not in dict(headers)
    assert unframe(headers, rest) == BODY


def test_ap7_framing_headers_on_a_2xx_are_the_proxys(tmp_path, monkeypatch):
    """Upstream's framing headers (its Content-Length, and here a stray
    Transfer-Encoding) never reach the agent beside the proxy's own, and the
    framing the agent reads delimits exactly upstream's body."""
    got, headers, rest = exchange(tmp_path, monkeypatch,
                                  ok_reply([("Transfer-Encoding", "gzip")]))
    assert got == 200
    assert ("transfer-encoding", "gzip") not in headers
    assert unframe(headers, rest) == BODY


SSE = (b"event: message_start\ndata: {\"type\":\"message_start\"}\n\n"
       b"event: content_block_delta\ndata: {\"type\":\"content_block_delta\",\"delta\":"
       b"{\"text\":\"hi\"}}\n\n"
       b"event: message_stop\ndata: {\"type\":\"message_stop\"}\n\n")


def test_ap7_sse_keeps_its_stream_headers_and_drops_the_forbidden_ones(tmp_path, monkeypatch):
    reply = ok_reply([("cache-control", "no-cache"), *KEPT[:2], *FORBIDDEN, *HOP],
                     body=SSE, content_type="text/event-stream; charset=utf-8")
    got, headers, rest = exchange(tmp_path, monkeypatch, reply)
    assert got == 200
    assert ("content-type", "text/event-stream; charset=utf-8") in headers
    assert ("cache-control", "no-cache") in headers
    for pair in KEPT[:2]:
        assert (pair[0].lower(), pair[1]) in headers
    assert [n for n, _ in headers if forbidden_name(n)] == []
    for name, value in HOP:
        assert all(value not in v for _, v in headers), f"upstream's {name} was forwarded"
    assert unframe(headers, rest) == SSE, "the stream must arrive byte for byte"


# ---------------------------------------------------------------------------
# A forwarded value carrying a literal or an e-mail address
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("method", ["POST", "GET", "HEAD"])
@pytest.mark.parametrize("secret", ["literal", "email"])
def test_ap7_a_2xx_header_whose_value_carries_a_secret_is_dropped(tmp_path, monkeypatch,
                                                                   method, secret):
    redact.register_literal(LITERAL)
    value = LITERAL if secret == "literal" else EMAIL
    reply = ap.Reply(200, [
        ("content-type", f"application/json; profile={value}"),
        ("request-id", value),
        ("x-ap7-note", f"issued for <{value}>, see docs"),
        ("anthropic-ratelimit-requests-remaining", f"49; {value}"),
        ("x-ap7-clean", "ap7-clean-kept"),
    ], BODY)
    got, headers, _ = exchange(tmp_path, monkeypatch, reply, method)
    assert got == 200
    assert all(value not in v for _, v in headers), headers
    names = [n for n, _ in headers]
    for name in ("request-id", "x-ap7-note", "anthropic-ratelimit-requests-remaining"):
        assert name not in names, f"{name} carried {secret} and must be dropped, not rewritten"
    assert ("content-type", "application/octet-stream") in headers
    assert names.count("content-type") == 1
    assert ("x-ap7-clean", "ap7-clean-kept") in headers, "a clean header is still forwarded"


@pytest.mark.parametrize("secret", ["literal", "email"])
def test_ap7_an_error_header_whose_value_carries_a_secret_is_dropped(tmp_path, monkeypatch,
                                                                      secret):
    redact.register_literal(LITERAL)
    value = LITERAL if secret == "literal" else EMAIL
    reply = ap.Reply(500, [("content-type", f"text/plain; owner={value}"),
                           ("request-id", value),
                           ("x-request-id", "xreq_ap7_clean")], b"upstream broke")
    got, headers, rest = exchange(tmp_path, monkeypatch, reply)
    assert got == 500
    assert all(value not in v for _, v in headers), headers
    assert "request-id" not in [n for n, _ in headers]
    assert ("x-request-id", "xreq_ap7_clean") in headers
    assert ("content-type", "application/octet-stream") in headers
    assert unframe(headers, rest) == b"upstream broke"


def test_ap7_a_clean_value_with_an_at_sign_that_is_not_an_address_is_kept(tmp_path, monkeypatch):
    """`user@host` without a dotted domain is not an e-mail address (AP1),
    so a header carrying it is forwarded as is."""
    got, headers, _ = exchange(tmp_path, monkeypatch, ok_reply([("x-ap7-ref", "build@ci")]))
    assert got == 200
    assert ("x-ap7-ref", "build@ci") in headers


def test_ap7_json_body_of_a_2xx_is_relayed_unaltered(tmp_path, monkeypatch):
    """Header filtering must not touch the success body (it is relayed raw)."""
    body = json.dumps({"content": [{"type": "text", "text": "x" * 5000}]}).encode()
    got, headers, rest = exchange(tmp_path, monkeypatch, ok_reply(FORBIDDEN, body=body))
    assert got == 200
    assert unframe(headers, rest) == body
