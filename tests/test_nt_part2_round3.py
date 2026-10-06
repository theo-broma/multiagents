"""NT part 2, round 3: the fixes decided after review ag-d8aa3b
(`context/specs/ntfy-notifications.md`, "Decisions after review ag-d8aa3b"):

1. **every hold path writes a `held` transition**, so the per-hold mark is always
   that transition's seq. The revision fallback stays only for a hold that
   predates the change. That fixes a duplicate `held` notification when an
   `update_node` bumps the revision of a held node, and a missed re-hold after a
   resume.
2. **the sender's polling does not scan the history**: the pause, backoff and
   rate-limit checks come before any staleness read, the staleness and scan
   queries are bounded to the outbox's node ids (through an index), and a
   paused or backing-off sender opens no transaction at all.
3. **(minor)** an accepted `notify test` clears the stored failure.

Numbering follows the decisions. The end-to-end tests use the NT part 2 harness
(a real host scheduler on a fake clock publishing to a fake ntfy server). The
worker's preflight is a re-check the scheduler process makes just before it
launches, so the two tests that need it stop the scheduler and run the worker's
own supervisor in this process; the recovery, cancel and migration holds are
reached through the real engine and service here as well.
"""
from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import sys
import threading
import time
import uuid
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

from nc_fixture.m4_agent import M4Provider  # noqa: E402
from nc_fixture.m4_world import M4World  # noqa: E402
from nc_fixture.world import World  # noqa: E402
from nt_harness import plan_db  # noqa: E402
from nt_part2_harness import HOUR, INTERVAL, TOPIC, NtWorld, field  # noqa: E402
from test_nt_part2_events import fake, make, message_after  # noqa: E402,F401

from multiagents.scheduler import worker  # noqa: E402
from multiagents.scheduler.engine import Engine, attempts, save_attempt  # noqa: E402
from multiagents.scheduler.model import create_record  # noqa: E402
from multiagents.scheduler.rpc import Service  # noqa: E402
from multiagents.tree import Tree, now  # noqa: E402

# One simple node with a session alias. Its checkout is host-reseated and
# dirtiness-checked before every launch (NC-R30/NC-R62): the hold path a plain
# node never walks.
ALIAS = """\
template: ntalias
version: 1
params: {}
root:
  key: top
  kind: simple
  agent: wk
  task: "implement the thing"
  session: B
"""

DIRTY = {"write": {"a.txt": "one\n"}, "commit": "r1",
         "leave": {"scratch.txt": "left behind\n"}}

NO_FAILURE = re.compile(r"failure\D*none", re.I)


def read_store(w, sql, *args):
    """One read of the scheduler's own database, with the scheduler stopped."""
    db = sqlite3.connect(f"{plan_db(w).as_uri()}?mode=ro", uri=True, timeout=5)
    try:
        return db.execute(sql, args).fetchall()
    finally:
        db.close()


def stored_node(w, node_id) -> dict:
    (raw,) = read_store(w, "SELECT record FROM nodes WHERE id=?", node_id)[0]
    return json.loads(raw)


def transitions_of(w, node) -> list[dict]:
    """The scheduler's own transitions about `node`, oldest first."""
    return [json.loads(raw) for raw, in read_store(
        w, "SELECT record FROM notifications ORDER BY seq")
        if json.loads(raw).get("node_id") == node]


def kinds_of(w, node) -> list[str]:
    return [t["kind"] for t in transitions_of(w, node)]


