"""Adversary round 3 (M1): RPC writes and host capability seams interleave on
one store; every write must either apply or be refused cleanly, never fail as
`internal` from a lock upgrade."""
from __future__ import annotations

import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

from nc_harness import live, nc  # noqa: E402,F401


def test_adv3_rpc_creates_and_host_capability_issues_interleave_without_internal_errors(live):
    node = live.create()
    token = live.root_token()
    stop = threading.Event()
    failures: list = []

    def issue(worker):
        i = 0
        while not stop.is_set():
            try:
                live.issue(f"run-{worker}-{i}", node["id"], {"read"})
            except Exception as exc:     # surfaced by the assertion below
                failures.append(exc)
            i += 1

    threads = [threading.Thread(target=issue, args=(w,)) for w in range(3)]
    for t in threads:
        t.start()
    try:
        replies = []
        for _ in range(40):
            base = live.ok("list_nodes", {}, token=token)["plan_revision"]
            replies.append(live.rpc("create_node", {"kind": "simple", "agent": "worker",
                                                    "task": "t", "plan_revision": base},
                                    token=token))
            replies.append(live.rpc("ack_nodes", {"cursor": 0}, token=token))
    finally:
        stop.set()
        for t in threads:
            t.join(timeout=60)
    bad = [r for r in replies if not r.get("ok")]
    assert not bad, bad[:3]
    assert not failures, failures[:3]
