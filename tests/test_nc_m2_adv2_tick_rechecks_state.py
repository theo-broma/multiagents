import pytest
import asyncio
from multiagents.scheduler.store import Store

@pytest.mark.anyio
async def test_tick_rechecks_node_state(tmp_path):
    """tick must re-check node state before claiming."""
    from multiagents.scheduler.engine import Engine, attempts
    
    store = Store(tmp_path)
    store.initialize()

    class DummyService:
        def configuration(self):
            class Config:
                project = {"scheduler": {"enabled": True, "tick_seconds": 1, "starvation_after_seconds": 60}}
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
        node = {"id": "n1", "kind": "simple", "state": "open", "revision": 1, "runs": [], "task": "task", "created_by": "root", "agent": "a", "locks": [], "depends_on": [], "inputs": [], "urgent": False, "parent": None, "pins": {}, "priority": 0, "created_at": "2026-10-04T12:00:00Z"}
        store.save_node(db, node)

    class DummyRunner:
        def __init__(self):
            self.tree = type('Tree', (), {'get': lambda self, id: None, 'active': lambda self: []})()
            self.runs = {}
            self.config = engine.service.configuration()
        async def adopt(self, exclude): pass
        def reload(self, config): pass
        def _steer_predecessor(self, run_id):
            return None
        async def adopt(self, exclude): pass
        def reload(self, config): pass
        async def _steer_predecessor_dead(self, pred):
            return True

    # wait
    engine = Engine(DummyService(store))
    engine.runner = DummyRunner()
    engine.runner.config = engine.service.configuration()

    # We need to hook admit() to modify the node state during evaluation.
    async def hooked_admit(*args, **kwargs):
        # Change node state in DB when admitted
        with store.transaction() as db:
            n = store.nodes(db)["n1"]
            n["state"] = "cancelled"
            n["revision"] += 1
            store.save_node(db, n)
        return {"admitted": True}
    
    engine.runner.start = hooked_admit

    # Run tick
    await engine.tick()

    with store.transaction(write=False) as db:
        # Check attempts
        # Since it was cancelled, it should NOT be claimed.
        # So no attempts should exist for n1.
        journal = attempts(db)
        assert not any(a["node_id"] == "n1" for a in journal.values()), "Node was claimed even though it was cancelled after admission"