def preflight_hold(w, node) -> dict:
    """The worker's own preflight (worker.py): a claimed activation of an
    aliased node whose checkout the host refuses to hand out again. The claim is
    written the way the engine's preparation writes it, then the worker's
    supervisor runs in this process — with the scheduler stopped, as the worker
    is a detached process of its own."""
    binding = json.loads(read_store(w, "SELECT record FROM aliases")[0][0])
    attempt = {"attempt_id": uuid.uuid4().hex, "activation_id": uuid.uuid4().hex,
               "node_id": node, "run_id": "ag-" + uuid.uuid4().hex[:6], "state": "claimed",
               "locks": [], "lock_owners": {}, "retry_count": 1, "at": now(),
               "alias_id": binding["id"]}
    with w.store_transaction() as db:
        save_attempt(db, attempt)
    asyncio.run(worker.supervise(w.root, attempt["attempt_id"]))
    return attempt


def resume_in_process(w, node) -> None:
    """`relaunch_node` answered by a service of its own: the hold is lifted and
    the node is eligible again."""
    reply = in_process(w, "relaunch_node",
                       {"id": node, "revision": stored_node(w, node)["revision"]})
    assert reply.get("ok"), reply
    assert stored_node(w, node)["state"] == "open", stored_node(w, node)


def in_process(w, op, args) -> dict:
    """One RPC answered by a service of its own, for a scheduler that is
    stopped (no socket to talk to)."""
    from multiagents.scheduler.store import root_capability
    service = Service(w.root, now())
    return service.request({"op": op, "request_id": uuid.uuid4().hex,
                            "token": root_capability(w.root), "args": args})


# ---------------------------------------------------------------------------
# 1. every hold path writes a `held` transition
# ---------------------------------------------------------------------------

class AliasWorld(NtWorld, M4World):
    """An `NtWorld` whose fixture provider can queue a run that leaves a file
    behind — what the next launch of an aliased node finds dirty."""

    def provider(self, name, **extra):
        fx = M4Provider(self.tmp, name, self.sock, **extra)
        self.providers[name] = fx
        return fx

    def store_transaction(self):
        from multiagents.scheduler.store import Store
        return Store(self.root).transaction()

    def dirty_alias_node(self) -> str:
        """An aliased node whose first activation left its checkout dirty, and
        which therefore cannot be launched again as it stands."""
        self.fxw.queue(DIRTY)
        reply = self.rpc("register_template", {"yaml": ALIAS})
        assert reply.get("ok"), reply
        node = self.instantiate_ok("ntalias")
        self.wait_state(node, "done", 20)
        assert self.fxw.spawns() == 1
        return node


@pytest.fixture
def alias(tmp_path, monkeypatch, fake):
    made = []

    def build(**kw):
        w = AliasWorld(tmp_path, monkeypatch, fake, **kw)
        w.fxw = w.provider("fxw")
        w.agent("wk", "fxw", writes=True)
        made.append(w)
        return w

    yield build
    fake.release.set()
    for w in made:
        w.close()


def held_by_the_worker(w, node) -> str:
    """The node as the worker's preflight leaves it: the scheduler is stopped, the
    resumed node's next claim is refused before the launch, and the hold lands."""
    w.stop_scheduler()
    resume_in_process(w, node)
    preflight_hold(w, node)
    kinds = kinds_of(w, node)
    assert "dirty_worktree" in kinds, f"the preflight wrote no reason transition: {kinds}"
    return stored_node(w, node)["hold"]["reason"]


def settled(w, count, seconds=1.0):
    w.quiet(seconds)
    return len(w.fake.requests) == count


@pytest.mark.parametrize("edit", [{"urgent": True}, {"task": "an edited task"}])
def test_nt_r3fix_1_editing_a_node_the_worker_preflight_held_announces_nothing_new(alias, edit):
    w = alias()
    n = w.first_message()
    node = w.dirty_alias_node()
    assert held_by_the_worker(w, node) == "dirty_worktree"
    w.start_scheduler()
    w.wait_held(node, "dirty_worktree", timeout=20)
    w.release()
    assert message_after(w, n, "dirty_worktree", timeout=8) is not None, \
        "the preflight hold was never announced"
    seen = len(w.fake.requests)
    rev = w.get(node)["revision"]
    reply = w.rpc("update_node", {"id": node, "revision": rev, **edit})
    assert reply.get("ok"), reply
    assert w.get(node)["state"] == "held" and w.get(node)["revision"] > rev
    for _ in range(3):
        w.release()
    assert settled(w, seen), \
        "an edit of a node the worker preflight held announced that hold a second time"


