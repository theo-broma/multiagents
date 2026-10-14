"""AP3 — F67: the per-account cooldown is the backoff.

Contract: `context/specs/findings-c1-c2-part2.md`, Part 2b, AP3, with
Decisions 8 and 11.

- a 429 or 529 marks the account limited for max(60 s, hint), or 3600 s
  without a usable hint; the hint is `retry-after` (delta-seconds or
  HTTP-date), else `anthropic-ratelimit-unified-reset` (epoch seconds or ISO
  8601); a hint that does not parse, is not finite or is in the past is not
  usable;
- a new mark never shortens a running cooldown, whichever request sets it;
- no attempt admitted after the mark uses the account before the end;
- within one request each account is tried at most once;
- the proxy never sleeps.

How a cooldown is observed: whether the next request reaches the fake
upstream with that account's token. Time is moved with `ap_support.FakeClock`
(the `time` module's `time()` and `monotonic()`); nothing sleeps. Each
cooldown is probed 5 s before and 5 s after its expected end, which absorbs
the HTTP-date's one-second resolution and the real milliseconds that pass.
"""

import contextlib
import email.utils
import json
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))
import ap_support as ap

MARGIN = 5.0
OK = ap.Reply(200, [("content-type", "application/json")], b'{"ok":true}')


def http_date(ts: float) -> str:
    return email.utils.formatdate(ts, usegmt=True)


def iso(ts: float, z: bool) -> str:
    text = datetime.fromtimestamp(int(ts), timezone.utc).isoformat()
    return text.replace("+00:00", "Z") if z else text


RESET = "anthropic-ratelimit-unified-reset"

# (id, status, headers(now) -> list of pairs, expected cooldown in seconds)
HINTS = [
    ("delta-120", 429, lambda now: [("retry-after", "120")], 120),
    ("delta-below-floor", 429, lambda now: [("retry-after", "5")], 60),
    ("delta-at-floor", 429, lambda now: [("retry-after", "60")], 60),
    ("delta-529", 529, lambda now: [("retry-after", "120")], 120),
    ("no-hint", 429, lambda now: [], 3600),
    ("no-hint-529", 529, lambda now: [], 3600),
    ("http-date-future", 429, lambda now: [("retry-after", http_date(now + 7200))], 7200),
    ("http-date-past", 429, lambda now: [("retry-after", http_date(now - 600))], 3600),
    ("delta-inf", 429, lambda now: [("retry-after", "inf")], 3600),
    ("delta-nan", 429, lambda now: [("retry-after", "nan")], 3600),
    ("delta-negative", 429, lambda now: [("retry-after", "-30")], 3600),
    ("delta-garbage", 429, lambda now: [("retry-after", "soon")], 3600),
    ("reset-epoch", 429, lambda now: [(RESET, str(int(now + 7200)))], 7200),
    ("reset-iso-z", 429, lambda now: [(RESET, iso(now + 7200, z=True))], 7200),
    ("reset-iso-offset", 429, lambda now: [(RESET, iso(now + 7200, z=False))], 7200),
    ("reset-epoch-past", 429, lambda now: [(RESET, str(int(now - 600)))], 3600),
    ("reset-iso-past", 429, lambda now: [(RESET, iso(now - 600, z=True))], 3600),
    ("reset-inf", 429, lambda now: [(RESET, "inf")], 3600),
    ("reset-nan", 429, lambda now: [(RESET, "nan")], 3600),
    # Epoch seconds, so 120 is a moment in 1970: in the past, not a duration.
    ("reset-small-epoch", 429, lambda now: [(RESET, "120")], 3600),
    ("retry-after-wins-over-reset", 429,
     lambda now: [("retry-after", "120"), (RESET, str(int(now + 7200)))], 120),
]


@contextlib.contextmanager
def one_account_world(tmp_path, monkeypatch, responder):
    clock = ap.FakeClock(monkeypatch)
    upstream = ap.Upstream(responder)
    with ap.upstream_server(upstream) as url, \
            ap.proxy_server(tmp_path, monkeypatch, accounts={"acc": "tok"},
                            upstream_url=url) as proxy:
        def reaches_upstream_at(offset: float, agent="agent-probe") -> bool:
            clock.offset = offset
            before = len(upstream.requests)
            ap.request(proxy, agent=agent)
            return len(upstream.requests) > before

        yield clock, upstream, proxy, reaches_upstream_at


