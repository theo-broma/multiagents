"""AP7 with AP12 — a registered literal split by an obs-fold.

Contract: `context/specs/findings-c1-c2-part2.md`, Part 2b. AP7: "A forwarded
header whose value contains a registered literal ... is dropped". AP12: the
comparison is made on the header bytes as received.

An upstream may fold a header value (RFC 9112 5.2, obs-fold: CRLF followed by
a space or tab). `http.client` keeps the fold inside the value, so the
literal `Acme Holdings-Secret` arrives as `Acme\\r\\n Holdings-Secret` and
does not "contain" the literal; the proxy then writes the fold to the agent
unchanged. A conforming client replaces each obs-fold with a single space
(RFC 9112 5.2), and reads the field value as `Acme Holdings-Secret`: the
registered literal, exactly. The tests read the agent's header block the way
such a client does and assert the literal is not in it.
"""

import re
import sys
from pathlib import Path

import pytest

from multiagents import redact

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))
import ap_support as ap
from ap_adversary3_raw import raw_upstream

AGENT = "agent-ap-adv3-fold"
LITERAL = "Acme Holdings-Secret"


@pytest.fixture(autouse=True)
def isolated_registration(monkeypatch):
    monkeypatch.setattr(redact, "_literals", set())
    redact.register_literal(LITERAL)


def unfolded_head(raw: bytes) -> str:
    """The first response's header block as a conforming recipient reads it:
    each obs-fold replaced by one space."""
    head = raw.partition(b"\r\n\r\n")[0].decode("latin-1")
    return re.sub(r"[ \t]*\r\n[ \t]+", " ", head)


def relay(tmp_path, monkeypatch, response: bytes) -> bytes:
    with raw_upstream(response) as url, \
            ap.proxy_server(tmp_path, monkeypatch, accounts={"acc": "tok-adv3"},
                            upstream_url=url) as proxy:
        raw, _ = ap.raw_exchange(proxy.port, (
            "POST /v1/messages HTTP/1.1\r\nHost: proxy\r\n"
            f"Authorization: Bearer {proxy.mint(AGENT)}\r\n"
            "Content-Length: 2\r\nConnection: close\r\n\r\n{}").encode())
    assert raw.startswith(b"HTTP/1.1 "), raw[:200]
    return raw


@pytest.mark.parametrize("fold", [b"\r\n ", b"\r\n\t"], ids=["space", "tab"])
def test_ap7_ap12_a_folded_literal_in_a_200_header_does_not_reach_the_agent(
        tmp_path, monkeypatch, fold):
    raw = relay(tmp_path, monkeypatch,
                b"HTTP/1.1 200 OK\r\nX-Account: Acme" + fold + b"Holdings-Secret\r\n"
                b"Content-Type: application/json\r\nContent-Length: 2\r\n\r\n{}")
    assert LITERAL not in unfolded_head(raw), raw[:400]


def test_ap7_ap12_a_folded_literal_in_an_error_request_id_does_not_reach_the_agent(
        tmp_path, monkeypatch):
    """`request-id` is on AP5's error allowlist, so it is forwarded on a 500."""
    raw = relay(tmp_path, monkeypatch,
                b"HTTP/1.1 500 Internal Server Error\r\nRequest-Id: Acme\r\n Holdings-Secret\r\n"
                b"Content-Type: application/json\r\nContent-Length: 2\r\n\r\n{}")
    assert ap.split_response(raw)[0] == 500
    assert LITERAL not in unfolded_head(raw), raw[:400]


def test_guard_an_unfolded_literal_is_dropped(tmp_path, monkeypatch):
    """Guard: the same value without the fold is dropped today, so the tests
    above fail on the fold and not on the registration."""
    raw = relay(tmp_path, monkeypatch,
                b"HTTP/1.1 200 OK\r\nX-Account: Acme Holdings-Secret\r\n"
                b"Content-Type: application/json\r\nContent-Length: 2\r\n\r\n{}")
    assert LITERAL not in unfolded_head(raw)
    assert b"X-Account" not in raw