def test_nt_r3fix_1_the_worker_preflight_hold_writes_a_held_transition(alias):
    w = alias()
    w.first_message()
    node = w.dirty_alias_node()
    assert held_by_the_worker(w, node) == "dirty_worktree"
    kinds = kinds_of(w, node)
    holds = [t for t in transitions_of(w, node) if t["kind"] == "held"]
    assert holds, f"the preflight hold wrote no `held` transition: {kinds}"
    assert holds[-1]["detail"] == stored_node(w, node)["hold"], holds[-1]


def test_nt_r3fix_1_a_re_hold_through_the_worker_path_after_a_resume_is_a_new_event(alias):
    """A node the engine held, resumed, then held again by the worker's preflight
    — with no scan in between, so nothing but the hold itself can mark the second
    event."""
    w = alias()
    n = w.first_message()
    node = w.dirty_alias_node()
    assert w.root_op("relaunch_node", node).get("ok")
    w.wait_held(node, "dirty_worktree", timeout=20)      # the engine's own preflight
    w.release()
    assert message_after(w, n, "dirty_worktree", timeout=8) is not None, \
        "the engine's hold was never announced"
    seen = len(w.fake.requests)

    assert held_by_the_worker(w, node) == "dirty_worktree"

    w.start_scheduler()
    w.wait_held(node, "dirty_worktree", timeout=20)
    w.release()
    assert message_after(w, seen, "dirty_worktree", timeout=8) is not None, \
        "a node re-held after a resume announced nothing"


# ------------------------------------------- the other hold paths, in process
@pytest.fixture
def store_world(tmp_path, monkeypatch):
    """A real project, store, service and engine in this process, with a
    `notify:` section and no ticking: the scan runs here, so two scans can be
    made to happen with no scheduler tick in between."""
    world = World(tmp_path, monkeypatch)
    world.project["notify"] = {"ntfy_url": "http://placeholder.invalid", "topic": TOPIC,
                               "min_interval_seconds": INTERVAL}
    world.write_config()
    world.paths.ensure()
    service = Service(world.root, now())
    service.store.initialize()
    engine = Engine(service)
    service.engine = engine
    yield world, service, engine
    for child in engine.children:
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=10)
    engine.loop.close()
    world.close()


def tree_of(world) -> Tree:
    return Tree(world.paths.tree_file, world.paths.events_file)


def scan(world, service, engine, instant=None) -> None:
    from multiagents import notify_sender
    notify_sender.scan(service, engine.store, tree_of(world), world.root.name,
                       instant if instant is not None else time.time())


def deposit(engine, **fields) -> dict:
    node = create_record({"kind": "simple", "agent": "worker", "task": "work", **fields}, "root")
    with engine.store.transaction() as db:
        engine.store.save_node(db, node)
    return node


def launched(engine, node) -> dict:
    """A claim of `node` that reads as a run already launched."""
    record = {"attempt_id": uuid.uuid4().hex, "activation_id": uuid.uuid4().hex,
              "node_id": node["id"], "run_id": "ag-" + uuid.uuid4().hex[:6],
              "state": "launched", "locks": node["locks"], "retry_count": 2, "at": now() - 5}
    with engine.store.transaction() as db:
        save_attempt(db, record)
        current = engine.store.nodes(db)[node["id"]]
        current.update(state="running", ready_since=None, revision=current["revision"] + 1)
        current["runs"] = [{"run_id": record["run_id"], "attempt_id": record["attempt_id"],
                            "activation_id": record["activation_id"]}]
        engine.store.save_node(db, current)
    return record


