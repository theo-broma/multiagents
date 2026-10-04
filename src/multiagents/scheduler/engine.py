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
                             "activation_id": attempt["activation_id"]})
    if node["state"] == "open":
        node.update(state="running", ready_since=None, revision=node["revision"] + 1)
        node.pop("starvation_notified", None)
    store.save_node(db, node)
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
            if cur is not node and cur["state"] in {"held", "cancelled", "suspended"}:
                return [{"code": "ancestor", "detail": cur["id"]}]
            for ref in cur["depends_on"]:
                other = nodes.get(ref["node"])
                if other is None:
                    return [{"code": "dependency", "detail": ref["node"]}]
                allowed = ({"completed", "approved"} if ref.get("require", "success") == "success"
                           else {"approved"} if ref["require"] == "approved" else None)
                if other["state"] != "done" or allowed and other["outcome"] not in allowed:
                    return [{"code": "dependency", "detail": ref["node"]}]
            for ref in cur["inputs"]:
                if not nodes.get(ref["node"], {}).get("generations"):
                    return [{"code": "input", "detail": ref["node"]}]
            parent = nodes.get(cur["parent"])
            if parent and parent["kind"] in {"sequence", "loop"}:
                index = parent["children"].index(cur["id"])
                if index and nodes[parent["children"][index-1]]["state"] != "done":
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

    def lock_blockers(self, node, nodes, journal):
        wanted = self.lock_set(node, nodes)
        held = set()
        for attempt in journal.values():
            if attempt["state"] not in {"claimed", "launched"} or attempt["node_id"] == node["id"]:
                continue
            held.update(attempt.get("locks", []))
        overlap = sorted(wanted & held)
        return [{"code": "lock", "detail": overlap}] if overlap else []

    def view(self, node, nodes, journal):
        structural = self.structural(node, nodes)
        ready = node["kind"] == "simple" and node["state"] == "open" and not structural
        active = next((a["run_id"] for a in journal.values()
                       if a["node_id"] == node["id"] and a["state"] in {"claimed", "launched"}), None)
        blocked = structural
        if ready:
            blocked = self.lock_blockers(node, nodes, journal) or self.reasons.get(node["id"], [])
        if node["state"] in {"running", "done", "cancelled", "suspended"}:
            blocked = []
        return {**copy.deepcopy(node), "ready": ready, "ready_since": node.get("ready_since"),
                "eligible": ready and not blocked and not active, "blocked": copy.deepcopy(blocked),
                "eligible_since": node.get("ready_since") if ready and not blocked else None,
                "active_run": active if node["state"] not in {"done", "cancelled"} else None}

    def status(self, nodes, journal):
        return {"last_tick": self.last_tick,
                "held": [{"node_id": n["id"], "hold": n["hold"]} for n in nodes.values() if n["state"] == "held"],
                "locks": [{"name": name, "holder_run": a["run_id"], "holder_node": a["node_id"]}
                          for a in journal.values() if a["state"] in {"claimed", "launched"}
                          for name in a.get("locks", [])], "aliases": [], "windows": []}

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
        await self.runner.shutdown(detach=True, preserve_status=True)

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
            result = await self.runner.start(node["agent"], node["task"],
                                             model=node["pins"].get("model"),
                                             launch_context=self.context(node, probe=True),
                                             **node.get("launch", {}))
            with self.service.changed:
                self.reasons[node["id"]] = result.get("blocked", [])
            if not result.get("admitted") or self.stopped.is_set():
                continue
            with self.service.changed, self.store.transaction() as db:
                if self.service.configuration() is not config:
                    continue
                current_nodes = self.store.nodes(db)
                current = current_nodes[node["id"]]
                if current["state"] != "open" or current["revision"] != node["revision"]:
                    continue
                if self.lock_blockers(current, current_nodes, attempts(db)):
                    continue
                activation = current.get("activation_id") or uuid.uuid4().hex
                current["activation_id"] = activation
                attempt = {"attempt_id": uuid.uuid4().hex, "activation_id": activation,
                           "node_id": node["id"], "run_id": "ag-" + uuid.uuid4().hex[:6],
                           "state": "claimed", "locks": sorted(self.lock_set(current, current_nodes)),
                           "retry_count": current.get("launch_tries", 0), "at": now()}
                current["launch_tries"] = attempt["retry_count"] + 1
                self.store.save_node(db, current)
                save_attempt(db, attempt)
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
                journal = attempts(db)
        self.store.mirror()

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
        with self.store.transaction(write=False) as db:
            journal = attempts(db)
        for attempt in journal.values():
            if attempt["state"] not in {"claimed", "launched"}:
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
                                    self.recovered(db, current, node)
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
        self.children = [p for p in self.children if p.poll() is None]

    def recovered(self, db, attempt, node):
        # Death is proved: lift the hold this recovery placed (and only that
        # one, at its revision) and take the ordinary path. A claim that never
        # launched leaves the node eligible again (NC-R17); a launched run
        # that died without a tree entry ends the node as a failed run.
        hold = attempt.get("recovery_hold")
        state = node["state"]
        if hold and state == "held" and node["revision"] == hold["revision"]:
            state = hold["state"]
        elif state in {"held", "cancelled", "done", "suspended"}:
            return
        launched = any(r["attempt_id"] == attempt["attempt_id"] for r in node.get("runs", []))
        if state == "running" and launched:
            node.update(state="done", outcome="failed", hold=None, revision=node["revision"] + 1)
            self.store.transition(db, "run_finished", node["id"],
                                  {"run_id": attempt["run_id"], "failure": "missing_tree"})
            self.store.transition(db, "done", node["id"], {"outcome": "failed"})
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

    def finished(self, attempt, run):
        with self.service.changed, self.store.transaction() as db:
            current = attempts(db)[attempt["attempt_id"]]
            if current["state"] == "recorded":
                return
            current.update(state="recorded", result={"status": run.status, "session_id": run.session_id,
                                                     "branch": run.branch, "run_dir": str(self.paths.run_dir(run.id))})
            save_attempt(db, current)
            node = self.store.nodes(db)[attempt["node_id"]]
            self.store.transition(db, "run_finished", node["id"], {"run_id": run.id})
            if node["state"] not in {"cancelled", "held"}:
                node.update(state="done", outcome="completed" if run.status == "done" else "failed",
                            revision=node["revision"] + 1)
                self.store.transition(db, "done", node["id"], {"outcome": node["outcome"]})
            self.store.save_node(db, node)
            db.execute("UPDATE capabilities SET revoked=1 WHERE subject=?", (run.id,))
            self.service.changed.notify_all()

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
