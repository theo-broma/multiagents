import pytest
import asyncio
from multiagents.scheduler.store import Store

@pytest.mark.anyio
async def test_worker_crash_before_tree_leaks_capabilities(tmp_path):
    """If a run is launched but crashes before writing to the tree,
    worker.py exits without revoking capabilities, and reconcile ignores it forever."""
    from multiagents.scheduler.engine import Engine, attempts
    from multiagents.scheduler.worker import supervise
    
    store = Store(tmp_path)
    store.initialize()

    class DummyService:
        def configuration(self):
            class Config:
                project = {"scheduler": {"enabled": True, "tick_seconds": 1}}
                providers = {}
                provider_sources = {}
            return Config()
        class Changed:
            def notify_all(self): pass
            def __enter__(self): return self
            def __exit__(self, *a): pass
        changed = Changed()
        def __init__(self, s): self.store = s; self.paths = s.paths

    with store.transaction() as db:
        node = {"id": "n1", "state": "open", "revision": 1, "runs": [], "task": "task", "agent": "a", "locks": []}
        store.save_node(db, node)
        
        attempt_id = "attempt1"
        run_id = "ag-123456"
        db.execute("INSERT INTO attempts VALUES (?, ?)", (attempt_id, f'{{"attempt_id": "{attempt_id}", "node_id": "n1", "run_id": "{run_id}", "state": "launched", "locks": [], "activation_id": "act1", "at": 0}}'))
        db.execute("INSERT INTO capabilities(hash, subject, node_id, permissions) VALUES (?, ?, ?, ?)", ("hash1", run_id, "n1", "[]"))

    class DummyRunner:
        def __init__(self):
            # run vanished from tree!
            self.tree = type('Tree', (), {'get': lambda self, id: None, 'active': lambda self: []})()
            self.runs = {}
        async def adopt(self, exclude):
            pass
        async def shutdown(self, **kwargs):
            pass
        def _steer_predecessor(self, run_id): return None
        async def _steer_predecessor_dead(self, pred): return True
        def start(self, *a, **k): pass

    engine = Engine(DummyService(store))
    engine.paths = type('Paths', (), {'root': tmp_path, 'run_dir': lambda self, id: tmp_path / "runs" / id})()
    engine.store = store
    engine.runner = DummyRunner()

    # Worker supervise call
    await supervise(tmp_path, attempt_id)

    # Now reconcile
    await engine.reconcile()

    # Check capabilities
    with store.transaction(write=False) as db:
        res = db.execute("SELECT revoked FROM capabilities WHERE subject=?", (run_id,)).fetchone()
    
    assert res[0] == 1, "Capability leaked: worker exited and reconcile ignored it!"
