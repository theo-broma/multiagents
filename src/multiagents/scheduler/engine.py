"""Admission, durable activation claims and reconciliation (NC-R16..R26)."""
from __future__ import annotations

import asyncio
import copy
from dataclasses import replace
from datetime import datetime, timezone
import fcntl
import json
import logging
import os
from pathlib import Path
from types import SimpleNamespace
import subprocess
import sys
import threading
import time
import uuid

from .. import procs
from ..runner import LaunchContext, Runner, admission_block
from ..tree import now
from ..tree import ACTIVE, PAUSED
from . import model
from .store import encode
from .results import Results, input_generation, top_node
from . import sessions, effects
from .. import gitops


def epoch(value):
    if isinstance(value, (int, float)):
        return value
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def attempts(db):
    return {id: json.loads(raw) for id, raw in db.execute("SELECT id, record FROM attempts")}


def save_attempt(db, record):
    db.execute("INSERT OR REPLACE INTO attempts VALUES (?, ?)", (record["attempt_id"], encode(record)))


def record_launch(store, db, attempt, run):
    current = attempts(db)[attempt["attempt_id"]]
    if current["state"] != "claimed":
        return False
    current["state"] = "launched"
    save_attempt(db, current)
    node = store.nodes(db)[attempt["node_id"]]
    if not any(r["attempt_id"] == attempt["attempt_id"] for r in node["runs"]):
        node["runs"].append({"run_id": run.id, "attempt_id": attempt["attempt_id"],
                             "activation_id": attempt["activation_id"],
                             **({"input_commit": attempt["input_commit"]} if "input_commit" in attempt else {})})
    if node["state"] == "open":
        node.update(state="running", ready_since=None, revision=node["revision"] + 1)
        node.pop("starvation_notified", None)
    store.save_node(db, node)
    nodes = store.nodes(db)
    parent = nodes.get(node["parent"])
    while parent:
        if parent["kind"] != "simple":
            parent["lock_claimed"] = True
            if parent["state"] == "open":
                parent.update(state="running", revision=parent["revision"] + 1)
            store.save_node(db, parent)
        parent = nodes.get(parent["parent"])
    store.transition(db, "launched", node["id"], {"run_id": run.id, "attempt_id": attempt["attempt_id"]})
    return True


