"""Adversary 3 — IC-C8 / IC-C13 / Q5: a pre-upgrade docker alias keeps its HOME.

Alias bindings written before slice 2 carry no `executor_kind`. Slice 2's
`sessions.freeze` defaults the missing field to "local" and mints a new
`home_id` whenever the recorded kind differs from the activation's. A docker
alias bound before the upgrade, activated again under docker, therefore gets
a fresh HOME while its `session_id` is kept: its conversation state is gone
although nothing about its executor changed (IC-C8: docker behaves exactly as
before; Q5: only a *change* of kind gives a fresh HOME).
"""
from __future__ import annotations

import hashlib
import sqlite3
from types import SimpleNamespace

import pytest

from multiagents.scheduler import sessions
from multiagents.scheduler.store import encode


def world(kind):
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE aliases (id TEXT PRIMARY KEY, record TEXT)")
    top = {"id": "top", "parent": None, "template": {"instance": "inst-1"}, "session": None,
           "pins": {}, "agent": None}
    node = {"id": "rv", "parent": "top", "session": "B", "pins": {}, "agent": "rv"}
    nodes = {"top": top, "rv": node}
    key = hashlib.sha256(encode(["inst-1", "B"]).encode()).hexdigest()
    # The shape NC-R30 bindings had before slice 2: no executor_kind.
    legacy = {"id": key, "alias": "B", "instance": "inst-1", "provider": "p", "account": "p",
              "model": "m", "session_id": "ses-legacy", "worktree": "/nonexistent/alias",
              "home_id": "alias-" + key[:24]}
    sessions.save_alias(db, legacy)
    config = SimpleNamespace(providers={"p": {}}, agents={}, executor=kind)
    return db, node, nodes, legacy, config


@pytest.mark.parametrize("kind", ["local", "docker"])
def test_ic_adv3_a_pre_upgrade_alias_keeps_its_home_under_its_unchanged_legacy_kind(kind):
    db, node, nodes, legacy, config = world(kind)
    result = {"provider": "p", "model": "m", "executor_selection": {"kind": kind}}
    frozen = sessions.freeze(db, node, nodes, result, None, config)
    assert frozen["session_id"] == "ses-legacy"
    assert frozen["alias_home"] == legacy["home_id"], (
        f"a {kind} alias bound before the upgrade lost its HOME while keeping its session id")
