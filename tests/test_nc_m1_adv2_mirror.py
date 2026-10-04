"""Adversary round 2 (M1, NC-R14/NC-R3): the events.jsonl mirror against a
container-writable file.

`.multiagents/` is mounted writable into the container, so any process there
(no token needed) can open events.jsonl. Property: every transition appears
in events.jsonl, whatever a non-host process does to the file's lock state.
The mirror takes LOCK_EX|LOCK_NB and gives up while anyone else holds a lock
on the file, and retries forever without ever writing.
"""
from __future__ import annotations

import fcntl
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

from nc_harness import live, nc  # noqa: E402,F401


def test_adv2_a_lock_held_on_events_jsonl_does_not_suppress_the_mirror(live):
    live.create(task="before")
    assert "created" in live.event_kinds()
    with open(live.events, "ab") as held:
        fcntl.flock(held, fcntl.LOCK_SH)      # what any reader in the container may do
        node = live.create(task="while a reader holds the file")
        deadline = time.time() + 5            # the daemon retries every second
        while time.time() < deadline and live.event_kinds().count("created") < 2:
            time.sleep(0.2)
        mirrored = live.event_kinds().count("created")
    assert mirrored == 2, (
        f"node {node['id']} was created while another process held a shared flock on "
        f"{live.events}; after 5s events.jsonl has {mirrored} node.created line(s)")