def only_attempt(engine) -> dict:
    with engine.store.transaction(write=False) as db:
        return list(attempts(db).values())[0]


def outbox(store) -> list[dict]:
    with store.transaction(write=False) as db:
        rows = db.execute("SELECT seq, kind, ref, data FROM notify_outbox ORDER BY seq").fetchall()
    return [{"seq": seq, "kind": kind, "ref": ref, "data": json.loads(data)}
            for seq, kind, ref, data in rows]


def accepted(store) -> None:
    """What an accepted send leaves behind: nothing pending, the rate limit set."""
    with store.transaction() as db:
        db.execute("DELETE FROM notify_outbox")
        store.set_meta(db, "notify_last_sent", time.time())


def log_transitions(store, node_id) -> list[dict]:
    with store.transaction(write=False) as db:
        rows = db.execute("SELECT record FROM notifications ORDER BY seq").fetchall()
    return [json.loads(raw) for raw, in rows if json.loads(raw).get("node_id") == node_id]


def resume(engine, node) -> None:
    """The resume `relaunch_node` performs: the hold is lifted, the revision moves."""
    with engine.store.transaction() as db:
        current = engine.store.nodes(db)[node["id"]]
        current.update(state="open", hold=None, outcome=None, ready_since=None,
                       revision=current["revision"] + 1)
        engine.store.save_node(db, current)
        engine.store.transition(db, "reopened", node["id"])


def recover(engine, monkeypatch, *, confirmed: bool) -> None:
    """One `reconcile()` over a claim whose run never wrote a tree entry: with the
    death unproved the engine places its recovery hold (NC-R17)."""
    monkeypatch.setattr(engine.runner, "_steer_predecessor", lambda _: object())
    monkeypatch.setattr(engine.runner, "_steer_predecessor_dead", AsyncMock(return_value=confirmed))
    asyncio.run(engine.reconcile())


def test_nt_r3fix_1_the_recovery_hold_writes_a_held_transition(store_world, monkeypatch):
    world, service, engine = store_world
    node = deposit(engine)
    launched(engine, node)
    engine.paths.run_dir(only_attempt(engine)["run_id"]).mkdir(parents=True)
    recover(engine, monkeypatch, confirmed=False)
    with engine.store.transaction(write=False) as db:
        held = engine.store.nodes(db)[node["id"]]
    assert held["state"] == "held" and held["hold"]["reason"] == "termination_unconfirmed", held
    kinds = [t["kind"] for t in log_transitions(engine.store, node["id"])]
    holds = [t for t in log_transitions(engine.store, node["id"]) if t["kind"] == "held"]
    assert holds, f"the recovery hold wrote no `held` transition: {kinds}"
    assert holds[-1]["detail"] == held["hold"], holds[-1]


