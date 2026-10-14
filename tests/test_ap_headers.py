"""AP5 — F22: which upstream headers reach an agent, and the exhausted 429.

Contract: `context/specs/findings-c1-c2-part2.md`, Part 2b, AP5, with
Decision 9.

On an upstream non-2xx passed through (including the saved 401 returned
after every account was tried), only `retry-after`, `request-id`,
`x-request-id` and `anthropic-ratelimit-*` are forwarded, minus anything
named `set-cookie`, `authorization`, `www-authenticate`, `location`,
`proxy-*`, or containing `organization` or `org-` (that exclusion wins).
`content-type`/`content-length` are the proxy's and describe the body sent.
The proxy's own exhausted 429 carries `retry-after`: whole seconds, rounded
up, to the earliest cooldown end among the limited accounts the agent may
use; none when no such account exists.

`server` and `date` are written by any HTTP server, the proxy included, so
those two are checked by value (the upstream's marker must not appear), not
by name.
"""

import json
import sys
from pathlib import Path

import pytest

from multiagents import redact

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))
import ap_support as ap

AGENT = "agent-ap5"
LITERAL = "ap-five-registered-sensitive-value"


@pytest.fixture(autouse=True)
def isolated_registration(monkeypatch):
    monkeypatch.setattr(redact, "_literals", set())

ALLOWED = [
    ("retry-after", "17"),
    ("request-id", "req_ap5_allowed"),
    ("x-request-id", "xreq_ap5_allowed"),
    ("anthropic-ratelimit-requests-limit", "50"),
    ("anthropic-ratelimit-unified-reset", "1790000000"),
    ("Anthropic-RateLimit-Tokens-Remaining", "12345"),
]

FORBIDDEN = [
    ("set-cookie", "sess=ap5-forbidden-cookie"),
    ("authorization", "Bearer ap5-forbidden-authorization"),
    ("www-authenticate", "Bearer realm=ap5-forbidden-realm"),
    ("location", "/ap5-forbidden-location"),
    ("proxy-authenticate", "Basic ap5-forbidden-proxy-auth"),
    ("proxy-status", "ap5-forbidden-proxy-status"),
    ("anthropic-organization-id", "ap5-forbidden-org-id"),
    ("x-organization-name", "ap5-forbidden-org-name"),
    ("x-org-id", "ap5-forbidden-x-org-id"),
    # Matches the allowed prefix AND the forbidden rule: not forwarded.
    ("anthropic-ratelimit-organization-id", "ap5-forbidden-ratelimit-org"),
    ("anthropic-ratelimit-org-tokens", "ap5-forbidden-ratelimit-org-dash"),
    ("x-upstream-marker", "ap5-forbidden-custom"),
    ("cf-ray", "ap5-forbidden-cf-ray"),
    ("via", "ap5-forbidden-via"),
    ("server", "ap5-forbidden-server"),
]

ERROR_BODY = json.dumps({"type": "error", "error": {"type": "x", "message": "m"}}).encode()


def _all_headers_responder(status):
    def responder(req):
        return ap.Reply(status, [*ALLOWED, *FORBIDDEN, ("content-type", "application/json")],
                        ERROR_BODY)
    return responder


def _raw_headers(raw: bytes) -> list[tuple[str, str]]:
    head = raw.partition(b"\r\n\r\n")[0].decode("latin-1").split("\r\n")[1:]
    return [(n.strip().lower(), v.strip()) for n, _, v in (line.partition(":") for line in head)]


def _post(proxy, agent=AGENT, provider=""):
    raw, _ = ap.raw_exchange(proxy.port, (
        "POST /v1/messages HTTP/1.1\r\nHost: proxy\r\nConnection: close\r\n"
        f"Authorization: Bearer {proxy.mint(agent, provider)}\r\n"
        "Content-Length: 2\r\n\r\n{}").encode())
    return raw


@pytest.mark.parametrize("status", [500, 400, 403, 404, 401],
                         ids=["500", "400", "403", "404", "saved-401"])
