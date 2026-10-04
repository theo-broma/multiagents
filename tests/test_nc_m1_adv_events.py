"""Adversary (M1, NC-R14/NC-R3): `.multiagents/events.jsonl` is shared with
Tree and is mounted writable into containers, so the scheduler's mirror must
tolerate any line in it. One hostile line must neither stop the scheduler
from starting nor turn a committed write into an error reply.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

from nc_harness import live, nc  # noqa: E402,F401


def _hostile_lines(sched):
    return [b'{"kind":"x","note":"\xff"}\n',                      # invalid UTF-8
            json.dumps({"scheduler_project": sched.slug,           # unhashable seq
                        "scheduler_seq": []}).encode() + b"\n"]


def test_adv_invalid_utf8_in_the_event_log_does_not_stop_startup(nc):
    nc.events.parent.mkdir(parents=True, exist_ok=True)
    nc.events.write_bytes(_hostile_lines(nc)[0])
    nc.start()
    assert nc.create(task="after a bad line")["id"]


def test_adv_unhashable_scheduler_seq_in_the_event_log_does_not_stop_startup(nc):
    nc.events.parent.mkdir(parents=True, exist_ok=True)
    nc.events.write_bytes(_hostile_lines(nc)[1])
    nc.start()
    assert nc.create(task="after a bad line")["id"]


def test_adv_a_committed_write_is_not_reported_as_invalid_after_a_bad_event_line(live):
    for line in _hostile_lines(live):
        with open(live.events, "ab") as f:
            f.write(line)
        before = len(live.snapshot()["nodes"])
        reply = live.create_raw({"kind": "simple", "agent": "worker", "task": "t"})
        after = len(live.snapshot()["nodes"])
        assert after == before + 1          # the write did commit
        assert reply.get("ok") is True and "error" not in reply, (
            f"committed create replied {reply!r} after event line {line!r}")