@pytest.mark.parametrize("confirmed", [False, True])
def test_nt_r3fix_1_a_cancel_hold_is_one_hold(store_world, monkeypatch, confirmed):
    """A cancel that stops a live run holds the node until the stop settles. The
    hold is announced once: an interim mark that the stop's own transition then
    replaces is the same hold, not a second one."""
    world, service, engine = store_world
    node = deposit(engine)
    launched(engine, node)
    with engine.store.transaction(write=False) as db:
        revision = engine.store.nodes(db)[node["id"]]["revision"]
    entered, release = threading.Event(), threading.Event()

    class Stopper:
        def __init__(self, *args):
            pass

        async def stop(self, run_id):
            entered.set()
            await asyncio.to_thread(release.wait, 10)
            return {"predecessor_death_confirmed": confirmed}

    monkeypatch.setattr("multiagents.scheduler.engine.Runner", Stopper)
    scan(world, service, engine)                     # the activation summary first
    accepted(engine.store)
    replies = []
    request = threading.Thread(target=lambda: replies.append(service.request(
        {"op": "cancel_node", "request_id": uuid.uuid4().hex, "token": world.root_token(),
         "args": {"id": node["id"], "revision": revision}})))
    request.start()
    try:
        assert entered.wait(10), "the stop never started"
        with engine.store.transaction(write=False) as db:
            interim = engine.store.nodes(db)[node["id"]]
        assert interim["state"] == "held", interim
        assert interim["hold"]["reason"] == "termination_unconfirmed", interim
        scan(world, service, engine)                    # the hold, as the tick sees it
    finally:
        release.set()
        request.join(10)
    assert replies and replies[0].get("ok"), replies
    if not confirmed:
        with engine.store.transaction(write=False) as db:
            still = engine.store.nodes(db)[node["id"]]
        assert still["state"] == "held", still
    scan(world, service, engine)                        # and once the stop has settled
    rows = [row for row in outbox(engine.store) if row["kind"] == "held"]
    assert [row["ref"] for row in rows] == [node["id"]], \
        f"the cancel's hold was announced more than once: {rows}"
    held = [t for t in log_transitions(engine.store, node["id"]) if t["kind"] == "held"]
    assert held, f"the cancel hold wrote no `held` transition: " \
        f"{[t['kind'] for t in log_transitions(engine.store, node['id'])]}"
    assert rows[0]["data"]["held_seq"] == held[0]["seq"], \
        f"the hold was announced by something other than its `held` transition: {rows}"


def test_nt_r3fix_1_the_migrated_refusal_hold_writes_a_held_transition(store_world):
    """A refused legacy `start_agent` entry becomes a held node on migration."""
    world, service, engine = store_world
    tree = tree_of(world)
    entry = tree.defer({"agent": "worker", "task": "legacy work"}, time.time() + 3600,
                       "quota window")
    with tree.transaction() as data:
        for queued in data["deferred"]:
            if queued["id"] == entry["id"]:
                queued["status"] = "refused"
    engine.migrate()
    with engine.store.transaction(write=False) as db:
        nodes = [n for n in engine.store.nodes(db).values()
                 if n.get("migration_id") == entry["id"]]
    assert len(nodes) == 1, nodes
    assert nodes[0]["state"] == "held" and nodes[0]["hold"]["reason"] == "admission:refused"
    kinds = [t["kind"] for t in log_transitions(engine.store, nodes[0]["id"])]
    holds = [t for t in log_transitions(engine.store, nodes[0]["id"]) if t["kind"] == "held"]
    assert holds, f"the migrated hold wrote no `held` transition: {kinds}"
    assert holds[-1]["detail"] == nodes[0]["hold"], holds[-1]


def test_nt_r3fix_1_a_re_hold_between_two_scans_is_a_new_event(store_world, monkeypatch):
    """The same shape as the worker's case, over the engine's recovery hold: two
    scans with no tick in between, so nothing but the hold itself can mark the
    second event."""
    world, service, engine = store_world
    node = deposit(engine)
    scan(world, service, engine)                     # the activation summary
    accepted(engine.store)
    with engine.store.transaction() as db:
        engine.hold(db, engine.store.nodes(db)[node["id"]], "input_conflict",
                    "two inputs wrote f.txt")
    scan(world, service, engine)
    first = outbox(engine.store)
    assert [row["ref"] for row in first] == [node["id"]], first
    first_seq = first[0]["data"]["held_seq"]
    assert isinstance(first_seq, int), first
    accepted(engine.store)

    resume(engine, node)
    launched(engine, node)
    engine.paths.run_dir(only_attempt(engine)["run_id"]).mkdir(parents=True)
    recover(engine, monkeypatch, confirmed=False)
    with engine.store.transaction(write=False) as db:
        again = engine.store.nodes(db)[node["id"]]
    assert again["state"] == "held" and again["hold"]["reason"] == "termination_unconfirmed", again

    scan(world, service, engine)
    rows = [row for row in outbox(engine.store) if row["kind"] == "held"]
    assert [row["ref"] for row in rows] == [node["id"]], \
        "a node re-held after a resume announced nothing"
    assert rows[0]["data"]["held_seq"] > first_seq, \
        f"the second hold reused the first one's seq: {rows[0]['data']}"