def test_ap5_only_the_allowed_upstream_headers_are_forwarded(tmp_path, monkeypatch, status):
    with ap.upstream_server(ap.Upstream(_all_headers_responder(status))) as url, \
            ap.proxy_server(tmp_path, monkeypatch, accounts={"acc": "tok"}, upstream_url=url) as proxy:
        raw = _post(proxy)
    got_status, _, body = ap.split_response(raw)
    assert got_status == status
    headers = _raw_headers(raw)
    names = {n for n, _ in headers}
    for name, value in ALLOWED:
        assert (name.lower(), value) in headers, f"{name} was not forwarded: {headers}"
    for name, value in FORBIDDEN:
        if name not in ("server",):
            assert name not in names, f"{name} was forwarded"
        assert all(value not in v for _, v in headers), f"{name}'s value was forwarded"


def test_ap5_redirect_headers_are_filtered_too(tmp_path, monkeypatch):
    with ap.upstream_server(ap.Upstream(_all_headers_responder(302))) as url, \
            ap.proxy_server(tmp_path, monkeypatch, accounts={"acc": "tok"}, upstream_url=url) as proxy:
        raw = _post(proxy)
    assert ap.split_response(raw)[0] == 302
    headers = _raw_headers(raw)
    assert ("request-id", "req_ap5_allowed") in headers
    for name, value in FORBIDDEN:
        assert all(value not in v for _, v in headers), f"{name}'s value was forwarded"


# ---------------------------------------------------------------------------
# content-type and content-length describe the body actually sent
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("upstream_type", ["application/json", "text/plain",
                                           "application/problem+json"])
def test_ap5_json_body_is_typed_as_utf8_json(tmp_path, monkeypatch, upstream_type):
    redact.register_literal(LITERAL)
    body = json.dumps({"error": {"message": "key ap-five-registered-sensitive-value"}},
                      indent=4).encode()
    upstream = ap.Upstream(lambda req: ap.Reply(500, [("content-type", upstream_type)], body))
    with ap.upstream_server(upstream) as url, \
            ap.proxy_server(tmp_path, monkeypatch, accounts={"acc": "tok"}, upstream_url=url) as proxy:
        raw = _post(proxy)
    status, headers, sent = ap.split_response(raw)
    assert status == 500
    assert headers.get("content-type") == "application/json; charset=utf-8"
    assert int(headers["content-length"]) == len(sent)
    assert json.loads(sent) == {"error": {"message": f"key {redact.MASK}"}}


def test_ap5_text_body_keeps_upstreams_content_type(tmp_path, monkeypatch):
    redact.register_literal(LITERAL)
    body = b"<html>error for ap-five-registered-sensitive-value</html>"
    upstream_type = "text/html; charset=iso-8859-1"
    upstream = ap.Upstream(lambda req: ap.Reply(502, [("content-type", upstream_type)], body))
    with ap.upstream_server(upstream) as url, \
            ap.proxy_server(tmp_path, monkeypatch, accounts={"acc": "tok"}, upstream_url=url) as proxy:
        raw = _post(proxy)
    status, headers, sent = ap.split_response(raw)
    assert status == 502
    assert sent == f"<html>error for {redact.MASK}</html>".encode()
    assert headers.get("content-type") == upstream_type
    assert int(headers["content-length"]) == len(sent)


def test_ap5_a_replaced_body_does_not_carry_upstreams_content_type(tmp_path, monkeypatch):
    """The redactor failing makes the proxy withhold the body (HF1.3); what
    it sends instead is the proxy's, and so is its type."""
    def broken(value, *a, **k):
        raise RuntimeError("redactor down")

    monkeypatch.setattr(redact, "scrub", broken)
    upstream_type = "text/html; charset=x-ap5-upstream"
    upstream = ap.Upstream(lambda req: ap.Reply(500, [("content-type", upstream_type)],
                                                b"<html>upstream page</html>"))
    with ap.upstream_server(upstream) as url, \
            ap.proxy_server(tmp_path, monkeypatch, accounts={"acc": "tok"}, upstream_url=url) as proxy:
        raw = _post(proxy)
    status, headers, sent = ap.split_response(raw)
    assert status == 500
    assert b"upstream page" not in sent
    assert headers.get("content-type") not in (None, upstream_type)
    assert int(headers["content-length"]) == len(sent)


# ---------------------------------------------------------------------------
# The exhausted 429's retry-after (Decision 9)
# ---------------------------------------------------------------------------

def _limited(seconds):
    return ap.Reply(429, [("retry-after", str(seconds))], b"{}")