@pytest.mark.parametrize("status,headers,expected",
                         [h[1:] for h in HINTS], ids=[h[0] for h in HINTS])
def test_ap3_cooldown_is_max_of_floor_and_hint_or_an_hour(
        tmp_path, monkeypatch, status, headers, expected):
    marks = []

    def responder(req):
        if not marks:
            marks.append(time.time())
            return ap.Reply(status, headers(time.time()), b"{}")
        return OK

    with one_account_world(tmp_path, monkeypatch, responder) as (clock, upstream, proxy, probe):
        first, _, _ = ap.request(proxy, agent="agent-limited")
        assert first == 429
        assert len(upstream.requests) == 1
        assert not probe(expected - MARGIN), \
            f"account used again {MARGIN}s before its {expected}s cooldown ended"
        assert probe(expected + MARGIN), \
            f"account still unused {MARGIN}s after its {expected}s cooldown ended"


# ---------------------------------------------------------------------------
# A new mark never shortens a running cooldown
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("order", [("long", "short"), ("short", "long")],
                         ids=["long-then-short", "short-then-long"])
def test_ap3_a_second_mark_never_shortens_the_cooldown(tmp_path, monkeypatch, order):
    """Two requests already upstream on the one account (Decision: attempts
    sent are not recalled); both come back 429, in either order."""
    hints = {"long": "7200", "short": "120"}
    gates = {"long": threading.Event(), "short": threading.Event()}

    def responder(req):
        try:
            which = json.loads(req.body).get("id")
        except ValueError:
            which = None
        if which in gates:
            gates[which].wait(ap.SOCKET_TIMEOUT)
            return ap.Reply(429, [("retry-after", hints[which])], b"{}")
        return OK

    with one_account_world(tmp_path, monkeypatch, responder) as (clock, upstream, proxy, probe):
        results = {}

        def send(which):
            results[which] = ap.request(proxy, agent=f"agent-{which}",
                                        body=json.dumps({"id": which}).encode())[0]

        threads = {w: threading.Thread(target=send, args=(w,), daemon=True) for w in order}
        try:
            for t in threads.values():
                t.start()
            assert ap.wait_for(lambda: len(upstream.requests) == 2), \
                "both requests must be upstream before either is answered"
            for which in order:
                gates[which].set()
                threads[which].join(ap.SOCKET_TIMEOUT)
                assert results.get(which) == 429
        finally:
            for gate in gates.values():
                gate.set()

        assert not probe(120 + MARGIN), "the 120 s mark shortened the 7200 s cooldown"
        assert not probe(7200 - MARGIN)
        assert probe(7200 + MARGIN)


# ---------------------------------------------------------------------------
# No attempt admitted after the mark uses the account
# ---------------------------------------------------------------------------

def test_ap3_requests_after_the_mark_go_elsewhere_until_the_cooldown_ends(tmp_path, monkeypatch):
    clock = ap.FakeClock(monkeypatch)
    limited_once = []

    def responder(req):
        if req.token == "tok-a" and not limited_once:
            limited_once.append(True)
            return ap.Reply(429, [("retry-after", "120")], b"{}")
        return OK

    upstream = ap.Upstream(responder)
    with ap.upstream_server(upstream) as url, \
            ap.proxy_server(tmp_path, monkeypatch, accounts={"acc-a": "tok-a", "acc-b": "tok-b"},
                            upstream_url=url) as proxy:
        assert ap.request(proxy, agent="agent-1")[0] == 200
        assert upstream.tokens() == ["tok-a", "tok-b"]
        # agent-2 would take acc-a (fewest agents) if it were usable.
        for agent in ("agent-2", "agent-3", "agent-1"):
            assert ap.request(proxy, agent=agent)[0] == 200
        assert upstream.tokens()[2:] == ["tok-b", "tok-b", "tok-b"], \
            "an attempt admitted after the mark used the limited account"
        clock.offset = 120 + MARGIN
        assert ap.request(proxy, agent="agent-4")[0] == 200
        assert upstream.tokens()[-1] == "tok-a", "the account is usable after its cooldown"


