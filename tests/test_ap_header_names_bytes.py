"""AP11 and AP12 — header names, and header bytes, against the masking rule.

Contract: `context/specs/findings-c1-c2-part2.md`, Part 2b, "AP — second
adversary pass".

- AP11: the masking rule of AP7 applies to header names as well as values:
  an upstream header whose name contains a registered literal or an e-mail
  address is dropped on every response, `anthropic-ratelimit-*` on an error
  included.
- AP12: header names and values are compared with the registered literals
  both as received (Latin-1, as `http.client` decodes them) and as UTF-8, so
  a non-ASCII literal sent as UTF-8 bytes is caught.

`tests/test_ap_adversary2_header_names.py` and
`tests/test_ap_adversary2_header_encoding.py` pin the adversary's cases (a
literal or address in a 2xx name for POST/GET/HEAD, in an
`anthropic-ratelimit-` name on a 400 and a 500, a UTF-8 literal in a 200 and
a 500 value). The tests here fill what those leave: the spec's own names on
a 200 and a 500, the relayed 401 and a 404, a literal whose case a
lowercasing name check would lose, that only the offending header goes, a
UTF-8 literal in a name, the Latin-1 reading kept, and the `content-type`
exception applied to a UTF-8 literal.

The wire is read raw so the header block is checked byte for byte.
"""

import sys
from pathlib import Path

import pytest

from multiagents import redact

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))
import ap_support as ap

AGENT = "agent-ap11"
LITERAL = "acct-ap11-registered-literal-0042"
MIXED_CASE_LITERAL = "AcctAP11-MixedCase-Literal-0043"
ADDRESS = "holder.ap11@billing.example.net"
UTF8_LITERAL = "Kontoinhaber-Müller-Geheim"
BODY = b'{"ok":true}'

# Forwarded on every status the tests use: allowed on a non-2xx (AP5) and
# not forbidden on a 2xx (AP7).
CLEAN = [("request-id", "req_ap11_clean"), ("anthropic-ratelimit-requests-limit", "50")]


@pytest.fixture(autouse=True)
def isolated_registration(monkeypatch):
    monkeypatch.setattr(redact, "_literals", set())
    for literal in (LITERAL, MIXED_CASE_LITERAL, UTF8_LITERAL):
        redact.register_literal(literal)


def wire(text: str) -> str:
    """`text`'s UTF-8 bytes as the fake upstream will write them (it encodes
    header text as Latin-1, so this puts UTF-8 bytes on the wire)."""
    return text.encode("utf-8").decode("latin-1")


def exchange(tmp_path, monkeypatch, status, headers, method="POST", body=BODY):
    """One request through the proxy. Returns (status, header pairs with
    lowercased names, the raw response). A 401 is relayed as the saved one:
    the only account is rejected, so the proxy answers with upstream's 401."""
    reply = ap.Reply(status, [("content-type", "application/json"), *headers], body)
    upstream = ap.Upstream(lambda req: reply)
    with ap.upstream_server(upstream) as url, \
            ap.proxy_server(tmp_path, monkeypatch, accounts={"acc": "tok-ap11"},
                            upstream_url=url) as proxy:
        payload = b"{}" if method == "POST" else b""
        length = f"Content-Length: {len(payload)}\r\n" if method == "POST" else ""
        raw, _ = ap.raw_exchange(proxy.port, (
            f"{method} /v1/messages HTTP/1.1\r\nHost: proxy\r\n"
            f"Authorization: Bearer {proxy.mint(AGENT)}\r\n{length}"
            "Connection: close\r\n\r\n").encode() + payload)
    head = raw.partition(b"\r\n\r\n")[0].decode("latin-1").split("\r\n")
    got = int(head[0].split(" ")[1]) if head[0].startswith("HTTP/") else 0
    pairs = [(n.strip().lower(), v.strip())
             for n, _, v in (line.partition(":") for line in head[1:])]
    return got, pairs, raw


def head_of(raw: bytes) -> bytes:
    return raw.partition(b"\r\n\r\n")[0]


# ---------------------------------------------------------------------------
# AP11 — names
# ---------------------------------------------------------------------------

SPEC_NAMES = [
    ("x-account-" + LITERAL, "1"),
    ("x-owner." + ADDRESS, "1"),
    ("anthropic-ratelimit-" + ADDRESS, "1"),
]


@pytest.mark.parametrize("status", [200, 401, 404, 500])
def test_ap11_a_name_carrying_a_literal_or_an_address_never_reaches_the_agent(
        tmp_path, monkeypatch, status):
    """The spec's three names on a 200 and a 500, and on the two other
    kinds of relayed response: the saved 401 and a plain 4xx."""
    got, _, raw = exchange(tmp_path, monkeypatch, status, SPEC_NAMES)
    assert got == status, raw[:300]
    head = head_of(raw).lower()
    assert LITERAL.encode() not in head, head
    assert ADDRESS.encode() not in head, head
    assert b"x-owner." not in head and b"x-account-" not in head, head


