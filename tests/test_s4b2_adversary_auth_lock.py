"""Adversary, slice 4B.2: one attempt's slow stream write freezes the shared gateway and revocation.

HOLE (severity MEDIUM, cross-attempt denial of service / revocation freeze):
In `src/multiagents/scheduler/auth_gateway.py`:
`_Auth.Handler.do_POST` streams response chunks to the guest client under the
handler-wide lock (line 173):
    while True:
        chunk = upstream.read(8192)
        with gateway.lock:
            if session["revoked"] or not chunk:
                return
            self.wfile.write(chunk)
            self.wfile.flush()
Additionally, `self.connection.settimeout(None)` is set at line 146.

Because `gateway.lock` (`self.lock = threading.RLock()`) is shared across all
attempts and all operations in `_Auth`, it guards:
* request authentication (`gateway.authenticate`) for every incoming request,
* dynamic grant updates (`gateway.update`),
* revocation enforcement (`gateway.revoke`).

If a guest client in Attempt 1 (grant `g1`) starts a streaming response but stops
reading from its socket (or reads slowly), the kernel socket buffer fills up and
`self.wfile.flush()` blocks on the TCP socket write without a timeout while holding
`gateway.lock`.

During this time:
1. `auth.revoke("g1")` or `auth.revoke("g2")` blocks trying to acquire `self.lock`.
   Revocation enforcement (IC-C29) is completely paralyzed by an uncooperative client.
2. Concurrent requests from a sibling attempt (grant `g2`) cannot authenticate
   because `gateway.authenticate` blocks on `gateway.lock`.
3. `auth.close()` hangs.

IC-C29: revocation "affects new requests and accepted tunnels/streams, closes their
connections and stops further forwarding. It does not revoke another attempt's
authority, even when accounts or gateways are shared."
K4B2.6: "The auth listener tracks every accepted request and stream by grant. A revoke
closes that grant's in-flight upstream reads, including streaming responses, and refuses
new requests (IC-C29). Other grants are untouched."
"""
import hashlib
import socket
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

from multiagents.scheduler.gateways import serve_auth  # noqa: E402
import ic_s4b as nb  # noqa: E402
import ic_s4b2_backend as be  # noqa: E402

BOUND = 1.0  # maximum acceptable time for revocation or sibling request


def verifier(bearer: str) -> str:
    return hashlib.sha256(bearer.encode()).hexdigest()


T1 = "ma-auth-v1." + "11" * 32
T2 = "ma-auth-v1." + "22" * 32


@pytest.fixture
def auth_env(tmp_path):
    grants = {
        "g1": {"verifier": verifier(T1), "networks": ["127.0.1.0/24"], "provider": "p1", "run_id": "r1"},
        "g2": {"verifier": verifier(T2), "networks": ["127.0.2.0/24"], "provider": "p2", "run_id": "r2"},
    }
    adapter = be.Adapter(tmp_path, project="test")
    # First response is a streaming response for g1
    adapter.next.append(be.Response(200, {"content-type": "text/event-stream"}, stream=True))
    # Second response is a standard response for g2
    adapter.next.append(be.Response(200, {"content-type": "application/json"}, body=b'{"ok":true}'))

    handle = serve_auth(("127.0.0.1", 0), grants, adapter)
    yield handle, adapter
    handle.close()


def test_s4b2_adversary_stream_write_contention_does_not_freeze_revocation(auth_env):
    """MEDIUM: revoking a grant hangs when a client's stream write blocks under the lock."""
    handle, adapter = auth_env

    # Client 1 connects and starts a streaming request, but does not read any data
    c1 = socket.socket()
    c1.bind(("127.0.1.5", 0))
    c1.connect(tuple(handle.address))
    c1.sendall((
        f"POST /v1/messages HTTP/1.1\r\n"
        f"Host: ma-auth:8930\r\n"
        f"Authorization: Bearer {T1}\r\n"
        f"Content-Length: 2\r\n\r\n{{}}"
    ).encode())

    feeder_stop = threading.Event()
    try:
        # Wait until upstream opens the stream
        deadline = time.monotonic() + nb.WAIT
        while not adapter.opened and time.monotonic() < deadline:
            time.sleep(0.01)
        assert adapter.opened, "stream request did not reach upstream adapter"

        upstream_resp = adapter.responses[0]

        # Feed enough data to fill the TCP loopback send buffer and stall wfile.flush()
        def feed_upstream():
            for _ in range(80):
                if feeder_stop.is_set():
                    break
                upstream_resp.feed(b"x" * 65536)

        feeder = threading.Thread(target=feed_upstream, daemon=True)
        feeder.start()
        time.sleep(0.3)

        # Attempt to revoke g1 while Client 1's write is blocked
        revoke_done = threading.Event()

        def do_revoke():
            handle.revoke("g1")
            revoke_done.set()

        rev_th = threading.Thread(target=do_revoke, daemon=True)
        rev_th.start()

        completed = revoke_done.wait(timeout=BOUND)
        assert completed, (
            f"handle.revoke('g1') hung waiting for the handler-wide lock while a client stream "
            f"write blocked: revocation cannot be delayed by uncooperative client stream I/O"
        )
    finally:
        feeder_stop.set()
        c1.close()
        if adapter.responses:
            adapter.responses[0].close()


def test_s4b2_adversary_stream_write_contention_does_not_block_sibling_requests(auth_env):
    """MEDIUM: a sibling request hangs when another attempt's stream write blocks under the lock."""
    handle, adapter = auth_env

    # Client 1 connects and stalls its stream
    c1 = socket.socket()
    c1.bind(("127.0.1.5", 0))
    c1.connect(tuple(handle.address))
    c1.sendall((
        f"POST /v1/messages HTTP/1.1\r\n"
        f"Host: ma-auth:8930\r\n"
        f"Authorization: Bearer {T1}\r\n"
        f"Content-Length: 2\r\n\r\n{{}}"
    ).encode())

    feeder_stop = threading.Event()
    try:
        deadline = time.monotonic() + nb.WAIT
        while not adapter.opened and time.monotonic() < deadline:
            time.sleep(0.01)
        assert adapter.opened, "stream request did not reach upstream adapter"

        upstream_resp = adapter.responses[0]

        def feed_upstream():
            for _ in range(80):
                if feeder_stop.is_set():
                    break
                upstream_resp.feed(b"x" * 65536)

        feeder = threading.Thread(target=feed_upstream, daemon=True)
        feeder.start()
        time.sleep(0.3)

        # Client 2 from grant g2 sends an independent request
        c2 = socket.socket()
        c2.settimeout(BOUND)
        c2.bind(("127.0.2.5", 0))
        c2.connect(tuple(handle.address))
        try:
            c2.sendall((
                f"POST /v1/messages HTTP/1.1\r\n"
                f"Host: ma-auth:8930\r\n"
                f"Authorization: Bearer {T2}\r\n"
                f"Content-Length: 2\r\n\r\n{{}}"
            ).encode())
            reply = c2.recv(1024)
            assert b"200 OK" in reply, (
                f"sibling attempt request failed or hung: {reply!r}; "
                f"handler-wide lock held during wfile.flush() starves sibling requests"
            )
        finally:
            c2.close()
    finally:
        feeder_stop.set()
        c1.close()
        if adapter.responses:
            adapter.responses[0].close()

