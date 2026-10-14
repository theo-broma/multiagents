"""Adversary (ag-23957f): AP boundaries the suite on c01dc05e does not hold.

These PASS today. Each one kills a mutation of `authproxy.py` that survived
every AP, HF1, HF6 and C1 characterization test; the mutation is named in
the test.
"""

import sys
import time
from pathlib import Path

import pytest

from multiagents import authproxy, redact

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))
import ap_support as ap

LITERAL = "LITERALxSECRETx0123"
RESET = "anthropic-ratelimit-unified-reset"


@pytest.fixture(autouse=True)
def _isolated_literals(monkeypatch):
    monkeypatch.setattr(redact, "_literals", set())


# AP3 "retry-after: 0 is a usable hint". Survived: `value >= 0` -> `value > 0`.
def test_ap3_retry_after_zero_is_usable_and_wins_over_the_reset_header():
    hint = authproxy._retry_after({"retry-after": "0", RESET: str(int(time.time()) + 7200)}, b"")
    assert hint == 0.0


# AP3 "an unusable retry-after falls back to the unified-reset header", for a
# retry-after that does not parse at all. Survived: `continue` -> `break` in
# the parse failure branch.
@pytest.mark.parametrize("garbage", ["soon", "", "Tue, 99 Foo 2026"])
def test_ap3_unparseable_retry_after_falls_back_to_the_reset_header(garbage):
    hint = authproxy._retry_after({"retry-after": garbage, RESET: str(int(time.time()) + 600)}, b"")
    assert 590 <= hint <= 600


# AP1 "a value nested deeper than the depth limit (_depth > 24) is replaced
# whole", at the exact boundary. Survived: `depth > 24` -> `depth > 25`, which
# lets a string at depth 25 reach `redact.scrub` past ITS limit, unmasked.
@pytest.mark.parametrize("levels", [23, 24, 25, 26])
def test_ap1_a_literal_at_the_depth_boundary_never_passes_through(levels):
    redact.register_literal(LITERAL)
    body = "[" * levels + '"' + LITERAL + '"' + "]" * levels
    assert LITERAL.encode() not in authproxy._scrub(body.encode())


# AP6 "a failure while relaying a successful response emits
# upstream_unreachable, closes the connection". The suite only cuts a chunked
# body. Survived: deleting the IncompleteRead raise for a Content-Length body
# that ends early, which then relays a truncated body as a complete one.
def test_ap6_truncated_content_length_body_is_a_relay_failure(tmp_path, monkeypatch):
    upstream = ap.Upstream(lambda req: ap.Reply(
        200, [("content-type", "application/json")], b'{"partial":', declared_length=4096))
    with ap.upstream_server(upstream) as url, \
            ap.proxy_server(tmp_path, monkeypatch, accounts={"a": "tok-a"},
                            upstream_url=url) as proxy:
        raw, closed = ap.raw_exchange(proxy.port, (
            "POST /v1/messages HTTP/1.1\r\nHost: proxy\r\n"
            f"Authorization: Bearer {proxy.mint('agent-trunc')}\r\nContent-Length: 2\r\n\r\n{{}}"
        ).encode())
        assert ap.wait_for(lambda: ap.new_events(proxy.events))
        events = ap.new_events(proxy.events)
    assert raw.startswith(b"HTTP/1.1 200")
    assert not raw.endswith(b"0\r\n\r\n"), "a truncated body was terminated as complete"
    assert closed
    assert [kind for kind, _ in events] == ["upstream_unreachable"]


# AP2 "a failure to write it is swallowed". The suite's stderr raises OSError.
# Survived: `except Exception` -> `except OSError` around the report; a closed
# stderr raises ValueError.
def test_ap2_a_closed_stderr_does_not_break_the_request(tmp_path, monkeypatch):
    import io

    closed = io.StringIO()
    closed.close()
    monkeypatch.setattr(sys, "stderr", closed)

    def callback(kind, fields):
        raise RuntimeError("callback broke")

    upstream = ap.Upstream()
    with ap.upstream_server(upstream) as url, \
            ap.proxy_server(tmp_path, monkeypatch, accounts={"a": "tok-a"},
                            upstream_url=url, on_event=callback) as proxy:
        assert ap.request(proxy, target=".attacker.invalid/x")[0] == 400
        assert ap.request(proxy)[0] == 200


# AP3 ISO reset without an offset is UTC whatever the host's zone. Survived
# on a UTC host: deleting the naive -> UTC replacement.
def test_ap3_naive_iso_reset_is_utc_on_a_non_utc_host(monkeypatch):
    from datetime import datetime, timezone

    monkeypatch.setenv("TZ", "America/Los_Angeles")
    time.tzset()
    try:
        naive = datetime.fromtimestamp(int(time.time()) + 7200, timezone.utc).replace(tzinfo=None)
        hint = authproxy._retry_after({RESET: naive.isoformat()}, b"")
    finally:
        monkeypatch.undo()
        time.tzset()
    assert 7190 <= hint <= 7200
