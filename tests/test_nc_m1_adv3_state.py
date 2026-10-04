"""Adversary round 3 (M1): NC-R3 modes of host state and transport, and the
cancel refusal while a descendant is live (NC-R59 with M2 deferred)."""
from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

from nc_harness import code, files_under, live, nc  # noqa: E402,F401


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_adv3_every_file_in_the_state_dir_is_0600_even_under_a_permissive_umask(nc):
    old = os.umask(0o022)       # the scheduler inherits it from its launcher
    try:
        nc.start()
    finally:
        os.umask(old)
    nc.create()
    for path in files_under(nc.state_dir):
        if path.is_dir():
            assert _mode(path) == 0o700, path
        else:
            assert _mode(path) == 0o600, (path, oct(_mode(path)))
    assert _mode(nc.rpc_dir) == 0o700
    assert _mode(nc.sock) == 0o600


def test_adv3_a_state_dir_left_permissive_is_made_private_at_start(nc):
    nc.state_dir.mkdir(parents=True, mode=0o755)
    nc.state_dir.chmod(0o755)
    nc.rpc_dir.mkdir(parents=True, mode=0o755)
    nc.rpc_dir.chmod(0o755)
    (nc.state_dir / "plan.sqlite3").touch()
    (nc.state_dir / "plan.sqlite3").chmod(0o644)
    nc.start()
    assert _mode(nc.state_dir) == 0o700
    assert _mode(nc.rpc_dir) == 0o700
    assert _mode(nc.state_dir / "plan.sqlite3") == 0o600


def test_adv3_cancel_is_refused_while_a_descendant_is_running(live):
    from multiagents.scheduler.store import Store
    child = live.create()
    group = live.create(kind="group", children=[child["id"]])
    store = Store(live.root)
    with store.transaction() as db:
        record = store.nodes(db)[child["id"]]
        record.update(state="running", runs=[{"id": "live-activation"}])
        store.save_node(db, record)
    before = live.snapshot()
    reply = live.cancel_raw(group["id"])
    assert code(reply) == "not_implemented", reply
    assert live.snapshot() == before
