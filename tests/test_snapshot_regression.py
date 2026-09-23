import time
from multiagents.monitor import snapshot as snap


def test_agent_tree_sorts_roots_by_last_activity():
    """History roots sort by last activity descending rather than started_at.

    An old node with recent activity is sorted above a newer node with older activity,
    and a node with no activity data falls back to started_at. Children (kids) stay
    sorted by started_at ascending.
    """
    now = time.time()
    nodes = {
        # Old node (started 1000s ago) with recent activity (100s ago)
        "ag-old-recent": {
            "id": "ag-old-recent",
            "agent": "advisor",
            "started_at": now - 1000,
            "last_event_at": now - 100,
            "parent": None,
        },
        # Newer node (started 500s ago) with older activity (400s ago)
        "ag-newer-older": {
            "id": "ag-newer-older",
            "agent": "implementer",
            "started_at": now - 500,
            "last_event_at": now - 400,
            "parent": None,
        },
        # Node with no activity data (no last_event_at, no ended_at/until)
        # falling back to started_at (300s ago)
        "ag-no-activity": {
            "id": "ag-no-activity",
            "agent": "tester",
            "started_at": now - 300,
            "parent": None,
        },
        # Node relying on ended_at / until fallback
        "ag-ended-fallback": {
            "id": "ag-ended-fallback",
            "agent": "reviewer",
            "started_at": now - 800,
            "ended_at": now - 200,
            "parent": None,
        },
        # Children under ag-old-recent, verifying kids stay sorted by started_at ascending
        "ag-child-older": {
            "id": "ag-child-older",
            "agent": "worker-1",
            "started_at": now - 800,
            "parent": "ag-old-recent",
        },
        "ag-child-newer": {
            "id": "ag-child-newer",
            "agent": "worker-2",
            "started_at": now - 700,
            "parent": "ag-old-recent",
        },
    }

    roots = snap.agent_tree(nodes, now)
    root_ids = [r["id"] for r in roots]

    # Effective sort keys (descending):
    # ag-old-recent:     now - 100 (from last_event_at / last_spoke)
    # ag-ended-fallback: now - 200 (from ended_at fallback)
    # ag-no-activity:    now - 300 (fallback to started_at)
    # ag-newer-older:    now - 400 (from last_event_at / last_spoke)
    assert root_ids == [
        "ag-old-recent",
        "ag-ended-fallback",
        "ag-no-activity",
        "ag-newer-older",
    ]

    # Kids under ag-old-recent are sorted by started_at ascending
    old_recent_node = next(r for r in roots if r["id"] == "ag-old-recent")
    assert [k["id"] for k in old_recent_node["kids"]] == [
        "ag-child-older",
        "ag-child-newer",
    ]
