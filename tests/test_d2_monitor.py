"""TM-R4: the monitor exposes 'watch in tmux'.

Contract gaps, resolved here as the least surprising interface (see the run
report): the action is named `tmux_open` in `monitor.actions.ACTIONS`, takes
`agent_id`, and a run row in `snapshot()["running"]` lists it under the key
`actions`.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent / "support"))
from d2_support import agent_id, proj  # noqa: E402

from multiagents.config import load  # noqa: E402
from multiagents.monitor import actions  # noqa: E402
from multiagents.monitor.snapshot import snapshot  # noqa: E402

AID = agent_id(1)
ACTION = "tmux_open"


@pytest.fixture
def cfg(proj, monkeypatch):
    monkeypatch.setenv("PATH", proj.env(tmux=True)["PATH"])
    monkeypatch.setenv("FAKE_TMUX_DIR", str(proj.tmux.dir))
    # Without this an unknown-action reply ("ok": False, a message) would satisfy
    # every refusal test below for the wrong reason.
    assert ACTION in actions.ACTIONS, f"monitor action {ACTION!r} is not registered yet"
    return load(proj.paths, seed=True)


def snap(proj, cfg):
    return snapshot(proj.paths, cfg, with_scripts=False)


def running_row(s, aid):
    (row,) = [r for r in s["running"] if r["id"] == aid]
    return row


def test_tm_r4_snapshot_with_tmux_lists_the_action_for_a_run(proj, cfg):
    proj.add_agent(AID, "running", [])
    row = running_row(snap(proj, cfg), AID)
    assert ACTION in row.get("actions", []), row


def test_tm_r4_every_run_row_gets_it(proj, cfg):
    ids = [agent_id(i) for i in (1, 2, 3)]
    for a in ids:
        proj.add_agent(a, "running", [])
    s = snap(proj, cfg)
    for a in ids:
        assert ACTION in running_row(s, a).get("actions", [])


def test_tm_r4_snapshot_without_tmux_lists_no_such_action(proj, cfg, monkeypatch):
    proj.add_agent(AID, "running", [])
    monkeypatch.setenv("PATH", "")
    s = snap(proj, cfg)
    assert ACTION not in json.dumps(s, default=str)
    assert "tmux" not in json.dumps(running_row(s, AID), default=str).lower()


def test_tm_r4_tmux_presence_is_read_at_snapshot_time(proj, cfg, monkeypatch):
    proj.add_agent(AID, "running", [])
    tmux_path = proj.env(tmux=True)["PATH"]
    monkeypatch.setenv("PATH", "")
    assert ACTION not in json.dumps(snap(proj, cfg), default=str)
    monkeypatch.setenv("PATH", tmux_path)
    assert ACTION in running_row(snap(proj, cfg), AID).get("actions", [])
    monkeypatch.setenv("PATH", "")
    assert ACTION not in json.dumps(snap(proj, cfg), default=str)


def test_tm_r4_the_action_is_registered_and_not_destructive(proj):
    assert ACTION in actions.ACTIONS
    assert ACTION not in actions.DESTRUCTIVE


def test_tm_r4_invoking_the_action_returns_the_attach_command(proj, cfg):
    proj.add_agent(AID, "running", [])
    result = actions.perform(proj.paths, ACTION, {"agent_id": AID})
    assert result["ok"] is True, result
    assert proj.attach_command(AID) in json.dumps(result)
    assert proj.tmux.windows(proj.sock, proj.session).count(AID) == 1


def test_tm_r4_invoking_twice_is_idempotent(proj, cfg):
    proj.add_agent(AID, "running", [])
    a = actions.perform(proj.paths, ACTION, {"agent_id": AID})
    b = actions.perform(proj.paths, ACTION, {"agent_id": AID})
    assert a["ok"] and b["ok"]
    assert proj.tmux.windows(proj.sock, proj.session).count(AID) == 1
    assert len(proj.tmux.calls_of("new-session", "new")) == 1


@pytest.mark.parametrize("bad", ["../x", "ag-zzz", "", "ag-abc123\n", "ag-abc123;x"])
def test_tm_r4_invalid_id_is_a_message_not_a_traceback_and_never_reaches_tmux(proj, cfg, bad):
    result = actions.perform(proj.paths, ACTION, {"agent_id": bad})
    assert result["ok"] is False
    assert result["message"]
    assert proj.tmux.calls == []


def test_tm_r4_missing_id_is_refused(proj, cfg):
    result = actions.perform(proj.paths, ACTION, {})
    assert result["ok"] is False and proj.tmux.calls == []


def test_tm_r4_without_tmux_the_action_fails_cleanly(proj, cfg, monkeypatch):
    proj.add_agent(AID, "running", [])
    monkeypatch.setenv("PATH", "")
    result = actions.perform(proj.paths, ACTION, {"agent_id": AID})
    assert result["ok"] is False
    assert "tmux" in result["message"].lower()
    assert not proj.sock_dir.exists()


def test_tm_r4_the_action_does_not_attach(proj, cfg):
    proj.add_agent(AID, "running", [])
    actions.perform(proj.paths, ACTION, {"agent_id": AID})
    assert proj.tmux.calls_of("attach", "attach-session", "a", "at") == []
