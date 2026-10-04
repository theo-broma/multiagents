"""Review regressions: authority, hostile mirrors, and host-store boundaries."""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))
from nc_harness import code, live, nc  # noqa: E402,F401


def delegating(live):
    own = live.create(task="run owner")
    return own, live.issue("delegate", own["id"], {"read", "delegate"})


@pytest.mark.parametrize("field", ["depends_on", "inputs"])
@pytest.mark.parametrize("op", ["create", "update"])
def test_nc_r79_foreign_references_are_forbidden_on_deposits_and_edits(live, field, op):
    foreign = live.create(task="outside")
    own, token = delegating(live)
    child = live.create(token, task="delegated")
    before = live.snapshot()
    fields = {field: [{"node": foreign["id"]}]}
    if op == "create":
        reply = live.create_raw({"kind": "simple", "agent": "worker", "task": "new", **fields}, token)
    else:
        reply = live.update_raw(child["id"], token=token, **fields)
    assert code(reply) == "forbidden", reply
    assert live.snapshot() == before
    # The same reference within scope is legal and has no dependency cycle.
    live.update(child["id"], token, **{field: [{"node": live.create(token)["id"]}]})


def test_nc_r59_a_run_cannot_deposit_under_a_root_created_descendant(live):
    own, token = delegating(live)
    root_group = live.create(kind="group", parent=own["id"])
    before = live.snapshot()
    reply = live.create_raw({"kind": "simple", "agent": "worker", "task": "t",
                             "parent": root_group["id"]}, token)
    assert code(reply) == "forbidden", reply
    assert live.snapshot() == before
    run_group = live.create(token, kind="group")
    assert live.create(token, parent=run_group["id"])["parent"] == run_group["id"]


def test_nc_r59_a_runs_composite_creation_checks_each_childs_creator(live):
    own, token = delegating(live)
    root_child = live.create(parent=own["id"])
    before = live.snapshot()
    reply = live.create_raw({"kind": "group", "children": [root_child["id"]]}, token)
    # Scope alone is insufficient: the child is readable but was made by root.
    assert code(reply) == "forbidden", reply
    assert live.snapshot() == before


def test_nc_r79_wait_checks_every_requested_node_id(live):
    foreign = live.create()
    own, token = delegating(live)
    reply = live.rpc("wait_for_nodes", {"cursor": 0, "timeout": 0,
                                       "node_ids": [own["id"], foreign["id"]]}, token)
    assert code(reply) == "forbidden", reply
    assert live.ok("wait_for_nodes", {"cursor": 0, "timeout": 0,
                                      "node_ids": [own["id"]]}, token)["transitions"]


def test_nc_r75_root_cannot_give_a_verdict_even_before_verdicts_exist(live):
    node = live.create()
    assert code(live.rpc("give_verdict", {"node_id": node["id"], "generation_seq": 1,
                                         "commit": "0" * 40, "verdict": "approved"})) == "forbidden"
    token = live.issue("review", node["id"], {"verdict"})
    # NC-R34 (M4): a run that is not the current activation of a loop's verdict
    # child is `forbidden`, whatever permission it holds; no longer `not_implemented`.
    assert code(live.rpc("give_verdict", {"node_id": node["id"]}, token)) == "forbidden"


@pytest.mark.parametrize("cancel_parent", [False, True])
def test_nc_r9_cancellation_revokes_the_nodes_capabilities(live, cancel_parent):
    parent = live.create(kind="group")
    child = live.create(parent=parent["id"])
    token = live.issue("child-run", child["id"], {"read"})
    assert live.get(child["id"], token)["id"] == child["id"]
    assert live.cancel_raw((parent if cancel_parent else child)["id"])["ok"]
    assert code(live.rpc("get_node", {"id": child["id"]}, token)) == "unauthenticated"


@pytest.mark.parametrize("state,runs", [(s, []) for s in ("running", "suspended", "held", "done", "cancelled")]
                         + [("open", [{"id": "prior-activation"}])])
def test_nc_r59_a_run_edits_only_open_never_launched_nodes(live, state, runs):
    from multiagents.scheduler.store import Store
    own, token = delegating(live)
    child = live.create(token)
    store = Store(live.root)
    # The host owns activation state; these are states M2 will persist.
    with store.transaction() as db:
        record = store.nodes(db)[child["id"]]
        record.update(state=state, runs=runs)
        store.save_node(db, record)
    before = live.snapshot()
    assert code(live.update_raw(child["id"], token=token, task="changed")) == "forbidden"
    assert live.snapshot() == before


def test_nc_r14_mirror_failure_preserves_success_and_retries_without_a_client_write(live):
    live.events.unlink()
    live.events.mkdir()
    reply = live.create_raw({"kind": "simple", "agent": "worker", "task": "durable"})
    assert reply["ok"] and "error" not in reply, reply
    live.events.rmdir()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if live.events.is_file() and "created" in live.event_kinds():
            break
        time.sleep(0.05)
    assert live.event_kinds() == ["scheduler_started", "created"]
    assert len(live.snapshot()["nodes"]) == 1


def test_nc_r14_public_events_cannot_forge_the_host_mirror_cursor(live):
    with live.events.open("ab") as out:
        out.write(json.dumps({"scheduler_project": live.slug, "scheduler_seq": 2,
                              "kind": "forged"}).encode() + b"\n")
    live.create()
    assert live.event_kinds() == ["scheduler_started", "created"]


