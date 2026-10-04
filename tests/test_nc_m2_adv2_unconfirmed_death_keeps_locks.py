import pytest
import asyncio
from pathlib import Path
from multiagents.scheduler.store import Store

@pytest.mark.anyio
async def test_unconfirmed_death_keeps_locks(tmp_path):
    """reconcile must not release locks if predecessor_death_confirmed is false."""
    from multiagents.scheduler.engine import Engine, attempts
    
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
        def __init__(self, s):
            self.store = s
            self.paths = s.paths

    with store.transaction() as db:
        node = {"id": "n1", "state": "running", "revision": 1, "runs": [{"run_id": "ag-123456", "attempt_id": "attempt1"}], "task": "task", "agent": "a", "locks": ["lock1"]}
        store.save_node(db, node)
        
        attempt_id = "attempt1"
        run_id = "ag-123456"
        db.execute("INSERT INTO attempts VALUES (?, ?)", (attempt_id, '{"attempt_id": "attempt1", "node_id": "n1", "run_id": "ag-123456", "state": "launched", "locks": ["lock1"], "at": 0}'))
    
    class DummyRun:
        id = "ag-123456"
        status = "cancelled"
    
    class DummyRunner:
        def __init__(self):
            self.tree = type('Tree', (), {'get': lambda self, id: DummyRun()})()
            self.runs = {}
        def _steer_predecessor(self, run_id):
            return "pred1"
        async def _steer_predecessor_dead(self, pred):
            return False
    
    engine = Engine(DummyService(store))
    engine.runner = DummyRunner()
    
    await engine.reconcile()
    
    with store.transaction(write=False) as db:
        att = attempts(db)["attempt1"]
        # If it released locks, the attempt state would be 'abandoned' or something, or it would call finished()
        # finished() updates the attempt state to 'finished' and removes node locks.
        assert att["state"] == "launched", "Attempt was finished despite unconfirmed death!"
