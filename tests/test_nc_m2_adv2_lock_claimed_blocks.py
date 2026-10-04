import pytest
import asyncio
from multiagents.scheduler.store import Store

@pytest.mark.anyio
async def test_claimed_attempt_holds_locks(tmp_path):
    """A claimed attempt (not yet launched) holds its locks and blocks other nodes."""
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

    # We need to simulate the engine's runner checking blockers.
    # We can just call lock_blockers directly, or call tick() with two nodes.
    
    with store.transaction() as db:
        node1 = {"id": "n1", "state": "open", "revision": 1, "runs": [], "task": "task", "agent": "a", "locks": ["lock1"], "parent": None, "run_parent": None}
        node2 = {"id": "n2", "state": "open", "revision": 1, "runs": [], "task": "task", "agent": "a", "locks": ["lock1"], "parent": None, "run_parent": None}
        store.save_node(db, node1)
        store.save_node(db, node2)
        
        # n1 is claimed
        attempt_id = "attempt1"
        run_id = "ag-123456"
        db.execute("INSERT INTO attempts VALUES (?, ?)", (attempt_id, '{"attempt_id": "attempt1", "node_id": "n1", "run_id": "ag-123456", "state": "claimed", "locks": ["lock1"], "parent": null, "run_parent": null, "at": 0}'))
    
    engine = Engine(DummyService(store))
    
    # We call lock_blockers for n2
    with store.transaction(write=False) as db:
        nodes = store.nodes(db)
        journal = attempts(db)
        blockers = engine.lock_blockers(nodes["n2"], nodes, journal)
    
    assert blockers, "Claimed attempt should hold lock1 and block n2, but didn't block it"
    assert blockers[0]["code"] == "lock"
    assert "lock1" in blockers[0]["detail"]
