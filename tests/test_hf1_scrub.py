"""HF1.1–HF1.5 / F24, F71: the contracted scrub and HTTP error surfaces."""

import concurrent.futures
import json
import random
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from multiagents import authproxy, redact

sys.path.insert(0, str(Path(__file__).parent / "support"))
import c1_harness as h

WAIT_SECONDS = 3
LITERAL = "hf-one-registered-sensitive-value"
BEARER = "Bearer abcdefghijklmnopqrstuvwxyz012345"


@pytest.fixture(autouse=True)
def isolated_registration(monkeypatch):
    # Fixture isolation only; assertions use the explicitly contracted _scrub
    # surface and the public registration API, never the registry's contents.
    monkeypatch.setattr(redact, "_literals", set())


@pytest.mark.parametrize("value", [
    {"access_token": "short", "items": [LITERAL, BEARER, "ordinary"]},
    [LITERAL, {"password": "short"}, 0, None],
    LITERAL,
    None,
])
def test_hf1_1_json_keeps_structured_redaction(value):
    redact.register_literal(LITERAL)
    result = authproxy._scrub(json.dumps(value, indent=2).encode())
    assert json.loads(result) == redact.scrub(value)


def test_hf1_2_html_masks_registered_literals_and_bearer_shapes():
    redact.register_literal(LITERAL)
    payload = f"<html>{LITERAL}; {BEARER}</html>".encode()
    result = authproxy._scrub(payload)
    assert result == f"<html>{redact.MASK}; {redact.MASK}</html>".encode()


def test_hf1_3_invalid_utf8_preserves_every_byte_outside_secret_spans():
    redact.register_literal(LITERAL)
    before = b"\xff\xc0\xaf<html>\x00"
    after = b"\xed\xa0\x80</html>\xfe"
    assert authproxy._scrub(before + LITERAL.encode() + after) == (
        before + redact.MASK.encode() + after)


def test_hf1_3_arbitrary_bytes_never_raise():
    rng = random.Random(2471)
    payloads = [b"", bytes(range(256)), *[bytes([n]) for n in range(256)]]
    payloads += [rng.randbytes(n) for n in (2, 3, 7, 31, 128, 1024, 4096)]
    for payload in payloads:
        assert isinstance(authproxy._scrub(payload), bytes)


@pytest.mark.parametrize("json_body", [False, True], ids=["non-json", "json"])
def test_hf1_3_redactor_failure_returns_a_fixed_body_without_upstream_content(
        monkeypatch, json_body):
    def broken_redactor(value):
        raise RuntimeError(f"redactor unavailable for {value!r}")

    monkeypatch.setattr(redact, "scrub", broken_redactor)
    markers = ["upstream-alpha-private-value", "upstream-beta-private-value"]
    payloads = [((json.dumps({"detail": marker * 80}) if json_body else
                  "<html>" + marker * 80 + "</html>").encode()) for marker in markers]
    results = [authproxy._scrub(payload) for payload in payloads]
    assert results[0] == results[1], "failure body must not depend on upstream content"
    assert 0 < len(results[0]) < min(map(len, payloads))
    for marker in markers:
        assert marker.encode() not in results[0]


@pytest.mark.parametrize("json_body", [False, True], ids=["non-json", "json"])
def test_hf1_3_concurrent_literal_registration_does_not_break_redaction(json_body):
    redact.register_literal(LITERAL)
    for n in range(1000):
        redact.register_literal(f"preexisting-hf-one-secret-{n:04d}")
    barrier = threading.Barrier(2)
    payload = (json.dumps(LITERAL).encode() if json_body else
               f"<p>{LITERAL}</p>".encode())
    expected = (json.dumps(redact.MASK).encode() if json_body else
                f"<p>{redact.MASK}</p>".encode())

    def register():
        barrier.wait(timeout=WAIT_SECONDS)
        for n in range(1200):
            redact.register_literal(f"concurrent-hf-one-secret-{n:04d}")

    def scrub():
        barrier.wait(timeout=WAIT_SECONDS)
        for _ in range(200):
            assert authproxy._scrub(payload) == expected

    # Frequent thread handoffs exercise registration during a scrub without
    # waiting through wall-clock sleeps or requiring an internal iterator seam.
    previous_interval = sys.getswitchinterval()
    try:
        sys.setswitchinterval(0.000001)
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(register), pool.submit(scrub)]
            for future in futures:
                future.result(timeout=WAIT_SECONDS)
    finally:
        sys.setswitchinterval(previous_interval)


@pytest.mark.parametrize("payload", [
    b"", b" \t\r\n", b"plain error text\n", "e\u0301 \u00e9".encode(),
    b"\x00\xff\xc0\xafplain\xed\xa0\x80\xfe\x00",
], ids=["empty", "whitespace", "plain", "no-normalisation", "binary"])
def test_hf1_4_non_json_without_secrets_is_byte_identical(payload):
    assert authproxy._scrub(payload) == payload


@pytest.mark.parametrize("literal", ["tiny", LITERAL], ids=["grows", "shrinks"])
def test_hf1_5_http_error_length_matches_redacted_non_json_body(
        tmp_path, monkeypatch, literal):
    redact.register_literal(literal, min_length=1)
    body = f"<html>{literal}</html>".encode()
    expected = f"<html>{redact.MASK}</html>".encode()
    with h.fake_http_server(h.fixed_response_upstream(400, body=body)) as upstream:
        with h.authproxy_server(tmp_path, monkeypatch, accounts={"acc": "t"},
                                upstream=upstream) as proxy:
            request = urllib.request.Request(
                proxy.base_url + "/v1/messages", data=b"{}",
                headers={"Authorization": "Bearer " + proxy.mint("hf1-agent")})
            with pytest.raises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(request, timeout=WAIT_SECONDS)
            with caught.value as response:
                received = response.read()
                assert response.code == 400
                assert received == expected
                assert int(response.headers["Content-Length"]) == len(received)
