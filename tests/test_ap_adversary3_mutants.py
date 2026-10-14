"""AP12 and AP13 — pins for the branches the suite left unguarded.

Contract: `context/specs/findings-c1-c2-part2.md`, Part 2b, "AP — second
adversary pass". These pass today. Each one fails under a mutation of
`authproxy.py` that the rest of `tests/test_ap_*.py` let through:

- a 204 relayed with `Transfer-Encoding: chunked` in its header block (the
  `if upstream.status == 204: pass` branch removed). RFC 9112 6.1: a server
  MUST NOT send Transfer-Encoding in a 204;
- a 304 relayed with the proxy's `content-type` and `content-length: 0`
  (`bodyless` limited to 204). RFC 9110 8.6: Content-Length on a 304 must be
  the 200's length, never a made-up 0;
- a `Content-Length` below `sys.maxsize` whose buffer cannot be allocated
  (`MemoryError` no longer caught): no response at all instead of AP13's 400.
  Reversed for AP17 (tester, third pass): such a length is above 64 MiB, so
  the contract answer is now 413, before any buffer is sized;
- a UTF-8 literal beside an invalid UTF-8 byte (`errors="strict"`): the
  literal is no longer seen, which the code's own comment rules out.
"""

import sys
from pathlib import Path

import pytest

from multiagents import redact

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))
import ap_support as ap
from ap_adversary3_raw import raw_upstream

AGENT = "agent-ap-adv3"
UTF8_LITERAL = "Łódź-Konto-Geheim-77"


@pytest.fixture(autouse=True)
def isolated_registration(monkeypatch):
    monkeypatch.setattr(redact, "_literals", set())
    redact.register_literal(UTF8_LITERAL)


def relay(tmp_path, monkeypatch, response: bytes, method="POST") -> bytes:
    body = b"{}" if method == "POST" else b""
    length = f"Content-Length: {len(body)}\r\n" if body else ""
    with raw_upstream(response) as url, \
            ap.proxy_server(tmp_path, monkeypatch, accounts={"acc": "tok-adv3"},
                            upstream_url=url) as proxy:
        raw, _ = ap.raw_exchange(proxy.port, (
            f"{method} /v1/messages HTTP/1.1\r\nHost: proxy\r\n"
            f"Authorization: Bearer {proxy.mint(AGENT)}\r\n{length}"
            "Connection: close\r\n\r\n").encode() + body)
    assert raw.startswith(b"HTTP/1.1 "), raw[:200]
    return raw


def header_names(raw: bytes) -> list[str]:
    head = raw.partition(b"\r\n\r\n")[0].decode("latin-1").split("\r\n")[1:]
    return [line.partition(":")[0].strip().lower() for line in head]


@pytest.mark.parametrize("method", ["POST", "GET"])
def test_ap13_a_204_carries_no_framing_headers(tmp_path, monkeypatch, method):
    raw = relay(tmp_path, monkeypatch,
                b"HTTP/1.1 204 No Content\r\nRequest-Id: req_adv3\r\n\r\n", method)
    assert ap.split_response(raw)[0] == 204
    names = header_names(raw)
    assert "transfer-encoding" not in names, raw
    assert "content-length" not in names, raw
    assert raw.endswith(b"\r\n\r\n") and raw.count(b"\r\n\r\n") == 1, raw


def test_ap13_a_304_carries_no_invented_length_or_type(tmp_path, monkeypatch):
    raw = relay(tmp_path, monkeypatch,
                b'HTTP/1.1 304 Not Modified\r\nETag: "adv3"\r\nRequest-Id: req_adv3\r\n\r\n',
                "GET")
    assert ap.split_response(raw)[0] == 304
    names = header_names(raw)
    assert "content-length" not in names, raw
    assert "content-type" not in names, raw
    assert "transfer-encoding" not in names, raw


@pytest.mark.parametrize("length", [2 ** 62, sys.maxsize - 64, sys.maxsize],
                         ids=["two-to-the-62", "just-below-maxsize", "maxsize"])
def test_ap13_ap17_a_length_below_maxsize_too_large_to_allocate_gets_413_and_closes(
        tmp_path, monkeypatch, length):
    upstream = ap.Upstream()
    with ap.upstream_server(upstream) as url, \
            ap.proxy_server(tmp_path, monkeypatch, accounts={"acc": "tok-adv3"},
                            upstream_url=url) as proxy:
        raw, closed = ap.raw_exchange(proxy.port, (
            "POST /v1/messages HTTP/1.1\r\nHost: proxy\r\n"
            f"Authorization: Bearer {proxy.mint(AGENT)}\r\n"
            f"Content-Length: {length}\r\n\r\n{{}}").encode())
        requests = list(upstream.requests)
    assert ap.split_response(raw)[0] == 413, raw[:300]
    assert closed
    assert requests == []


def test_ap12_a_utf8_literal_beside_an_invalid_byte_is_still_caught(tmp_path, monkeypatch):
    """In a value only: `http.client` ends the header block at a non-ASCII
    name, so a name never carries one to the proxy."""
    literal = UTF8_LITERAL.encode("utf-8")
    line = b"X-Account: " + literal + b"\xff"
    raw = relay(tmp_path, monkeypatch,
                b"HTTP/1.1 200 OK\r\n" + line + b"\r\nRequest-Id: req_adv3\r\n"
                b"Content-Type: application/json\r\nContent-Length: 2\r\n\r\n{}")
    assert ap.split_response(raw)[0] == 200
    assert literal not in raw, raw[:400]
    assert b"req_adv3" in raw, "the clean header is still forwarded"
