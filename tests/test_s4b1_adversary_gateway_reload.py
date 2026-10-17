"""Adversary, slice 4B.1: another attempt's grant change drops my connections.

HOLE (severity MEDIUM, cross-attempt revocation):
the trusted gateway program `_GATEWAY_MAIN` (src/multiagents/scheduler/gateways.py)
reloads `/authority/policy.json` by `handle.close()` and a fresh
`serve_egress(...)`. `_Egress.close()` shuts down EVERY accepted connection of
EVERY grant. The host rewrites policy.json on every grant (`_publish`), every
revoke (the grant is popped and the generation bumped) and every reconcile, so
ending, cancelling or starting any one attempt kills all other attempts'
in-flight requests and live CONNECT tunnels on the shared gateway, and
leaves a window in which the listener is closed.

IC-C29: revocation "affects new requests and accepted tunnels/streams ... It
does not revoke another attempt's authority, even when accounts or gateways are
shared." D-S4B.6 allows existing connections to fail only on gateway
replacement, which this is not (same container, same receipt).

The gateway program is run as shipped, with only its fixed listen ports and
its `/authority` mount path rebound to a temporary directory so it can run on
this machine. No external host is contacted: the in-flight request is for a
port the policy refuses, so its correct answer is the gateway's own 403.
"""
import json
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import ic_s4b as nb  # noqa: E402

from multiagents.scheduler import gateways  # noqa: E402

G1 = {"username": "ma1-att_0001", "password": "a1" * 32, "networks": ["127.0.1.0/24"]}
G2 = {"username": "ma1-att_0002", "password": "b2" * 32, "networks": ["127.0.2.0/24"]}


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Gateway:
    def __init__(self, tmp_path):
        self.code, self.authority = tmp_path / "gateway", tmp_path / "authority"
        self.code.mkdir()
        self.authority.mkdir()
        self.port, self.management = free_port(), free_port()
        shutil.copyfile(Path(gateways.__file__).with_name("egress.py"), self.code / "egress.py")
        main = gateways._GATEWAY_MAIN
        for old, new in (("pathlib.Path('/authority')", f"pathlib.Path({str(self.authority)!r})"),
                         ("('0.0.0.0', 8888)", f"('127.0.0.1', {self.port})"),
                         ("('127.0.0.1', 8889)", f"('127.0.0.1', {self.management})")):
            assert old in main, f"the gateway program changed shape: {old!r} not found"
            main = main.replace(old, new)
        (self.code / "main.py").write_text(main)
        (self.authority / "management").write_text("m" * 64)
        self.generation = 0
        self.publish({"g1": G1, "g2": G2})
        self.proc = subprocess.Popen([sys.executable, str(self.code / "main.py")], cwd=self.code,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.wait_applied()

    def publish(self, grants):
        self.generation += 1
        policy = {"allowlist": ["example.com"], "grants": grants, "forbidden_networks": [],
                  "generation": self.generation, "policy_digest": "d" * 64}
        tmp = self.authority / "policy.json.tmp"
        tmp.write_text(json.dumps(policy))
        tmp.replace(self.authority / "policy.json")

    def wait_applied(self):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                request = urllib.request.Request(f"http://127.0.0.1:{self.management}/ready",
                                                 headers={"Authorization": "Bearer " + "m" * 64})
                with urllib.request.urlopen(request, timeout=1) as reply:
                    if json.loads(reply.read()).get("generation") == self.generation:
                        return
            except OSError:
                pass
            time.sleep(.05)
        raise AssertionError("the gateway never applied the policy")

    def open(self, source):
        c = socket.socket()
        c.settimeout(nb.SHORT)
        c.bind((source, 0))
        c.connect(("127.0.0.1", self.port))
        return c

    def close(self):
        self.proc.kill()
        self.proc.wait(5)


@pytest.fixture
def gw(tmp_path):
    g = Gateway(tmp_path)
    yield g
    g.close()


def test_s4b1_adversary_revoking_attempt_two_does_not_drop_attempt_ones_in_flight_request(gw):
    """MEDIUM: attempt 1's request in flight is cut when attempt 2's grant is revoked."""
    c = gw.open("127.0.1.5")
    try:
        c.sendall(b"CONNECT example.com:25 HTTP/1.1\r\nHost: example.com:25\r\n")
        time.sleep(.2)
        gw.publish({"g1": G1})          # the host's revoke("att_0002"): g2 popped, generation bumped
        gw.wait_applied()
        try:
            c.sendall(("Proxy-Authorization: " + nb.basic(G1["username"], G1["password"]) + "\r\n\r\n").encode())
            status, _, _ = nb.read_response(c)
        except OSError:
            status = 0
    finally:
        c.close()
    assert status == 403, (
        f"attempt 1 got {status or 'a closed connection'} instead of the gateway's 403: revoking attempt 2 "
        "reloaded the policy by closing every connection of every grant")


def test_s4b1_adversary_granting_a_new_attempt_does_not_drop_attempt_ones_in_flight_request(gw):
    """MEDIUM: starting any new attempt (a grant) cuts the others' in-flight requests too."""
    c = gw.open("127.0.1.5")
    try:
        c.sendall(b"CONNECT example.com:25 HTTP/1.1\r\nHost: example.com:25\r\n")
        time.sleep(.2)
        gw.publish({"g1": G1, "g2": G2, "g3": {"username": "ma1-att_0003", "password": "c3" * 32,
                                                 "networks": ["127.0.3.0/24"]}})
        gw.wait_applied()
        try:
            c.sendall(("Proxy-Authorization: " + nb.basic(G1["username"], G1["password"]) + "\r\n\r\n").encode())
            status, _, _ = nb.read_response(c)
        except OSError:
            status = 0
    finally:
        c.close()
    assert status == 403, f"attempt 1 got {status or 'a closed connection'}: a sibling's grant reset it"
