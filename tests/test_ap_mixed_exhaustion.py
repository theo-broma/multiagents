"""AP10 — mixed exhaustion, and a callback raising any exception.

Contract: `context/specs/findings-c1-c2-part2.md`, Part 2b, "AP — adversary
findings", AP10 (with AP5 and AP2).

- when no account is usable and at least one of the agent's accounts is
  limited (the others rejected), the proxy answers 429 with `retry-after`
  from the limited accounts, as in AP5; only when every account is rejected
  does it answer 401;
- AP2 covers a callback raising any exception, `SystemExit` and
  `KeyboardInterrupt` included: the request still completes, and the
  failure is reported on stderr naming the event kind and exception type.
"""

import sys
from pathlib import Path

import pytest

from multiagents import redact

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))
import ap_support as ap

AGENT = "agent-ap10"


@pytest.fixture(autouse=True)
def isolated_registration(monkeypatch):
    monkeypatch.setattr(redact, "_literals", set())


def rejected_or_limited(rejected_token, seconds=120):
    def responder(req):
        if req.token == rejected_token:
            return ap.Reply(401, [("content-type", "application/json")],
                            b'{"type":"error","error":{"type":"authentication_error"}}')
        return ap.Reply(429, [("retry-after", str(seconds))], b"{}")
    return responder


# ---------------------------------------------------------------------------
# One rejected, one limited
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("rejected", ["tok-a", "tok-b"])
def test_ap10_one_rejected_and_one_limited_gives_429_with_retry_after(tmp_path, monkeypatch,
                                                                      rejected):
    ap.FakeClock(monkeypatch)
    upstream = ap.Upstream(rejected_or_limited(rejected))
    with ap.upstream_server(upstream) as url, \
            ap.proxy_server(tmp_path, monkeypatch, accounts={"acc-a": "tok-a", "acc-b": "tok-b"},
                            upstream_url=url) as proxy:
        status, headers, _ = ap.request(proxy, agent=AGENT)
    assert sorted(upstream.tokens()) == ["tok-a", "tok-b"], "each account tried once"
    assert status == 429
    assert headers.get("retry-after") == "120"


def test_ap10_a_later_request_meets_the_same_mix_and_still_gets_429(tmp_path, monkeypatch):
    clock = ap.FakeClock(monkeypatch)
    upstream = ap.Upstream(rejected_or_limited("tok-a"))
    with ap.upstream_server(upstream) as url, \
            ap.proxy_server(tmp_path, monkeypatch, accounts={"acc-a": "tok-a", "acc-b": "tok-b"},
                            upstream_url=url) as proxy:
        assert ap.request(proxy, agent=AGENT)[0] == 429
        clock.advance(30)
        status, headers, _ = ap.request(proxy, agent="agent-ap10-later")
        assert "tok-b" not in upstream.tokens()[2:], "the limited account is cooling down"
    assert status == 429
    assert headers.get("retry-after") == "90"


def test_ap10_a_rejection_before_the_mix_still_gives_429(tmp_path, monkeypatch):
    """acc-a is rejected by one request and acc-b limited by the next: the
    second request's exhaustion is the mix, not the rejection alone."""
    ap.FakeClock(monkeypatch)
    limited = {"on": False}

    def responder(req):
        if req.token == "tok-a":
            return ap.Reply(401, [], b'{"error":"expired"}')
        if limited["on"]:
            return ap.Reply(429, [("retry-after", "600")], b"{}")
        return ap.Reply(200, [("content-type", "application/json")], b'{"ok":true}')

    upstream = ap.Upstream(responder)
    with ap.upstream_server(upstream) as url, \
            ap.proxy_server(tmp_path, monkeypatch, accounts={"acc-a": "tok-a", "acc-b": "tok-b"},
                            upstream_url=url) as proxy:
        # Until acc-a has been reached, whichever account each agent is given.
        for n in range(8):
            assert ap.request(proxy, agent=f"agent-ap10-{n}")[0] == 200
            if "tok-a" in upstream.tokens():
                break
        assert "tok-a" in upstream.tokens()
        limited["on"] = True
        status, headers, _ = ap.request(proxy, agent="agent-ap10-third")
    assert status == 429
    assert headers.get("retry-after") == "600"


