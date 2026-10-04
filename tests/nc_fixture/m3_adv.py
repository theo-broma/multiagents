"""Deterministic M3 harness for the adversary tests (tests/test_nc_m3_adv_*.py).

Builds a scheduler `Service` + `Engine` on a `GitWorld` without starting the
evaluation thread, and lets a test lay out node records and launched
activations by hand (the same shape `tests/test_nc_m3_result_replay.py` uses),
so crash and interleaving points can be hit exactly instead of by timing.
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nc_fixture.gitworld import GitWorld  # noqa: E402
from multiagents import gitops  # noqa: E402
from multiagents.scheduler.engine import Engine, attempts, save_attempt  # noqa: E402
from multiagents.scheduler.model import create_record  # noqa: E402
from multiagents.scheduler.results import Results  # noqa: E402
from multiagents.scheduler.rpc import Service  # noqa: E402
from multiagents.tree import Node, now  # noqa: E402


class Harness:
    def __init__(self, tmp_path, monkeypatch):
        self.world = GitWorld(tmp_path, monkeypatch)
        self.world.write_config()
        self.service = Service(self.world.root, now())
        self.service.store.initialize()
        self.engine = Engine(self.service)
        self.service.engine = self.engine
        self.results = Results(self.world.paths, self.service.configuration())

    def close(self):
        self.engine.loop.close()
        self.world.close()

    # ------------------------------------------------------------- records
    def record(self, **fields):
        fields.setdefault("kind", "simple")
        if fields["kind"] == "simple":
            fields.setdefault("agent", "coder")
            fields.setdefault("task", "work")
        return create_record(fields, "root")

    def save(self, *records):
        with self.service.store.transaction() as db:
            for record in records:
                self.service.store.save_node(db, record)

    def nodes(self):
        with self.service.store.transaction(write=False) as db:
            return self.service.store.nodes(db)

    def journal(self):
        with self.service.store.transaction(write=False) as db:
            return attempts(db)

    def tree(self, parent_kind, n):
        """A composite of `n` simple children, saved, all `open`."""
        children = [self.record() for _ in range(n)]
        parent = self.record(kind=parent_kind, children=[c["id"] for c in children])
        for child in children:
            child["parent"] = parent["id"]
        self.save(parent, *children)
        return parent, children

    # --------------------------------------------------------- activations
    def launch(self, node_id, readonly=()):
        """Claim + launch `node_id` as the tick would, with a real checkout."""
        nodes = self.nodes()
        node = nodes[node_id]
        prepared = self.results.prepare(node, nodes)
        top = nodes[prepared["branch_node"]]
        run_id = "ag-" + uuid.uuid4().hex[:6]
        attempt = {"attempt_id": uuid.uuid4().hex, "activation_id": uuid.uuid4().hex,
                   "run_id": run_id, "node_id": node_id, "state": "launched", "locks": [],
                   "at": now(), "readonly_paths": list(readonly), **prepared}
        node.update(state="running", runs=node["runs"] + [{
            "run_id": run_id, "attempt_id": attempt["attempt_id"],
            "input_commit": prepared["input_commit"]}])
        with self.service.store.transaction() as db:
            self.service.store.save_node(db, top)
            self.service.store.save_node(db, node)
            save_attempt(db, attempt)
        self.results.ensure_branch(self.nodes()[prepared["branch_node"]])
        checkout = self.world.paths.worktree(run_id)
        branch = "agents/coder/" + run_id[3:]
        self.results.create_checkout(checkout, branch, prepared["input_commit"])
        run = Node(id=run_id, agent="coder", provider="fx", model="fx/m1", parent=None,
                   depth=1, branch=branch, worktree=str(checkout), status="done",
                   node_id=node_id, attempt_id=attempt["attempt_id"])
        self.engine.runner.tree.add(run)
        self.engine.runner.authority.add(run)
        return attempt, run

    @staticmethod
    def commit(run, files, message="work"):
        checkout = Path(run.worktree)
        for path, text in files.items():
            target = checkout / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)
        gitops.run(checkout, "add", "-A", check=True)
        gitops.run(checkout, "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                   "commit", "-q", "-m", message, check=True)
        return gitops.run(checkout, "rev-parse", "HEAD", check=True).out