def test_ap5_exhausted_429_carries_retry_after_to_the_earliest_cooldown_end(tmp_path, monkeypatch):
    ap.FakeClock(monkeypatch)
    upstream = ap.Upstream(lambda req: _limited(7200 if req.token == "tok-a" else 120))
    with ap.upstream_server(upstream) as url, \
            ap.proxy_server(tmp_path, monkeypatch, accounts={"acc-a": "tok-a", "acc-b": "tok-b"},
                            upstream_url=url) as proxy:
        status, headers, _ = ap.request(proxy, agent=AGENT)
    assert status == 429
    assert headers.get("retry-after") == "120"


def test_ap5_exhausted_429_retry_after_is_rounded_up(tmp_path, monkeypatch):
    clock = ap.FakeClock(monkeypatch)
    upstream = ap.Upstream(lambda req: _limited(120))
    with ap.upstream_server(upstream) as url, \
            ap.proxy_server(tmp_path, monkeypatch, accounts={"acc": "tok"}, upstream_url=url) as proxy:
        assert ap.request(proxy, agent=AGENT)[0] == 429
        clock.advance(59.5)
        status, headers, _ = ap.request(proxy, agent=AGENT)
        assert len(upstream.requests) == 1
    assert status == 429
    # 120 - 59.5 - (a few real milliseconds) rounds up to 61, never down to 60.
    assert headers.get("retry-after") == "61"


def test_ap5_exhausted_429_retry_after_with_little_time_left_is_at_least_1(tmp_path, monkeypatch):
    clock = ap.FakeClock(monkeypatch)
    upstream = ap.Upstream(lambda req: _limited(120))
    with ap.upstream_server(upstream) as url, \
            ap.proxy_server(tmp_path, monkeypatch, accounts={"acc": "tok"}, upstream_url=url) as proxy:
        assert ap.request(proxy, agent=AGENT)[0] == 429
        clock.advance(119.9)
        status, headers, _ = ap.request(proxy, agent=AGENT)
    assert status == 429, "0.1 s of the cooldown is left"
    assert headers.get("retry-after") == "1"


def test_ap5_exhausted_429_counts_only_accounts_the_agent_may_use(tmp_path, monkeypatch):
    """acc-a is pinned to provider `prov`, so an agent without that provider
    may use acc-b only; acc-a's shorter cooldown is not its answer."""
    ap.FakeClock(monkeypatch)
    upstream = ap.Upstream(lambda req: _limited(60 if req.token == "tok-a" else 7200))
    with ap.upstream_server(upstream) as url, \
            ap.proxy_server(tmp_path, monkeypatch, accounts={"acc-a": "tok-a", "acc-b": "tok-b"},
                            upstream_url=url, pins={"prov": "acc-a"}) as proxy:
        pinned, pinned_headers, _ = ap.request(proxy, agent="agent-prov", provider="prov")
        status, headers, _ = ap.request(proxy, agent=AGENT)
    assert upstream.tokens() == ["tok-a", "tok-b"]
    assert (pinned, pinned_headers.get("retry-after")) == (429, "60")
    assert status == 429
    assert headers.get("retry-after") == "7200"


def test_ap5_exhausted_429_without_any_limited_account_has_no_retry_after(tmp_path, monkeypatch):
    with ap.proxy_server(tmp_path, monkeypatch, accounts={},
                         upstream_url=ap.closed_port_url()) as proxy:
        status, headers, _ = ap.request(proxy, agent=AGENT)
    assert status == 429
    assert "retry-after" not in headers


def test_ap5_no_retry_after_when_every_account_was_rejected(tmp_path, monkeypatch):
    upstream = ap.Upstream(lambda req: ap.Reply(401, [("content-type", "application/json"),
                                                      ("request-id", "req_401")],
                                                b'{"error":"expired"}'))
    with ap.upstream_server(upstream) as url, \
            ap.proxy_server(tmp_path, monkeypatch, accounts={"acc-a": "tok-a", "acc-b": "tok-b"},
                            upstream_url=url) as proxy:
        first = ap.request(proxy, agent=AGENT)
        again = ap.request(proxy, agent="agent-later")
    assert first[0] == 401
    assert "retry-after" not in first[1]
    assert again[0] != 200
    assert "retry-after" not in again[1]
