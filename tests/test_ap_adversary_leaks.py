"""Adversary (ag-23957f) against AP1/AP4/AP5: what still reaches an agent.

Each test FAILS on c01dc05e. Property under attack: no response the proxy
sends to an agent carries an e-mail address, an organisation identifier, a
registered literal, or a forbidden upstream header, on any path.
"""

import sys
from pathlib import Path

import pytest

from multiagents import authproxy, redact

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))
import ap_support as ap

LITERAL = "LITERALxSECRETx0123"
EMAIL = "alice@example.com"

FORBIDDEN = [("set-cookie", "session=abc"), ("www-authenticate", "Bearer realm=x"),
             ("location", "https://elsewhere.invalid/"), ("proxy-authenticate", "Basic"),
             ("authorization", "Bearer upstream"), ("anthropic-organization-id", "org-uuid-1"),
             ("x-organization-name", "Acme")]


@pytest.fixture(autouse=True)
def _isolated_literals(monkeypatch):
    monkeypatch.setattr(redact, "_literals", set())


# AP5 (+ AP4 "HEAD"), now AP7: the success relay copies every upstream header except
# transfer-encoding/connection/content-length (authproxy.py:576-578), so the
# header filter of `_error_response` is bypassed on every 2xx. The real model
# API sends `anthropic-organization-id` on successful responses.
@pytest.mark.parametrize("method", ["POST", "GET", "HEAD"])
def test_ap5_success_response_carries_no_forbidden_header(tmp_path, monkeypatch, method):
    upstream = ap.Upstream(lambda req: ap.Reply(
        200, [("content-type", "application/json")] + FORBIDDEN, b'{"ok":true}'))
    with ap.upstream_server(upstream) as url, \
            ap.proxy_server(tmp_path, monkeypatch, accounts={"a": "tok-a"},
                            upstream_url=url) as proxy:
        status, headers, _ = ap.request(proxy, method=method,
                                        body=b"{}" if method == "POST" else None)
    assert status == 200
    leaked = sorted(set(headers) & {name for name, _ in FORBIDDEN})
    assert leaked == [], f"{method} 200 forwarded {leaked}"


# AP5/AP1: the headers that ARE forwarded on an error keep upstream's values
# verbatim (authproxy.py:628-629, 622), so a registered literal or an e-mail
# address in one of them reaches the agent.
# AP7 decided how: such a header is dropped, and a content-type carrying one
# is replaced by application/octet-stream (tester ag-d64682, 2026-10-13).
@pytest.mark.parametrize("name", ["request-id", "x-request-id", "anthropic-ratelimit-unified-status",
                                  "content-type"])
@pytest.mark.parametrize("value", [LITERAL, EMAIL])
def test_ap5_forwarded_error_header_values_are_scrubbed(tmp_path, monkeypatch, name, value):
    redact.register_literal(LITERAL)
    sent = f"text/plain; note={value}" if name == "content-type" else value
    upstream = ap.Upstream(lambda req: ap.Reply(500, [(name, sent)], b"upstream broke"))
    with ap.upstream_server(upstream) as url, \
            ap.proxy_server(tmp_path, monkeypatch, accounts={"a": "tok-a"},
                            upstream_url=url) as proxy:
        status, headers, body = ap.request(proxy)
    assert status == 500
    assert value not in " ".join(f"{k}: {v}" for k, v in headers.items())
    if name == "content-type":
        assert headers.get("content-type") == "application/octet-stream"
    else:
        assert name not in headers, f"{name} must be dropped, not rewritten"


# AP1 vs HF1.2, now AP8: e-mail masking runs BEFORE literal masking (authproxy.py:673
# and 674), so a registered literal that contains an address-shaped part is
# broken up first and its remainder is no longer recognised. A withheld
# connection string is the realistic case.
DSN = "postgres://svc_reporting:hunter2hunter2@db.internal.example.com/prod_ledger"


@pytest.mark.parametrize("body", [
    f"<html>could not connect: {DSN}</html>".encode(),
    ('{"error":{"type":"x","message":"could not connect: ' + DSN + '"}}').encode(),
])
def test_ap1_a_registered_literal_containing_an_address_is_masked_whole(body):
    redact.register_literal(DSN)
    out = authproxy._scrub(body)
    for part in ("svc_reporting", "prod_ledger"):
        assert part.encode() not in out, out


# Finding 6 (a registered literal arriving as a JSON number re-serialised
# unchanged) is accepted by Decision 15 and is not a requirement; its test was
# removed (tester ag-d64682, 2026-10-13).