class Engine:
    def __init__(self, service, clock_file=None):
        self.service = service
        self.store = service.store
        self.paths = service.paths
        self.clock_file = Path(clock_file) if clock_file else None
        self.runner = Runner(self.paths, service.configuration())
        self.reasons = {}
        self.last_tick = None
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.run, name="node-evaluation", daemon=True)
        self.stopped = threading.Event()
        self.children = []
        self.migrate()
        # Runtime configuration is host state, not project work to integrate.
        exclude = self.paths.root / ".git" / "info" / "exclude"
        if exclude.parent.is_dir():
            text = exclude.read_text() if exclude.exists() else ""
            if ".multiagents/" not in text.splitlines():
                exclude.write_text(text + "\n.multiagents/\n")

    def instant(self):
        if self.clock_file:
            value = datetime.fromisoformat(self.clock_file.read_text().strip().replace("Z", "+00:00"))
            if value.tzinfo is None:
                raise ValueError("scheduler clock must have a timezone")
            return value.timestamp()
        return now()

    def migrate(self):
        # Store first, queue second: a crash between them replays the entry's
        # durable migration key without duplicating its node.
        with self.runner.tree.transaction() as tree:
            entries = [e for e in tree["deferred"] if isinstance(e, dict) and e.get("id")]
            with self.store.transaction() as db:
                nodes = self.store.nodes(db)
                migrated = {n.get("migration_id") for n in nodes.values()}
                revision = int(self.store.meta(db, "plan_revision"))
                for entry in entries:
                    if entry["id"] in migrated:
                        continue
                    spec = entry.get("spec") or {}
                    if spec.get("op", "start") != "start":
                        continue
                    if not spec.get("agent") or not spec.get("task"):
                        continue
                    pins = {k: spec[k] for k in ("model", "effort", "provider") if spec.get(k)}
                    provider = entry.get("provider") or spec.get("provider")
                    if provider:
                        pins["provider"] = provider
                    node = model.create_record(dict(kind="simple", agent=spec["agent"],
                                                    task=spec["task"], pins=pins), "root")
                    node.update(migration_id=entry["id"], created_at=entry.get("queued_at") or entry.get("at") or now(),
                                run_parent=spec.get("parent") or entry.get("deferred_by"),
                                launch_caller=entry.get("deferred_by"),
                                depth=spec.get("depth") or 1,
                                launch={k: spec[k] for k in ("timeout", "workdir", "verifies", "budget_tag") if k in spec})
                    if entry.get("status") == "refused":
                        node.update(state="held", hold={"reason": "admission:refused"})
                    self.store.save_node(db, node)
                    self.store.transition(db, "created", node["id"], {"migration_id": entry["id"]})
                    revision += 1
                    migrated.add(entry["id"])
                self.store.set_meta(db, "plan_revision", revision)
            tree["deferred"] = [e for e in tree["deferred"] if not isinstance(e, dict) or e.get("id") not in migrated]

    def context(self, node, attempt=None, probe=False):
        parent = node.get("run_parent")
        if "run_parent" not in node and node["created_by"] != "root":
            parent = node["created_by"]
        creator = self.runner.tree.get(parent) if parent else None
        return LaunchContext(caller=node.get("launch_caller", parent), run_parent=parent,
                             depth=node.get("depth") or (creator.depth + 1 if creator else 1),
                             node_id=node["id"], attempt_id=(attempt or {}).get("attempt_id", ""),
                             run_id=(attempt or {}).get("run_id", ""),
                             provider=node["pins"].get("provider", ""),
                             effort=node["pins"].get("effort", ""), admission_only=probe)

    def structural(self, node, nodes):
        if node["state"] == "held":
            return [{"code": "held", "detail": node["hold"]}]
        if node["state"] != "open" or node["kind"] != "simple":
            return []
        # Delegated nodes can run after their creator finishes. A done simple
        # ancestor therefore imposes no composite gate on them (NC-R12).
        cur = node
        while cur:
            if cur.get("completion_pending") or cur.get("disposal_pending"):
                return [{"code": "ancestor", "detail": cur["id"]}]
            if cur is not node and (cur["state"] in {"held", "cancelled", "suspended"}
                    or cur["kind"] != "simple" and cur["state"] == "done"):
                return [{"code": "ancestor", "detail": cur["id"]}]
            for ref in cur["depends_on"]:
                other = nodes.get(ref["node"])
                if other is None:
                    return [{"code": "dependency" if cur is node else "ancestor", "detail": ref["node"]}]
                allowed = ({"completed", "approved"} if ref.get("require", "success") == "success"
                           else {"approved"} if ref["require"] == "approved" else None)
                if other["state"] != "done" or allowed and other["outcome"] not in allowed:
                    return [{"code": "dependency" if cur is node else "ancestor", "detail": ref["node"]}]
            for ref in cur["inputs"]:
                if not input_generation(nodes.get(ref["node"]), ref, nodes):
                    return [{"code": "input", "detail": ref["node"]}]
            parent = nodes.get(cur["parent"])
            if parent and parent["kind"] in {"sequence", "loop"}:
                index = parent["children"].index(cur["id"])
                if index and not model.succeeded(nodes[parent["children"][index-1]]):
                    return [{"code": "ancestor", "detail": parent["id"]}]
            cur = parent
        return []

    def lock_set(self, node, nodes):
        locks = set(node["locks"])
        parent = nodes.get(node["parent"])
        while parent:
            locks.update(parent["locks"])
            parent = nodes.get(parent["parent"])
        return locks

    def lock_owners(self, node, nodes):
        owners = {name: node["id"] for name in node["locks"]}
        parent = nodes.get(node["parent"])
        while parent:
            for name in parent["locks"]:
                owners[name] = parent["id"]
            parent = nodes.get(parent["parent"])
        return owners

    def lock_blockers(self, node, nodes, journal):
        wanted = self.lock_owners(node, nodes)
        overlap = set()
        for attempt in journal.values():
            if attempt["state"] not in {"claimed", "launched", "captured"} or attempt["node_id"] == node["id"]:
                continue
            holders = attempt.get("lock_owners") or {name: attempt["node_id"] for name in attempt.get("locks", [])}
            overlap.update(name for name in wanted.keys() & holders.keys() if wanted[name] != holders[name])
        for composite in nodes.values():
            if composite.get("lock_claimed") and composite["state"] not in {"done", "cancelled"}:
                holders = self.lock_owners(composite, nodes)
                overlap.update(name for name in wanted.keys() & holders.keys() if wanted[name] != holders[name])
        return [{"code": "lock", "detail": sorted(overlap)}] if overlap else []

    def session_blockers(self, node, nodes, journal, db=None):
        if db is None:
            with self.store.transaction(write=False) as connection:
                bindings = sessions.aliases(connection)
        else:
            bindings = sessions.aliases(db)
        return sessions.blockers(node, nodes, journal, bindings, self.runner)

    def view(self, node, nodes, journal, db=None):
        structural = self.structural(node, nodes)
        ready = node["kind"] == "simple" and node["state"] == "open" and not structural
        active = next((a["run_id"] for a in journal.values()
                       if a["node_id"] == node["id"] and a["state"] in {"claimed", "launched"}), None)
        blocked = structural
        if ready:
            blocked = self.lock_blockers(node, nodes, journal) or self.session_blockers(node, nodes, journal, db) or self.reasons.get(node["id"], [])
        if node["state"] in {"running", "done", "cancelled", "suspended"}:
            blocked = []
        return {**copy.deepcopy(node), "ready": ready, "ready_since": node.get("ready_since"),
                "eligible": ready and not blocked and not active, "blocked": copy.deepcopy(blocked),
                "eligible_since": node.get("ready_since") if ready and not blocked else None,
                "active_run": active if node["state"] not in {"done", "cancelled"} else None}

    def status(self, nodes, journal, db=None):
        return {"last_tick": self.last_tick,
                "held": [{"node_id": n["id"], "hold": n["hold"]} for n in nodes.values() if n["state"] == "held"],
                "locks": [{"name": name, "holder_run": a["run_id"], "holder_node": a["node_id"]}
                          for a in journal.values() if a["state"] in {"claimed", "launched"}
                          for name in a.get("locks", [])], "aliases": self.alias_status(db), "windows": []}

    def alias_status(self, db=None):
        if db is not None:
            return list(sessions.aliases(db).values())
        with self.store.transaction(write=False) as db:
            return list(sessions.aliases(db).values())

    def start(self):
        self.thread.start()

    def run(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self.evaluate_forever())
        self.loop.close()

    async def evaluate_forever(self):
        while not self.stopped.is_set():
            try:
                await self.tick()
            except Exception:
                logging.getLogger(__name__).exception("scheduler evaluation failed")
            deadline = time.monotonic() + self.service.configuration().project["scheduler"]["tick_seconds"]
            while time.monotonic() < deadline and not self.stopped.is_set():
                await asyncio.sleep(min(0.1, max(0, deadline - time.monotonic())))
                if self.completion_changed():
                    break
        await self.runner.shutdown(detach=True, preserve_status=True)

    def completion_changed(self):
        # Detached supervisors cannot signal this process's condition variable.
        # Keep waking until reconciliation records the result. Terminal tree
        # status can precede confirmed wrapper death; consuming that first
        # wakeup would leave subsequent rounds waiting for the periodic tick.
        with self.store.transaction(write=False) as db:
            active = {a["run_id"] for a in attempts(db).values() if a["state"] in {"claimed", "launched"}}
        return any(id in active and run["status"] not in ACTIVE | PAUSED | {"quota_paused"}
                   for id, run in self.runner.tree.read()["nodes"].items())

    def stop(self):
        self.stopped.set()
        self.thread.join(timeout=10)
        with self.service.changed, self.store.transaction() as db:
            for attempt in attempts(db).values():
                # The worker marks entry into Runner.start atomically with its
                # claim check. Once entered, it owns the launch independently
                # of scheduler shutdown (NC-R57), including its capability.
                if (attempt["state"] == "claimed" and not attempt.get("launch_in_progress")
                        and self.runner.tree.get(attempt["run_id"]) is None
                        and not self.paths.run_dir(attempt["run_id"]).exists()):
                    attempt.update(state="abandoned", ended_at=now())
                    save_attempt(db, attempt)
                    db.execute("UPDATE capabilities SET revoked=1 WHERE subject=?", (attempt["run_id"],))

    def order(self, node, nodes):
        top = node
        urgent = node["urgent"]
        while top["parent"]:
            top = nodes[top["parent"]]
            urgent |= top["urgent"]
        return not urgent, epoch(top["created_at"]), epoch(node["created_at"]), node["id"]

    async def tick(self):
        config = self.service.configuration()
        if not config.project["scheduler"]["enabled"]:
            return
        if self.runner.config is not config:
            self.runner.reload(config)
        self.last_tick = datetime.fromtimestamp(self.instant(), timezone.utc).isoformat()
        with self.store.transaction(write=False) as db:
            candidates = set(self.store.nodes(db))
            managed = {a["run_id"] for a in attempts(db).values()}
        await self.runner.adopt(exclude=managed)
        await self.reconcile()
        self.composites()
        # Mirror recorded results before admission awaits launches, so a
        # client that sees a node done finds its transitions in the log.
        self.store.mirror()
        with self.store.transaction(write=False) as db:
            nodes, journal = self.store.nodes(db), attempts(db)
        # Deposits arriving while reconciliation awaits I/O belong to the
        # next tick, so every admission pass has a coherent candidate set.
        for node in sorted((n for id, n in nodes.items() if id in candidates),
                           key=lambda n: self.order(n, nodes)):
            ready = node["kind"] == "simple" and node["state"] == "open" and not self.structural(node, nodes)
            if node["state"] == "open" and not ready:
                self.episode(node["id"], False)
            if not ready:
                continue
            self.episode(node["id"], True)
            if any(a["node_id"] == node["id"] and a["state"] in {"claimed", "launched"} for a in journal.values()):
                continue
            if self.lock_blockers(node, nodes, journal):
                continue
            session_blocked = self.session_blockers(node, nodes, journal)
            if session_blocked:
                self.reasons[node["id"]] = session_blocked
                if session_blocked[0]["code"] == "session_unavailable":
                    self.session_notice(node["id"], session_blocked[0])
                continue
            with self.store.transaction(write=False) as db:
                alias_key = sessions.alias_id(node, nodes)
                checked_binding = sessions.aliases(db).get(alias_key)
                checked_attempts = {id: a for id, a in attempts(db).items()
                                    if alias_key and a.get("alias_id") == alias_key}
            binding = checked_binding
            probe = self.context(node, probe=True)
            if binding and binding.get("renew"):
                binding = None
            if binding:
                probe = replace(probe, provider=binding["provider"])
            result = await self.runner.start(node["agent"], node["task"],
                                             model=binding["model"] if binding else node["pins"].get("model"),
                                             launch_context=probe,
                                             **node.get("launch", {}))
            blocked = result.get("blocked", [])
            if binding and result.get("error"):
                blocked = [{"code": "session_unavailable", "detail": result.get("reason", result["error"])}]
                self.session_notice(node["id"], blocked[0])
            with self.service.changed:
                self.reasons[node["id"]] = blocked
            if not result.get("admitted") or self.stopped.is_set():
                continue
            clean_failure = None
            try:
                if checked_binding:
                    sessions.check_clean(self.paths, checked_binding, self.runner.authority)
            except model.Refused as exc:
                clean_failure = (exc.result["error"], exc.result.get("detail", exc.result.get("problems", "")))
            except (gitops.GitError, OSError, ValueError) as exc:
                clean_failure = ("reseat_failed", str(exc))
            with self.store.transaction(write=False) as db:
                preparation_nodes = self.store.nodes(db)
            prospective = preparation_nodes[node["id"]]
            if prospective["state"] != "open" or prospective["revision"] != node["revision"]:
                continue
            prepared_nodes = copy.deepcopy(preparation_nodes)
            preparation_failure = None
            if not clean_failure:
                try:
                    with effects.serialized(self.store):
                        prepared = Results(self.paths, config).prepare(prepared_nodes[node["id"]], prepared_nodes)
                except gitops.GitError as exc:
                    preparation_failure = str(exc)
            with self.service.changed, self.store.transaction() as db:
                if self.service.configuration() is not config:
                    continue
                current_nodes = self.store.nodes(db)
                if current_nodes != preparation_nodes:
                    continue
                current = current_nodes[node["id"]]
                if current["state"] != "open" or current["revision"] != node["revision"]:
                    continue
                if sessions.alias_id(current, current_nodes) != alias_key:
                    continue
                current_journal = attempts(db)
                if alias_key and (sessions.aliases(db).get(alias_key) != checked_binding
                        or {id: a for id, a in current_journal.items() if a.get("alias_id") == alias_key} != checked_attempts):
                    continue
                if self.structural(current, current_nodes):
                    continue
                if self.lock_blockers(current, current_nodes, current_journal) or self.session_blockers(current, current_nodes, current_journal, db):
                    continue
                if clean_failure:
                    self.hold(db, current, *clean_failure)
                    continue
                if preparation_failure:
                    self.hold(db, current, "input_conflict", preparation_failure)
                    continue
                top = current_nodes[prepared["branch_node"]]
                top.update(branch=prepared_nodes[top["id"]]["branch"], branch_tip=prepared_nodes[top["id"]]["branch_tip"])
                self.store.save_node(db, top)
                try:
                    alias = sessions.freeze(db, current, current_nodes, result, self.paths, config)
                    if alias:
                        bound = sessions.aliases(db)[alias["alias_id"]]
                        if bound.get("commit"):
                            alias["previous_commit"] = bound["commit"]
                        prepared.update(alias)
                except model.Refused as exc:
                    self.hold(db, current, exc.result["error"], exc.result.get("detail", exc.result.get("problems", "")))
                    continue
                except (gitops.GitError, OSError, ValueError) as exc:
                    self.hold(db, current, "reseat_failed", str(exc))
                    continue
                current.pop("session_unavailable_notified", None)
                spec = config.agents[current["agent"]]
                prepared["readonly_paths"] = list(config.readonly_paths_for(spec))
                parent = current_nodes.get(current["parent"])
                if parent and parent["kind"] == "loop" and parent["loop"]["verdict_child"] == current["id"]:
                    reviewed = parent["generations"][-1]
                    prepared["review"] = {"node_id": parent["id"], "generation_seq": reviewed["seq"], "commit": reviewed["commit"]}
                    prepared["input_commit"] = reviewed["commit"]
                if current.get("findings"):
                    prepared["findings"] = current["findings"]
                activation = current.get("activation_id") or uuid.uuid4().hex
                current["activation_id"] = activation
                attempt = {"attempt_id": uuid.uuid4().hex, "activation_id": activation,
                           "node_id": node["id"], "run_id": "ag-" + uuid.uuid4().hex[:6],
                           "state": "claimed", "locks": sorted(self.lock_set(current, current_nodes)),
                           "lock_owners": self.lock_owners(current, current_nodes),
                           "retry_count": current.get("launch_tries", 0), "at": now(), **prepared}
                current["launch_tries"] = attempt["retry_count"] + 1
                self.store.save_node(db, current)
                save_attempt(db, attempt)
            # The branch intent and input are durable before any git ref is
            # created. A crash here abandons only this claim, not its input.
            results = Results(self.paths, config)
            try:
                results.ensure_branch(current_nodes[attempt["branch_node"]])
                results.move("refs/heads/node-inputs/" + attempt["attempt_id"], attempt["input_commit"], "")
            except effects.FAILURES as exc:
                # Nothing was spawned: abandon this claim and hold the node,
                # rather than failing every later evaluation at this point.
                with self.service.changed, self.store.transaction() as db:
                    claimed = attempts(db)[attempt["attempt_id"]]
                    if claimed["state"] == "claimed":
                        claimed.update(state="abandoned", ended_at=now())
                        save_attempt(db, claimed)
                        db.execute("UPDATE capabilities SET revoked=1 WHERE subject=?", (attempt["run_id"],))
                        self.hold(db, self.store.nodes(db)[attempt["node_id"]], "input_conflict",
                                  str(exc) or type(exc).__name__)
                with self.store.transaction(write=False) as db:
                    nodes, journal = self.store.nodes(db), attempts(db)
                continue
            self.spawn(attempt)
            # The next candidate observes the tree reservation made by Runner,
            # so a deposited urgent node cannot lose its place to worker latency.
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and not self.stopped.is_set():
                with self.store.transaction(write=False) as db:
                    state = attempts(db)[attempt["attempt_id"]]["state"]
                if state != "claimed" or self.runner.tree.get(attempt["run_id"]):
                    break
                await asyncio.sleep(0.05)
            with self.store.transaction(write=False) as db:
                nodes, journal = self.store.nodes(db), attempts(db)
        self.composites()
        self.store.mirror()

    def session_notice(self, id, detail):
        with self.service.changed, self.store.transaction() as db:
            node = self.store.nodes(db)[id]
            if not node.get("session_unavailable_notified"):
                node["session_unavailable_notified"] = True
                self.store.save_node(db, node)
                self.store.transition(db, "session_unavailable", id, detail)

    def episode(self, id, ready):
        with self.service.changed, self.store.transaction() as db:
            node = self.store.nodes(db)[id]
            if node["state"] != "open":
                return
            if ready:
                if not node.get("ready_since"):
                    node["ready_since"] = datetime.fromtimestamp(self.instant(), timezone.utc).isoformat()
                age = self.instant() - epoch(node["ready_since"])
                if age >= self.service.configuration().project["scheduler"]["starvation_after_seconds"] and not node.get("starvation_notified"):
                    self.store.transition(db, "starving", id)
                    node["starvation_notified"] = True
            else:
                node.pop("ready_since", None)
                node.pop("starvation_notified", None)
            self.store.save_node(db, node)

    def spawn(self, attempt):
        with (self.store.directory / (attempt["attempt_id"] + ".lock")).open("a+") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            # Pass the locked open-file description through exec: ownership
            # spans Python boot, without a recovery gap before supervise().
            with (self.store.directory / "supervisors.log").open("ab") as log:
                proc = subprocess.Popen([sys.executable, "-m", "multiagents.scheduler.worker",
                                         str(self.paths.root), attempt["attempt_id"], str(lock.fileno())],
                                        stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                        pass_fds=(lock.fileno(),), cwd=self.paths.root, start_new_session=True)
        self.children.append(proc)

    async def reconcile(self):
        effects.finish_completions(self)
        effects.finish_operations(self)
        with self.store.transaction(write=False) as db:
            journal = attempts(db)
        settled = []
        for attempt in journal.values():
            if attempt["state"] == "captured":
                self.integrate(attempt)
                continue
            if attempt["state"] not in {"claimed", "launched"}:
                continue
            if attempt.get("capture_intent"):
                self.finished(attempt, SimpleNamespace(**attempt["capture_intent"]))
                continue
            if attempt.get("completion_proven"):
                self.finished(attempt, self.missing_result(attempt))
                continue
            pending_commands = any("result" not in c for c in attempt.get("steer_commands", {}).values())
            if pending_commands:
                self.spawn(attempt)
            run = self.runner.tree.get(attempt["run_id"])
            if run:
                self.spawn(attempt)
                if attempt["state"] == "claimed":
                    self.launched(attempt, run)
                pending = attempt.get("resume_pending")
                if pending and run.turn_started_at > attempt.get("previous_turn", 0):
                    with self.service.changed, self.store.transaction() as db:
                        current = attempts(db)[attempt["attempt_id"]]
                        current.pop("resume_pending", None)
                        save_attempt(db, current)
                    pending = None
                if pending_commands or (pending and pending > now()):
                    continue
                if run.status not in ACTIVE | PAUSED | {"quota_paused"}:
                    # Terminal status alone is insufficient; a foreign stop can
                    # record cancelled while the wrapper still lives (NC-R26).
                    predecessor = self.runner._steer_predecessor(run.id)
                    if not await self.runner._steer_predecessor_dead(predecessor):
                        continue
                    self.finished(attempt, run)
                continue
            # A supervisor may have started but not written the tree yet. Its
            # flock serializes recovery of this same claim, even if SIGKILL
            # landed before the scheduler recorded a process identity.
            if attempt["state"] in {"claimed", "launched"}:
                with (self.store.directory / (attempt["attempt_id"] + ".lock")).open("a+") as lock:
                    try:
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        continue
                    if now() - attempt["at"] > 1:
                        evidence = self.paths.run_dir(attempt["run_id"])
                        confirmed = False
                        if attempt.get("missing_tree_since"):
                            predecessor = self.missing_predecessor(attempt)
                            confirmed = await self.runner._steer_predecessor_dead(predecessor)
                        with self.service.changed, self.store.transaction() as db:
                            current = attempts(db)[attempt["attempt_id"]]
                            if current["state"] not in {"claimed", "launched"}:
                                continue
                            node = self.store.nodes(db)[attempt["node_id"]]
                            db.execute("UPDATE capabilities SET revoked=1 WHERE subject=?", (attempt["run_id"],))
                            if (evidence.exists() or current.get("launch_evidence")
                                    or current.get("launch_in_progress") or current["state"] == "launched"):
                                if confirmed:
                                    current.update(state="abandoned", ended_at=now())
                                    save_attempt(db, current)
                                    if self.recovered(db, current, node):
                                        settled.append(current)
                                    continue
                                current.setdefault("missing_tree_since", now())
                                # Missing tree data cannot erase launch evidence:
                                # keep its locks until death is positively proved.
                                if node["state"] not in {"held", "cancelled", "done"}:
                                    prior = node["state"]
                                    node.update(state="held", hold={"reason": "termination_unconfirmed",
                                                "detail": str(evidence), "since": now()},
                                                revision=node["revision"] + 1)
                                    self.store.save_node(db, node)
                                    current["recovery_hold"] = {"state": prior, "revision": node["revision"]}
                                save_attempt(db, current)
                            else:
                                current.update(state="abandoned", ended_at=now())
                                save_attempt(db, current)
        for attempt in settled:
            self.finished(attempt, self.missing_result(attempt))
        self.children = [p for p in self.children if p.poll() is None]

    def missing_result(self, attempt):
        # Missing diagnostics cannot turn a run into success, but its branch
        # and immutable result must still be captured through the same boundary.
        return SimpleNamespace(id=attempt["run_id"], status="failed", session_id="")

    def recovered(self, db, attempt, node):
        # Death is proved: lift the hold this recovery placed (and only that
        # one, at its revision) and take the ordinary path. A claim that never
        # launched leaves the node eligible again (NC-R17); a launched run
        # that died without a tree entry ends the node as a failed run.
        hold = attempt.get("recovery_hold")
        state = node["state"]
        if hold and state == "held" and node["revision"] == hold["revision"]:
            state = hold["state"]
        launched = any(r["attempt_id"] == attempt["attempt_id"] for r in node.get("runs", []))
        launch_evidence = "input_commit" in attempt and (attempt.get("launch_evidence") or {}).get("pid")
        if launched or launch_evidence:
            if not launched:
                node["runs"].append({"run_id": attempt["run_id"], "attempt_id": attempt["attempt_id"],
                                     "activation_id": attempt["activation_id"], "input_commit": attempt["input_commit"]})
            if state not in {"held", "cancelled", "done", "suspended"}:
                node.update(state="running", hold=None, revision=node["revision"] + 1)
            attempt.update(state="launched", completion_proven="missing_tree")
            save_attempt(db, attempt)
            self.store.save_node(db, node)
            return True
        elif state in {"held", "cancelled", "done", "suspended"}:
            return
        elif node["state"] == "held":
            node.update(state=state, hold=None, revision=node["revision"] + 1)
        else:
            return
        self.store.save_node(db, node)

    def missing_predecessor(self, attempt):
        predecessor = self.runner._steer_predecessor(attempt["run_id"])
        if not getattr(predecessor, "absent", False):
            return predecessor
        # Without a tree entry, Runner's usual absent-predecessor result is
        # insufficient. Use the common launcher's host-written identity;
        # missing identity is unknown, never proof of death.
        from ..runner import _unknown_probe
        from types import SimpleNamespace
        evidence = attempt.get("launch_evidence")
        if not evidence:
            return replace(predecessor, captured=False, probe_raw=_unknown_probe)
        executor = self.runner._predecessor_executor(
            SimpleNamespace(exec_identity=evidence.get("executor", {})), None)
        probe = self.runner._raw_alive_probe(executor, attempt["run_id"])
        return replace(predecessor, captured=bool(evidence.get("pid") or probe),
                       pid=evidence.get("pid"), pid_start=evidence.get("pid_start", ""),
                       executor=executor, probe_raw=probe if evidence.get("pid") or probe else _unknown_probe)

    def launched(self, attempt, run):
        with self.service.changed, self.store.transaction() as db:
            if record_launch(self.store, db, attempt, run):
                self.service.changed.notify_all()

    def hold(self, db, node, reason, detail=""):
        node.update(state="held", hold={"reason": reason, "detail": detail},
                    revision=node["revision"] + 1)
        self.store.save_node(db, node)
        self.store.transition(db, reason, node["id"], node["hold"])
        self.store.transition(db, "held", node["id"], node["hold"])

    def finished(self, attempt, run):
        """Journal confirmed death, capture outside the lock, then save its result.

        finished + integrate form the completion boundary for quota handover.
        The launched attempt retains ownership until its capture outcome is
        durable. Reconcile resumes either the intent or the captured result.
        """
        with self.service.changed, self.store.transaction() as db:
            current = attempts(db)[attempt["attempt_id"]]
            if current["state"] in {"recorded", "captured"}:
                return
            current.setdefault("capture_intent", {"id": run.id, "status": run.status,
                                                   "session_id": run.session_id})
            save_attempt(db, current)
            db.execute("UPDATE capabilities SET revoked=1 WHERE subject=?", (run.id,))
            config = self.service.configuration()
        run = SimpleNamespace(**current["capture_intent"])
        result = {"status": run.status, "session_id": run.session_id,
                  "run_dir": str(self.paths.run_dir(run.id))}
        if "input_commit" in current:
            from .results import MissingCheckout
            try:
                result = Results(self.paths, config).capture(run, current, self.runner.authority)
            except MissingCheckout as exc:
                result.update(status="failed", failure="missing_tree", detail=str(exc))
            except (gitops.GitError, OSError, ValueError) as exc:
                result["capture_error"] = str(exc)
        if current.get("completion_proven") == "missing_tree":
            result.update(status="failed", failure="missing_tree")
        with self.service.changed, self.store.transaction() as db:
            current = attempts(db)[attempt["attempt_id"]]
            if current["state"] in {"recorded", "captured"}:
                return
            node = self.store.nodes(db)[attempt["node_id"]]
            if result.get("capture_error") and result.get("failure") != "missing_tree":
                self.hold(db, node, "result_capture_failed", result["capture_error"])
            current.update(state="captured", result=result)
            save_attempt(db, current)
            self.store.transition(db, "run_finished", node["id"], {"run_id": run.id, "status": result["status"], **({"failure": result["failure"]} if result.get("failure") else {})})
            self.service.changed.notify_all()
        self.integrate(current)

    def integrate(self, attempt):
        with effects.serialized(self.store):
            self._integrate(attempt)

    def _integrate(self, attempt):
        # Snapshot, compute, journal, apply, checkpoint. Every Git call runs
        # after its store transaction has closed, including retention refs.
        while True:
            with self.store.transaction(write=False) as db:
                journal = attempts(db)
                current = journal[attempt["attempt_id"]]
                nodes = self.store.nodes(db)
            if current["state"] != "captured":
                return
            node = nodes[current["node_id"]]
            result = current["result"]
            parent = nodes.get(node.get("parent"))
            reviewer = parent and parent["kind"] == "loop" and parent["loop"]["verdict_child"] == node["id"]
            integration = current.get("integration")
            if result["status"] == "done" and result.get("commit") and not result.get("capture_error") and not reviewer:
                results = Results(self.paths, self.service.configuration())
                top = nodes[current["branch_node"]]
                try:
                    actual = results.tip(top["branch"])
                except effects.FAILURES as exc:
                    if self.integration_failed(attempt, nodes, current, node, exc):
                        continue
                    return
                pending = self.pending_integration(journal, current, top, actual)
                if pending:
                    self.integrate(pending)
                    continue
                if not integration or top["branch_tip"] != integration["before"]:
                    try:
                        generation, before = results.integrate(node, nodes, current)
                    except effects.FAILURES as exc:
                        with self.service.changed, self.store.transaction() as db:
                            if self.store.nodes(db) != nodes or attempts(db)[attempt["attempt_id"]] != current:
                                continue
                            self.hold(db, node, "integration_conflict", str(exc))
                            current["state"] = "recorded"
                            save_attempt(db, current)
                        return
                    integration = {"generation": generation, "before": before}
                    with self.service.changed, self.store.transaction() as db:
                        if self.store.nodes(db) != nodes or attempts(db)[attempt["attempt_id"]] != current:
                            continue
                        current["integration"] = integration
                        save_attempt(db, current)
                    continue
                generation = integration["generation"]
                if actual not in {integration["before"], generation["commit"]}:
                    with self.service.changed, self.store.transaction() as db:
                        if self.store.nodes(db) != nodes or attempts(db)[attempt["attempt_id"]] != current:
                            continue
                        self.hold(db, node, "integration_conflict", "node branch differs from its host-recorded tip")
                        current["state"] = "recorded"
                        save_attempt(db, current)
                    return
                if "refs" not in integration:
                    refs = [[f"refs/heads/node-generations/{node['id']}/{generation['seq']}", generation["commit"]]]
                    if parent and parent["kind"] == "loop":
                        refs.append([f"refs/heads/node-generations/{parent['id']}/{len(parent['generations']) + 1}", generation["commit"]])
                    with self.store.transaction() as db:
                        if self.store.nodes(db) != nodes or attempts(db)[attempt["attempt_id"]] != current:
                            continue
                        current["integration"]["refs"] = refs
                        save_attempt(db, current)
                    continue
                try:
                    results.move(top["branch"], generation["commit"], integration["before"])
                except effects.FAILURES as exc:
                    if self.integration_failed(attempt, nodes, current, node, exc):
                        continue
                    return
                try:
                    for ref, target in integration["refs"]:
                        results.move(ref, target, "")
                except effects.FAILURES as exc:
                    # Leave the node branch where the host record says it is.
                    try:
                        if results.tip(top["branch"]) == generation["commit"] != integration["before"]:
                            results.git("update-ref", "--no-deref", top["branch"],
                                        integration["before"], generation["commit"], check=True)
                    except effects.FAILURES:
                        pass
                    if self.integration_failed(attempt, nodes, current, node, exc):
                        continue
                    return
            with self.service.changed, self.store.transaction() as db:
                current = attempts(db)[attempt["attempt_id"]]
                if current["state"] != "captured":
                    return
                nodes = self.store.nodes(db)
                node = nodes[current["node_id"]]
                integration = current.get("integration")
                if integration:
                    generation = integration["generation"]
                    top = nodes[current["branch_node"]]
                    top["branch_tip"] = generation["commit"]
                    node["generations"].append(generation)
                    parent = nodes.get(node["parent"])
                    if parent and parent["kind"] == "loop":
                        parent_seq = len(parent["generations"]) + 1
                        parent["generations"].append({**generation, "seq": parent_seq})
                        self.store.save_node(db, parent)
                    self.store.save_node(db, top)
                    entry = next(r for r in node["runs"] if r["attempt_id"] == current["attempt_id"])
                    entry["generation"] = generation["seq"]
                    self.store.transition(db, "integrated", node["id"], generation)
                elif reviewer:
                    self.store.transition(db, "reviewer_commits_ignored", parent["id"], {"run_id": current["run_id"]})
                # Legacy launch journals have no immutable input identity. Keep
                # their abandoned state after missing-tree recovery; modern runs
                # retain their captured result as a recorded attempt.
                current["state"] = ("abandoned" if current.get("completion_proven") == "missing_tree"
                                    and "input_commit" not in current else "recorded")
                save_attempt(db, current)
                if current.get("alias_id"):
                    bound = sessions.aliases(db)[current["alias_id"]]
                    if result.get("session_id"):
                        bound["session_id"] = result["session_id"]
                    if result.get("commit"):
                        bound["commit"] = result.get("checkout_commit", result["commit"])
                    bound["last_run"] = current["run_id"]
                    sessions.save_alias(db, bound)
                if node["state"] not in {"cancelled", "held"}:
                    loop = nodes.get(node.get("parent"))
                    while loop and loop["kind"] != "loop":
                        loop = nodes.get(loop["parent"])
                    if result["status"] != "done" and loop:
                        loop.pop("pending_verdict", None)
                        self.store.save_node(db, loop)
                    if (result["status"] != "done" and loop
                            and loop["state"] not in {"held", "cancelled", "done"}
                            and node.get("crash_retry_round") != loop["loop"]["rounds_rejected"]):
                        node.update(state="open", outcome=None, revision=node["revision"] + 1,
                                    crash_retry_round=loop["loop"]["rounds_rejected"])
                        self.store.transition(db, "run_retry", node["id"], {"attempt_id": current["attempt_id"]})
                    else:
                        verdict = loop.get("pending_verdict") if reviewer and loop else None
                        outcome = "completed" if result["status"] == "done" else "failed"
                        if (result["status"] == "done" and verdict
                                and verdict["attempt_id"] == current["attempt_id"]):
                            round_generations = loop["generations"][loop.get("round_generation_start", 0):verdict["generation_seq"]]
                            identities = {(g["run_id"], g["commit"]) for g in round_generations}
                            for owner in nodes.values():
                                for generation in owner["generations"]:
                                    if (generation["run_id"], generation["commit"]) in identities:
                                        generation["verdict"] = verdict["verdict"]
                                        self.store.save_node(db, owner)
                            verdict["settled"] = True
                            self.store.save_node(db, loop)
                            self.store.transition(db, "verdict_settled", loop["id"], verdict)
                            outcome = verdict["verdict"]
                        node.update(state="done", outcome=outcome,
                                    revision=node["revision"] + 1)
                        self.store.transition(db, "done", node["id"], {"outcome": node["outcome"]})
                        if result["status"] != "done" and loop and loop["state"] not in {"held", "cancelled", "done"}:
                            self.hold(db, loop, "run_failed", node["id"])
                tree_run = self.runner.tree.get(current["run_id"])
                if current.get("alias_id") and tree_run and tree_run.reason == "session_lost":
                    self.hold(db, node, "session_lost", current["run_id"])
                self.store.save_node(db, node)
                self.service.changed.notify_all()
            return

    def integration_failed(self, attempt, nodes, current, node, exc):
        """Hold the node on a ref that is not as journalled; True to re-read."""
        with self.service.changed, self.store.transaction() as db:
            if self.store.nodes(db) != nodes or attempts(db)[attempt["attempt_id"]] != current:
                return True
            self.hold(db, node, "integration_conflict", str(exc) or type(exc).__name__)
            current["state"] = "recorded"
            save_attempt(db, current)
        return False

    def pending_integration(self, journal, current, top, actual):
        own = current.get("integration", {})
        if (actual == top["branch_tip"] or (own.get("before") == top["branch_tip"]
                and own.get("generation", {}).get("commit") == actual)):
            return None
        return next((other for other in journal.values()
                     if other["attempt_id"] != current["attempt_id"] and other["state"] == "captured"
                     and other.get("branch_node") == current["branch_node"]
                     and other.get("integration", {}).get("before") == top["branch_tip"]
                     and other.get("integration", {}).get("generation", {}).get("commit") == actual), None)

    def composites(self):
        with self.service.changed, self.store.transaction() as db:
            nodes = self.store.nodes(db)
            for node in sorted((n for n in nodes.values() if n["kind"] != "simple"),
                               key=lambda n: len(model.subtree(nodes, n["id"]))):
                if node["state"] in {"done", "cancelled", "suspended"} or node.get("completion_pending") or node.get("disposal_pending"):
                    continue
                children = [nodes[id] for id in node["children"]]
                held = next((c for c in children if c["state"] == "held"), None)
                if node["state"] == "held":
                    if (node["hold"] or {}).get("reason") != "child_held" or held:
                        continue
                    node.update(state="running", hold=None, revision=node["revision"] + 1)
                    self.store.save_node(db, node)
                if held:
                    self.hold(db, node, "child_held", held["id"])
                    continue
                if node["kind"] == "sequence" and any(c["state"] == "done" and not model.succeeded(c) for c in children):
                    self.complete(db, node, "failed")
                    continue
                if children and all(c["state"] == "done" for c in children):
                    if node["kind"] == "loop":
                        verdict = node.get("pending_verdict")
                        if not verdict or not verdict.get("settled"):
                            self.hold(db, node, "unresolved_round")
                            continue
                        if verdict["verdict"] == "rejected":
                            node["loop"]["rounds_rejected"] += 1
                            node["findings"] = verdict["findings"]
                            self.store.transition(db, "round_rejected", node["id"], verdict)
                            if node["loop"]["rounds_rejected"] >= node["loop"]["max_rounds"]:
                                self.hold(db, node, "loop_max")
                            else:
                                self.reset_round(db, node, nodes)
                            continue
                        self.store.transition(db, "loop_exited", node["id"], verdict)
                    self.complete(db, node, "approved" if all(model.succeeded(c) for c in children) else "failed")
                elif children and node["state"] == "open":
                    node.update(state="running", revision=node["revision"] + 1)
                    self.store.save_node(db, node)
            self.service.changed.notify_all()
        if effects.finish_completions(self):
            self.composites()

    def complete(self, db, node, outcome):
        if node.get("completion_pending"):
            return
        if node["kind"] in {"sequence", "group"} and outcome == "approved":
            nodes = self.store.nodes(db)
            top = top_node(node, nodes)
            commit = top.get("branch_tip")
            produced = [g for id in model.subtree(nodes, node["id"])
                        for g in nodes[id]["generations"] if g["commit"] == commit]
            if commit and produced and (not node["generations"] or node["generations"][-1]["commit"] != commit):
                generation = {"seq": len(node["generations"]) + 1, "commit": commit,
                              "run_id": produced[-1]["run_id"], "verdict": "approved"}
                node["completion_pending"] = {"generation": generation, "outcome": outcome,
                    "ref": f"refs/heads/node-generations/{node['id']}/{generation['seq']}"}
                self.store.save_node(db, node)
                return
        node.update(state="done", outcome=outcome, hold=None, revision=node["revision"] + 1)
        self.store.save_node(db, node)
        self.store.transition(db, "done", node["id"], {"outcome": outcome})

    def reset_round(self, db, node, nodes, verdict_only=False):
        findings = node.get("findings", [])
        children = [node["loop"]["verdict_child"]] if verdict_only else node["children"]
        for id in children:
            for descendant in model.subtree(nodes, id):
                model.reset(nodes[descendant])
                self.store.save_node(db, nodes[descendant])
        if not verdict_only and findings:
            nodes[node["children"][0]]["findings"] = findings
            self.store.save_node(db, nodes[node["children"][0]])
        node.update(state="running", outcome=None, hold=None, revision=node["revision"] + 1)
        node.pop("pending_verdict", None)
        node.pop("findings", None)
        if not verdict_only:
            node["round_generation_start"] = len(node["generations"])
        self.store.save_node(db, node)

    async def cancel(self, ids):
        results = {}
        with self.store.transaction(write=False) as db:
            journal = attempts(db)
        for attempt in journal.values():
            if attempt["node_id"] in ids and attempt["state"] in {"claimed", "launched"}:
                result = await self.runner.stop(attempt["run_id"])
                results[attempt["node_id"]] = result.get("predecessor_death_confirmed", False)
        return results

    def request_cancel(self, ids, db):
        """Record cancellation durably; return what is decided now and what needs a stop.

        Stopping takes seconds, so it never runs inside this write
        transaction (stop_runs, after commit). The worker refuses to launch
        a claim carrying this request and stops a handle that arrives later.
        """
        decided, stops = {}, {}
        for attempt in attempts(db).values():
            if attempt["node_id"] in ids and attempt["state"] in {"claimed", "launched"}:
                attempt["cancel_requested"] = True
                save_attempt(db, attempt)
                if attempt.get("launch_in_progress"):
                    # Do not confirm absence while the independent worker
                    # can still return a freshly spawned handle. It will
                    # stop that handle after seeing this durable request.
                    decided[attempt["node_id"]] = False
                elif (attempt["state"] == "claimed" and self.runner.tree.get(attempt["run_id"]) is None
                        and not self.paths.run_dir(attempt["run_id"]).exists()):
                    decided[attempt["node_id"]] = True
                else:
                    stops[attempt["node_id"]] = attempt["run_id"]
        return decided, stops

    def stop_runs(self, stops):
        """Stop runs outside any store transaction; True only for confirmed death."""
        async def stop_all():
            stopper = Runner(self.paths, self.service.configuration())
            results = {}
            for node_id, run_id in stops.items():
                try:
                    result = await stopper.stop(run_id)
                except Exception:
                    logging.getLogger(__name__).exception("cancel could not stop %s", run_id)
                    result = {}
                results[node_id] = result.get("predecessor_death_confirmed", False)
            return results
        return asyncio.run(stop_all())

    def resume_admission(self, run_id, db=None):
        run = self.runner.tree.get(run_id)
        if run is None:
            return {"error": "not_found"}
        if db is not None:
            nodes, journal = self.store.nodes(db), attempts(db)
        else:
            with self.store.transaction(write=False) as connection:
                nodes, journal = self.store.nodes(connection), attempts(connection)
        if not self.service.configuration().project["scheduler"]["enabled"]:
            return {"error": "scheduler_disabled",
                    "pending_nodes": sum(n["state"] not in {"done", "cancelled"} for n in nodes.values())}
        owner = next((a for a in journal.values() if a["run_id"] == run_id), None)
        node_id = owner["node_id"] if owner else None
        if node_id:
            state = nodes[node_id]["state"]
            if state == "suspended":
                return {"error": "node_suspended"}
            if state == "held":
                return {"blocked": [{"code": "held", "detail": nodes[node_id]["hold"]}]}
        try:
            spec, provider = self.runner._spec_of(run)
            data = self.runner.tree.read()
            maximum = int(self.runner.config.limits.get("max_concurrent", 4))
            if self.runner._occupants(data["nodes"], exclude=run_id) >= maximum:
                return admission_block("max_concurrent: all tree slots are held")
            full = self.runner._pc_admit(data, run.provider, exclude=run_id)
            if full:
                return admission_block(full)
            cap = self.runner._cap_refusal(run.provider, run.model)
            if cap:
                return admission_block(cap["reason"], cap.get("until"))
            if not provider.enabled:
                return admission_block("provider is disabled")
            if node_id:
                node = nodes[node_id]
                if node["state"] != "running":
                    return {"error": "invalid", "message": f"node is {node['state']}; it cannot be resumed"}
                blockers = self.lock_blockers(node, nodes, journal)
                if blockers:
                    return {"blocked": blockers}
            if db is not None and node_id:
                node = self.store.nodes(db)[node_id]
                journal = attempts(db)
                attempt = next(a for a in journal.values() if a["run_id"] == run_id)
                attempt.update(state="launched", resume_pending=now() + 60,
                               previous_turn=run.turn_started_at)
                save_attempt(db, attempt)
                node.update(state="running", outcome=None, revision=node["revision"] + 1)
                self.store.save_node(db, node)
            return {"admitted": True}
        except (RuntimeError, ValueError, KeyError) as exc:
            return admission_block(exc)

    def agent_admission(self, args, principal):
        async def check():
            runner = Runner(self.paths, self.service.configuration())
            caller = None if principal["root"] else principal["subject"]
            parent = runner.tree.get(caller) if caller else None
            context = LaunchContext(caller=caller, run_parent=caller,
                                    depth=parent.depth + 1 if parent else 1,
                                    node_id="", attempt_id="", admission_only=True)
            return await runner.start(args.get("agent"), args.get("task", "admission"),
                                      model=args.get("model"), launch_context=context)
        return asyncio.run(check())