def test_nc_r3_a_container_writer_cannot_block_rpc_by_locking_the_mirror(live):
    import fcntl
    with live.events.open("ab") as out:
        fcntl.flock(out, fcntl.LOCK_EX)
        reply = live.create_raw({"kind": "simple", "agent": "worker", "task": "durable"}, timeout=3)
        assert reply["ok"] and "error" not in reply, reply
        assert live.rpc("scheduler_status", timeout=3)["ok"]
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and "created" not in live.event_kinds():
        time.sleep(0.05)
    assert "created" in live.event_kinds()


def test_nc_r71_issuing_a_capability_queries_only_its_node(live, monkeypatch):
    from multiagents.scheduler.store import Store
    node = live.create()

    def no_whole_plan(*args):
        raise AssertionError("issuing a capability must not load every node")

    monkeypatch.setattr(Store, "nodes", no_whole_plan)
    token = live.issue("reader", node["id"], {"read"})
    assert live.get(node["id"], token)["id"] == node["id"]


def test_nc_r3_reading_root_capability_does_not_wait_for_a_reserved_writer(live):
    from multiagents.scheduler import root_capability
    from multiagents.scheduler.store import Store
    done = threading.Event()
    results = []

    def read():
        results.append(root_capability(live.root))
        done.set()

    with Store(live.root).transaction():
        thread = threading.Thread(target=read)
        thread.start()
        concurrent = done.wait(1)
    thread.join(5)
    assert concurrent, "a root capability read waited for the write reservation"
    assert results == [live.root_token()]


def test_nc_r8_read_requests_do_not_wait_for_a_reserved_writer(live):
    from multiagents.scheduler.store import Store
    done = threading.Event()
    results = []

    def read():
        results.append(live.rpc("scheduler_status"))
        done.set()

    with Store(live.root).transaction():
        thread = threading.Thread(target=read)
        thread.start()
        concurrent = done.wait(1)
    thread.join(5)
    assert concurrent, "a read-only RPC waited for the write reservation"
    assert results[0]["ok"]


def test_nc_r7_transactions_close_connections_on_success_and_failure(nc, monkeypatch):
    from multiagents.scheduler import store as module
    store = module.Store(nc.root)
    store.initialize()
    real_connect = module.sqlite3.connect
    connections = []

    class Tracked(module.sqlite3.Connection):
        closed = False

        def close(self):
            self.closed = True
            super().close()

    def connect(*args, **kwargs):
        db = real_connect(*args, **kwargs, factory=Tracked)
        connections.append(db)
        return db

    monkeypatch.setattr(module.sqlite3, "connect", connect)
    for write in (False, True):
        with store.transaction(write=write) as db:
            assert db.execute("SELECT 1").fetchone() == (1,)
        with pytest.raises(RuntimeError):
            with store.transaction(write=write):
                raise RuntimeError("rollback")
    assert len(connections) == 4 and all(db.closed for db in connections)


def test_nc_r2_request_config_is_cached_until_source_files_change(nc, monkeypatch):
    from multiagents.scheduler import rpc
    from multiagents.scheduler.store import root_capability
    service = rpc.Service(nc.root, "now")
    service.store.initialize()
    real_load = rpc.load
    calls = []

    def load(*args, **kwargs):
        calls.append(True)
        return real_load(*args, **kwargs)

    monkeypatch.setattr(rpc, "load", load)
    for revision in range(3):
        request = {"op": "create_node", "token": root_capability(nc.root),
                   "request_id": str(revision), "args": {"kind": "simple", "agent": "worker",
                                                         "task": "t", "plan_revision": revision}}
        assert service.request(request)["ok"]
    assert len(calls) == 1
    nc.set_gate(False)
    request["request_id"] = "gate-changed"
    request["args"]["plan_revision"] = 3
    assert code(service.request(request)) == "scheduler_disabled"
    assert len(calls) == 2


def test_nc_r8_internal_failures_are_logged_as_internal_not_invalid(nc, monkeypatch, caplog):
    from multiagents.scheduler.rpc import Service
    from multiagents.scheduler.store import root_capability
    service = Service(nc.root, "now")
    service.store.initialize()

    def broken(*args):
        raise TypeError("scheduler bug, not client input")

    monkeypatch.setattr(service, "dispatch", broken)
    reply = service.request({"op": "scheduler_status", "args": {},
                             "token": root_capability(nc.root), "request_id": "broken"})
    assert code(reply) == "internal" and reply["ok"] is False
    assert "scheduler bug, not client input" in caplog.text


def test_nc_r2_scheduler_policy_uses_the_loaders_layer_selection(nc, monkeypatch):
    from multiagents import config, scheduler_config
    from multiagents.paths import ProjectPaths
    low, high = nc.tmp / "low", nc.tmp / "high"
    for path in (low, high):
        path.mkdir()
    (low / "project.yaml").write_text("scheduler:\n  enabled: true\n  timezone: UTC\n")
    (high / "project.yaml").write_text("scheduler:\n  tick_seconds: 9\n")
    monkeypatch.setattr(config, "layer_dirs", lambda paths: [low, high])
    assert scheduler_config.settings(nc.root) == config.load(ProjectPaths(nc.root), seed=False).project["scheduler"]
