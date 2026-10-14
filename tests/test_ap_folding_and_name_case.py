"""AP14 (folded headers) and AP15 (case-insensitive header names).

Contract: `context/specs/findings-c1-c2-part2.md`, Part 2b, "AP — third
adversary pass", AP14 and AP15, and Decision 19.

- AP14: an upstream obs-fold (CRLF then a space or tab) is unfolded before
  any check, each fold becoming one space; AP7, AP11 and AP12 apply to the
  unfolded value; a header the proxy keeps is written on one line.
- AP15: AP11's name check matches registered literals ignoring ASCII case.

The literal-in-a-folded-value cases on a 200 and on a 500's `request-id` are
pinned by `tests/test_ap_adversary3_folding.py`; this file covers the rest of
AP14's `Verified by:` (a harmless folded header kept on one line) and the
combinations a plausible unfolding gets wrong (a fold followed by several
whitespace characters, a UTF-8 literal split by a fold).

Every upstream is local (`ap_adversary3_raw.raw_upstream` writes the exact
bytes an obs-fold needs; `ap_support.Upstream` otherwise). Nothing leaves
127.0.0.1.
"""

import re
import sys
from pathlib import Path

import pytest

from multiagents import redact

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))
import ap_support as ap
from ap_adversary3_raw import raw_upstream

AGENT = "agent-ap14"
LITERAL = "Acme Holdings-Secret"
UTF8_LITERAL = "Kontoinhaber Müller-Geheim"
CASE_LITERAL = "SECRETLITERAL"
LOWER_LITERAL = "quietliteral-ap15"


@pytest.fixture(autouse=True)
def isolated_registration(monkeypatch):
    monkeypatch.setattr(redact, "_literals", set())
    for literal in (LITERAL, UTF8_LITERAL, CASE_LITERAL, LOWER_LITERAL):
        redact.register_literal(literal)


def relay_raw(tmp_path, monkeypatch, response: bytes) -> bytes:
    with raw_upstream(response) as url, \
            ap.proxy_server(tmp_path, monkeypatch, accounts={"acc": "tok-ap14"},
                            upstream_url=url) as proxy:
        raw, _ = ap.raw_exchange(proxy.port, (
            "POST /v1/messages HTTP/1.1\r\nHost: proxy\r\n"
            f"Authorization: Bearer {proxy.mint(AGENT)}\r\n"
            "Content-Length: 2\r\nConnection: close\r\n\r\n{}").encode())
    assert raw.startswith(b"HTTP/1.1 "), raw[:200]
    return raw


def head_lines(raw: bytes) -> list[str]:
    """The first response's header lines exactly as written, status line excluded."""
    return raw.partition(b"\r\n\r\n")[0].decode("latin-1").split("\r\n")[1:]


def unfolded_head(raw: bytes) -> str:
    """The header block as a conforming recipient reads it (RFC 9112 5.2)."""
    head = raw.partition(b"\r\n\r\n")[0].decode("latin-1")
    return re.sub(r"[ \t]*\r\n[ \t]+", " ", head)


def assert_no_fold(raw: bytes) -> None:
    folded = [line for line in head_lines(raw) if line[:1] in (" ", "\t")]
    assert folded == [], f"the proxy wrote an obs-fold to the agent: {raw[:400]!r}"


def header_value(raw: bytes, name: str) -> str | None:
    for line in head_lines(raw):
        key, _, value = line.partition(":")
        if key.strip().lower() == name:
            return value.strip(" \t")
    return None


# ---------------------------------------------------------------------------
# AP14 — a harmless folded header is kept, on one line
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("fold", [b"\r\n ", b"\r\n\t"], ids=["space", "tab"])
def test_ap14_a_folded_harmless_header_on_a_200_is_kept_on_one_line(tmp_path, monkeypatch,
                                                                     fold):
    raw = relay_raw(tmp_path, monkeypatch,
                    b"HTTP/1.1 200 OK\r\nX-Trace: alpha" + fold + b"beta" + fold + b"gamma\r\n"
                    b"Content-Type: application/json\r\nContent-Length: 2\r\n\r\n{}")
    assert ap.split_response(raw)[0] == 200
    assert_no_fold(raw)
    assert header_value(raw, "x-trace") == "alpha beta gamma", raw[:400]


