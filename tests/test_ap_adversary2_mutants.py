"""AP1 / AP3 / AP5 / AP7, second adversary pass: guards for surviving mutants.

Contract: `context/specs/findings-c1-c2-part2.md`, AP section. These pass
today. Each pins a line of `authproxy.py` that the existing AP, HF1 and HF6
suites let a mutation through unnoticed:

- AP1 depth limit: `_scrub`'s `depth > 24` raised to `> 25` survived, which
  lets a secret-shaped string at depth 25 past both the mask and
  `redact.scrub` (whose own guard returns it raw);
- AP5 retry-after: dropping `l not in rejected` from the cooldown ends
  survived, turning "every account rejected" into a 429;
- AP7 hop-by-hop: ignoring the names listed in upstream's `Connection`
  survived, forwarding a nominated hop header on a 2xx;
- AP3 under concurrency: a cooldown must end at the latest hint whatever
  order concurrent 429s land in.
"""

import json
import math
import sys
import threading
import time
from pathlib import Path

import pytest

from multiagents import authproxy, redact

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))
import ap_support as ap

SECRET_SHAPE = "sk-" + "Q7" * 12


@pytest.fixture(autouse=True)
def isolated_registration(monkeypatch):
    monkeypatch.setattr(redact, "_literals", set())


@pytest.mark.parametrize("depth", [23, 24, 25, 26, 40])
def test_ap1_secret_shape_at_every_depth_is_masked(depth):
    value = SECRET_SHAPE
    for _ in range(depth):
        value = [value]
    out = authproxy._scrub(json.dumps({"error": value}).encode())
    assert SECRET_SHAPE.encode() not in out, (depth, out[-120:])


def test_ap5_limited_but_rejected_account_gives_401_without_retry_after(tmp_path, monkeypatch):
    upstream = ap.Upstream()
    with ap.upstream_server(upstream) as url, ap.proxy_server(
            tmp_path, monkeypatch, accounts={"a": "tok-a"}, upstream_url=url) as proxy:
        accounts = authproxy.Handler.accounts
        accounts.token("a")
        with accounts.lock:
            accounts.rejected["a"] = accounts._cache["a"][0]
            accounts.limited["a"] = time.monotonic() + 120
        status, headers, _ = ap.request(proxy)
    assert upstream.requests == []
    assert status == 401
    assert "retry-after" not in headers


def test_ap7_connection_nominated_header_is_not_relayed(tmp_path, monkeypatch):
    upstream = ap.Upstream(lambda req: ap.Reply(200, [
        ("Connection", "keep-alive, X-Hop-Marker"), ("X-Hop-Marker", "hop-adv2"),
        ("content-type", "application/json")], b'{"ok":true}'))
    with ap.upstream_server(upstream) as url, ap.proxy_server(
            tmp_path, monkeypatch, accounts={"a": "tok-a"}, upstream_url=url) as proxy:
        status, headers, _ = ap.request(proxy)
    assert status == 200
    assert "x-hop-marker" not in headers


@pytest.mark.parametrize("seed", [11, 12, 13])
def test_ap3_concurrent_429s_keep_the_longest_cooldown(tmp_path, monkeypatch, seed):
    import random

    rng = random.Random(seed)
    hints = [rng.randint(60, 5000) for _ in range(8)]
    order = list(range(len(hints)))
    rng.shuffle(order)
    arrived = threading.Barrier(len(hints), timeout=ap.SOCKET_TIMEOUT)
    turn = threading.Condition()
    state = {"next": 0, "seen": 0}

    def responder(req):
        with turn:
            mine = state["seen"]
            state["seen"] += 1
        arrived.wait()
        # Answer in a shuffled order so the longest hint is not always last.
        with turn:
            turn.wait_for(lambda: order[state["next"]] == mine, timeout=ap.SOCKET_TIMEOUT)
            state["next"] += 1
            turn.notify_all()
        return ap.Reply(429, [("retry-after", str(hints[mine]))], b"{}")

    upstream = ap.Upstream(responder)
    with ap.upstream_server(upstream) as url, ap.proxy_server(
            tmp_path, monkeypatch, accounts={"a": "tok-a"}, upstream_url=url) as proxy:
        start = time.monotonic()
        threads = [threading.Thread(target=ap.request, args=(proxy,), kwargs={"agent": f"ag{i}"})
                   for i in range(len(hints))]
        for t in threads:
            t.start()
        for t in threads:
            t.join(ap.SOCKET_TIMEOUT * 2)
        until = authproxy.Handler.accounts.limited["a"]
        status, headers, _ = ap.request(proxy, agent="late")
    assert len(upstream.requests) == len(hints) + 0
    assert until >= start + max(hints)
    assert status == 429
    assert int(headers["retry-after"]) >= max(hints) - math.ceil(time.monotonic() - start) - 1
