"""TF-R4: real exec failures and public admission exceptions are bounded."""
import asyncio
import sqlite3
import uuid

import pytest

from test_tf_support import w, WAIT, cause, field, objects, retry_success
from test_nc_m2_guard_recovery import local


def broken_interpreter(w):
    script = w.fxw.dir / 'agent.py'
    original = script.read_text()
    script.write_text('#!/tf-missing-interpreter\n' + original.split('\n', 1)[1])
    return script, original


def test_tf_r4_persistent_spawn_failure_holds_with_exception_text(w):
    script, original = broken_interpreter(w)
    try:
        w.start_scheduler()
        node = w.simple('TF', 'wk')
        held = w.wait_held(node, 'retries_exhausted', timeout=WAIT)
        c = cause(held['hold']['detail'], 'launch')
        excerpt = field(c, 'excerpt', 'error_excerpt')
        assert 'FileNotFoundError' in excerpt
        assert field(c, 'exit_code', 'exit') is None
        assert c['signal'] is None
        assert not w.fxw.calls()
        w.restart_scheduler()
        assert w.get(node)['state'] == 'held'
        assert not w.fxw.calls()
    finally:
        script.write_text(original)


def test_tf_r4_spawn_failure_recovers_after_executable_is_restored(w):
    script, original = broken_interpreter(w)
    try:
        w.start_scheduler()
        node = w.simple('TF', 'wk')
        def recorded():
            n = w.get(node)
            if n['state'] == 'done':
                raise AssertionError(f'launch exception silently failed the node: {n}')
            return n if any(c.get('class') == 'launch' for c in objects(n)) else None
        first = w.until(recorded, WAIT, what='launch failure record')
        cause(first, 'launch')
    finally:
        script.write_text(original)
    retry_success(w, node)
    assert len(w.fxw.calls()) == 1


@pytest.mark.parametrize('exception', [sqlite3.OperationalError('database is locked'),
                                        KeyError('checkout binding missing')])
def test_tf_r4_admission_exception_is_caught_and_classified(local, monkeypatch, exception):
    world, engine = local
    service = engine.service
    reply = service.request({'op': 'create_node', 'token': world.root_token(),
                             'request_id': 'tf-create',
                             'args': {'plan_revision': 0, 'kind': 'simple', 'agent': 'worker', 'task': 'TF launch'}})
    assert reply.get('ok'), reply
    node = reply['result']['id']

    async def unavailable(*args, **kwargs):
        raise exception
    monkeypatch.setattr(engine.runner, 'start', unavailable)
    try:
        asyncio.run(engine.tick())
    except type(exception) as escaped:
        pytest.fail(f'admission exception escaped without classifying the node: {escaped}')
    view = service.request({'op': 'get_node', 'token': world.root_token(),
                            'request_id': 'tf-read', 'args': {'id': node}})['result']
    c = cause(view, 'launch')
    excerpt = field(c, 'excerpt', 'error_excerpt')
    assert type(exception).__name__ in excerpt and str(exception) in excerpt


def test_tf_r4_cancellation_during_admission_is_not_a_launch_failure(local, monkeypatch):
    world, engine = local
    service = engine.service
    reply = service.request({'op': 'create_node', 'token': world.root_token(),
                             'request_id': 'tf-create',
                             'args': {'plan_revision': 0, 'kind': 'simple', 'agent': 'worker', 'task': 'TF launch'}})
    assert reply.get('ok'), reply
    node = reply['result']['id']
    async def cancelled(*args, **kwargs):
        raise asyncio.CancelledError()
    monkeypatch.setattr(engine.runner, 'start', cancelled)
    try:
        asyncio.run(engine.tick())
    except asyncio.CancelledError:
        pass  # propagation is permitted; classification as launch is not
    view = service.request({'op': 'get_node', 'token': world.root_token(),
                            'request_id': 'tf-read', 'args': {'id': node}})['result']
    from test_tf_support import objects
    assert not any(c.get('class') == 'launch' for c in objects(view))
    assert not world.fx.calls()


def test_tf_r2_crash_and_launch_failures_share_one_budget(w):
    script = w.fxw.dir / 'agent.py'
    original = script.read_text()
    w.fxw.queue({'break_next_launch': True, 'exit': 1})
    try:
        w.start_scheduler()
        node = w.simple('TF', 'wk')
        held = w.wait_held(node, 'retries_exhausted', timeout=WAIT)
        # One crashed attempt and only three further launch attempts: a
        # separate per-class budget would permit a fifth attempt.
        assert len(held['runs']) == 4
        cause(held['hold']['detail'], 'launch')
        assert len(w.fxw.calls()) == 1
    finally:
        script.write_text(original)


def test_tf_r4_a_locked_database_at_record_time_reconciles_on_the_next_tick(local, monkeypatch):
    """TF-R4: the recording write fails while the database is locked; the
    attempt is left claimed with no record, and the next tick (database
    available again) reconciles it — the failure becomes a classified cause
    on the public view instead of being lost."""
    import contextlib
    from types import SimpleNamespace
    from multiagents.scheduler.engine import attempts as read_attempts, save_attempt
    from multiagents.scheduler.model import create_record
    from multiagents.tree import Node, now
    world, engine = local
    service = engine.service
    node = create_record({"kind": "simple", "agent": "worker", "task": "TF record"}, "root")
    attempt = {"attempt_id": uuid.uuid4().hex, "activation_id": uuid.uuid4().hex,
               "run_id": "ag-" + uuid.uuid4().hex[:6], "node_id": node["id"],
               "state": "launched", "locks": [], "at": now()}
    node.update(state="running", runs=[{"run_id": attempt["run_id"],
                                        "attempt_id": attempt["attempt_id"]}])
    with engine.store.transaction() as db:
        engine.store.save_node(db, node)
        save_attempt(db, attempt)
    run = Node(id=attempt["run_id"], agent="worker", provider="fx", model="fx/m1",
               parent=None, depth=1, branch="agents/worker/x", worktree="",
               status="failed", node_id=node["id"], attempt_id=attempt["attempt_id"])
    engine.runner.tree.add(run)
    engine.runner.authority.add(run)

    real_transaction = engine.store.transaction
    import contextlib

    @contextlib.contextmanager
    def locked(*args, **kwargs):
        if kwargs.get("write", True) and locked.on:
            raise sqlite3.OperationalError("database is locked")
        with real_transaction(*args, **kwargs) as db:
            yield db
    locked.on = True
    monkeypatch.setattr(engine.store, "transaction", locked)
    try:
        engine.finished(attempt, run)   # may raise or be caught inside; both allowed
    except sqlite3.OperationalError:
        pass
    locked.on = False
    with engine.store.transaction(write=False) as db:
        still = read_attempts(db)[attempt["attempt_id"]]
        assert still["state"] == "launched" and "result" not in still, \
            "the locked write must not leave a half-record"
    asyncio.run(engine.tick())
    reply = service.request({"op": "get_node", "token": world.root_token(),
                             "request_id": "tf-db-read", "args": {"id": node["id"]}})
    assert reply.get("ok"), reply
    view = reply["result"]
    cause(view, "crash")            # reconciled, classified, public
    assert view["state"] in {"open", "running", "held"}, \
        f"the reconciled retryable crash ended the node silently: {view}"
