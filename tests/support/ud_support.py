"""Shared helpers for the user-decisions slice 1 tests (UD-C1..UD-C6).

Everything here goes through the public surface: `multiagents.decisions`, the
`Tree` question calls, and the files the tree writes (`tree.json`,
`events.jsonl`). `decisions` is imported lazily so a missing module fails each
test on its own call rather than the whole file at collection.
"""
from __future__ import annotations

import importlib
import json
import time
from pathlib import Path

import pytest

from multiagents.tree import Tree

REPO = Path(__file__).resolve().parents[2]

# A bound on every wait in this suite, in seconds.
WAIT_S = 20.0


class _Decisions:
    def __getattr__(self, name):
        return getattr(importlib.import_module("multiagents.decisions"), name)


D = _Decisions()

ORCH = {"role": "orchestrator", "agent": "", "channel": "mcp"}
INIT = {"role": "initializer", "agent": "", "channel": "mcp"}
AGENT = {"role": "agent", "agent": "ag-1", "channel": "mcp"}
USER = {"role": "user", "agent": "", "channel": "cli"}

# 23 characters, shaped like a key the scrubber masks (`\bsk-[A-Za-z0-9_-]{16,}`)
# and also a legal option id (`^[a-z0-9][a-z0-9_-]{0,31}$`).
TOKEN = "sk-" + "a" * 20


def make_tree(tmp_path: Path, name: str = "") -> Tree:
    return Tree(tmp_path / f"tree{name}.json", tmp_path / f"events{name}.jsonl")


def events(tree: Tree) -> list[dict]:
    if not tree.events_path.exists():
        return []
    return [json.loads(line) for line in tree.events_path.read_text().splitlines()
            if line.strip()]


def decision_events(tree: Tree) -> list[dict]:
    return [e for e in events(tree) if e.get("kind") == "decision"]


def code_of(exc: BaseException) -> str:
    """The error code of a DecisionError: `DecisionError("invalid_record",
    field=...)` puts it first."""
    return getattr(exc, "code", None) or str(exc.args[0])


def detail(exc: BaseException, name: str):
    """A keyword the error was built with (`field`, `status`, `revision`,
    `superseded_by`)."""
    return getattr(exc, name, None)


OPTS = [
    {"id": "yes", "label": "Yes", "description": "Go ahead", "implies": "merge runs"},
    {"id": "no", "label": "No", "description": "Hold", "implies": "nothing runs"},
    {"id": "later", "label": "Later", "description": "Ask again", "implies": "re-ask"},
]
IMPL = {"blocks": "the merge of run D", "if_unanswered": "the merge stays held"}


def kwargs(mode: str = "single", **over) -> dict:
    """Keyword arguments for a valid `decisions.create` in `mode`."""
    base = dict(by=ORCH, audience="user", topic="Run D merge", title="Merge run D?",
                context="Run D finished green. Merge it into main?",
                implications=dict(IMPL), mode=mode)
    if mode in ("single", "multi"):
        base["options"] = [dict(o) for o in OPTS]
    if mode == "multi":
        base.update(min=1, max=2)
    base.update(over)
    return base


def create(tree: Tree, mode: str = "single", **over) -> dict:
    return D.create(tree, **kwargs(mode, **over))


def answered(tree: Tree, mode: str = "single", **over) -> dict:
    rec = create(tree, mode, **over)
    if mode == "free_text":
        return D.answer(tree, rec["id"], revision=rec["revision"], by=USER,
                        free_text="ship it")["decision"]
    return D.answer(tree, rec["id"], revision=rec["revision"], by=USER,
                    options=["yes"])["decision"]


def in_status(tree: Tree, status: str) -> dict:
    """A decision in `status`, reached only through the public transitions."""
    rec = create(tree)
    if status == "open":
        return rec
    if status in ("answered", "handled"):
        rec = D.answer(tree, rec["id"], revision=rec["revision"], by=USER,
                       options=["yes"])["decision"]
        if status == "handled":
            rec = D.mark_handled(tree, rec["id"], revision=rec["revision"], by=ORCH,
                                 outcome="applied")
        return rec
    if status == "withdrawn":
        return D.withdraw(tree, rec["id"], revision=rec["revision"], by=ORCH,
                          reason="no longer needed")
    if status == "superseded":
        other = create(tree, topic="Replacement")
        return D.supersede(tree, rec["id"], revision=rec["revision"], by=ORCH,
                           superseded_by=other["id"])
    raise AssertionError(status)


class Clock:
    def __init__(self, t: float):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def tick(self, dt: float = 10.0) -> float:
        self.t += dt
        return self.t


@pytest.fixture
def clock(monkeypatch):
    """A frozen wall clock, advanced only by `clock.tick()`. Patches
    `time.time` and the tree's `now`, so ordering by time needs no sleeping."""
    import multiagents.tree as tree_mod
    c = Clock(1_700_000_000.0)
    monkeypatch.setattr(time, "time", c)
    monkeypatch.setattr(tree_mod, "now", c)
    return c


def schema_path() -> Path:
    for candidate in (REPO / "schemas" / "decision.schema.json",
                      REPO / "src" / "multiagents" / "schemas" / "decision.schema.json"):
        if candidate.is_file():
            return candidate
    return REPO / "schemas" / "decision.schema.json"


def validator():
    import jsonschema
    schema = json.loads(schema_path().read_text())
    return jsonschema.Draft202012Validator(schema)


def assert_valid(record: dict) -> None:
    errors = sorted(validator().iter_errors(record), key=lambda e: list(e.path))
    assert not errors, "; ".join(f"{list(e.path)}: {e.message}" for e in errors[:5])


LEGACY_OPEN = {
    "id": "q-0a1b2c", "agent": "ag-old", "topic": "store",
    "question": "Postgres or SQLite?\nAlso: which version?", "proposed_default": "SQLite",
    "asked_at": 1_699_000_000.5, "status": "open", "answer": "",
    "answered_at": None, "answered_by": "",
}
LEGACY_ANSWERED = {
    "id": "q-ffee01", "agent": "ag-old2", "topic": "naming",
    "question": "Call it widget or gadget?", "proposed_default": "",
    "asked_at": 1_699_000_100.0, "status": "answered", "answer": "widget",
    "answered_at": 1_699_000_200.25, "answered_by": "orchestrator",
}


def seed_legacy(tree: Tree, *records: dict) -> None:
    """Write legacy `questions` records into the store, as an older version
    of the tree would have left them."""
    with tree.transaction() as data:
        data["questions"].extend(dict(r) for r in records)
