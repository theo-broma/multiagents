"""Regression tests for the two defects review ag-30876b found in OG round 2.

1. Loading a tree rewrote the `provider` of past runs and of their segments to
   the renamed route. Those are history and stay as written (spec
   Clarifications: "Scheduler attempt and history records stay as they are");
   only live, provider-keyed state moves. A reader that aggregates by provider
   maps the old name through the rename declaration instead.
2. The migration ran under the tree's lock and the first call read the shipped
   providers.yaml there. The declaration is resolved before the lock is taken,
   so no providers file is read while it is held.
"""
from __future__ import annotations

import contextlib
import json
import pathlib
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent / "support"))
import og_support as og  # noqa: E402
from og_support import GO_MODEL, NEW, OLD  # noqa: E402

from multiagents import renames as renames_mod  # noqa: E402
from multiagents.tree import Tree  # noqa: E402

T0 = time.time()
FUTURE = T0 + 7200
MODEL_B = "opencode-go/placeholder-model"


def _node(node_id, provider, model, cost, tokens, **extra):
    return {"id": node_id, "agent": "worker", "provider": provider, "model": model,
            "parent": None, "depth": 0, "status": "done", "task": "placeholder task",
            "usage": {"total": tokens, "cost_usd": cost}, **extra}


def pre_rename() -> dict:
    return {
        "version": 1,
        "nodes": {
            "h1": _node("h1", OLD, GO_MODEL, 0.25, 1000, home_provider=OLD),
            "h2": {**_node("h2", "claude", "sonnet", 1.0, 500),
                   "segments": [{"provider": OLD, "model": MODEL_B,
                                 "usage": {"total": 300, "cost_usd": 0.1}},
                                {"provider": "claude", "model": "sonnet",
                                 "usage": {"total": 200, "cost_usd": 0.9}}]},
            "h3": _node("h3", NEW, GO_MODEL, 0.5, 2000),
        },
        "provider_health": {OLD: {"consecutive_failures": 2, "last_reason": "placeholder"}},
        "cooldowns": {OLD: {"until": FUTURE, "reason": "placeholder", "cause": "quota"}},
        "deferred": [{"id": "df-bbbb01",
                      "spec": {"op": "start", "agent": "worker", "provider": OLD,
                               "model": GO_MODEL, "task": "deferred placeholder"},
                      "retry_after": FUTURE, "reason": "quota", "queued_at": T0 - 5,
                      "status": "waiting", "deferred_by": None}],
        "pause": {}, "questions": [], "tickets": [],
    }


@pytest.fixture
def tree(tmp_path):
    def make(data: dict) -> Tree:
        path = tmp_path / ".multiagents" / "tree.json"
        og.write_tree_json(path, data)
        return Tree(path, tmp_path / ".multiagents" / "events.jsonl")
    return make


# ===========================================================================
# 1. history keeps the name it was written under
# ===========================================================================

def _history(data: dict) -> dict:
    return {node_id: (node.get("provider"), node.get("home_provider"),
                      [s.get("provider") for s in node.get("segments") or []])
            for node_id, node in data["nodes"].items()}


def test_og3_a_past_run_keeps_its_provider_after_load(tree):
    before = pre_rename()
    data = tree(before).read()
    assert data["nodes"] == before["nodes"]
    assert _history(data)["h1"] == (OLD, OLD, [])
    assert _history(data)["h2"] == ("claude", None, [OLD, "claude"])


def test_og3_a_past_run_keeps_its_provider_on_disk_after_a_write(tree):
    before = pre_rename()
    t = tree(before)
    with t.transaction() as data:
        data["tickets"] = []
    on_disk = json.loads(t.path.read_text())
    assert on_disk["nodes"] == before["nodes"]
    # and again from a fresh process: still history, still the old name
    assert Tree(t.path, t.events_path).read()["nodes"] == before["nodes"]


def test_og3_live_state_still_moves_alongside_untouched_history(tree):
    data = tree(pre_rename()).read()
    assert OLD not in data["provider_health"] and NEW in data["provider_health"]
    assert OLD not in data["cooldowns"] and NEW in data["cooldowns"]
    assert data["deferred"][0]["spec"]["provider"] == NEW
    assert data["nodes"]["h1"]["provider"] == OLD


