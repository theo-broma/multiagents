"""AP16 — an upstream interim (1xx) response is never relayed as the response.

Contract: `context/specs/findings-c1-c2-part2.md`, Part 2b, "AP — third
adversary pass", AP16, and Decision 19.

An upstream 1xx other than 100 Continue gives the agent one `502` carrying
no upstream header, emits `upstream_error` with that status (AP6's fields),
and closes the connection. 100 Continue is not covered by AP16: it stays an
interim response the final one follows.

The upstream is `ap_adversary3_raw.raw_upstream` (exact bytes, loopback);
`ap_support.Upstream` cannot write a bare 1xx.
"""

import sys
from pathlib import Path

import pytest

from multiagents import redact

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))
import ap_support as ap
from ap_adversary3_raw import raw_upstream

AGENT = "agent-ap16"
# A correct proxy closes at once; a red one keeps the connection open.
CLOSE_WAIT = 1.0
# The event comes with the 502, so a short wait suffices.
EVENT_WAIT = 0.5

FINAL_200 = (b"HTTP/1.1 200 OK\r\nX-Final-Marker: final-ap16\r\nRequest-Id: req_ap16_final\r\n"
             b"Content-Type: application/json\r\nContent-Length: 11\r\n\r\n{\"ok\":true}")

INTERIM = {
    "103-early-hints": (103, b"HTTP/1.1 103 Early Hints\r\nLink: </ap16.css>; rel=preload\r\n"
                             b"Request-Id: req_ap16_interim\r\nX-Interim-Marker: interim-ap16\r\n"
                             b"\r\n" + FINAL_200),
    "101-switching": (101, b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                           b"Connection: Upgrade\r\nRequest-Id: req_ap16_interim\r\n"
                           b"X-Interim-Marker: interim-ap16\r\n\r\n" + FINAL_200),
}


@pytest.fixture(autouse=True)
def isolated_registration(monkeypatch):
    monkeypatch.setattr(redact, "_literals", set())


def exchange(tmp_path, monkeypatch, response: bytes, method="POST", close=False):
    body = b"{}" if method == "POST" else b""
    length = f"Content-Length: {len(body)}\r\n" if body else ""
    if close:
        length += "Connection: close\r\n"
    with raw_upstream(response) as url, \
            ap.proxy_server(tmp_path, monkeypatch, accounts={"acc": "tok-ap16"},
                            upstream_url=url) as proxy:
        # Without `close`, the close must come from the proxy.
        raw, closed = ap.raw_exchange(proxy.port, (
            f"{method} /v1/messages HTTP/1.1\r\nHost: proxy\r\n"
            f"Authorization: Bearer {proxy.mint(AGENT)}\r\n{length}\r\n").encode() + body,
            wait=CLOSE_WAIT)
        ap.wait_for(lambda: ap.new_events(proxy.events),
                    ap.NEGATIVE_GRACE if close else EVENT_WAIT)
        events = ap.new_events(proxy.events)
    return raw, closed, events


@pytest.mark.parametrize("case", list(INTERIM), ids=list(INTERIM))
@pytest.mark.parametrize("method", ["POST", "GET"])
def test_ap16_an_interim_response_gives_one_502_and_closes(tmp_path, monkeypatch, case, method):
    interim_status, response = INTERIM[case]
    raw, closed, events = exchange(tmp_path, monkeypatch, response, method)
    status, headers, _ = ap.split_response(raw)
    assert status == 502, raw[:400]
    assert raw.count(b"HTTP/1.1 ") == 1, f"more than one response reached the agent: {raw!r}"
    assert closed, "the proxy must close the connection after the 502"
    lowered = raw.lower()
    for marker in (b"req_ap16_interim", b"interim-ap16", b"req_ap16_final", b"final-ap16",
                   b"ap16.css", b"upgrade: websocket", b'{"ok":true}'):
        assert marker not in lowered, f"an upstream header or body reached the agent: {raw!r}"
    assert "link" not in headers and "upgrade" not in headers
    assert events == [("upstream_error", {"agent": AGENT, "label": "acc",
                                          "status": interim_status})], events


def test_ap16_100_continue_is_not_an_interim_refusal(tmp_path, monkeypatch):
    """Guard: 100 Continue is excluded from AP16; the final 200 is relayed."""
    raw, _, events = exchange(tmp_path, monkeypatch,
                              b"HTTP/1.1 100 Continue\r\n\r\n" + FINAL_200, close=True)
    status, headers, rest = ap.split_response(raw)
    assert status == 200, raw[:400]
    assert headers.get("x-final-marker") == "final-ap16"
    assert b'{"ok":true}' in rest
    assert events == []