def test_ap14_a_folded_request_id_on_a_500_is_kept_on_one_line(tmp_path, monkeypatch):
    """`request-id` is on AP5's error allowlist, so the unfolded value is forwarded."""
    raw = relay_raw(tmp_path, monkeypatch,
                    b"HTTP/1.1 500 Internal Server Error\r\nRequest-Id: req_ap14\r\n part2\r\n"
                    b"Content-Type: application/json\r\nContent-Length: 2\r\n\r\n{}")
    assert ap.split_response(raw)[0] == 500
    assert_no_fold(raw)
    assert header_value(raw, "request-id") == "req_ap14 part2", raw[:400]


# ---------------------------------------------------------------------------
# AP14 — the checks see the unfolded value
# ---------------------------------------------------------------------------

def test_ap14_a_fold_with_several_whitespace_characters_becomes_one_space(tmp_path,
                                                                          monkeypatch):
    """The whole fold (CRLF and every space or tab after it) is one space, so
    `Acme` + fold + `Holdings-Secret` is the literal and is dropped."""
    raw = relay_raw(tmp_path, monkeypatch,
                    b"HTTP/1.1 200 OK\r\nX-Account: Acme\r\n \t  Holdings-Secret\r\n"
                    b"Request-Id: req_ap14_keep\r\n"
                    b"Content-Type: application/json\r\nContent-Length: 2\r\n\r\n{}")
    assert ap.split_response(raw)[0] == 200
    assert_no_fold(raw)
    assert "Holdings-Secret" not in unfolded_head(raw), raw[:400]
    assert header_value(raw, "request-id") == "req_ap14_keep", "the clean header is still kept"


@pytest.mark.parametrize("status_line", [b"HTTP/1.1 200 OK", b"HTTP/1.1 500 Internal Server Error"],
                         ids=["200", "500-request-id"])
def test_ap14_ap12_a_utf8_literal_split_by_a_fold_does_not_reach_the_agent(
        tmp_path, monkeypatch, status_line):
    name = b"Request-Id" if b"500" in status_line else b"X-Account"
    first, _, rest = UTF8_LITERAL.encode("utf-8").partition(b" ")
    raw = relay_raw(tmp_path, monkeypatch,
                    status_line + b"\r\n" + name + b": " + first + b"\r\n " + rest + b"\r\n"
                    b"Content-Type: application/json\r\nContent-Length: 2\r\n\r\n{}")
    assert_no_fold(raw)
    assert rest not in raw, raw[:400]


# ---------------------------------------------------------------------------
# AP15 — the name check ignores ASCII case
# ---------------------------------------------------------------------------

def relay(tmp_path, monkeypatch, status, header):
    upstream = ap.Upstream(lambda req: ap.Reply(
        status, [header, ("request-id", "req_ap15"), ("content-type", "application/json")],
        b'{"ok":true}'))
    with ap.upstream_server(upstream) as url, \
            ap.proxy_server(tmp_path, monkeypatch, accounts={"acc": "tok-ap15"},
                            upstream_url=url) as proxy:
        raw, _ = ap.raw_exchange(proxy.port, (
            "POST /v1/messages HTTP/1.1\r\nHost: proxy\r\n"
            f"Authorization: Bearer {proxy.mint('agent-ap15')}\r\n"
            "Content-Length: 2\r\nConnection: close\r\n\r\n{}").encode())
    assert ap.split_response(raw)[0] == status, raw[:300]
    return raw


@pytest.mark.parametrize("status,name", [
    (200, "x-account-secretliteral"),
    (200, "X-Account-SecretLiteral"),
    (500, "anthropic-ratelimit-secretliteral"),
    (200, "X-OWNER-QUIETLITERAL-AP15"),
    (500, "Anthropic-Ratelimit-QuietLiteral-Ap15"),
], ids=["200-lower", "200-mixed", "500-allowlisted-prefix", "200-upper-of-lower",
        "500-mixed-of-lower"])
def test_ap15_a_literal_in_a_header_name_is_dropped_whatever_its_case(tmp_path, monkeypatch,
                                                                      status, name):
    raw = relay(tmp_path, monkeypatch, status, (name, "1"))
    lowered = raw.partition(b"\r\n\r\n")[0].lower()
    assert b"secretliteral" not in lowered and b"quietliteral" not in lowered, raw[:400]
    assert header_value(raw, "request-id") == "req_ap15", "the clean header is still kept"
