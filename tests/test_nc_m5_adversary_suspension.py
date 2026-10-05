import pytest

def test_suspended_run_cannot_complete():
    # A suspended run is never recorded as completed.
    # If record_completion is called on an attempt in state "suspended", it should return False.
    # Currently, it accepts it and sets state="launched", reverting the suspension.
    
    # We construct a dummy store/db mock
    class DummyStore:
        def nodes(self, db): return {"node-1": {"state": "suspended", "revision": 1, "id": "node-1"}}
        def save_node(self, db, node): pass
    
    import multiagents.scheduler.suspension as sus
    
    # Mock save_attempt safely for this test
    original_save = sus.save_attempt
    sus.save_attempt = lambda db, current: None
    
    try:
        attempt = {
            "node_id": "node-1",
            "state": "suspended",
            "run_id": "run-1"
        }
        
        result = {"turn_started_at": None, "completed": True}
        
        # This should return False, but in M5 it currently returns True
        success = sus.record_completion(DummyStore(), None, attempt, result)
        
        assert not success, "A suspended run was recorded as completed!"
    finally:
        sus.save_attempt = original_save