def test_ap10_every_account_rejected_gives_401_without_retry_after(tmp_path, monkeypatch):
    upstream = ap.Upstream(lambda req: ap.Reply(401, [("content-type", "application/json")],
                                                b'{"error":"expired"}'))
    with ap.upstream_server(upstream) as url, \
            ap.proxy_server(tmp_path, monkeypatch, accounts={"acc-a": "tok-a", "acc-b": "tok-b"},
                            upstream_url=url) as proxy:
        first = ap.request(proxy, agent=AGENT)
        later = ap.request(proxy, agent="agent-ap10-later")
    for status, headers, _ in (first, later):
        assert status == 401
        assert "retry-after" not in headers


# ---------------------------------------------------------------------------
# A callback raising SystemExit or KeyboardInterrupt
# ---------------------------------------------------------------------------

def _raising(exc_type):
    calls = []

    def callback(kind, fields):
        calls.append(kind)
        raise exc_type("callback gave up")
    return callback, calls


def _limited_then_ok(req):
    if req.token == "tok-a":
        return ap.Reply(429, [("retry-after", "120")], b"{}")
    return ap.Reply(200, [("content-type", "application/json")], b'{"served":true}')


def _scenarios():
    # (id, accounts, responder, request kwargs, expected status)
    return [
        ("refused-target", {"acc-a": "tok-a"}, None, {"target": ".attacker.invalid/x"}, 400),
        ("switch-then-200", {"acc-a": "tok-a", "acc-b": "tok-b"}, _limited_then_ok, {}, 200),
        ("upstream-500", {"acc-a": "tok-a"},
         lambda req: ap.Reply(500, [("content-type", "application/json")], b'{"e":1}'), {}, 500),
        ("exhausted-429", {"acc-a": "tok-a"},
         lambda req: ap.Reply(429, [("retry-after", "120")], b"{}"), {}, 429),
        ("unauthenticated-401", {"acc-a": "tok-a"},
         lambda req: ap.Reply(401, [], b'{"error":"expired"}'), {}, 401),
    ]


@pytest.mark.parametrize("exc_type", [SystemExit, KeyboardInterrupt, GeneratorExit],
                         ids=["SystemExit", "KeyboardInterrupt", "GeneratorExit"])
@pytest.mark.parametrize("scenario", _scenarios(), ids=[s[0] for s in _scenarios()])
def test_ap10_a_callback_raising_a_base_exception_still_completes_the_request(
        tmp_path, monkeypatch, capfd, exc_type, scenario):
    _, accounts, responder, kwargs, expected = scenario
    callback, calls = _raising(exc_type)
    upstream = ap.Upstream(responder)
    with ap.upstream_server(upstream) as url, \
            ap.proxy_server(tmp_path, monkeypatch, accounts=accounts, upstream_url=url,
                            on_event=callback) as proxy:
        status, _, _ = ap.request(proxy, agent=AGENT, **kwargs)
        assert status == expected
        # The proxy keeps serving: an unsigned request is answered too.
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", proxy.port, timeout=ap.SOCKET_TIMEOUT)
        try:
            conn.request("POST", "/v1/messages", body=b"{}")
            assert conn.getresponse().status == 401
        finally:
            conn.close()
    assert calls, "the scenario must have produced at least one event"
    err = capfd.readouterr().err
    reports = [line for line in err.splitlines() if exc_type.__name__ in line]
    assert reports, f"no stderr report names {exc_type.__name__}: {err!r}"
    for kind in set(calls):
        assert any(kind in line for line in reports), f"no report names {kind!r}: {reports}"
