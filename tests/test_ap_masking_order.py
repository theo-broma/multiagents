"""AP8 — masking order in an upstream body passed back to an agent, and
what Decision 15 still requires of bodies it otherwise accepts.

Contract: `context/specs/findings-c1-c2-part2.md`, Part 2b, "AP — adversary
findings", AP8 and Decision 15; AP1 for `authproxy._scrub`, which the spec
names as the scrubber of upstream bodies.

- registered literals are masked before e-mail addresses, longest literal
  first, so a literal containing an e-mail-shaped part or another literal
  is masked whole (HF1.2);
- Decision 15 accepts that SSE and JSON-with-trailing-text bodies get no
  structured organisation-key masking, but registered literals and plain
  e-mail addresses are still masked in them.

"Masked whole" is asserted as the exact output: the literal replaced by one
mask and nothing around it changed.
"""

import json
import sys
from pathlib import Path

import pytest

from multiagents import authproxy, redact
from multiagents.redact import MASK

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))
import ap_support as ap

DSN = "postgres://svc_ap8:pa55w0rd-ap8@db.internal.example.com/ledger_ap8"
EMAIL = "carol@example.org"


@pytest.fixture(autouse=True)
def isolated_registration(monkeypatch):
    monkeypatch.setattr(redact, "_literals", set())


# ---------------------------------------------------------------------------
# A literal containing an e-mail-shaped part
# ---------------------------------------------------------------------------

def test_ap8_a_connection_string_literal_is_masked_whole_in_a_text_body():
    redact.register_literal(DSN)
    out = authproxy._scrub(f"<html>could not connect: {DSN} (retrying)</html>".encode())
    assert out == f"<html>could not connect: {MASK} (retrying)</html>".encode()


def test_ap8_a_connection_string_literal_is_masked_whole_in_a_json_value():
    redact.register_literal(DSN)
    body = json.dumps({"type": "error",
                       "error": {"type": "api_error", "message": f"could not connect: {DSN}"}})
    out = json.loads(authproxy._scrub(body.encode()))
    assert out == {"type": "error",
                   "error": {"type": "api_error", "message": f"could not connect: {MASK}"}}


def test_ap8_a_connection_string_literal_used_as_a_json_key_is_masked_whole():
    redact.register_literal(DSN)
    out = json.loads(authproxy._scrub(json.dumps({DSN: "down", "type": "x"}).encode()))
    assert out == {MASK: "down", "type": "x"}


def test_ap8_the_address_alone_elsewhere_in_the_body_is_still_masked():
    redact.register_literal(DSN)
    text = f"dsn {DSN}; owner {EMAIL}"
    assert authproxy._scrub(text.encode()) == f"dsn {MASK}; owner {MASK}".encode()


def test_ap8_a_literal_that_is_an_address_with_more_around_it_is_masked_whole():
    literal = f"mailto:{EMAIL}?subject=ap8-ticket-4411"
    redact.register_literal(literal)
    assert authproxy._scrub(f"see {literal} now".encode()) == f"see {MASK} now".encode()


def test_ap8_literal_containing_an_address_is_masked_whole_on_the_wire(tmp_path, monkeypatch):
    redact.register_literal(DSN)
    upstream = ap.Upstream(lambda req: ap.Reply(
        502, [("content-type", "text/html")], f"<p>bad gateway: {DSN}</p>".encode()))
    with ap.upstream_server(upstream) as url, \
            ap.proxy_server(tmp_path, monkeypatch, accounts={"acc": "tok"},
                            upstream_url=url) as proxy:
        status, headers, body = ap.request(proxy)
    assert status == 502
    assert body == f"<p>bad gateway: {MASK}</p>".encode()


# ---------------------------------------------------------------------------
# Overlapping literals: longest first
# ---------------------------------------------------------------------------

INNER = "ap8-inner-secret"
OUTER = "ap8-outer-ap8-inner-secret-tail"


@pytest.mark.parametrize("order", ["short-first", "long-first"])
def test_ap8_a_literal_containing_another_is_masked_whole(order):
    for literal in ([INNER, OUTER] if order == "short-first" else [OUTER, INNER]):
        redact.register_literal(literal)
    out = authproxy._scrub(f"a {OUTER} b {INNER} c".encode())
    assert out == f"a {MASK} b {MASK} c".encode()


@pytest.mark.parametrize("order", ["short-first", "long-first"])
def test_ap8_a_literal_containing_another_is_masked_whole_in_json(order):
    for literal in ([INNER, OUTER] if order == "short-first" else [OUTER, INNER]):
        redact.register_literal(literal)
    out = json.loads(authproxy._scrub(json.dumps({"error": {"message": OUTER}}).encode()))
    assert out == {"error": {"message": MASK}}


@pytest.mark.parametrize("order", ["short-first", "long-first"])
def test_ap8_partly_overlapping_literals_mask_the_longer_one_first(order):
    """Neither contains the other; where they overlap, the longer one is
    masked first and the shorter no longer occurs."""
    longer, shorter = "ap8-left-part-overlap", "overlap-right"
    for literal in ([shorter, longer] if order == "short-first" else [longer, shorter]):
        redact.register_literal(literal)
    out = authproxy._scrub(b"x ap8-left-part-overlap-right y")
    assert out == f"x {MASK}-right y".encode()


# ---------------------------------------------------------------------------
# Decision 15: what is still masked in the accepted body shapes
# ---------------------------------------------------------------------------

LITERAL = "ap8-d15-registered-value"


def test_d15_sse_error_body_still_masks_literals_and_plain_addresses():
    redact.register_literal(LITERAL)
    body = (f'event: error\ndata: {{"type":"error","error":{{"message":"key {LITERAL} '
            f'for {EMAIL}"}}}}\n\n').encode()
    out = authproxy._scrub(body)
    assert LITERAL.encode() not in out
    assert EMAIL.encode() not in out
    assert out.startswith(b"event: error\ndata: ")


def test_d15_json_with_trailing_text_still_masks_literals_and_plain_addresses():
    redact.register_literal(LITERAL)
    body = f'{{"error":{{"message":"{LITERAL}"}}}}\ncontact {EMAIL} trailing'.encode()
    out = authproxy._scrub(body)
    assert LITERAL.encode() not in out
    assert EMAIL.encode() not in out


# Decision 15 also accepts, and so this suite does not require: a registered
# literal arriving as a JSON number, gzip-encoded error bodies, HTML-entity
# or percent-encoded addresses, and the structured organisation-key masking
# in SSE and JSON-with-trailing-text bodies.
