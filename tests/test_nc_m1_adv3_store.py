"""Adversary round 3 (M1): capability seams and the host event mirror."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

from nc_harness import code, live, nc  # noqa: E402,F401


def test_adv3_renewing_a_runs_capability_revokes_the_previous_token(live):
    node = live.create()
    old = live.issue("run-1", node["id"], {"read"})
    new = live.issue("run-1", node["id"], {"read"})
    assert code(live.rpc("get_node", {"id": node["id"]}, token=old)) == "unauthenticated"
    assert live.rpc("get_node", {"id": node["id"]}, token=new)["ok"] is True


def test_adv3_a_capability_for_an_unknown_node_is_not_issued(live):
    with pytest.raises(ValueError):
        live.issue("run-1", "nd-00000000", {"read"})


@pytest.mark.parametrize("permissions", [{"admin"}, {"read", "root"}, {"cancel"}])
def test_adv3_an_unknown_permission_is_not_issued(live, permissions):
    node = live.create()
    with pytest.raises(ValueError):
        live.issue("run-1", node["id"], permissions)


def test_adv3_a_mirror_truncated_in_place_gets_the_whole_history_again(live):
    live.create()
    with live.events.open("r+b") as f:
        f.truncate(0)
    assert os.path.getsize(live.events) == 0
    live.create()
    assert live.event_kinds() == ["scheduler_started", "created", "created"]


def test_adv3_a_mirror_replaced_by_a_larger_file_gets_the_whole_history_again(live):
    live.create()
    size = os.path.getsize(live.events)
    replacement = live.events.with_name("events.new")
    replacement.write_bytes(b'{"kind":"unrelated"}\n' * (size // 10 + 10))
    os.replace(replacement, live.events)
    live.create()
    assert live.event_kinds() == ["scheduler_started", "created", "created"]