# ---------------------------------------------------------------------------
# Within one request, each account is tried at most once
# ---------------------------------------------------------------------------

def test_ap3_one_request_tries_each_account_once_even_if_a_cooldown_ends(tmp_path, monkeypatch):
    clock = ap.FakeClock(monkeypatch)

    def responder(req):
        if req.token == "tok-b":
            # acc-a's 60 s cooldown is long over by the time acc-b answers.
            clock.advance(3600)
        return ap.Reply(429, [("retry-after", "1")], b"{}")

    upstream = ap.Upstream(responder)
    with ap.upstream_server(upstream) as url, \
            ap.proxy_server(tmp_path, monkeypatch, accounts={"acc-a": "tok-a", "acc-b": "tok-b"},
                            upstream_url=url) as proxy:
        status, _, _ = ap.request(proxy, agent="agent-1")
        assert status != 200
        assert upstream.tokens() == ["tok-a", "tok-b"]


def test_ap3_one_account_is_tried_once_per_request(tmp_path, monkeypatch):
    clock = ap.FakeClock(monkeypatch)

    def responder(req):
        clock.advance(3600)
        return ap.Reply(529, [("retry-after", "1")], b"{}")

    upstream = ap.Upstream(responder)
    with ap.upstream_server(upstream) as url, \
            ap.proxy_server(tmp_path, monkeypatch, accounts={"acc": "tok"},
                            upstream_url=url) as proxy:
        assert ap.request(proxy)[0] == 429
        assert upstream.tokens() == ["tok"]


# ---------------------------------------------------------------------------
# The proxy never sleeps (Decision 8)
# ---------------------------------------------------------------------------

def test_ap3_the_proxy_never_sleeps(tmp_path, monkeypatch):
    slept = []
    real_sleep = time.sleep
    main = threading.current_thread()

    def recording_sleep(seconds):
        if threading.current_thread() is not main:
            slept.append((threading.current_thread().name, seconds))
        real_sleep(min(seconds, 0.01))

    monkeypatch.setattr(time, "sleep", recording_sleep)
    upstream = ap.Upstream(lambda req: ap.Reply(429, [("retry-after", "30")], b"{}"))
    with ap.upstream_server(upstream) as url, \
            ap.proxy_server(tmp_path, monkeypatch, accounts={"acc-a": "tok-a", "acc-b": "tok-b"},
                            upstream_url=url) as proxy:
        started = time.monotonic()
        assert ap.request(proxy)[0] == 429
        assert ap.request(proxy, agent="agent-again")[0] == 429
        elapsed = time.monotonic() - started
    assert slept == [], f"the proxy slept: {slept}"
    assert elapsed < 2.0, f"two refusals took {elapsed:.1f}s"


# ---------------------------------------------------------------------------
# Decision 11: cooldowns live in memory (green today, stays green)
# ---------------------------------------------------------------------------

def test_ap3_a_restart_forgets_the_cooldown(tmp_path, monkeypatch):
    ap.FakeClock(monkeypatch)
    marks = []

    def responder(req):
        if not marks:
            marks.append(True)
            return ap.Reply(429, [("retry-after", "7200")], b"{}")
        return OK

    upstream = ap.Upstream(responder)
    with ap.upstream_server(upstream) as url:
        with ap.proxy_server(tmp_path, monkeypatch, accounts={"acc": "tok"},
                             upstream_url=url) as proxy:
            assert ap.request(proxy)[0] == 429
            assert ap.request(proxy)[0] == 429
            assert len(upstream.requests) == 1
        with ap.proxy_server(tmp_path, monkeypatch, accounts={"acc": "tok"},
                             upstream_url=url) as proxy:
            assert ap.request(proxy)[0] == 200
            assert len(upstream.requests) == 2
