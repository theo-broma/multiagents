"""Adversary, slice 4B.1: one attempt's slow dial freezes every other attempt.

HOLE (severity MEDIUM, cross-attempt interference / denial of egress):
`_Egress._handle` calls the injected `connect(address, port)` while holding
the handler-wide `self.lock` (src/multiagents/scheduler/egress.py, the
`with self.lock: ... upstream = self.connect(address, port)` block). The same
lock guards every other connection's authentication, every `_send`, every
relay liveness check and `revoke()`. The production dialer is
`socket.create_connection(..., timeout=10)`, so a guest that CONNECTs to an
allowlisted name whose address drops SYNs holds the lock for up to 10 s per
request, in a loop. During that time:

* a sibling attempt (another grant, another network) cannot authenticate or
  move a byte through its already-open tunnel;
* `revoke()` of any grant blocks, so revocation enforcement (IC-C29) is
  delayed by an unrelated attempt.

IC-C29: revocation "does not revoke another attempt's authority, even when
accounts or gateways are shared"; D-S4B.2: gateways are shared by several
attempt networks. One attempt must not be able to stall the shared gateway.

Driven only through the K4B.1 seam with injected resolver and dialer.
"""
import socket
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import ic_s4b as nb  # noqa: E402

SLOW = 2.5      # how long the hostile attempt's dial stalls
BOUND = 1.0     # what a sibling may reasonably wait for an unrelated dial


class SlowWorld(nb.Egress):
    """`slow.example.com` dials slowly (a blackholed address); everything else is immediate."""

    def __init__(self):
        super().__init__(answers={"slow.example.com": [nb.PUBLIC_V4], "example.com": [nb.PUBLIC_V4_B]})
        self.dialling = threading.Event()

    def connect(self, address, port):
        if address == nb.PUBLIC_V4:
            self.dialling.set()
            time.sleep(SLOW)
            raise OSError("connection timed out")
        return super().connect(address, port)


@pytest.fixture
def world():
    w = SlowWorld().start()
    yield w
    w.close()


def stall(world):
    """Attempt 1 (g1) starts a CONNECT whose dial blackholes."""
    c = world.connect_raw("slow.example.com:443", grant="g1", source="127.0.1.5")
    assert world.dialling.wait(nb.SHORT), "the hostile dial never started"
    return c


def test_s4b1_adversary_a_sibling_request_is_not_blocked_by_another_attempts_slow_dial(world):
    """MEDIUM: g2's plain request waits for g1's unrelated dial to time out."""
    hostile = stall(world)
    try:
        began = time.monotonic()
        status, _, _ = world.get("http://example.com/", grant="g2", source="127.0.2.5")
        took = time.monotonic() - began
    finally:
        hostile.close()
    assert status == 200, status
    assert took < BOUND, (
        f"attempt g2's request took {took:.2f}s: it waited for attempt g1's dial "
        f"(stalled {SLOW}s) because connect() runs under the handler-wide lock")


def test_s4b1_adversary_a_sibling_open_tunnel_does_not_freeze_during_another_attempts_dial(world):
    """MEDIUM: bytes on g2's established tunnel stop while g1 is dialling."""
    tunnel = world.connect_raw("example.com:443", grant="g2", source="127.0.2.5")
    try:
        status, _, _ = nb.read_response(tunnel)
        assert status == 200, status
        assert nb.tunnel_alive(tunnel, b"warm"), "the sibling tunnel never worked"
        hostile = stall(world)
        try:
            began = time.monotonic()
            alive = nb.tunnel_alive(tunnel, b"ping")
            took = time.monotonic() - began
        finally:
            hostile.close()
    finally:
        tunnel.close()
    assert alive
    assert took < BOUND, f"g2's live tunnel stalled {took:.2f}s behind g1's dial"


def test_s4b1_adversary_revoke_is_not_delayed_by_another_attempts_dial(world):
    """MEDIUM: revoking g2 blocks until g1's unrelated dial gives up (IC-C29 timeliness)."""
    hostile = stall(world)
    try:
        began = time.monotonic()
        world.handle.revoke("g2")
        took = time.monotonic() - began
    finally:
        hostile.close()
    assert took < BOUND, f"revoke('g2') took {took:.2f}s: it waited for g1's dial under the shared lock"