def test_og3_spend_by_provider_reads_history_under_the_renamed_route(tree):
    rows = {(r["provider"], r["model"]): r for r in tree(pre_rename()).usage_by_model()}
    assert not [k for k in rows if k[0] == OLD], rows
    go = rows[(NEW, GO_MODEL)]
    assert go["runs"] == 2 and go["tokens"] == 3000
    assert go["cost_usd"] == pytest.approx(0.75)
    assert rows[(NEW, MODEL_B)]["cost_usd"] == pytest.approx(0.1)
    total = sum(r["cost_usd"] for r in rows.values())
    assert total == pytest.approx(0.25 + 0.1 + 0.9 + 0.5)        # nothing lost or doubled


def test_og3_an_injected_rename_table_drives_the_migration(tmp_path):
    """The table a Tree migrates with is the one it was given: no second read."""
    table = renames_mod.Renames(aliases={"old-placeholder": "new-placeholder"},
                                unroutable=frozenset())
    path = tmp_path / ".multiagents" / "tree.json"
    og.write_tree_json(path, {
        "version": 1,
        "nodes": {"n1": _node("n1", "old-placeholder", "m", 0.1, 10)},
        "provider_health": {"old-placeholder": {"consecutive_failures": 1}},
        "deferred": [], "cooldowns": {}, "pause": {}, "questions": [], "tickets": []})
    data = Tree(path, tmp_path / ".multiagents" / "events.jsonl", renames=table).read()
    assert set(data["provider_health"]) == {"new-placeholder"}
    assert data["nodes"]["n1"]["provider"] == "old-placeholder"


# ===========================================================================
# 2. no providers file is read while the tree lock is held
# ===========================================================================

@pytest.fixture
def lock_watch(monkeypatch):
    """Record every providers.yaml read, and whether the tree lock was held."""
    held = {"depth": 0}
    reads: list[tuple[str, bool]] = []

    original_locked = Tree._locked

    @contextlib.contextmanager
    def locked(self):
        with original_locked(self) as handle:
            held["depth"] += 1
            try:
                yield handle
            finally:
                held["depth"] -= 1

    monkeypatch.setattr(Tree, "_locked", locked)

    def spy(name):
        original = getattr(pathlib.Path, name)

        def wrapped(self, *args, **kwargs):
            if self.name == "providers.yaml":
                reads.append((str(self), held["depth"] > 0))
            return original(self, *args, **kwargs)
        monkeypatch.setattr(pathlib.Path, name, wrapped)

    for name in ("read_text", "read_bytes", "open"):
        spy(name)
    # Forget the process-wide table, so the next caller has to read the file.
    monkeypatch.setattr(renames_mod, "_shipped", None)
    return reads


def test_og3_the_rename_table_is_read_before_the_lock(tree, lock_watch):
    t = tree(pre_rename())
    data = t.read()
    with t.transaction() as written:
        written["tickets"] = []
    assert NEW in data["provider_health"], "the migration did not run"
    assert lock_watch, "the shipped providers file was never read: the test proves nothing"
    assert not [path for path, under_lock in lock_watch if under_lock], lock_watch


def test_og3_a_fresh_tree_reads_nothing_under_the_lock(tree, lock_watch, monkeypatch):
    t = tree(pre_rename())
    lock_watch.clear()
    monkeypatch.setattr(renames_mod, "_shipped", None)
    fresh = Tree(t.path, t.events_path)
    fresh.read()
    fresh.usage_by_model()
    assert not [path for path, under_lock in lock_watch if under_lock], lock_watch


def test_og3_the_spend_ledger_reads_nothing_under_its_lock(tmp_path, lock_watch, monkeypatch):
    """The same rule for the ledger, which reads old charges as the new route."""
    from multiagents import spendcap

    path = tmp_path / ".multiagents" / "spend-ledger.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"kind": "charge", "key": "k-placeholder", "ts": T0 - 10,
                                "usd": 0.5, "provider": OLD, "model": GO_MODEL}) + "\n")
    held = {"depth": 0}
    original_locked = spendcap.Ledger._locked

    @contextlib.contextmanager
    def locked(self, exclusive):
        with original_locked(self, exclusive):
            held["depth"] += 1
            try:
                yield
            finally:
                held["depth"] -= 1

    monkeypatch.setattr(spendcap.Ledger, "_locked", locked)
    original_read_text = pathlib.Path.read_text
    under_lock: list[str] = []

    def read_text(self, *args, **kwargs):
        if self.name == "providers.yaml" and held["depth"]:
            under_lock.append(str(self))
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "read_text", read_text)
    ledger = spendcap.Ledger(path)
    ledger.refresh()
    assert lock_watch, "the shipped providers file was never read: the test proves nothing"
    assert not under_lock, under_lock
    assert ledger.spend(NEW, "", "day", T0) == pytest.approx(0.5)