# ---------------------------------------------------------------------------
# 2. the sender's polling does not scan the history
# ---------------------------------------------------------------------------

REAL_CONNECT = sqlite3.connect


def record_queries(monkeypatch) -> list[tuple[str, tuple]]:
    """Every statement the store runs, as (sql, parameters). A statement that
    binds one list (an `IN (...)` of ids) is recorded with its elements, the
    way sqlite3 binds them."""
    seen: list[tuple[str, tuple]] = []

    class Counting(sqlite3.Connection):
        def execute(self, sql, *args):
            bound = tuple(args[0]) if len(args) == 1 and isinstance(args[0], (list, tuple)) else args
            seen.append((sql, bound))
            return super().execute(sql, *args)

    def connect(*args, **kwargs):
        kwargs.setdefault("factory", Counting)
        return REAL_CONNECT(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", connect)
    return seen


def sender_over(world, service, engine, *, rows=1, node_ids=(), meta=None, clock=None):
    """An outbox of `rows` pending messages plus the meta the sender reads."""
    from multiagents import notify_sender
    instant = clock() if clock else time.time()
    with engine.store.transaction() as db:
        db.execute("DELETE FROM notify_outbox")
        db.execute("DELETE FROM meta WHERE key LIKE 'notify%'")
        for index in range(rows):
            db.execute("INSERT INTO notify_outbox(kind, ref, data, enqueued_at)"
                       " VALUES ('held', ?, ?, ?)",
                       (node_ids[index % len(node_ids)],
                        json.dumps({"reason": "input_conflict"}), instant))
        for key, value in (meta or {}).items():
            db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, str(value)))
    return notify_sender.Sender(service, engine.store, tree_of(world), world.root.name,
                                clock or (lambda: time.time()))


def history_queries(seen) -> list[tuple[str, tuple]]:
    return [(sql, args) for sql, args in seen if "notifications" in sql]


@pytest.mark.parametrize("state", ["paused", "backing_off"])
def test_nt_r3fix_2_a_stopped_sender_asks_nothing_about_the_history(store_world, monkeypatch, state):
    world, service, engine = store_world
    node = deposit(engine, state="held")
    with engine.store.transaction() as db:
        held = engine.store.nodes(db)[node["id"]]
        held["hold"] = {"reason": "input_conflict", "detail": ""}
        engine.store.save_node(db, held)
    meta = {"notify_paused": "1"} if state == "paused" else {"notify_next_retry": time.time() + 600}
    sender = sender_over(world, service, engine, rows=3, node_ids=[node["id"]], meta=meta)
    seen = record_queries(monkeypatch)
    sender.pump()
    assert history_queries(seen) == [], \
        f"a {state} sender read the notification history: {history_queries(seen)}"
    seen.clear()
    sender.pump()
    assert history_queries(seen) == [], \
        f"a {state} sender read the notification history: {history_queries(seen)}"
    assert [sql for sql, _ in seen if sql.startswith("BEGIN")] == [], \
        f"a {state} sender opened a transaction: {seen}"
    assert outbox(engine.store), "the control proves nothing: the outbox drained"


def test_nt_r3fix_2_a_paused_sender_notices_a_pause_cleared_elsewhere(store_world, monkeypatch):
    """`notify test` clears the pause from another process: the sender has to look
    again, or the outbox would wait for the next restart."""
    from multiagents import notify_sender
    world, service, engine = store_world
    node = deposit(engine)
    sender = sender_over(world, service, engine, rows=1, node_ids=[node["id"]],
                         meta={"notify_paused": "1"})
    seen = record_queries(monkeypatch)
    sender.pump()                                    # the first poll has to learn the state
    seen.clear()
    sender.pump()
    assert seen == [], f"a paused sender kept querying: {seen}"
    notify_sender.clear_pause(engine.store)          # as `notify test` does
    seen.clear()
    sender.pump()
    assert history_queries(seen), "the sender never noticed that the pause was cleared"


