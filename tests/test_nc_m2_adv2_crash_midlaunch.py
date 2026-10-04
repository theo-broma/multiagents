import pytest
import asyncio
import os
import fcntl
from pathlib import Path
from multiagents.scheduler.store import Store

@pytest.mark.anyio
async def test_recovery_after_crash_midlaunch_leaks_locks(tmp_path, monkeypatch):
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
        node = {"id": "n1", "state": "open", "revision": 1, "runs": [], "task": "task", "agent": "a"}
        store.save_node(db, node)
        
        attempt_id = "attempt1"
        run_id = "ag-123456"
        db.execute("INSERT INTO attempts VALUES (?, ?)", (attempt_id, '{"attempt_id": "attempt1", "node_id": "n1", "run_id": "ag-123456", "state": "claimed", "locks": ["my_lock"], "at": 0}'))
    
    store.paths.run_dir(run_id).mkdir(parents=True)
    
    class DummyRunner:
        def __init__(self):
            self.tree = type('Tree', (), {'get': lambda self, id: None})()
            self.runs = {}
        def _steer_predecessor(self, run_id):
            return None
        async def _steer_predecessor_dead(self, pred):
            return True
    
    engine = Engine(DummyService(store))
    engine.runner = DummyRunner()
    
    print("RECONCILE 1")
    await engine.reconcile()
    
    with store.transaction() as db:
        node = store.nodes(db)["n1"]
        assert node["state"] == "held"
        att = attempts(db)["attempt1"]
        assert att["state"] == "claimed"
        
        node["state"] = "cancelled"
        store.save_node(db, node)
    
    print("RECONCILE 1")
    await engine.reconcile()
    
    with store.transaction() as db:
        att = attempts(db)["attempt1"]
        assert att["state"] != "claimed", "Attempt is still claimed, leaking its locks"
