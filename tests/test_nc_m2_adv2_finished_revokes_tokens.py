import pytest
import asyncio
from multiagents.scheduler.store import Store

@pytest.mark.anyio
async def test_finished_revokes_tokens(tmp_path):
    """finished() must revoke capabilities for the run."""
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
        def __init__(self, s): self.store = s; self.paths = s.paths

    with store.transaction() as db:
        node = {"id": "n1", "state": "running", "revision": 1, "runs": [], "task": "task", "agent": "a", "locks": []}
        store.save_node(db, node)
        
        attempt_id = "attempt1"
        run_id = "ag-123456"
        db.execute("INSERT INTO attempts VALUES (?, ?)", (attempt_id, f'{{"attempt_id": "{attempt_id}", "node_id": "n1", "run_id": "{run_id}", "state": "launched", "locks": [], "activation_id": "act1", "at": 0}}'))
        db.execute("INSERT INTO capabilities(hash, subject, node_id, permissions) VALUES (?, ?, ?, ?)", ("hash1", run_id, "n1", "[]"))

    engine = Engine(DummyService(store))
    engine.store = store
    
    class DummyRun:
        status = "success"
        session_id = "ses"
        branch = "br"
        id = run_id
    
    engine.finished({"attempt_id": attempt_id, "run_id": run_id, "node_id": "n1", "state": "launched", "locks": []}, DummyRun())
    
    with store.transaction(write=False) as db:
        res = db.execute("SELECT revoked FROM capabilities WHERE subject=?", (run_id,)).fetchone()
    
    assert res[0] == 1, "Capabilities were not revoked by finished()"
