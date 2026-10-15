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
from ..renames import shipped as shipped_renames
from ..runner import LaunchContext, Runner, VERDICT, admission_block, _run_read, _control_text
from .. import failures as tf
from ..tree import now, new_id
from ..tree import ACTIVE, PAUSED
from . import model
from .store import encode, records, select_records, matching_records
from .results import Results, RecoveryConflict, input_generation, recording_loops, top_node
from . import sessions, effects, windows
from .. import notify_sender
from .. import gitops


def epoch(value):
    if isinstance(value, (int, float)):
        return value
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


# The only two things a verdict can say, whoever said them (VR-R1).
VERDICTS = {"approved", "rejected"}


def attempts(db):
    return records(db, "attempts", lazy=True)


def attempt_records(db):
    return records(db, "attempts", lazy=True)


def save_attempt(db, record):
    db.execute("INSERT OR REPLACE INTO attempts VALUES (?, ?)", (record["attempt_id"], encode(record)))


def consume_recovery(store, db, attempt):
    """RR-R3: called only after the host has recorded an actual process launch."""
    if attempt.get("cleanup"):
        return
    node = store.node_records(db)[attempt["node_id"]]
    recovery = node.get("recovery")
    source = attempt.get("recovery_source")
    if (recovery and recovery.get("pending") and source
            and all(recovery[k] == source[k] for k in ("attempt", "commit"))):
        recovery.update(pending=False, consumed_by={"attempt": attempt["attempt_id"], "run_id": attempt["run_id"]})
        node.setdefault("recovery_history", []).append(copy.deepcopy(recovery))
        store.save_node(db, node)
        attempt["recovery_consumed"] = True
        save_attempt(db, attempt)


def confirm_recovery_launch(store, paths, attempt):
    """Replay actual launch evidence after a supervisor or setup failure."""
    if not attempt.get("recovery_source") or attempt.get("recovery_consumed") or attempt.get("cleanup"):
        return False
    kind = (attempt.get("launch_evidence") or {}).get("executor", {}).get("kind")
    if kind not in {"local", "docker"}:
        return False
    name = "container.pid" if kind == "docker" else "agent.pid"
    try:
        value = _run_read(paths.run_dir(attempt["run_id"]), name, 4096).split()
    except OSError:
        return False
    if not value or not value[0].isdigit() or int(value[0]) <= 0:
        return False
    with store.transaction() as db:
        consume_recovery(store, db, attempts(db)[attempt["attempt_id"]])
    return True


def record_launch(store, db, attempt, run):
    current = attempt_records(db)[attempt["attempt_id"]]
    if current["state"] != "claimed":
        return False
    current["state"] = "launched"
    save_attempt(db, current)
    if current.get("cleanup"):
        return True
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
    top = nodes.get(current.get("branch_node"))
    if top and "branch" not in top and current.get("prepared_branch"):
        # Launches outside tick still checkpoint the prepared host intent.
        top.update(branch=current["prepared_branch"], branch_tip=current["prepared_tip"])
        store.save_node(db, top)
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
    STOP_TIMEOUT = 10
    NOTIFIER_STOP_TIMEOUT = 2

    def __init__(self, service, clock_file=None):
        self.service = service
        self.store = service.store
        self.paths = service.paths
        self.clock_file = Path(clock_file) if clock_file else None
        self.runner = Runner(self.paths, service.configuration())
        self.runner._failure_clock = self.instant
        self.runner._scheduler_supervisor = True
        self.reasons = {}
        self.last_tick = None
        # Read from the configuration at each tick, so status() can name the
        # starving nodes without reading files inside a store transaction.
        self.starvation_after = None
        self.window_instant = self.instant()
        # AN: held_idle ages are read on the scheduler's clock only. Every
        # hold Engine.hold writes records (window_instant, log seq) as it
        # is written, so a hold laid down between two checks is aged from
        # when it happened, not from either neighbouring check, and no
        # real-time transition stamp is ever mapped onto the scheduler
        # clock. Holds the engine did not write (RPC/suspension holds,
        # run events racing a hold, this check's own anomaly emission, or
        # a record dropped by a restart) fall back to the previous check,
        # or to first observation when there is none: never early by more
        # than the stored episode allows. The per-episode times persist in
        # anomaly_seen meta keys, so a restart resumes from the stored
        # scheduler-clock time.
        self.anomaly_last_hold = {}
        self.anomaly_tick_start = time.time()
        self.last_anomaly_check = None
        # Nodes the admission probe refused this tick, with the scheduler
        # clock of the first consecutive refusal and the refusal itself.
        self.admission_refusals = {}
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.run, name="node-evaluation", daemon=True)
        self.stopped = threading.Event()
        # NT-R6: sending happens on its own thread, outside every tick and
        # every store transaction, so a slow, hanging or failing ntfy server
        # never delays a tick and never changes a node, run or transition.
        self.notifier = notify_sender.Sender(service, self.store, self.runner.tree,
                                             self.paths.root.name, self.instant)
        self.children = []
        # A node whose admission raised is retried after the others, so one
        # failing evaluation cannot starve every node ordered behind it.
        self.evaluating = None
        self.evaluation_failures = {}