@pytest.mark.parametrize("status", [200, 500])
def test_ap11_only_the_offending_header_is_dropped(tmp_path, monkeypatch, status):
    """Dropping by name is per header: the clean headers beside it, the
    `anthropic-ratelimit-` ones included, are still forwarded."""
    got, headers, _ = exchange(tmp_path, monkeypatch, status, [*SPEC_NAMES, *CLEAN])
    assert got == status
    for pair in CLEAN:
        assert pair in headers, f"{pair} was dropped with the offending header: {headers}"


@pytest.mark.parametrize("status", [200, 500])
def test_ap11_a_mixed_case_literal_in_a_name_is_caught(tmp_path, monkeypatch, status):
    """Header names are case-insensitive, literals are not: a check against
    the lowercased name alone would miss this literal and forward it."""
    name = "anthropic-ratelimit-" + MIXED_CASE_LITERAL
    got, _, raw = exchange(tmp_path, monkeypatch, status, [(name, "1")])
    assert got == status
    assert MIXED_CASE_LITERAL.lower().encode() not in head_of(raw).lower(), head_of(raw)


# ---------------------------------------------------------------------------
# AP12 — bytes
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("status", [200, 500])
def test_ap12_a_utf8_literal_in_a_name_is_caught(tmp_path, monkeypatch, status):
    """The clean headers go first: `http.client` stops reading a header block
    at a name that is not a token, so headers after it never reach the proxy,
    and losing them is not what is tested here."""
    name = wire("anthropic-ratelimit-" + UTF8_LITERAL)
    got, headers, raw = exchange(tmp_path, monkeypatch, status, [*CLEAN, (name, "1")])
    assert got == status, raw[:300]
    assert UTF8_LITERAL.encode("utf-8") not in raw, head_of(raw)
    for pair in CLEAN:
        assert pair in headers, headers


@pytest.mark.parametrize("status", [200, 401, 500])
def test_ap12_a_literal_received_as_latin1_is_still_caught(tmp_path, monkeypatch, status):
    """The UTF-8 comparison is in addition to the reading as received: the
    same literal sent as Latin-1 bytes (`ü` as 0xFC) is caught too."""
    got, headers, raw = exchange(tmp_path, monkeypatch, status, [
        ("request-id", UTF8_LITERAL), ("x-request-id", "xreq_ap12_clean")])
    assert got == status, raw[:300]
    assert UTF8_LITERAL.encode("latin-1") not in raw, head_of(raw)
    assert "request-id" not in [n for n, _ in headers]
    assert ("x-request-id", "xreq_ap12_clean") in headers


@pytest.mark.parametrize("status", [200, 401])
def test_ap12_a_utf8_literal_in_a_value_is_caught_on_the_saved_401(tmp_path, monkeypatch,
                                                                    status):
    """The adversary pins a 200 and a 500; the relayed 401 goes through the
    same rule."""
    got, headers, raw = exchange(tmp_path, monkeypatch, status, [
        ("request-id", wire(UTF8_LITERAL)), ("x-request-id", "xreq_ap12_clean")])
    assert got == status, raw[:300]
    assert UTF8_LITERAL.encode("utf-8") not in raw, head_of(raw)
    assert ("x-request-id", "xreq_ap12_clean") in headers


@pytest.mark.parametrize("status", [200, 500])
def test_ap12_a_utf8_literal_in_content_type_replaces_it(tmp_path, monkeypatch, status):
    """AP7's exception holds for the UTF-8 reading: `content-type` carrying
    the literal is replaced by `application/octet-stream`, not dropped."""
    reply_type = wire(f"application/json; owner={UTF8_LITERAL}")
    reply = ap.Reply(status, [("content-type", reply_type)], BODY)
    upstream = ap.Upstream(lambda req: reply)
    with ap.upstream_server(upstream) as url, \
            ap.proxy_server(tmp_path, monkeypatch, accounts={"acc": "tok-ap12"},
                            upstream_url=url) as proxy:
        raw, _ = ap.raw_exchange(proxy.port, (
            "POST /v1/messages HTTP/1.1\r\nHost: proxy\r\n"
            f"Authorization: Bearer {proxy.mint(AGENT)}\r\n"
            "Content-Length: 2\r\nConnection: close\r\n\r\n{}").encode())
    got, headers, _ = ap.split_response(raw)
    assert got == status, raw[:300]
    assert UTF8_LITERAL.encode("utf-8") not in raw, head_of(raw)
    assert headers.get("content-type") == "application/octet-stream", head_of(raw)
