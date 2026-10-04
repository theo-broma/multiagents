import pytest
import asyncio
from multiagents.scheduler.store import Store

@pytest.mark.anyio
async def test_migrate_is_idempotent(tmp_path):
    """migrate must not re-process the same legacy queue item twice."""
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

    engine = Engine(DummyService(store))
    
    class DummyRunner:
        def __init__(self):
            class Tree:
                def __init__(self):
                    self.data = {"deferred": [{"id": "legacy_item_1", "spec": {"agent": "a", "task": "t", "op": "start"}}]}
                def transaction(self):
                    class Tx:
                        def __init__(self, d): self.d = d
                        def __enter__(self): return self.d
                        def __exit__(self, *a): pass
                    return Tx(self.data)
            self.tree = Tree()

    engine.runner = DummyRunner()
    
    engine.migrate()
    
    with store.transaction(write=False) as db:
        nodes1 = store.nodes(db)
        num_nodes = len(nodes1)
    
    assert num_nodes == 1, "Should have created exactly one node"
    
    # Run migrate again
    engine.migrate()
    
    with store.transaction(write=False) as db:
        nodes2 = store.nodes(db)
        num_nodes_2 = len(nodes2)
    
    assert num_nodes_2 == 1, "Migrate should be idempotent and not create a duplicate node for legacy_item_1"