# VR-R5: the one sweep that settles the rounds an older engine held
        # `unresolved_round` although their reviewer had answered in writing.
        # Once per engine, at the first tick: a settled round is no longer held,
        # so a restart finds nothing to do and nothing is settled twice.
        self.settle_held_once = True
        self.migrate_provider_names()
        self.migrate()
        with self.store.transaction(write=False) as db:
            window_nodes = self.store.node_records(db)
        windows.prepare(self.runner.config.project["scheduler"].get("timezone", "Europe/Paris"), window_nodes)
        with self.store.transaction() as db:
            self.store.set_meta(db, "clock_file", str(self.clock_file) if self.clock_file else "")
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

    def migrate_provider_names(self):
        """OG-R3: the plan's live routing keys move to the route's own name.

        A node's `pins.provider` and a session binding's `provider`/`account`
        are read on every tick, so a plan written before the rename would keep
        launching on a name no provider answers to. Attempt and history records
        are left exactly as written: they are a record of what happened under
        the name that happened.

        Once per engine, before the queue is drained into nodes, and idempotent
        — after the first pass no old name is left. Driven by the renames the
        shipped providers declare, so no provider is named here. Pure sqlite
        and json: no git, no subprocess, no file I/O inside the transaction.
        """
        renames = shipped_renames()
        with self.store.transaction() as db:
            for record in self.store.node_records(db).values():
                pins = record.get("pins")
                if isinstance(pins, dict) and renames.is_alias(pins.get("provider")):
                    record["pins"] = {**pins,
                                      "provider": renames.canonical(pins["provider"])}
                    self.store.save_node(db, record)
            for binding_id, raw in list(db.execute("SELECT id, record FROM aliases")):
                binding = json.loads(raw)
                moved = {key: renames.canonical(value)
                         for key, value in binding.items()
                         if key in ("provider", "account") and renames.is_alias(value)}
                if not moved:
                    continue
                db.execute("UPDATE aliases SET record=? WHERE id=?",
                           (encode({**binding, **moved}), binding_id))

    def migrate(self):
        # Store first, queue second: a crash between them replays the entry's
        # durable migration key without duplicating its node.
        with self.runner.tree.transaction() as tree:
            entries = [e for e in tree["deferred"] if isinstance(e, dict) and e.get("id")]
            with self.store.transaction() as db:
                nodes = self.store.node_records(db)
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
                    if entry.get("status") == "refused":
                        # NT-R3: the mark of a hold is the seq of its `held`
                        # transition, this one included.
                        self.store.transition(db, "held", node["id"], node["hold"])
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
                             effort=node["pins"].get("effort", ""), admission_only=probe,
                             executor_kind=((attempt or {}).get("launch_evidence", {}).get("executor", {}).get("kind", "")
                                            or (node.get("retry_route") or {}).get("executor_kind", "")))

    def window(self, node, nodes, instant=None):
        return windows.effective(node, nodes, self.runner.config.project["scheduler"].get("timezone", "Europe/Paris"),
                                 self.window_instant if instant is None else instant)

    def window_blockers(self, node, nodes, instant=None):
        window = self.window(node, nodes, instant)
        if window["empty"]:
            return [{"code": "empty_window", "detail": node["id"]}]
        if not window["open"]:
            return [{"code": "window", "detail": window}]
        return []

    def structural(self, node, nodes):
        if node["state"] == "held":
            return [{"code": "held", "detail": node["hold"]}]
        if node["state"] != "open" or node["kind"] != "simple":
            return []
        if node.get("retry_pending"):
            # TF-R2: an automatic retry is pending — its backoff has not
            # elapsed, or the predecessor's death is not yet established.
            return [{"code": "retry_backoff", "detail": node["retry_pending"]}]
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
                if not input_generation(nodes.get(ref["node"]), ref, nodes, consumer=node):
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
        for attempt in select_records(journal, "claimed", "launched", "captured"):
            if attempt["state"] not in {"claimed", "launched", "captured"} or attempt["node_id"] == node["id"]:
                continue
            holders = attempt.get("lock_owners") or {name: attempt["node_id"] for name in attempt.get("locks", [])}
            overlap.update(name for name in wanted.keys() & holders.keys() if wanted[name] != holders[name])
        for composite in select_records(nodes, "open", "running", "suspended", "held"):
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

    def view(self, node, nodes, journal, db=None, instant=None):
        structural = self.structural(node, nodes)
        if node["state"] in {"open", "suspended"}:
            structural = self.window_blockers(node, nodes, instant) or structural
        ready = node["kind"] == "simple" and node["state"] == "open" and not structural
        active = next((a["run_id"] for a in select_records(journal, "claimed", "launched", "suspended")
                       if a["node_id"] == node["id"] and a["state"] in {"claimed", "launched", "suspended"}), None)
        blocked = structural
        if ready:
            blocked = self.lock_blockers(node, nodes, journal) or self.session_blockers(node, nodes, journal, db) or self.reasons.get(node["id"], [])
        if node["state"] == "suspended" and not blocked:
            blocked = self.lock_blockers(node, nodes, journal) or self.reasons.get(node["id"], [])
        if node["state"] in {"running", "done", "cancelled"}:
            blocked = []
        projected = copy.deepcopy(node)
        for index, row in enumerate(projected.get("runs") or []):
            if not isinstance(row, dict):
                row = projected["runs"][index] = {}
            attempt_id = row.get("attempt_id")
            attempt = journal.get(attempt_id) if isinstance(attempt_id, str) else None
            launch = attempt.get("launch_evidence") if isinstance(attempt, dict) else None
            evidence = launch.get("executor") if isinstance(launch, dict) else None
            if (not isinstance(evidence, dict)
                    or evidence.get("kind") not in ("local", "docker", "isolated")
                    or any(not isinstance(evidence.get(key, ""), str)
                           for key in ("backend", "backend_version"))):
                evidence = {"kind": "unknown"}
            row["executor"] = {key: evidence.get(key, "")
                               for key in ("kind", "backend", "backend_version")}
        return {**projected, "ready": ready, "ready_since": node.get("ready_since"),
                "eligible": ready and not blocked and not active, "blocked": copy.deepcopy(blocked),
                "eligible_since": node.get("ready_since") if ready and not blocked else None,
                "active_run": active if node["state"] not in {"done", "cancelled"} else None}

    def status(self, nodes, journal, db=None, at=None):
        counts = {"last_tick": self.last_tick,
                "held": [{"node_id": n["id"], "hold": n["hold"]} for n in select_records(nodes, "held") if n["state"] == "held"],
                "locks": [{"name": name, "holder_run": a["run_id"], "holder_node": a["node_id"]}
                          for a in select_records(journal, "claimed", "launched") if a["state"] in {"claimed", "launched"}
                          for name in a.get("locks", [])], "aliases": self.alias_status(db),
                "windows": [{"node_id": n["id"], **{k: v for k, v in self.window(n, nodes, at).items()
                             if k in {"open", "next_open", "next_close"}}} for n in select_records(nodes, "open", "running", "suspended", "held")
                            if n["state"] not in {"done", "cancelled"}],
                "starving": self.starving(nodes, at)}
        # NT-R6: the state of sending — pending count, last accepted message
        # and current failure — read from the store only, so it never stalls
        # behind a hanging send. The open connection is passed down: a second
        # one inside this read would deadlock against a commit-waiting writer.
        try:
            counts["notify"] = notify_sender.snapshot(self.store, db)
        except Exception:
            counts["notify"] = {"pending": 0, "last_accepted_at": None, "failure": None}
        return counts

    def starving(self, nodes, at):
        """NC-R53 as shown (NC-R43): open nodes ready for longer than the
        threshold, by the scheduler's own clock rather than the last tick.
        `at` is that clock, read by the caller before any store transaction."""
        if self.starvation_after is None or at is None:
            return []                   # before the first tick
        return [n["id"] for n in select_records(nodes, "open") if n["state"] == "open" and n.get("ready_since")
                and at - epoch(n["ready_since"]) >= self.starvation_after]

    def alias_status(self, db=None):
        if db is not None:
            return list(sessions.aliases(db).values())
        with self.store.transaction(write=False) as db:
            return list(sessions.aliases(db).values())

    def start(self):
        self.thread.start()
        self.notifier.start()

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
                if self.evaluating:
                    self.evaluation_failures[self.evaluating] = time.monotonic()
            self.evaluating = None
            policy = self.service.configuration().project["scheduler"]
            deadline = time.monotonic() + min(policy["tick_seconds"], policy.get("window_tolerance_seconds", 60))
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
            active = {run_id for (run_id,) in db.execute(
                "SELECT json_extract(record, '$.run_id') FROM attempts "
                "WHERE json_extract(record, '$.state') IN ('claimed', 'launched')")}
        if not active:
            return False
        runs = self.runner.tree.read()["nodes"]
        return any(id in runs and runs[id]["status"] not in ACTIVE | PAUSED | {"quota_paused"}
                   for id in active)

    def stop(self):
        self.stopped.set()
        self.notifier.stopped.set()
        self.thread.join(timeout=self.STOP_TIMEOUT)
        self.notifier.join(timeout=self.NOTIFIER_STOP_TIMEOUT)
        with self.service.changed, self.store.transaction() as db:
            for attempt in attempt_records(db).values():
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

    def admission_order(self, nodes, candidates):
        """Candidates in admission order; a node whose evaluation or order key
        raised is recorded as failing and goes after the others."""
        keyed = []
        eligible = (nodes.selected("open", "running", "suspended", "held")
                    if hasattr(nodes, "selected") else nodes.values())
        for node in eligible:
            id = node["id"]
            if id not in candidates:
                continue
            try:
                key = self.order(node, nodes)
            except Exception:
                logging.getLogger(__name__).exception("scheduler could not order node %s", id)
                self.evaluation_failures[id] = time.monotonic()
                key = (True, float("inf"), float("inf"), id)
            keyed.append(((self.evaluation_failures.get(id, 0), key), node))
        return [node for _, node in sorted(keyed, key=lambda pair: pair[0])]

    async def tick(self):
        self.anomaly_tick_start = time.time()
        config = self.service.configuration()
        if not config.project["scheduler"]["enabled"]:
            return
        if self.runner.config is not config:
            self.runner.reload(config)
        self.window_instant = self.instant()
        self.starvation_after = config.project["scheduler"]["starvation_after_seconds"]
        with self.store.transaction(write=False) as db:
            initial_nodes = self.store.node_records(db)
            candidates = initial_nodes.indexes["ids"]
            managed = attempt_records(db).run_ids
        # A terminal child can be reopened during this tick. Prepare its zone
        # too, from the cached names rather than walking the terminal records.
        windows.prepare(config.project["scheduler"].get("timezone", "Europe/Paris"),
                        [{"timezone": name} for name in initial_nodes.indexes["timezones"]])
        await self.runner.adopt(exclude=managed)
        await self.evaluate_windows()
        await self.reconcile()
        self.evaluate_rate_limits()
        # TF-R2: settle pending retries (liveness, backoff) before admission.
        await self.evaluate_retries()
        # TF-R3: recheck quota-exhausted accounts, release recovered routes.
        await self.evaluate_quota()
        self.last_tick = datetime.fromtimestamp(self.window_instant, timezone.utc).isoformat()
        self.composites()
        # Mirror recorded results before admission awaits launches, so a
        # client that sees a node done finds its transitions in the log.
        self.store.mirror()
        with self.store.transaction(write=False) as db:
            nodes, journal = self.store.node_records(db), attempt_records(db)
        # Deposits arriving while reconciliation awaits I/O belong to the
        # next tick, so every admission pass has a coherent candidate set.
        for remembered in (self.evaluation_failures, self.admission_refusals):
            for id in list(remembered):
                if id in candidates and id in nodes and nodes[id]["state"] in {"done", "cancelled"}:
                    remembered.pop(id, None)
        for node in self.admission_order(nodes, candidates):
            if self.evaluating:
                self.evaluation_failures.pop(self.evaluating, None)
            self.evaluating = node["id"]
            ready = (node["kind"] == "simple" and node["state"] == "open"
                     and not self.structural(node, nodes) and not self.window_blockers(node, nodes))
            if node["state"] == "open" and not ready:
                self.episode(node["id"], False)
            if not ready:
                self.admission_refusals.pop(node["id"], None)
                continue
            self.episode(node["id"], True)
            if any(a["node_id"] == node["id"] and a["state"] in {"claimed", "launched"} for a in select_records(journal, "claimed", "launched")):
                self.admission_refusals.pop(node["id"], None)
                continue
            failed_cleanup = next((a for a in matching_records(journal, "node_id", node["id"]) if a.get("cleanup")
                                   and a["node_id"] == node["id"]
                                   and a["activation_id"] == node.get("activation_id")
                                   and a["state"] in {"abandoned", "recorded"}
                                   and not a.get("cleanup_succeeded")), None)
            if failed_cleanup:
                with self.service.changed, self.store.transaction() as db:
                    current = self.store.node_records(db)[node["id"]]
                    if current["state"] == "open" and current["revision"] == node["revision"]:
                        self.hold(db, current, "dirty_worktree", failed_cleanup["cleanup_archive"])
                continue
            if self.lock_blockers(node, nodes, journal):
                self.admission_refusals.pop(node["id"], None)
                continue
            session_blocked = self.session_blockers(node, nodes, journal)
            if session_blocked and session_blocked[0]["code"] == "session_busy":
                self.reasons[node["id"]] = session_blocked
                self.admission_refusals.pop(node["id"], None)
                continue
            with self.store.transaction(write=False) as db:
                alias_key = sessions.alias_id(node, nodes)
                checked_binding = sessions.aliases(db).get(alias_key)
                checked_attempts = {a["attempt_id"]: a for a in
                                    matching_records(attempt_records(db), "alias_id", alias_key)} if alias_key else {}
            clean_failure = None
            try:
                if checked_binding:
                    sessions.check_clean(self.paths, checked_binding, self.runner.authority)
            except model.Refused as exc:
                clean_failure = (exc.result["error"], exc.result.get("detail", exc.result.get("problems", "")))
            except (gitops.GitError, OSError, ValueError) as exc:
                clean_failure = ("reseat_failed", str(exc))
            if clean_failure and clean_failure[0] == "dirty_worktree":
                cleanup = None
                task = sessions.cleanup_message(clean_failure[1])
                with self.service.changed, self.store.transaction() as db:
                    current_nodes = self.store.node_records(db)
                    current = current_nodes[node["id"]]
                    blockers = self.session_blockers(current, current_nodes, attempt_records(db), db)
                    if (current["state"] != "open" or current["revision"] != node["revision"]
                            or sessions.aliases(db).get(alias_key) != checked_binding
                            or (blockers and blockers[0]["code"] == "session_busy")):
                        continue
                    cleanup = sessions.request_cleanup(self.store, db, current, checked_binding,
                                                       self.runner, clean_failure[1], task)
                    if cleanup:
                        cleanup.update(locks=sorted(self.lock_set(current, current_nodes)),
                                       lock_owners=self.lock_owners(current, current_nodes))
                        save_attempt(db, cleanup)
                    else:
                        self.hold(db, current, *clean_failure)
                if cleanup:
                    self.store.mirror()
                    self.spawn(cleanup)
                with self.store.transaction(write=False) as db:
                    nodes, journal = self.store.node_records(db), attempt_records(db)
                continue
            if session_blocked:
                self.reasons[node["id"]] = session_blocked
                self.session_notice(node["id"], session_blocked[0])
                self.admission_refusals.pop(node["id"], None)
                continue
            binding = checked_binding
            probe = self.context(node, probe=True)
            if binding and binding.get("renew"):
                binding = None
            if binding:
                probe = replace(probe, provider=binding["provider"])
            try:
                result = await self.runner.start(node["agent"], node["task"],
                                                 model=binding["model"] if binding else node["pins"].get("model"),
                                                 launch_context=probe,
                                                 **node.get("launch", {}))
            except Exception as exc:
                # TF-R4: an exception while admitting or launching is a
                # classified `launch` failure — bounded by TF-R2's budget,
                # never a silent drop and never an unbounded loop.
                # Cancellation and shutdown are not failure classes.
                if isinstance(exc, asyncio.CancelledError) or self.stopped.is_set():
                    raise
                self.launch_failure(node, nodes, f"{type(exc).__name__}: {exc}")
                with self.store.transaction(write=False) as db:
                    nodes, journal = self.store.node_records(db), attempt_records(db)
                continue
            blocked = result.get("blocked", [])
            if binding and result.get("error"):
                blocked = [{"code": "session_unavailable", "detail": result.get("reason", result["error"])}]
                self.session_notice(node["id"], blocked[0])
            with self.service.changed:
                self.reasons[node["id"]] = blocked
            if not result.get("admitted") or self.stopped.is_set():
                # AN-R2: a ready node the probe refused. Only a refusal with
                # a reason counts; a transport error carries none. The clock
                # of the first consecutive refusal is what the anomaly ages.
                if not result.get("admitted") and blocked and not result.get("error"):
                    entry = self.admission_refusals.get(node["id"])
                    if entry is None:
                        self.admission_refusals[node["id"]] = {"since": self.window_instant,
                                                               "blocked": blocked}
                    else:
                        entry["blocked"] = blocked
                else:
                    self.admission_refusals.pop(node["id"], None)
                continue
            self.admission_refusals.pop(node["id"], None)
            with self.store.transaction(write=False) as db:
                preparation_nodes = self.store.node_records(db)
            prospective = preparation_nodes[node["id"]]
            if prospective["state"] != "open" or prospective["revision"] != node["revision"]:
                continue
            # RecordView shares cached history and copies only local edits;
            # prepare can mutate its records without changing the comparison
            # snapshot used to validate the claim below.
            prepared_nodes = copy.deepcopy(preparation_nodes)
            preparation_failure = None
            if not clean_failure:
                try:
                    with effects.serialized(self.store):
                        prepared = Results(self.paths, config).prepare(prepared_nodes[node["id"]], prepared_nodes)
                except gitops.GitError as exc:
                    preparation_failure = exc
            current_config = self.service.configuration()
            with self.service.changed, self.store.transaction() as db:
                if current_config is not config:
                    continue
                current_nodes = self.store.node_records(db)
                if current_nodes != preparation_nodes:
                    continue
                current = current_nodes[node["id"]]
                if current["state"] != "open" or current["revision"] != node["revision"]:
                    continue
                if sessions.alias_id(current, current_nodes) != alias_key:
                    continue
                current_journal = attempt_records(db)
                if alias_key and (sessions.aliases(db).get(alias_key) != checked_binding
                        or {a["attempt_id"]: a for a in matching_records(current_journal, "alias_id", alias_key)} != checked_attempts):
                    continue
                if self.structural(current, current_nodes) or self.window_blockers(current, current_nodes):
                    continue
                if self.lock_blockers(current, current_nodes, current_journal) or self.session_blockers(current, current_nodes, current_journal, db):
                    continue
                if clean_failure:
                    self.hold(db, current, *clean_failure)
                    continue
                if preparation_failure:
                    if isinstance(preparation_failure, RecoveryConflict):
                        self.hold(db, current, "recovery_conflict", {
                            "attempt": current["recovery"]["attempt"], "cause": str(preparation_failure)})
                    else:
                        self.hold(db, current, "input_conflict", str(preparation_failure))
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
                prepared["writes"] = spec.writes
                parent = current_nodes.get(current["parent"])
                if current.get("launch", {}).get("_review_commit"):
                    prepared["input_commit"] = current["launch"]["_review_commit"]
                if parent and parent["kind"] == "loop" and parent["loop"]["verdict_child"] == current["id"]:
                    if not parent["generations"]:
                        self.hold(db, current, "input_conflict", "the loop recorded no generation to review")
                        continue
                    reviewed = parent["generations"][-1]
                    prepared["review"] = {"node_id": parent["id"], "generation_seq": reviewed["seq"], "commit": reviewed["commit"]}
                    if not (current.get("recovery") or {}).get("pending"):
                        prepared["input_commit"] = reviewed["commit"]
                if current.get("findings"):
                    prepared["findings"] = current["findings"]
                activation = current.get("activation_id") or uuid.uuid4().hex
                current["activation_id"] = activation
                attempt = {"attempt_id": uuid.uuid4().hex, "activation_id": activation,
                           "node_id": node["id"], "run_id": new_id(self.store.paths, db),
                           "state": "claimed", "locks": sorted(self.lock_set(current, current_nodes)),
                           "lock_owners": self.lock_owners(current, current_nodes),
                           "retry_count": current.get("launch_tries", 0), "at": now(), **prepared}
                from ..executor.lifecycle import LaunchSpec, spec_digest
                selection = dict(result.get("executor_selection") or {
                    "kind": spec.executor or config.executor, "backend": "", "backend_version": "1"})
                launch_spec = LaunchSpec(owner=attempt["attempt_id"], lifetime="attempt",
                                         labels={"multiagents.project": self.paths.slug})
                selection.update(spec_version=1, spec_digest=spec_digest(launch_spec))
                attempt["launch_evidence"] = {"executor": selection}
                current["launch_tries"] = attempt["retry_count"] + 1
                # TF-R2: a retry whose route is the provider the failed
                # attempt ran on resumes its session; a route that lands
                # elsewhere launches fresh.
                route = current.pop("retry_route", None)
                if isinstance(route, dict):
                    if route.get("session"):
                        attempt["session_id"] = route["session"]
                    attempt["session_provider"] = route.get("provider", "")
                    if route.get("attempt"):
                        retry = {"source_attempt": route["attempt"],
                                 "attempt_id": attempt["attempt_id"],
                                 "source_run": route.get("from"),
                                 "run_id": attempt["run_id"], "at": self.instant()}
                        current.setdefault("retry_history", []).append(retry)
                        self.store.transition(db, "run_retried", current["id"], retry)
                for stale in ("retry_pending", "launch_failure"):
                    current.pop(stale, None)
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
                    claimed = attempt_records(db)[attempt["attempt_id"]]
                    if claimed["state"] == "claimed":
                        claimed.update(state="abandoned", ended_at=now())
                        save_attempt(db, claimed)
                        db.execute("UPDATE capabilities SET revoked=1 WHERE subject=?", (attempt["run_id"],))
                        self.hold(db, self.store.node_records(db)[attempt["node_id"]], "input_conflict",
                                  str(exc) or type(exc).__name__)
                with self.store.transaction(write=False) as db:
                    nodes, journal = self.store.node_records(db), attempt_records(db)
                continue
            self.spawn(attempt)
            # The next candidate observes the tree reservation made by Runner,
            # so a deposited urgent node cannot lose its place to worker latency.
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and not self.stopped.is_set():
                with self.store.transaction(write=False) as db:
                    state = attempt_records(db)[attempt["attempt_id"]]["state"]
                if state != "claimed" or self.runner.tree.get(attempt["run_id"]):
                    break
                await asyncio.sleep(0.05)
            with self.store.transaction(write=False) as db:
                nodes, journal = self.store.node_records(db), attempt_records(db)
        if self.evaluating:
            self.evaluation_failures.pop(self.evaluating, None)
        self.evaluating = None
        self.composites()
        self.store.mirror()
        # AN runs after VR's settle step (composites above): a round VR just
        # settled is no longer held, so there is nothing left to report.
        self.anomaly_check()
        # NT-R3: detect new scheduler events into the durable notification
        # outbox. The scan only enqueues; sending happens on the notifier
        # thread. It never raises out of the tick.
        try:
            notify_sender.scan(self.service, self.store, self.runner.tree,
                               self.paths.root.name, self.window_instant)
        except Exception:
            logging.getLogger(__name__).exception("scheduler notification scan failed")

    async def evaluate_windows(self):
        # Persist stop intent before a supervisor signals anything. Reconcile
        # cannot mistake the ensuing terminal tree status for a completion.
        with self.service.changed, self.store.transaction() as db:
            nodes, journal = self.store.node_records(db), attempt_records(db)
            for node in nodes.selected("open", "running", "suspended", "held"):
                window = self.window(node, nodes)
                if node["state"] not in {"done", "cancelled"}:
                    if window["empty"] and not node.get("empty_window_notified"):
                        node["empty_window_notified"] = True
                        self.store.save_node(db, node)
                        self.store.transition(db, "empty_window", node["id"])
                    elif not window["empty"] and node.pop("empty_window_notified", None):
                        self.store.save_node(db, node)
                if window["open"] or node["state"] != "running":
                    continue
                for attempt in select_records(journal, "launched"):
                    if (attempt["node_id"] == node["id"] and attempt["state"] == "launched"
                            and not attempt.get("capture_intent") and not attempt.get("cancel_requested")):
                        attempt.setdefault("window_stop", True)
                        save_attempt(db, attempt)
        with self.store.transaction(write=False) as db:
            nodes, journal = self.store.node_records(db), attempt_records(db)
        for attempt in journal.selected("suspended", "field:window_stop"):
            if attempt.get("window_stop"):
                self.spawn(attempt)
                continue
            if attempt["state"] != "suspended" or attempt.get("rate_limit_pending"):
                continue
            node = nodes[attempt["node_id"]]
            if node["state"] != "suspended" or self.window_blockers(node, nodes):
                continue
            admission = self.resume_admission(attempt["run_id"], window=True)
            self.reasons[node["id"]] = admission.get("blocked", [])
            if not admission.get("admitted"):
                if admission.get("error"):
                    self.reasons[node["id"]] = [{"code": "session_unavailable", "detail": admission["error"]}]
                continue
            run = self.runner.tree.get(attempt["run_id"])
            with self.service.changed, self.store.transaction() as db:
                current_nodes, current_journal = self.store.node_records(db), attempt_records(db)
                current = current_journal[attempt["attempt_id"]]
                current_node = current_nodes[node["id"]]
                if (current != attempt or current_node != node or current_node["state"] != "suspended"
                        or self.window_blockers(current_node, current_nodes)
                        or self.lock_blockers(current_node, current_nodes, current_journal)):
                    continue
                message = ("Resume your interrupted task.\n\n" + run.task
                           + f"\n\nworking directory: {run.worktree}\nnode: {node['id']}")
                if current.get("review"):
                    review = current["review"]
                    message += (f"\nreview: node_id={review['node_id']} generation_seq={review['generation_seq']}"
                                f" commit={review['commit']}")
                current.update(state="launched", launch_in_progress=True,
                               locks=sorted(self.lock_set(current_node, current_nodes)),
                               lock_owners=self.lock_owners(current_node, current_nodes),
                               window_resume={"previous_turn": run.turn_started_at,
                                              "previous_pid": (current.get("launch_evidence") or {}).get("pid"),
                                              "message": message})
                save_attempt(db, current)
            self.spawn(current)

    def evaluate_rate_limits(self):
        with self.store.transaction(write=False) as db:
            nodes, journal = self.store.node_records(db), attempt_records(db)
        for attempt in journal.selected("suspended"):
            node = nodes[attempt["node_id"]]
            pending = node.get("pending_resume")
            if (attempt["state"] != "suspended" or not attempt.get("rate_limit_pending")
                    or node["state"] != "running" or not pending or pending["at"] > now()
                    or self.window_blockers(node, nodes)):
                continue
            admission = self.resume_admission(attempt["run_id"])
            self.reasons[node["id"]] = admission.get("blocked", [])
            if not admission.get("admitted"):
                continue
            run = self.runner.tree.get(attempt["run_id"])
            if not run:
                continue
            with self.service.changed, self.store.transaction() as db:
                current_nodes, current_journal = self.store.node_records(db), attempt_records(db)
                current = current_journal[attempt["attempt_id"]]
                own = current_nodes[node["id"]]
                if (current != attempt or own != node or current.get("cancel_requested")
                        or self.window_blockers(own, current_nodes)
                        or self.lock_blockers(own, current_nodes, current_journal)):
                    continue
                message = ("Resume your task after the provider rate limit.\n\n" + run.task
                           + f"\n\nworking directory: {run.worktree}\nnode: {node['id']}")
                if current.get("review"):
                    review = current["review"]
                    message += (f"\nreview: node_id={review['node_id']} generation_seq={review['generation_seq']}"
                                f" commit={review['commit']}")
                for key in ("capture_intent", "result", "integration", "window_completion"):
                    current.pop(key, None)
                current.update(state="launched", launch_in_progress=True,
                               locks=sorted(self.lock_set(own, current_nodes)),
                               lock_owners=self.lock_owners(own, current_nodes),
                               rate_limit_resume={"previous_turn": run.turn_started_at,
                                                  "previous_pid": (current.get("launch_evidence") or {}).get("pid"),
                                                  "message": message})
                save_attempt(db, current)
            self.spawn(current)

    def session_notice(self, id, detail):
        with self.service.changed, self.store.transaction() as db:
            node = self.store.node_records(db)[id]
            if not node.get("session_unavailable_notified"):
                node["session_unavailable_notified"] = True
                self.store.save_node(db, node)
                self.store.transition(db, "session_unavailable", id, detail)

    def episode(self, id, ready):
        # Prepare file-backed inputs before taking the notification/store locks.
        starvation_after = self.service.configuration().project["scheduler"]["starvation_after_seconds"]
        instant = self.instant()
        with self.service.changed, self.store.transaction() as db:
            node = self.store.node_records(db)[id]
            if node["state"] != "open":
                return
            if ready:
                if not node.get("ready_since"):
                    node["ready_since"] = datetime.fromtimestamp(instant, timezone.utc).isoformat()
                age = instant - epoch(node["ready_since"])
                if age >= starvation_after and not node.get("starvation_notified"):
                    self.store.transition(db, "starving", id)
                    node["starvation_notified"] = True
            else:
                node.pop("ready_since", None)
                node.pop("starvation_notified", None)
            self.store.save_node(db, node)

    def observe(self, deposited):
        """NC-R53: a node deposited ready is ready from its deposit, as
        `get_node` already shows it, not from the next tick that sees it.

        Only the nodes the request just deposited are evaluated, and outside
        any write: the write itself is one batch proportional to the deposit."""
        if not deposited:
            return
        with self.store.transaction(write=False) as db:
            rows = list(db.execute("SELECT id, record FROM nodes"))
        nodes = {id: json.loads(record) for id, record in rows}
        ready = [id for id in deposited if (n := nodes.get(id)) and n["state"] == "open"
                 and n["kind"] == "simple" and not n.get("ready_since") and not self.structural(n, nodes)]
        if not ready:
            return
        since = datetime.fromtimestamp(self.instant(), timezone.utc).isoformat()
        with self.service.changed, self.store.transaction() as db:
            for id in ready:
                row = db.execute("SELECT record FROM nodes WHERE id=?", (id,)).fetchone()
                node = json.loads(row[0]) if row else None
                # A tick may have stamped or launched it since; the next tick
                # corrects a readiness that changed in between (episode).
                if node and node["state"] == "open" and not node.get("ready_since"):
                    node["ready_since"] = since
                    self.store.save_node(db, node)

    # ------------------------------------------------------------ anomalies
    # AN-R1..AN-R5 (context/specs/scheduler-anomalies.md): the scheduler
    # reports what is stuck instead of waiting silently. The check only
    # reports: it never changes a node, a run or a verdict.
    #
    # Lock scope: phase one reads the store read-only, takes one locked tree
    # read and reads run result files, all outside any store transaction.
    # Phase two appends `anomaly` transitions and bookkeeping keys in a
    # single short write transaction, then wakes the waiters. No git,
    # subprocess or file I/O runs inside the transaction or a runner lock.
    def anomaly_check(self):
        try:
            scheduler = self.service.configuration().project["scheduler"]
            interval = scheduler.get("anomaly_interval_seconds", 120)
            admission_after = scheduler.get("anomaly_admission_seconds", 600)
            held_after = scheduler.get("anomaly_held_seconds", 600)
        except Exception:
            logging.getLogger(__name__).exception("scheduler anomaly check failed")
            return
        at = self.window_instant
        if self.last_anomaly_check is not None and at - self.last_anomaly_check < interval:
            return
        try:
            candidates, revisions, aging, held_ids, pending_writes, pending_clears = \
                self._anomaly_candidates(at, admission_after, held_after)
        except Exception:
            logging.getLogger(__name__).exception("scheduler anomaly check failed")
            return
        self.last_anomaly_check = at
        try:
            self._anomaly_commit(candidates, revisions, aging, held_ids,
                                 pending_writes, pending_clears)
        except Exception:
            logging.getLogger(__name__).exception("scheduler anomaly check failed")
            return
        self.store.mirror()

    def _anomaly_text(self, run_id, tree_nodes):
        try:
            record = json.loads((self.paths.run_dir(run_id) / "result.json").read_text())
            if record.get("text"):
                return record["text"]
        except (OSError, ValueError):
            pass
        return (tree_nodes.get(run_id) or {}).get("summary") or ""

    def _anomaly_candidates(self, at, admission_after, held_after):
        with self.store.transaction(write=False) as db:
            nodes = self.store.node_records(db)
            latest = records(db, "notifications", lazy=True).indexes["latest"]
            seen = {key: value for key, value in
                    db.execute("SELECT key, value FROM meta WHERE key LIKE 'anomaly_seen:%'")}
            pending = {key for key, in
                       db.execute("SELECT key FROM meta WHERE key LIKE 'anomaly_pending:%'")}
        for nid in list(self.admission_refusals):
            if nid not in nodes:
                self.admission_refusals.pop(nid, None)
        # One locked tree read, outside any store transaction.
        tree_nodes = self.runner.tree.read()["nodes"]
        candidates, aging, held_ids = [], {}, set()
        pending_writes, pending_clears = set(), set()
        selected_ids = {n["id"] for n in nodes.selected("open", "running", "suspended", "held")}
        for run_id, raw in tree_nodes.items():
            if raw.get("status") == "stuck":
                selected_ids.update(nodes.indexes["last_runs"].get(run_id, ()))
        selected_ids.update(key[len("anomaly_pending:"):].rpartition(":")[0] for key in pending)
        selected_nodes = [nodes[id] for id in selected_ids if id in nodes]
        revisions = {node["id"]: node["revision"] for node in selected_nodes}
        for node in selected_nodes:
            nid = node["id"]
            runs = node.get("runs") or []
            if runs:
                run_id = runs[-1]["run_id"]
                raw = tree_nodes.get(run_id) or {}
                if raw.get("status") == "stuck":
                    candidates.append({"node_id": nid, "kind": "run_stuck",
                                       "reason": raw.get("reason") or f"run {run_id} is stuck",
                                       "run_id": run_id, "revision": node["revision"]})
            # verdict_unrecorded has no age threshold, so it needs
            # observation stability instead: the round must have been seen
            # unresolved with a contradictory text at the previous check.
            # A round VR just settled, or one composites just re-held while
            # a late verdict lands, is not reported yet.
            verdict_key = f"anomaly_pending:{nid}:verdict_unrecorded"
            if (node["kind"] == "loop" and node["state"] == "held"
                    and (node.get("hold") or {}).get("reason") == "unresolved_round"):
                child = nodes.get((node.get("loop") or {}).get("verdict_child"))
                child_runs = (child or {}).get("runs") or []
                contradictory = False
                run_id = child_runs[-1]["run_id"] if child_runs else None
                if run_id is not None:
                    verdicts = {match.group(1).lower() for match in
                                VERDICT.finditer(_control_text(self._anomaly_text(run_id, tree_nodes)))}
                    contradictory = "approved" in verdicts and "rejected" in verdicts
                if contradictory:
                    # A hold (re)written during this very tick — composites
                    # re-holding a round that was open when the tick began —
                    # is not observed yet. It may still be settling.
                    _, last_at = latest.get(nid, (-1, None))
                    try:
                        held_this_tick = last_at is not None and epoch(last_at) >= self.anomaly_tick_start
                    except (ValueError, OverflowError, OSError, TypeError):
                        held_this_tick = False
                    if not held_this_tick:
                        if verdict_key in pending:
                            candidates.append({
                                "node_id": nid, "kind": "verdict_unrecorded",
                                "reason": (f"loop {nid} round is held unresolved_round but verdict "
                                           f"child {child['id']}'s run {run_id} holds contradictory "
                                           "verdict lines (approved and rejected)"),
                                "run_id": run_id, "revision": node["revision"]})
                        else:
                            pending_writes.add(verdict_key)
                elif verdict_key in pending:
                    pending_clears.add(verdict_key)
            elif verdict_key in pending:
                pending_clears.add(verdict_key)
            if (node["kind"] == "simple" and node["state"] == "open"
                    and not self.structural(node, nodes)
                    and not self.window_blockers(node, nodes)):
                refusal = self.admission_refusals.get(nid)
                ready = node.get("ready_since")
                if refusal and refusal.get("blocked") and ready:
                    try:
                        since = max(epoch(ready), refusal["since"])
                    except (ValueError, OverflowError, OSError, TypeError):
                        since = None
                    if since is not None and at - since >= admission_after:
                        parts = [f"{block.get('code')}: {block.get('detail')}"
                                 for block in refusal["blocked"] if isinstance(block, dict)]
                        candidates.append({"node_id": nid, "kind": "admission_blocked",
                                           "reason": "; ".join(parts) or "admission refused",
                                           "run_id": None, "revision": node["revision"]})
            if node["state"] == "held":
                held_ids.add(nid)
                seq = latest.get(nid, (-1, None))[0]
                try:
                    stored = json.loads(seen["anomaly_seen:" + nid])
                except (KeyError, ValueError, TypeError):
                    stored = None
                # The write-time record is consumed here: it belongs to the
                # episode that starts now, never to a later one.
                wrote = self.anomaly_last_hold.pop(nid, None)
                previous = self.last_anomaly_check
                if stored is not None and seq <= stored["seq"]:
                    attributed = stored["at"]
                elif (wrote is not None and wrote[0] <= at
                        and seq == wrote[1]):
                    # The log ends with the hold this engine wrote: age the
                    # episode from when it happened on the scheduler clock,
                    # not from either neighbouring check.
                    attributed = wrote[0]
                elif previous is None:
                    # Never observed, and not written by this engine (an
                    # RPC/suspension hold, or a restart dropped the
                    # in-memory record): first observation. Never early,
                    # at most an interval late.
                    attributed = at
                else:
                    # A transition this engine did not write landed since
                    # the stored episode (a run event from an attempt that
                    # raced the hold, or this check's own anomaly): it
                    # happened since the previous check, so the episode
                    # restarts there. test_an_r3_a_changed_node_state pins
                    # this: the re-fire must survive the stray worker
                    # transitions, which carry no scheduler-clock time.
                    attributed = previous
                aging[nid] = (seq, attributed)
                if at - attributed >= held_after:
                    hold = node.get("hold") or {}
                    reason = (f"held as {hold.get('reason') or 'held'} with no transition "
                              f"for {at - attributed:.0f}s")
                    if hold.get("detail"):
                        reason += f": {hold['detail']}"
                    candidates.append({"node_id": nid, "kind": "held_idle",
                                       "reason": reason, "run_id": None,
                                       "revision": node["revision"]})
        # A write-time record for a node that is not held belongs to an
        # episode that already ended (releases write no transition, so the
        # seq cannot tell): drop it, or a later hold would inherit its age.
        for nid in list(self.anomaly_last_hold):
            if nid not in held_ids:
                del self.anomaly_last_hold[nid]
        return candidates, revisions, aging, held_ids, pending_writes, pending_clears

    def _anomaly_commit(self, candidates, revisions, aging, held_ids, pending_writes, pending_clears):
        with self.service.changed:
            with self.store.transaction() as db:
                nodes = self.store.node_records(db)
                fresh = [c for c in candidates
                         if nodes.get(c["node_id"], {}).get("revision") == revisions.get(c["node_id"])
                         and c["revision"] == revisions.get(c["node_id"])]
                current = {(c["node_id"], c["kind"]) for c in fresh}
                for c in fresh:
                    key = f"anomaly_emitted:{c['node_id']}:{c['kind']}"
                    if db.execute("SELECT 1 FROM meta WHERE key=?", (key,)).fetchone():
                        continue
                    detail = {"kind": c["kind"], "reason": c["reason"]}
                    if c.get("run_id"):
                        detail["run_id"] = c["run_id"]
                    self.store.transition(db, "anomaly", c["node_id"], detail)
                    self.store.set_meta(db, key, "1")
                for (key,) in db.execute("SELECT key FROM meta WHERE key LIKE 'anomaly_emitted:%'"):
                    nid, _, kind = key[len("anomaly_emitted:"):].rpartition(":")
                    if (nid, kind) not in current:
                        db.execute("DELETE FROM meta WHERE key=?", (key,))
                for key in pending_writes:
                    self.store.set_meta(db, key, "1")
                for key in pending_clears:
                    db.execute("DELETE FROM meta WHERE key=?", (key,))
                for (key,) in db.execute("SELECT key FROM meta WHERE key LIKE 'anomaly_pending:%'"):
                    nid = key[len("anomaly_pending:"):].rpartition(":")[0]
                    if nid not in nodes:
                        db.execute("DELETE FROM meta WHERE key=?", (key,))
                for nid, (seq, attributed) in aging.items():
                    if nid in nodes and nodes[nid]["revision"] == revisions.get(nid):
                        self.store.set_meta(db, "anomaly_seen:" + nid,
                                            encode({"seq": seq, "at": attributed}))
                for (key,) in db.execute("SELECT key FROM meta WHERE key LIKE 'anomaly_seen:%'"):
                    nid = key[len("anomaly_seen:"):]
                    if nid not in held_ids or nid not in nodes or nid not in aging:
                        db.execute("DELETE FROM meta WHERE key=?", (key,))
            self.service.changed.notify_all()

    def spawn(self, attempt):
        from .owner import _check, _worker_env
        with (self.store.directory / (attempt["attempt_id"] + ".lock")).open("a+") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            # Pass the locked open-file description through exec: ownership
            # spans Python boot, without a recovery gap before supervise().
            with (self.store.directory / "supervisors.log").open("ab") as log:
                env = _worker_env(attempt.get("run_id"))
                if not _check():
                    self.stopped.set()
                    return
                proc = subprocess.Popen([sys.executable, "-m", "multiagents.scheduler.worker",
                                         str(self.paths.root), attempt["attempt_id"], str(lock.fileno())],
                                        stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                        pass_fds=(lock.fileno(),), cwd=self.paths.root, start_new_session=True,
                                        **({"env": env} if env is not None else {}))
        self.children.append(proc)

    async def reconcile(self):
        effects.finish_completions(self)
        effects.finish_operations(self)
        with self.store.transaction(write=False) as db:
            journal = attempt_records(db)
        settled = []
        for attempt in journal.selected("captured", "claimed", "launched"):
            launch = attempt.get("launch_evidence")
            executor = launch.get("executor") if isinstance(launch, dict) else None
            if isinstance(executor, dict) and executor.get("kind") == "isolated":
                if attempt.get("isolation_unknown"):
                    await self.runner._recover_isolated_predecessor(attempt)
                    continue
                if self.runner._restore_isolated_predecessor_hold(attempt):
                    continue
            confirm_recovery_launch(self.store, self.paths, attempt)
            if attempt["state"] == "captured":
                self.integrate(attempt)
                continue
            if attempt["state"] not in {"claimed", "launched"}:
                continue
            if attempt.get("rate_limit_resume"):
                self.spawn(attempt)
                continue
            if attempt.get("window_stop") or attempt.get("window_stop_started") or attempt.get("window_resume"):
                run = self.runner.tree.get(attempt["run_id"])
                from . import suspension
                own_result = suspension.natural_result(self.paths, run) if run else None
                if (not attempt.get("window_resume") and own_result
                        and await self.runner._steer_predecessor_dead(self.runner._steer_predecessor(run.id))):
                    self.finished(attempt, run)
                else:
                    self.spawn(attempt)
                continue
            if attempt.get("window_completion"):
                self.finished(attempt, SimpleNamespace(**attempt["window_completion"]))
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
                if attempt.get("cleanup") and run.status in PAUSED | {"quota_paused"}:
                    predecessor = self.runner._steer_predecessor(run.id)
                    if await self.runner._steer_predecessor_dead(predecessor):
                        self.finished(attempt, SimpleNamespace(id=run.id, status="failed", session_id=run.session_id))
                    continue
                pending = attempt.get("resume_pending")
                if pending and (run.turn_started_at or 0) > (attempt.get("previous_turn") or 0):
                    with self.service.changed, self.store.transaction() as db:
                        current = attempt_records(db)[attempt["attempt_id"]]
                        current.pop("resume_pending", None)
                        save_attempt(db, current)
                    pending = None
                if pending_commands or (pending and pending > now()):
                    continue
                if run.status == "awaiting_user":
                    # A parked question retains its attempt and locks. Replay
                    # publication if the runner could not write the hold.
                    try:
                        message = json.loads(_run_read(self.paths.run_dir(run.id), "result.json"))
                    except (OSError, ValueError):
                        message = {}
                    if isinstance(message, dict):
                        markers = Runner.question_markers(message.get("final_text") or message.get("text", ""))
                        cause = self.failure_cause(attempt, run, message, markers)
                        if markers and cause:
                            self.runner._publish_failure_cause(run.id, cause, markers)
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
                            current = attempt_records(db)[attempt["attempt_id"]]
                            if current["state"] not in {"claimed", "launched"}:
                                continue
                            node = self.store.node_records(db)[attempt["node_id"]]
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
                                    node.update(state="held", hold={"reason": "dirty_worktree" if current.get("cleanup") else "termination_unconfirmed",
                                                "detail": current.get("cleanup_archive", str(evidence)), "since": now()},
                                                revision=node["revision"] + 1)
                                    self.store.save_node(db, node)
                                    # NT-R3: the per-hold mark is the seq of
                                    # this `held` transition, as it is for
                                    # Engine.hold. A hold that records none
                                    # can only be told apart from its
                                    # successor by the revision, so a re-hold
                                    # would announce nothing.
                                    self.store.transition(db, "held", node["id"], node["hold"])
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
            SimpleNamespace(id=attempt["run_id"], exec_identity=evidence.get("executor", {})), None)
        probe = self.runner._raw_alive_probe(executor, attempt["run_id"])
        return replace(predecessor, captured=bool(evidence.get("pid") or probe),
                       pid=evidence.get("pid"), pid_start=evidence.get("pid_start", ""),
                       executor=executor, probe_raw=probe if evidence.get("pid") or probe else _unknown_probe)

    def launched(self, attempt, run):
        with self.service.changed, self.store.transaction() as db:
            if record_launch(self.store, db, attempt, run):
                self.service.changed.notify_all()

    def hold(self, db, node, reason, detail="", *, markers=None):
        hold = {"reason": reason, "detail": detail} if markers is None else {"reason": reason, "markers": markers}
        for field in ("retry_pending", "retry_route"):
            node.pop(field, None)
        node.update(state="held", hold=hold,
                    revision=node["revision"] + 1)
        if markers is not None:
            node["outcome"] = None
        self.store.save_node(db, node)
        self.store.transition(db, reason, node["id"], node["hold"])
        held = self.store.transition(db, "held", node["id"], node["hold"])
        # AN-R2: when this hold happened on the scheduler clock, anchored
        # to its log position so a later transition cannot steal its age.
        # window_instant is the current tick's reading inside a tick, and
        # the last tick's reading between ticks.
        self.anomaly_last_hold[node["id"]] = (self.window_instant, held["seq"])

    # ------------------------------------------------------------- verdicts
    # Two paths reach a round's verdict: the reviewer's `give_verdict` call,
    # and the `VERDICT(...)` line the runner parses out of that run's own
    # final text. Both are bound to the attempt that judged the round, the
    # generation it judged and the commit that generation sits on, so a run's
    # text can only ever settle its own round.

    def written_verdict(self, run, status):
        """The verdict the runner parsed from `run`'s own final text (VR-R1).

        None unless the run finished `done`: text left behind by a run that
        failed or was stopped is not a verdict (VR-R3). The runner persists
        no verdict at all when a text's verdict lines contradict each other,
        so a parsed one is always lines that agreed.
        """
        if status != "done" or run is None:
            return None
        return run.verdict if run.verdict in VERDICTS else None

    def round_verdict(self, loop, attempt, written):
        """The verdict that settles this round, and where it came from
        (VR-R1..VR-R4). An explicit `give_verdict` always wins; a verdict the
        reviewer wrote is used when the tool was not called, and the two
        disagreeing is recorded rather than resolved. None when neither path
        produced one: the round stays unresolved, as it does today.

        `written` is None unless `attempt`'s run finished `done` with a
        verdict the runner parsed from its own text.
        """
        proposed = loop.get("pending_verdict")
        if proposed and proposed.get("attempt_id") == attempt["attempt_id"]:
            verdict = dict(proposed, verdict_source="tool")
            if written and written != verdict["verdict"]:
                verdict["verdict_disagreement"] = {"tool": verdict["verdict"], "text": written}
            return verdict
        review = attempt.get("review") or {}
        judged = (review.get("node_id") == loop["id"]
                  and any(g["seq"] == review.get("generation_seq") and g["commit"] == review.get("commit")
                          for g in loop["generations"]))
        if written and judged:
            # No findings: a verdict line is a judgement, not a list of
            # defects, and nothing reads prose to invent one.
            return {**review, "verdict": written, "findings": [], "verdict_source": "text",
                    "attempt_id": attempt["attempt_id"]}
        return None

    def record_round(self, db, loop, nodes, verdict):
        """Write a settled round's verdict on every record of the work it
        judged — each mirrored generation (VR-R4) — and on the loop's pending
        verdict, which is what `composites` reads to exit or relaunch."""
        round_generations = loop["generations"][loop.get("round_generation_start", 0):verdict["generation_seq"]]
        identities = {(g["run_id"], g["commit"]) for g in round_generations}
        for owner in nodes.values():
            # Another loop's record is its own judgement (NC-R97).
            if owner["kind"] == "loop" and owner["id"] != loop["id"]:
                continue
            for generation in owner["generations"]:
                if (generation["run_id"], generation["commit"]) in identities:
                    generation["verdict"] = verdict["verdict"]
                    generation["verdict_source"] = verdict["verdict_source"]
                    if verdict.get("verdict_disagreement"):
                        generation["verdict_disagreement"] = verdict["verdict_disagreement"]
                    self.store.save_node(db, owner)
        loop["pending_verdict"] = verdict
        self.store.save_node(db, loop)

    def finished(self, attempt, run):
        """Journal confirmed death, capture outside the lock, then save its result.

        finished + integrate form the completion boundary for quota handover.
        The launched attempt retains ownership until its capture outcome is
        durable. Reconcile resumes either the intent or the captured result.
        """
        if attempt.get("cleanup"):
            self.finished_cleanup(attempt, run)
            return
        confirm_recovery_launch(self.store, self.paths, attempt)
        from . import suspension
        config = self.service.configuration()
        own_result = suspension.natural_result(self.paths, run) if hasattr(run, "turn_started_at") else None
        try:
            message = json.loads(_run_read(self.paths.run_dir(run.id), "result.json"))
        except (OSError, ValueError):
            message = {}
        if not isinstance(message, dict):
            message = {}
        markers = Runner.question_markers(message.get("final_text") or message.get("text", ""))
        # TF-R1: the classified cause of this attempt, settled once here so
        # every public view and hold shows the same record. The runner's own
        # classification (written to result.json with the Run in hand) wins;
        # a missing or partial record is classified from what survived.
        cause = self.failure_cause(attempt, run, message, markers)
        if cause and cause.get("class") == "quota" and not cause.get("reset_time"):
            # TF-R3: source 3 of a reset time — the account's usage window,
            # read now, before the hold is laid down with its detail.
            reset = self.quota_window_reset(cause.get("provider") or cause.get("account", ""))
            if reset:
                cause["reset_time"] = reset
        if cause and cause.get("class") == "quota":
            # TF-R3: the cooldown binds the ACCOUNT that ran out — until its
            # announced reset when one is known, else the recheck interval.
            # It is lifted the moment a recheck reads the account usable.
            until = 0.0
            if cause.get("reset_time"):
                try:
                    until = datetime.fromisoformat(cause["reset_time"]).timestamp()
                except ValueError:
                    until = 0.0
            if not until:
                until = self.instant() + self.quota_interval()
            account = cause.get("provider") or cause.get("account", "")
            if account:
                self.runner.tree.set_cooldown(account, until, "quota exhausted", cause="quota")
        with self.service.changed, self.store.transaction() as db:
            current = attempt_records(db)[attempt["attempt_id"]]
            if (current.get("window_resumed_at") != attempt.get("window_resumed_at")
                    or (getattr(run, "turn_started_at", None) is not None
                        and current.get("turn_started_at") is not None
                        and run.turn_started_at != current["turn_started_at"])):
                return
            if own_result and (current.get("window_stop") or current.get("window_stop_started")):
                suspension.record_completion(self.store, db, current, own_result)
            if (current["state"] in {"recorded", "captured", "suspended"}
                    or current.get("window_stop") or current.get("window_stop_started") or current.get("window_resume")
                    or current.get("window_resumed_at") != attempt.get("window_resumed_at")
                    or (current["state"] == "abandoned" and current.get("cancel_requested") and current.get("window_suspended_at"))):
                return
            if current.get("rate_limit_resume"):
                return
            from . import rate_limits
            if message.get("cause") == "rate_limited" and message.get("turn_started_at") == getattr(run, "turn_started_at", None):
                db.execute("UPDATE capabilities SET revoked=1 WHERE subject=?", (run.id,))
                node = self.store.node_records(db)[current["node_id"]]
                rate_limits.interrupted(self, db, current, node, message)
                self.service.changed.notify_all()
                return
            tree_run = self.runner.tree.get(run.id)
            if (current.get("rate_limit_recovery") and tree_run and
                    (tree_run.reason == "session_lost" or (message.get("status") == "failed"
                     and Runner._session_resume_failure(message.get("stderr_tail", ""))))):
                node = self.store.node_records(db)[current["node_id"]]
                rate_limits.fresh(self.store, db, current, node, self.paths)
                db.execute("UPDATE capabilities SET revoked=1 WHERE subject=?", (run.id,))
                self.service.changed.notify_all()
                return
            current.setdefault("capture_intent", {**(current.get("window_completion") or
                               {"id": run.id, "status": run.status, "session_id": run.session_id}),
                               "need_info": markers})
            save_attempt(db, current)
            db.execute("UPDATE capabilities SET revoked=1 WHERE subject=?", (run.id,))
            generation_commits = set()
            generation_policies = {}
            if "input_commit" in current and current.get("readonly_capture") is None:
                journal, policies = attempt_records(db), {}
                for node in self.store.node_records(db).values():
                    for generation in node["generations"]:
                        commit, run_id = generation["commit"], generation.get("run_id")
                        generation_commits.add(commit)
                        if run_id not in policies:
                            producers = matching_records(journal, "run_id", run_id) if run_id else []
                            policies[run_id] = [producer.get("readonly_paths") for producer in producers] or [None]
                        generation_policies.setdefault(commit, {})[run_id] = policies[run_id]
        run = SimpleNamespace(**current["capture_intent"])
        if current.get("window_completion"):
            # The journal owns confirmed completion before the tree looks
            # terminal. Replaying this publication never recaptures a stop.
            self.runner._settle_holds()
            self.runner.tree.update(run.id, status=run.status, reason="", session_id=run.session_id)
            self.runner._release(run.id)
        result = {"status": run.status, "session_id": run.session_id, "need_info": getattr(run, "need_info", []),
                  "run_dir": str(self.paths.run_dir(run.id))}
        executor_evidence = current.get("launch_evidence", {}).get("executor", {})
        unstarted_isolated = (executor_evidence.get("kind") == "isolated"
                              and executor_evidence.get("failure")
                              and not executor_evidence.get("invocation_id"))
        if "input_commit" in current and not unstarted_isolated:
            from .results import MissingCheckout

            def persist_capture(decision):
                # Durable before Results publishes the retention ref. Keep
                # unrelated attempt fields written during capture intact.
                with self.store.transaction() as db:
                    pending = attempt_records(db)[current["attempt_id"]]
                    pending["readonly_capture"] = decision
                    save_attempt(db, pending)

            try:
                result.update(Results(self.paths, config, generation_commits=generation_commits,
                                      generation_policies=generation_policies,
                                      persist_capture=persist_capture).capture(run, current, self.runner.authority))
            except MissingCheckout as exc:
                result.update(status="failed", failure="missing_tree", detail=str(exc))
            except (gitops.GitError, OSError, ValueError) as exc:
                result["capture_error"] = str(exc)
        if current.get("completion_proven") == "missing_tree":
            result.update(status="failed", failure="missing_tree")
        if result.get("failure") == "missing_tree" and cause:
            # The provider's own turn ended fine; the capture lost it. TF-R1
            # keeps the provider's class out of it — this is not a crash.
            cause = {**cause, "class": "other",
                     "excerpt": (result.get("detail") or "the result checkout disappeared")[:2000]}
        if cause:
            result["cause"] = cause
        with self.service.changed, self.store.transaction() as db:
            current = attempt_records(db)[attempt["attempt_id"]]
            if (current["state"] in {"recorded", "captured", "suspended"}
                    or current.get("window_stop") or current.get("window_stop_started") or current.get("window_resume")
                    or current.get("window_resumed_at") != attempt.get("window_resumed_at")
                    or (current["state"] == "abandoned" and current.get("cancel_requested") and current.get("window_suspended_at"))):
                return
            node = self.store.node_records(db)[attempt["node_id"]]
            if cause:
                entry = next((r for r in node["runs"]
                              if r["attempt_id"] == current["attempt_id"]), None)
                if entry is not None:
                    entry["cause"] = cause
                    self.store.save_node(db, node)
            if result.get("capture_error") and result.get("failure") != "missing_tree":
                self.hold(db, node, "result_capture_failed", result["capture_error"])
            # TF: no flag left on a finished attempt may block a relaunch —
            # a resume deadline that outlived its turn is dropped with it.
            if isinstance(current.get("resume_pending"), (int, float)):
                current.pop("resume_pending", None)
            current.update(state="captured", result=result)
            save_attempt(db, current)
            self.store.transition(db, "run_finished", node["id"], {"run_id": run.id, "status": result["status"], **({"failure": result["failure"]} if result.get("failure") else {})})
            self.service.changed.notify_all()
        self.integrate(current)

    def finished_cleanup(self, attempt, run):
        sessions.finish_cleanup(self.store, self.runner, attempt, run)
        with self.service.changed:
            self.service.changed.notify_all()

    # ------------------------------------------------------------------ TF
    # Transient failures (context/specs/transient-failures.md): one bounded
    # retry path (TF-R2), quota holds with rechecks (TF-R3), and launch
    # exceptions that are caught, classified and reconciled (TF-R4). Every
    # backoff and recheck reads the scheduler's own clock (`instant`).

    def failure_cause(self, attempt, run, message, markers):
        """TF-R1: the classified cause of a finished attempt, or None."""
        # A successful resume must not overwrite its earlier throttle with
        # a synthetic `other` cause. Questions still take precedence.
        if not markers and (message.get("status") or run.status) in {"done", "merged", "idle"}:
            return None
        tree_run = self.runner.tree.get(run.id)
        provider_name = (attempt.get("binding") or {}).get("provider") or getattr(tree_run, "provider", "") or ""
        model = (attempt.get("binding") or {}).get("model") or getattr(tree_run, "model", "") or ""
        from .. import failures as tf
        isolation_failure = (attempt.get("launch_evidence") or {}).get("executor", {}).get("failure")
        if isolation_failure:
            cause = dict(isolation_failure)
        elif message.get("failure_cause"):
            cause = dict(message["failure_cause"])
        elif provider_name:
            cause = tf.cause_from_result(
                {**message, "status": message.get("status") or run.status},
                markers, self.runner.providers.get(provider_name),
                run_provider=provider_name, model=model,
                account=(self.runner.providers.get(provider_name).container_account
                         or provider_name) if self.runner.providers.get(provider_name) else "")
        else:
            cause = tf.classify(status=message.get("status") or run.status,
                                markers=markers, model=model)
        if not cause.get("class"):
            return None
        if markers:
            # TF-R1 precedence: a question outranks every other class.
            cause = {**cause, "class": "question"}
        return cause

    def quota_interval(self):
        scheduler = self.service.configuration().project["scheduler"]
        try:
            return max(1.0, float(scheduler.get("quota_recheck_seconds", 1800)))
        except (TypeError, ValueError):
            return 1800.0

    def quota_window_reset(self, provider_name):
        """TF-R3: the account usage window's `resets_at`, read once, or ""."""
        provider = self.runner.providers.get(provider_name)
        if provider is None:
            return ""
        try:
            from ..budget import read_provider
            from ..config import global_config_dir
            reading = read_provider(provider_name, provider, self.runner.executor(),
                                    global_config_dir(), self.paths.config,
                                    use_cache=False, force=True,
                                    limits=self.runner.config.limits)
        except Exception:
            return ""
        return tf.parse_reset(getattr(reading, "resets_at", None) or "")

    def settle_failure(self, db, nodes, node, loop, current, result):
        """TF-R2/TF-R3: settle a failed attempt inside the integrate
        transaction. Returns the node's outcome when it is final, None when
        the node stays alive (a pending retry, retries_exhausted or a quota
        hold)."""
        from .. import failures as tf
        cause = result.get("cause") or {}
        kind = cause.get("class", "other")
        if kind == "other" and cause.get("detail_code") == "isolation_unavailable":
            self.hold(db, node, "isolation_unavailable", cause)
            return None
        if kind in tf.RETRYABLE:
            budget, backoff = tf.retry_policy(
                self.service.configuration().project["scheduler"])
            used = int(node.get("tf_retries", 0))
            if used >= budget:
                node.pop("retry_route", None)
                self.hold(db, node, "retries_exhausted", {**cause, "retries": used})
                return None
            used += 1
            node["tf_retries"] = used
            node.update(state="open", outcome=None, revision=node["revision"] + 1)
            node["retry_pending"] = {
                "at": self.instant() + tf.backoff_for(backoff, used),
                "from": current["run_id"], "attempt": current["attempt_id"],
                "used": used, "kind": kind,
                "session": result.get("session_id") or "",
                "provider": (current.get("binding") or {}).get("provider")
                or cause.get("provider", "")}
            self.store.transition(db, "run_retry", node["id"],
                                  {"attempt_id": current["attempt_id"], "class": kind, "retry": used})
            return None
        if kind == "quota":
            reset = cause.get("reset_time") or ""
            epoch_reset = datetime.fromisoformat(reset).timestamp() if reset else 0.0
            recheck = epoch_reset or (self.instant() + self.quota_interval())
            node["quota_hold"] = {"provider": cause.get("provider", ""),
                                  "account": cause.get("account", ""),
                                  "reset": reset or None,
                                  "recheck_at": recheck,
                                  "session": result.get("session_id") or "",
                                  "agent": node["agent"]}
            detail = dict(cause)
            if reset:
                detail["resets_at"] = reset
            self.hold(db, node, "quota_exhausted", detail)
            return None
        return "failed"

    def launch_failure(self, node, nodes, text):
        """TF-R4: an exception while admitting or launching, recorded as a
        classified `launch` cause and bounded by the retry budget."""
        from .. import failures as tf
        provider_name = node["pins"].get("provider", "")
        spec = self.service.configuration().agents.get(node["agent"])
        if not provider_name and spec is not None:
            provider_name = spec.provider
        provider = self.runner.providers.get(provider_name)
        cause = tf.classify(status="failed", launched=False, error_text=text,
                            patterns=tf.pattern_lists(provider) if provider else None,
                            provider=provider_name,
                            account=(provider.container_account or provider_name) if provider else "",
                            model=node["pins"].get("model") or (spec.model if spec else None) or "")
        budget, backoff = tf.retry_policy(
            self.service.configuration().project["scheduler"])
        with self.service.changed, self.store.transaction() as db:
            current = self.store.node_records(db)[node["id"]]
            if current["state"] != "open":
                return
            current["launch_failure"] = cause
            used = int(current.get("tf_retries", 0))
            if used >= budget:
                current.pop("retry_route", None)
                self.hold(db, current, "retries_exhausted", {**cause, "retries": used})
            else:
                used += 1
                current["tf_retries"] = used
                current.update(revision=current["revision"] + 1)
                current["retry_pending"] = {"at": self.instant() + tf.backoff_for(backoff, used),
                                            "from": "", "attempt": "", "used": used,
                                            "kind": "launch"}
                self.store.transition(db, "launch_failed", node["id"],
                                      {"detail": text[:500], "retry": used})
            self.store.save_node(db, current)
            self.service.changed.notify_all()

    def predecessor_liveness(self, run_id, attempt_id=None):
        """TF-R2: the dead-alive-unknown verdict on a retry's predecessor,
        from the run directory's own identity — the wrapper's pid and the
        exit status it recorded, or the worker's proof it never spawned.
        Anything unreadable is unknown, never death."""
        if not run_id:
            return "dead"
        from ..runner import _positively_ended, _recorded_wrapper
        run_dir = self.paths.run_dir(run_id)
        try:
            if (run_dir / "exit_status").is_file():
                return "dead"
            pid = _recorded_wrapper(run_dir)
            if pid:
                try:
                    start = (run_dir / "wrapper.pid.start").read_text().strip()
                except OSError:
                    start = ""
                return "alive" if not _positively_ended(pid, start, None) else "dead"
        except OSError:
            pass
        if attempt_id:
            with self.store.transaction(write=False) as db:
                attempt = attempt_records(db).get(attempt_id)
            if (attempt and attempt["run_id"] == run_id
                    and attempt.get("never_spawned") is True):
                return "dead"
        return "unknown"

    async def evaluate_retries(self):
        """TF-R2: one pass over pending retries — liveness first, then the
        backoff clock; a due retry whose predecessor is dead hands its route
        (session continuity included) to normal admission."""
        with self.store.transaction(write=False) as db:
            nodes = self.store.node_records(db)
        for node in [n for n in nodes.selected("open")
                     if n["state"] == "open" and n.get("retry_pending")]:
            pending = node["retry_pending"]
            liveness = self.predecessor_liveness(pending.get("from"), pending.get("attempt"))
            if liveness == "unknown":
                with self.service.changed, self.store.transaction() as db:
                    current = self.store.node_records(db)[node["id"]]
                    if (current["state"] == "open"
                            and current.get("retry_pending") == pending):
                        self.hold(db, current, "liveness_unknown",
                                  {"run_id": pending.get("from"), "kind": pending.get("kind"),
                                   "retry": pending.get("used")})
                continue
            if liveness == "alive" or self.instant() < float(pending.get("at", 0) or 0):
                continue
            with self.service.changed, self.store.transaction() as db:
                current = self.store.node_records(db)[node["id"]]
                if (current["state"] == "open"
                        and current.get("retry_pending") == pending):
                    from . import control
                    control.recovery(current, attempt_records(db), True)
                    current.pop("retry_pending", None)
                    source = attempt_records(db).get(pending.get("attempt"), {})
                    route = {"attempt": pending.get("attempt"), "from": pending.get("from"),
                             "executor_kind": source.get("launch_evidence", {}).get("executor", {}).get("kind", "")}
                    if pending.get("session"):
                        route["session"] = pending["session"]
                    if pending.get("provider"):
                        route["provider"] = pending["provider"]
                    if route:
                        current["retry_route"] = route
                    self.store.save_node(db, current)
                    self.service.changed.notify_all()

    def quota_fallback_usable(self, node, hold):
        """TF-R3: is there a configured fallback route this node's agent can
        run on now — usable, not the exhausted account and not its family?"""
        spec = self.service.configuration().agents.get(hold.get("agent", ""))
        if spec is None:
            return False
        exhausted = hold.get("provider", "")
        family = (self.runner.providers.get(exhausted).family
                  if self.runner.providers.get(exhausted) else "") or exhausted
        models = spec.models or spec.extra.get("models") or {}
        for name in models:
            if name == exhausted:
                continue
            other = self.runner.providers.get(name)
            if other is None or not other.enabled:
                continue
            if other.family and other.family == family:
                continue
            if self.runner.tree.cooldown(name):
                continue
            model, _ = spec.replace(models=models).fallback_for(name)
            if self.runner._cap_refusal(name, model or ""):
                continue
            return True
        return False

    async def evaluate_quota(self):
        """TF-R3: a held quota exhaustion is released when routing finds a
        usable route — a recovered account, or a configured fallback. A
        reset time is a recheck time, never proof; the account is read at or
        after it, and every 30 minutes when no reset is known."""
        from ..budget import read_provider
        from ..config import global_config_dir
        with self.store.transaction(write=False) as db:
            nodes = self.store.node_records(db)
        config = self.service.configuration()
        reserve = float(config.project.get("budget", {}).get("reserve_headroom", 0.15))
        for node in [n for n in nodes.selected("held") if n["state"] == "held"
                     and (n.get("hold") or {}).get("reason") == "quota_exhausted"]:
            hold = node.get("quota_hold") or {}
            try:
                fallback = self.quota_fallback_usable(node, hold)
            except Exception:
                # TF-R4: a bad route must not abort every node's tick. Keep
                # its quota hold until routing can be evaluated safely.
                logging.getLogger(__name__).exception("scheduler could not recheck quota for node %s", node["id"])
                self.evaluation_failures[node["id"]] = time.monotonic()
                continue
            if fallback:
                self.release_quota(node, hold, resume=False)
                continue
            if self.instant() < float(hold.get("recheck_at", 0) or 0):
                continue
            provider_name = hold.get("provider", "")
            provider = self.runner.providers.get(provider_name)
            if provider is None:
                continue
            try:
                reading = await asyncio.to_thread(
                    read_provider, provider_name, provider, self.runner.executor(),
                    global_config_dir(), self.paths.config,
                    None, False, True, self.runner.config.limits)
            except Exception:
                continue
            usable = bool(reading.known and reading.headroom is not None
                          and reading.headroom >= reserve)
            if usable:
                self.release_quota(node, hold, resume=True)
                continue
            reset = tf.parse_reset(getattr(reading, "resets_at", None) or "")
            epoch_reset = datetime.fromisoformat(reset).timestamp() if reset else 0.0
            recheck = (epoch_reset if epoch_reset > self.instant()
                       else self.instant() + self.quota_interval())
            with self.service.changed, self.store.transaction() as db:
                current = self.store.node_records(db)[node["id"]]
                if (current["state"] == "held"
                        and (current.get("hold") or {}).get("reason") == "quota_exhausted"):
                    held = current.get("quota_hold") or {}
                    held["recheck_at"] = recheck
                    if reset:
                        held["reset"] = reset
                    current["quota_hold"] = held
                    detail = dict((current.get("hold") or {}).get("detail") or {})
                    if reset:
                        detail["resets_at"] = reset
                    current["hold"]["detail"] = detail
                    self.store.save_node(db, current)
                    self.service.changed.notify_all()

    def release_quota(self, node, hold, *, resume):
        """TF-R3: a usable route exists — the node works again, from the
        failed attempt's committed work; the same provider resumes the same
        session, a fallback launches fresh."""
        from . import control
        with self.service.changed, self.store.transaction() as db:
            current = self.store.node_records(db)[node["id"]]
            if (current["state"] != "held"
                    or (current.get("hold") or {}).get("reason") != "quota_exhausted"):
                return
            journal = attempt_records(db)
            current.pop("quota_hold", None)
            current.update(state="open", hold=None, revision=current["revision"] + 1)
            source = next((journal.get(row.get("attempt_id"), {})
                           for row in reversed(current.get("runs") or [])
                           if isinstance(row, dict) and isinstance(row.get("attempt_id"), str)), {})
            selection = (source.get("launch_evidence") or {}).get("executor") or {}
            route = {"executor_kind": selection["kind"]} if selection.get("kind") else {}
            if resume and hold.get("session"):
                route.update(session=hold["session"], provider=hold.get("provider", ""))
            if route:
                current["retry_route"] = route
            control.recovery(current, journal, True)
            self.store.save_node(db, current)
            self.store.transition(db, "quota_released", current["id"],
                                  {"provider": hold.get("provider", ""),
                                   "route": "session" if resume else "fallback"})
            self.service.changed.notify_all()
        if resume and hold.get("provider"):
            # The account is usable again; nothing may keep routing away.
            self.runner.tree.clear_cooldown(hold["provider"])

    def integrate(self, attempt):
        with effects.serialized(self.store):
            self._integrate(attempt)

    def _integrate(self, attempt):
        # Snapshot, compute, journal, apply, checkpoint. Every Git call runs
        # after its store transaction has closed, including retention refs.
        while True:
            with self.store.transaction(write=False) as db:
                journal = attempt_records(db)
                current = journal[attempt["attempt_id"]]
                nodes = self.store.node_records(db)
            if current["state"] != "captured":
                return
            node = nodes[current["node_id"]]
            result = current["result"]
            parent = nodes.get(node.get("parent"))
            reviewer = parent and parent["kind"] == "loop" and parent["loop"]["verdict_child"] == node["id"]
            # The runner's parse of this run's own final text, read once here:
            # only the verdict child has a verdict to give (VR-R3).
            written = (self.written_verdict(self.runner.tree.get(current["run_id"]), result["status"])
                       if reviewer else None)
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
                            if self.store.node_records(db) != nodes or attempt_records(db)[attempt["attempt_id"]] != current:
                                continue
                            self.hold(db, node, "integration_conflict", str(exc))
                            current["state"] = "recorded"
                            save_attempt(db, current)
                        return
                    integration = {"generation": generation, "before": before}
                    with self.service.changed, self.store.transaction() as db:
                        if self.store.node_records(db) != nodes or attempt_records(db)[attempt["attempt_id"]] != current:
                            continue
                        current["integration"] = integration
                        save_attempt(db, current)
                    continue
                generation = integration["generation"]
                if actual not in {integration["before"], generation["commit"]}:
                    with self.service.changed, self.store.transaction() as db:
                        if self.store.node_records(db) != nodes or attempt_records(db)[attempt["attempt_id"]] != current:
                            continue
                        self.hold(db, node, "integration_conflict", "node branch differs from its host-recorded tip")
                        current["state"] = "recorded"
                        save_attempt(db, current)
                    return
                if "refs" not in integration:
                    refs = [[f"refs/heads/node-generations/{node['id']}/{generation['seq']}", generation["commit"]]]
                    for loop in recording_loops(node, nodes):
                        refs.append([f"refs/heads/node-generations/{loop['id']}/{len(loop['generations']) + 1}", generation["commit"]])
                    with self.store.transaction() as db:
                        if self.store.node_records(db) != nodes or attempt_records(db)[attempt["attempt_id"]] != current:
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
                current = attempt_records(db)[attempt["attempt_id"]]
                if current["state"] != "captured":
                    return
                nodes = self.store.node_records(db)
                node = nodes[current["node_id"]]
                # Decided here, in the transaction that settles it, so a
                # written verdict is receipted exactly where an explicit one
                # already was: before this run's own bookkeeping.
                verdict = (self.round_verdict(nodes.get(node.get("parent")), current, written)
                           if reviewer and result["status"] == "done" else None)
                if verdict and verdict["verdict_source"] == "text":
                    self.store.transition(db, "verdict", verdict["node_id"], dict(verdict))
                integration = current.get("integration")
                if integration:
                    generation = integration["generation"]
                    top = nodes[current["branch_node"]]
                    top["branch_tip"] = generation["commit"]
                    node["generations"].append(generation)
                    # Every loop whose work subtree produced it records it (NC-R97).
                    for loop in recording_loops(node, nodes):
                        loop["generations"].append({**generation, "seq": len(loop["generations"]) + 1})
                        self.store.save_node(db, loop)
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
                tree_run = self.runner.tree.get(current["run_id"])
                node.pop("rate_limit_attempts", None)
                node.pop("rate_limit_fresh_from", None)
                # TF-R1: the classified cause travels with the run entry the
                # public views show, whatever settles below.
                if result.get("cause"):
                    entry = next((r for r in node["runs"]
                                  if r["attempt_id"] == current["attempt_id"]), None)
                    if entry is not None:
                        entry["cause"] = result["cause"]
                # TB-R1: hold in the settlement transaction, before any node
                # can see this attempt as a successful dependency.
                if (node["state"] not in {"cancelled", "held"} and result.get("need_info")
                        and not verdict and not (tree_run and tree_run.verdict)):
                    self.hold(db, node, "needs_info", markers=result["need_info"])
                if node["state"] not in {"cancelled", "held"}:
                    loop = nodes.get(node.get("parent"))
                    while loop and loop["kind"] != "loop":
                        loop = nodes.get(loop["parent"])
                    if result["status"] != "done" and loop:
                        loop.pop("pending_verdict", None)
                        self.store.save_node(db, loop)
                    outcome = None
                    if result["status"] != "done":
                        outcome = self.settle_failure(db, nodes, node, loop, current, result)
                    if outcome is None and result["status"] == "done":
                        outcome = "completed"
                    if outcome is not None:
                        if verdict:
                            verdict["settled"] = True
                            self.record_round(db, loop, nodes, verdict)
                            self.store.transition(db, "verdict_settled", loop["id"], verdict)
                            outcome = verdict["verdict"]
                        node.update(state="done", outcome=outcome,
                                    revision=node["revision"] + 1)
                        self.store.transition(db, "done", node["id"], {"outcome": node["outcome"]})
                        if result["status"] != "done" and loop and loop["state"] not in {"held", "cancelled", "done"}:
                            self.hold(db, loop, "run_failed", node["id"])
                if current.get("alias_id") and tree_run and tree_run.reason == "session_lost":
                    self.hold(db, node, "session_lost", current["run_id"])
                self.store.save_node(db, node)
                self.service.changed.notify_all()
            return

    def integration_failed(self, attempt, nodes, current, node, exc):
        """Hold the node on a ref that is not as journalled; True to re-read."""
        with self.service.changed, self.store.transaction() as db:
            if self.store.node_records(db) != nodes or attempt_records(db)[attempt["attempt_id"]] != current:
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
        return next((other for other in select_records(journal, "captured")
                     if other["attempt_id"] != current["attempt_id"] and other["state"] == "captured"
                     and other.get("branch_node") == current["branch_node"]
                     and other.get("integration", {}).get("before") == top["branch_tip"]
                     and other.get("integration", {}).get("generation", {}).get("commit") == actual), None)

    def settle_held_rounds(self, db, nodes, journal):
        """VR-R5: settle, once, the rounds an earlier engine held
        `unresolved_round` although their own reviewer had answered in
        writing. This is how the trial loops resolve — from their reviewers'
        verdicts, not from the orchestrator's reading of them.

        A round whose last verdict-child run does not qualify is left exactly
        as it was; a settled round is no longer held, so nothing is settled
        twice and a restart finds nothing to do.
        """
        for loop in [n for n in nodes.selected("held")
                     if n["kind"] == "loop" and n["state"] == "held"
                     and (n["hold"] or {}).get("reason") == "unresolved_round"]:
            child = nodes.get((loop["loop"] or {}).get("verdict_child"))
            runs = (child or {}).get("runs") or []
            if not runs:
                continue
            run_id = runs[-1]["run_id"]
            attempt = next((a for a in matching_records(journal, "run_id", run_id) if a["run_id"] == run_id and a.get("review")), None)
            if attempt is None:
                continue          # nothing binds a verdict to this round
            run = self.runner.tree.get(run_id)
            verdict = self.round_verdict(loop, attempt, self.written_verdict(run, run.status if run else ""))
            if not verdict:
                continue
            if verdict["verdict_source"] == "text":
                self.store.transition(db, "verdict", loop["id"], dict(verdict))
            verdict["settled"] = True
            self.record_round(db, loop, nodes, verdict)
            # Lift the hold: the loop below settles the round as it would
            # after a give_verdict, emitting loop_exited or round_rejected.
            loop.update(state="running", hold=None, revision=loop["revision"] + 1)
            self.store.save_node(db, loop)

    def composites(self):
        with self.service.changed, self.store.transaction() as db:
            nodes = self.store.node_records(db)
            if self.settle_held_once:
                self.settle_held_once = False
                self.settle_held_rounds(db, nodes, attempt_records(db))
            for node in sorted((n for n in nodes.selected("open", "running", "held") if n["kind"] != "simple"
                                and n["state"] not in {"done", "cancelled", "suspended"}),
                               key=lambda n: len(model.subtree(nodes, n["id"]))):
                if node["state"] in {"done", "cancelled", "suspended"} or node.get("completion_pending") or node.get("disposal_pending"):
                    continue
                children = [nodes[id] for id in node["children"]]
                held = next((c for c in children if c["state"] == "held"), None)
                if node["state"] == "held":
                    reason = (node["hold"] or {}).get("reason")
                    if reason == "run_failed" and node["kind"] == "loop":
                        child = nodes.get(node["hold"].get("detail"))
                        if (not child or child["id"] not in model.subtree(nodes, node["id"])
                                or child["state"] not in {"open", "running"}
                                or child.get("relaunch_rounds", {}).get(node["id"]) != node["loop"]["rounds_rejected"]):
                            continue
                        self.store.transition(db, "hold_repaired", node["id"], {"child": child["id"]})
                    elif reason != "child_held" or held:
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

    def complete(self, db, node, outcome, *, approve_generation=True):
        if node.get("completion_pending"):
            return
        if approve_generation and node["kind"] in {"sequence", "group"} and outcome == "approved":
            nodes = self.store.node_records(db)
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

    def resume_failed(self, db, loop, child):
        # LR-R1/R2: this is a continuation of the current round. Preserve its
        # crash retry allowance, sibling results, findings and alias bindings.
        retry_round = child.get("crash_retry_round")
        model.reset(child)
        child["crash_retry_round"] = retry_round
        self.store.save_node(db, child)
        loop.update(state="running", outcome=None, hold=None, revision=loop["revision"] + 1)
        self.store.save_node(db, loop)
        self.store.transition(db, "reopened", child["id"])
        self.store.transition(db, "reopened", loop["id"])

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
        for field in ("pending_verdict", "findings", "closed_by", "closure"):
            node.pop(field, None)
        if not verdict_only:
            node["round_generation_start"] = len(node["generations"])
        self.store.save_node(db, node)

    async def cancel(self, ids):
        results = {}
        with self.store.transaction(write=False) as db:
            journal = attempt_records(db)
        for attempt in select_records(journal, "claimed", "launched"):
            if attempt["node_id"] in ids and attempt["state"] in {"claimed", "launched"}:
                result = await self.runner.stop(attempt["run_id"])
                results[attempt["node_id"]] = result.get("predecessor_death_confirmed", False)
        return results

    def request_cancel(self, ids, db, nodes=None):
        """Record cancellation durably; return what is decided now and what needs a stop.

        Stopping takes seconds, so it never runs inside this write
        transaction (stop_runs, after commit). The worker refuses to launch
        a claim carrying this request and stops a handle that arrives later.
        """
        decided, stops = {}, {}
        nodes = nodes if nodes is not None else self.store.node_records(db)
        for node_id in ids:
            node = nodes[node_id]
            for field in ("retry_pending", "retry_route", "quota_hold"):
                node.pop(field, None)
            if node.pop("pending_resume", None) is not None:
                self.store.save_node(db, node)
        for attempt in select_records(attempt_records(db), "claimed", "launched", "suspended"):
            if attempt["node_id"] in ids:
                node = nodes[attempt["node_id"]]
                run = self.runner.tree.get(attempt["run_id"])
                cause = tf.classify(status="cancelled",
                                    provider=getattr(run, "provider", ""),
                                    account=getattr(run, "provider", ""),
                                    model=getattr(run, "model", ""))
                for entry in node["runs"]:
                    if entry["attempt_id"] == attempt["attempt_id"]:
                        entry["cause"] = cause
                self.store.save_node(db, node)
            if attempt["node_id"] in ids and attempt["state"] == "suspended":
                attempt.update(state="abandoned", cancel_requested=True, cancel_confirmed=True)
                save_attempt(db, attempt)
                decided[attempt["node_id"]] = True
            if attempt["node_id"] in ids and attempt["state"] in {"claimed", "launched"}:
                attempt["cancel_requested"] = True
                if attempt.get("window_resume") or attempt.get("window_stop") or attempt.get("rate_limit_resume"):
                    attempt["cancel_kind"] = "node"
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

    def window_launch_admission(self, run_id, instant):
        admission = self.resume_admission(run_id, window=True)
        if not admission.get("admitted"):
            return admission
        with self.store.transaction(write=False) as db:
            nodes, journal = self.store.node_records(db), attempt_records(db)
            owner = next((a for a in matching_records(journal, "run_id", run_id) if a["run_id"] == run_id), None)
            if not owner or not owner.get("window_resume") or owner.get("cancel_requested"):
                return {"error": "node_suspended"}
            node = nodes[owner["node_id"]]
            if node["state"] != "suspended" or self.window_blockers(node, nodes, instant):
                return {"error": "node_suspended"}
            return {"blocked": self.lock_blockers(node, nodes, journal)} if self.lock_blockers(node, nodes, journal) else admission

    def resume_admission(self, run_id, db=None, *, window=False, config=None):
        config = config or (self.service.configuration() if db is None else self.runner.config)
        run = self.runner.tree.get(run_id)
        if run is None:
            return {"error": "not_found"}
        if db is not None:
            nodes, journal = self.store.node_records(db), attempt_records(db)
        else:
            with self.store.transaction(write=False) as connection:
                nodes, journal = self.store.nodes(connection), attempts(connection)
        if not config.project["scheduler"]["enabled"]:
            return {"error": "scheduler_disabled",
                    "pending_nodes": sum(n["state"] not in {"done", "cancelled"} for n in nodes.values())}
        owner = next((a for a in matching_records(journal, "run_id", run_id) if a["run_id"] == run_id), None)
        node_id = owner["node_id"] if owner else None
        if node_id:
            state = nodes[node_id]["state"]
            if state == "suspended" and not window:
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
                if node["state"] != "running" and not (window and node["state"] == "suspended"):
                    return {"error": "invalid", "message": f"node is {node['state']}; it cannot be resumed"}
                if window:
                    ancestor = nodes.get(node["parent"])
                    while ancestor:
                        if ancestor["state"] in {"held", "cancelled", "done", "suspended"}:
                            return {"blocked": [{"code": "ancestor", "detail": ancestor["id"]}]}
                        ancestor = nodes.get(ancestor["parent"])
                blockers = self.lock_blockers(node, nodes, journal)
                if blockers:
                    return {"blocked": blockers}
            if db is not None and node_id and not window:
                node = self.store.node_records(db)[node_id]
                journal = attempt_records(db)
                attempt = next(a for a in matching_records(journal, "run_id", run_id) if a["run_id"] == run_id)
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