def test_nt_r3fix_2_a_due_sender_reads_the_history_only_through_the_index(store_world, monkeypatch):
    """The staleness read covers the outbox's node ids only, and reaches the log
    through an index: never a scan of the history."""
    world, service, engine = store_world
    node = deposit(engine, state="done")             # a row about it is stale: nothing is sent
    other = deposit(engine, state="done")
    with engine.store.transaction() as db:
        for _ in range(200):
            db.execute("INSERT INTO notifications(record) VALUES (?)",
                       (json.dumps({"kind": "held", "node_id": other["id"], "at": now(),
                                    "detail": {"reason": "input_conflict"}, "seq": 0}),))
    sender = sender_over(world, service, engine, rows=2, node_ids=[node["id"], node["id"]])
    seen = record_queries(monkeypatch)
    sender.pump()
    queries = history_queries(seen)
    assert queries, "the control proves nothing: the staleness read never ran"
    for sql, args in queries:
        assert node["id"] in args and other["id"] not in args, \
            f"a history query was not bound to the outbox's node ids: {sql!r} {args!r}"
        plans = [row[-1] for row in plan_of(engine.store, sql, args)]
        assert all("SCAN notifications" not in plan for plan in plans), plans
    assert outbox(engine.store) == [], "the stale rows were not dropped"


def plan_of(store, sql, args):
    with store.transaction(write=False) as db:
        return db.execute("EXPLAIN QUERY PLAN " + sql, args).fetchall()


def test_nt_r3fix_2_an_upgraded_store_does_not_re_announce_a_hold(store_world):
    """A store that recorded the destination but not the config fingerprint (an
    upgrade from round 1) with `held` disabled at the time: the activation branch
    marks a hold by the seq of its `held` transition. Marking it by the revision
    instead would not match the mark a later scan computes once `held` is enabled,
    and the same hold would go out then."""
    world, service, engine = store_world
    world.project["notify"]["events"] = ["question"]     # no `held` at the upgrade
    world.write_config()
    scan(world, service, engine)                         # records the destination and the fingerprint
    with engine.store.transaction() as db:
        db.execute("DELETE FROM meta WHERE key=?", ("notify_cfg",))
    node = deposit(engine)
    with engine.store.transaction() as db:
        engine.hold(db, engine.store.nodes(db)[node["id"]], "input_conflict", "two inputs")
    accepted(engine.store)                               # clear the activation summary
    scan(world, service, engine)                         # the activation branch marks the hold
    accepted(engine.store)
    world.project["notify"]["events"] = ["held"]         # `held` enabled after the upgrade
    world.write_config()
    scan(world, service, engine)                         # the next scan must not re-announce it
    rows = [row for row in outbox(engine.store) if row["kind"] == "held"]
    assert rows == [], f"the upgraded store re-announced the hold: {rows}"


# ---------------------------------------------------------------------------
# 3. an accepted `notify test` clears the stored failure
# ---------------------------------------------------------------------------

def test_nt_r3fix_3_an_accepted_notify_test_clears_the_stored_failure(make):
    w = make()
    w.fake.mode, w.fake.status = "status", 401
    w.first_message()
    w.advance(seconds=HOUR)
    assert field(w.notify_status(), "fail", "error"), "the 401 recorded no failure"
    w.fake.mode = "accept"
    done = w.cli("notify", "test")
    assert done.returncode == 0, done.stdout + done.stderr
    assert not field(w.notify_status(), "fail", "error"), \
        f"an accepted notify test left the failure: {w.notify_status()}"
    printed = w.cli("notify", "status").stdout
    assert NO_FAILURE.search(printed), printed