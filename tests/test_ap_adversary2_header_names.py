"""AP7 / AP5, second adversary pass: an account identifier in a header NAME.

Contract: `context/specs/findings-c1-c2-part2.md`, AP5 and AP7 (headers on
every response), with the task's property that no registered literal or
e-mail address reaches an agent in any byte, "any header name or value".

`authproxy._response_headers` drops a forwarded header whose *value* holds a
registered literal or an e-mail address, and drops by *name* only for the
fixed forbidden fragments. The name itself is never checked against the
literals or the e-mail shape, so:

- on a 2xx (POST, GET, HEAD) every upstream header name is forwarded, and a
  name that embeds a registered literal or an address goes to the agent
  verbatim;
- on a non-2xx, the allowlisted prefix `anthropic-ratelimit-` admits any
  suffix, so `anthropic-ratelimit-<address>` goes to the agent too.

The upstream is the local fake in `tests/support/ap_support.py`; the wire is
read raw so the header block is checked byte for byte.
"""

import sys
from pathlib import Path

import pytest

from multiagents import redact

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))
import ap_support as ap

LITERAL = "acct-adv2-registered-literal-0001"
EMAIL = "owner.adv2@corp.example.com"


@pytest.fixture(autouse=True)
def isolated_registration(monkeypatch):
    monkeypatch.setattr(redact, "_literals", set())
    redact.register_literal(LITERAL)


def exchange(tmp_path, monkeypatch, status, header, method="POST"):
    upstream = ap.Upstream(lambda req: ap.Reply(
        status, [header, ("content-type", "application/json")], b'{"ok":true}'))
    with ap.upstream_server(upstream) as url, ap.proxy_server(
            tmp_path, monkeypatch, accounts={"a": "tok-adv2"}, upstream_url=url) as proxy:
        body = b"{}" if method == "POST" else b""
        length = f"Content-Length: {len(body)}\r\n" if method == "POST" else ""
        request = (f"{method} /v1/messages HTTP/1.1\r\nHost: proxy\r\n"
                   f"Authorization: Bearer {proxy.mint('agent-adv2')}\r\n{length}"
                   "Connection: close\r\n\r\n").encode() + body
        raw, _ = ap.raw_exchange(proxy.port, request)
    assert raw.startswith(b"HTTP/1.1 "), raw[:200]
    return raw


@pytest.mark.parametrize("method", ["POST", "GET", "HEAD"])
@pytest.mark.parametrize("header", [
    ("x-account-" + LITERAL, "1"),
    ("x-billing-" + EMAIL, "1"),
], ids=["literal-in-name", "email-in-name"])
def test_ap7_success_header_name_carries_no_account_identifier(tmp_path, monkeypatch,
                                                               method, header):
    raw = exchange(tmp_path, monkeypatch, 200, header, method)
    assert ap.split_response(raw)[0] == 200
    assert LITERAL.encode() not in raw, raw[:600]
    assert EMAIL.encode() not in raw, raw[:600]


@pytest.mark.parametrize("header", [
    ("anthropic-ratelimit-" + EMAIL, "1"),
    ("anthropic-ratelimit-" + LITERAL, "1"),
], ids=["email-in-name", "literal-in-name"])
@pytest.mark.parametrize("status", [400, 500])
def test_ap5_error_header_name_carries_no_account_identifier(tmp_path, monkeypatch,
                                                             status, header):
    raw = exchange(tmp_path, monkeypatch, status, header)
    assert ap.split_response(raw)[0] == status
    assert LITERAL.encode() not in raw, raw[:600]
    assert EMAIL.encode() not in raw, raw[:600]
