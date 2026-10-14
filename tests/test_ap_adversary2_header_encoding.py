"""AP7, second adversary pass: a non-ASCII registered literal in a header value.

Contract: `context/specs/findings-c1-c2-part2.md`, AP7: "a forwarded header
whose value contains a registered literal ... is dropped".

`http.client` decodes header bytes as Latin-1 and `http.server` re-encodes
them as Latin-1, so the bytes pass through unchanged. A literal with a
non-ASCII character, sent by upstream as UTF-8 (the encoding the body path
assumes, HF1.3), never equals the Latin-1 reading, the value check misses
it, and the agent receives the literal's exact UTF-8 bytes. The body path
masks the same bytes.

Possibly within the encoding residual of part 1's Decision 4 / Decision 15;
kept apart from the header-name tests so it can be judged on its own.
"""

import sys
from pathlib import Path

import pytest

from multiagents import redact

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))
import ap_support as ap

LITERAL = "Kontoinhaber-Müller-Geheim"


@pytest.fixture(autouse=True)
def isolated_registration(monkeypatch):
    monkeypatch.setattr(redact, "_literals", set())
    redact.register_literal(LITERAL)


@pytest.mark.parametrize("status,name", [(200, "x-account-note"),
                                         (500, "request-id")])
def test_ap7_utf8_literal_in_header_value_is_not_relayed(tmp_path, monkeypatch, status, name):
    # The fake upstream writes header text as Latin-1, so this sends UTF-8 bytes.
    wire_value = LITERAL.encode("utf-8").decode("latin-1")
    upstream = ap.Upstream(lambda req: ap.Reply(
        status, [(name, wire_value), ("content-type", "application/json")], b'{"ok":true}'))
    with ap.upstream_server(upstream) as url, ap.proxy_server(
            tmp_path, monkeypatch, accounts={"a": "tok-adv2"}, upstream_url=url) as proxy:
        request = (f"POST /v1/messages HTTP/1.1\r\nHost: proxy\r\n"
                   f"Authorization: Bearer {proxy.mint('agent-adv2')}\r\n"
                   "Content-Length: 2\r\nConnection: close\r\n\r\n{}").encode()
        raw, _ = ap.raw_exchange(proxy.port, request)
    assert ap.split_response(raw)[0] == status
    assert LITERAL.encode("utf-8") not in raw, raw[:600]
