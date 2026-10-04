"""Authenticated node operations and the newline JSON Unix socket service."""
from __future__ import annotations

import copy
import hmac
import json
import logging
import math
import os
import socketserver
import threading
import time
import uuid

from ..config import load, source_version
from ..paths import ProjectPaths
from . import model
from .model import Refused, invalid
from .store import Store, encode, token_hash

OPS = {"create_node", "update_node", "cancel_node", "get_node", "list_nodes",
       "instantiate_template", "register_template", "list_templates", "wait_for_nodes",
       "ack_nodes", "give_verdict", "relaunch_node", "close_node", "merge_node",
       "dispose_node", "scheduler_status", "start_agent", "admit_run", "admit_agent", "steer_run", "steer_result"}
MUTATING = OPS - {"get_node", "list_nodes", "list_templates", "wait_for_nodes", "scheduler_status", "admit_agent", "steer_result"}
ROOT_ONLY = {"register_template", "ack_nodes", "relaunch_node", "close_node", "merge_node", "dispose_node"}


def validate_wire_value(value):
    pending = [(value, 0)]
    while pending:
        item, depth = pending.pop()
        if depth > 128:
            invalid("request: JSON nesting exceeds 128 levels")
        if isinstance(item, dict):
            pending.extend((v, depth + 1) for v in item.values())
        elif isinstance(item, list):
            pending.extend((v, depth + 1) for v in item)
        elif isinstance(item, float) and not math.isfinite(item):
            invalid("request: numbers must be finite")
        elif isinstance(item, str):
            try:
                item.encode()
            except UnicodeEncodeError:
                invalid("request: strings must be valid UTF-8")


def validate_arguments(args):
    for key in ("id", "node_id", "parent"):
        if key in args and args[key] is not None and not isinstance(args[key], str):
            invalid(f"{key}: expected a node id")
    for key in ("children", "node_ids"):
        if key in args and (not isinstance(args[key], list)
                            or any(not isinstance(id, str) for id in args[key])):
            invalid(f"{key}: expected a list of node ids")
    for key in ("depends_on", "inputs"):
        if key in args and (not isinstance(args[key], list)
                            or any(not isinstance(ref, dict) or not isinstance(ref.get("node"), str)
                                   for ref in args[key])):
            invalid(f"{key}: expected a list of node references")
    if "cursor" in args and (type(args["cursor"]) is not int
                              or not 0 <= args["cursor"] < 2 ** 63):
        invalid("cursor: expected a non-negative 64-bit integer")
    if "timeout" in args:
        value = args["timeout"]
        try:
            valid = type(value) in (int, float) and math.isfinite(value) and value >= 0
        except OverflowError:
            valid = False
        if not valid:
            invalid("timeout: expected finite non-negative seconds")


class Service:
    def __init__(self, project_root, since):
        self.paths = ProjectPaths(project_root)
        self.store = Store(project_root)
        self.since = since
        self.changed = threading.Condition(threading.RLock())
        self.stopping = False
        self.engine = None
        self._config = None
        self._config_version = None

    def configuration(self):
        version = source_version(self.paths)
        if self._config is None or version != self._config_version:
            self._config = load(self.paths, seed=False)
            self._config_version = version
        return self._config

    def status(self, nodes, db=None):
        counts = {s: 0 for s in ("open", "running", "suspended", "held", "done", "cancelled")}
        for node in nodes.values():
            counts[node["state"]] += 1
        extra = {}
        if self.engine:
            from .engine import attempts
            if db is not None:
                extra = self.engine.status(nodes, attempts(db))
            else:
                with self.store.transaction(write=False) as connection:
                    extra = self.engine.status(nodes, attempts(connection))
        return {"pid": os.getpid(), "since": self.since, "gate": True, "counts": counts, **extra}

    @staticmethod
    def view(node, plan_revision):
        # M1 has no admission or evaluation. These fields are a read view,
        # never evidence that the scheduler has admitted work for launching.
        return {**copy.deepcopy(node), "plan_revision": plan_revision}

    def authenticate(self, db, token):
        if not isinstance(token, str) or not token:
            raise Refused("unauthenticated")
        root_token = self.store.meta(db, "root_token")
        if hmac.compare_digest(token.encode(), root_token.encode()):
            return {"subject": "root", "root": True}
        row = db.execute("SELECT subject, node_id, permissions FROM capabilities WHERE hash=? AND revoked=0",
                         (token_hash(token),)).fetchone()
        if row is None:
            raise Refused("unauthenticated")
        return {"subject": row[0], "node_id": row[1], "permissions": json.loads(row[2]), "root": False}

    def authorize(self, principal, op, args, nodes):
        if principal["root"]:
            if op == "give_verdict":
                raise Refused("forbidden")
            return
        subject = principal["subject"]
        if op in {"admit_run", "steer_run", "steer_result"} and args.get("run_id") != subject:
            run = self.engine.runner.tree.get(args.get("run_id")) if self.engine else None
            from .engine import attempts
            with self.store.transaction(write=False) as db:
                target = next((a["node_id"] for a in attempts(db).values()
                               if a["run_id"] == args.get("run_id")), None)
            recorded = self.engine.runner.authority.get(run.id) if run and self.engine.runner.authority else None
            if target not in model.subtree(nodes, principal["node_id"]) and not (
                    recorded and recorded.get("parent") == subject):
                raise Refused("forbidden")
        scope = model.subtree(nodes, principal["node_id"])
        permissions = principal["permissions"]
        if op in ROOT_ONLY:
            raise Refused("forbidden")
        if op == "give_verdict" and "verdict" not in permissions:
            raise Refused("forbidden")
        if op in {"create_node", "start_agent", "admit_agent", "steer_run", "instantiate_template", "update_node", "cancel_node"} and "delegate" not in permissions:
            raise Refused("forbidden")
        for field in ("id", "node_id"):
            if field in args and args[field] not in scope:
                raise Refused("forbidden")
        if op in {"get_node", "update_node", "cancel_node", "give_verdict"}:
            target = args.get("node_id" if op == "give_verdict" else "id", principal["node_id"])
            if not isinstance(target, str) or target not in scope:
                raise Refused("forbidden")
            if op in {"update_node", "cancel_node"}:
                node = nodes[target]
                if target == principal["node_id"] or node["created_by"] != subject:
                    raise Refused("forbidden")
        if op in {"create_node", "start_agent", "instantiate_template"}:
            parent = args.get("parent") or principal["node_id"]
            if parent not in scope or (parent != principal["node_id"] and nodes[parent]["created_by"] != subject):
                raise Refused("forbidden")
        if "parent" in args and args["parent"] is not None and args["parent"] not in scope:
            raise Refused("forbidden")
        for field in ("depends_on", "inputs"):
            for ref in args.get(field, []) or []:
                if isinstance(ref, dict) and ref.get("node") not in scope:
                    raise Refused("forbidden")
        for child in args.get("children", []) or []:
            if child not in scope or nodes[child]["created_by"] != subject:
                raise Refused("forbidden")
        for id in args.get("node_ids", []) or []:
            if id not in scope:
                raise Refused("forbidden")

    def request(self, request):
        request_id = request.get("request_id") if isinstance(request, dict) else None
        reply = {"request_id": request_id if isinstance(request_id, str) else None, "ok": False}
        try:
            if not isinstance(request, dict):
                invalid("request: expected JSON object")
            validate_wire_value(request)
            op, args = request.get("op"), request.get("args", {})
            after = []
            with self.changed:
                with self.store.transaction(write=isinstance(op, str) and op in MUTATING) as db:
                    principal = self.authenticate(db, request.get("token"))
                    if not isinstance(op, str) or not isinstance(args, dict):
                        invalid("request: expected op string and args object")
                    validate_arguments(args)
                    nodes = self.store.nodes(db)
                    self.authorize(principal, op, args, nodes)
                    if op not in OPS:
                        raise Refused("unknown_op")
                    if self.stopping:
                        raise Refused("scheduler_unavailable")
                    if not isinstance(request_id, str) or not request_id:
                        invalid("request_id: required string")
                    payload = encode({"op": op, "args": args})
                    previous = None
                    if op in MUTATING:
                        previous = db.execute("SELECT payload, reply FROM requests WHERE subject=? AND request_id=?",
                                              (principal["subject"], request_id)).fetchone()
                    if previous:
                        if previous[0] != payload:
                            raise Refused("request_id_reused")
                        reply = json.loads(previous[1])
                    elif op != "wait_for_nodes":
                        result = self.dispatch(db, principal, op, args, nodes, after)
                        reply.update(ok=True, result=result)
                        if op in MUTATING:
                            db.execute("INSERT INTO requests VALUES (?, ?, ?, ?)",
                                       (principal["subject"], request_id, payload, encode(reply)))
                if op in {"get_node", "list_nodes", "scheduler_status"}:
                    self.store.mirror()
                if op == "wait_for_nodes":
                    reply.update(ok=True, result=self.wait(request, principal, args))
                elif op in MUTATING:
                    self.store.mirror()
                    self.changed.notify_all()
            for pending in after:
                reply = self.finish_cancel(principal, request_id, reply, pending)
        except Refused as exc:
            reply = {"request_id": reply["request_id"], "ok": False, "error": exc.result}
        except Exception:
            logging.getLogger(__name__).exception("scheduler request failed: %s", request_id)
            reply = {"request_id": reply["request_id"], "ok": False, "error": {"error": "internal"}}
        return reply

    def dispatch(self, db, principal, op, args, nodes, after=None):
        plan_revision = int(self.store.meta(db, "plan_revision"))
        def view(node):
            if self.engine and op in {"get_node", "list_nodes"}:
                from .engine import attempts
                return {**self.engine.view(node, nodes, attempts(db)), "plan_revision": plan_revision}
            return self.view(node, plan_revision)
        if op in {"steer_run", "steer_result"}:
            from .engine import attempts, save_attempt
            model.check_fields(args, {"run_id", "message", "command_id"}, set())
            attempt = next((a for a in attempts(db).values() if a["run_id"] == args.get("run_id")), None)
            if attempt is None:
                raise Refused("not_found")
            if op == "steer_result":
                command = attempt.get("steer_commands", {}).get(args.get("command_id"))
                if command is None:
                    raise Refused("not_found")
                return {"result": command.get("result")}
            if not isinstance(args.get("message"), str):
                invalid("message: expected string")
            admission = self.engine.resume_admission(args.get("run_id"), db)
            if admission.get("error") or admission.get("blocked"):
                return admission
            attempt = attempts(db)[attempt["attempt_id"]]
            command_id = uuid.uuid4().hex
            attempt.setdefault("steer_commands", {})[command_id] = {"message": args["message"]}
            save_attempt(db, attempt)
            self.engine.spawn(attempt)
            return {"command_id": command_id}
        if op == "admit_agent":
            return self.engine.agent_admission(args, principal)
        if op == "admit_run":
            return self.engine.resume_admission(args.get("run_id"), db)
        if op == "start_agent":
            config = self.configuration()
            allowed = {"agent", "task", "urgent", "model", "timeout", "workdir", "verifies", "budget_tag", "budget_tokens"}
            model.check_fields(args, allowed, set())
            fields = {"kind": "simple", "agent": args.get("agent"), "task": args.get("task"),
                      "urgent": args.get("urgent", False), "plan_revision": plan_revision}
            if args.get("model"):
                fields["pins"] = {"model": args["model"]}
            made = self.dispatch(db, principal, "create_node", fields, nodes)
            node = nodes[made["id"]]
            node["launch"] = {k: v for k, v in args.items() if k in allowed - {"agent", "task", "urgent", "model"}}
            self.store.save_node(db, node)
            return self.view(node, plan_revision + 1)
        if op == "scheduler_status":
            result = self.status(nodes, db)
            result["launch_context"] = principal["root"] or bool(
                self.engine and self.engine.runner.tree.get(principal["subject"]))
            return result
        if op == "list_templates":
            return {"templates": []}
        if op == "get_node":
            node = nodes.get(args.get("id"))
            if node is None:
                raise Refused("not_found")
            return view(node)
        if op == "list_nodes":
            scope = set(nodes) if principal["root"] else model.subtree(nodes, principal["node_id"])
            selected = [view(n) for id, n in nodes.items() if id in scope]
            selected = [n for n in selected if all(n.get(field) == args[field]
                         for field in ("state", "parent", "eligible") if field in args)]
            return {"nodes": selected, "plan_revision": plan_revision}
        if op == "ack_nodes":
            cursor = args.get("cursor")
            top = db.execute("SELECT coalesce(max(seq), 0) FROM notifications").fetchone()[0]
            if type(cursor) is not int or cursor < 0 or cursor > top:
                raise Refused("invalid_cursor")
            cursor = max(cursor, int(self.store.meta(db, "ack")))
            self.store.set_meta(db, "ack", cursor)
            return {"cursor": cursor, "plan_revision": plan_revision}
        if op not in {"create_node", "update_node", "cancel_node"}:
            raise Refused("not_implemented")
        config = self.configuration()
        if not config.project["scheduler"]["enabled"]:
            raise Refused("scheduler_disabled", pending_nodes=len(self.store.pending()))
        original = copy.deepcopy(nodes)
        reply_node = None
        if op == "create_node":
            model.check_fields(args, model.CREATABLE, {"plan_revision"})
            if args.get("kind") == "simple" and "children" in args:
                invalid("children: only composites accept client-supplied children")
            model.revision(args.get("plan_revision"), plan_revision)
            fields = dict(args)
            if not principal["root"]:
                fields["parent"] = fields.get("parent") or principal["node_id"]
            if isinstance(fields.get("loop"), dict) and "rounds_rejected" in fields["loop"]:
                invalid("loop.rounds_rejected: field is not client-writable")
            node = model.create_record(fields, principal["subject"])
            while node["id"] in nodes:
                node = model.create_record(fields, principal["subject"])
            nodes[node["id"]] = node
            model.attach(nodes, node)
            plan_revision += 1
            transition = "created"
        else:
            node = nodes.get(args.get("id"))
            if node is None:
                raise Refused("not_found")
            model.check_fields(args, model.EDITABLE if op == "update_node" else set(),
                               {"id", "revision", "parent"})
            model.revision(args.get("revision"), node["revision"])
            if op == "update_node":
                if not principal["root"] and (node["state"] != "open" or node["runs"]):
                    raise Refused("forbidden")
                if node["state"] in {"done", "cancelled"}:
                    invalid("state: terminal node cannot be edited")
                if "parent" in args:
                    invalid("parent: field is not client-writable")
                if "loop" in args:
                    if (not isinstance(args["loop"], dict) or set(args["loop"]) != {"max_rounds"}
                            or node["kind"] != "loop"):
                        invalid("loop: only max_rounds is editable")
                if "session" in args and node["runs"]:
                    invalid("session: already launched")
                if "children" in args:
                    if node["kind"] == "simple":
                        invalid("children: only composites are editable")
                    new_children = args["children"]
                    if not isinstance(new_children, list):
                        invalid("children: expected a list")
                    if not principal["root"] and set(node["children"]) - set(new_children):
                        raise Refused("forbidden")
                    changed = set(node["children"]) ^ set(new_children)
                    if new_children != node["children"]:
                        changed |= set(node["children"])
                    if any(nodes[id]["runs"] for id in changed if id in nodes):
                        invalid("children: launched child cannot be changed")
                old_children = list(node["children"])
                for key in model.EDITABLE & args.keys():
                    if key == "loop":
                        node[key].update(args[key])
                    else:
                        node[key] = args[key]
                node["revision"] += 1
                model.attach(nodes, node, old_children)
                transition = "updated"
            else:
                if node["state"] == "cancelled":
                    return view(node)
                if node["state"] == "done":
                    invalid("state: terminal node cannot be cancelled")
                descendants = model.subtree(nodes, node["id"])
                from .engine import attempts
                managed = {a["node_id"] for a in attempts(db).values()}
                if any(nodes[id]["state"] in {"running", "suspended"} and id not in managed
                       for id in descendants):
                    raise Refused("not_implemented")
                decided, stops = self.engine.request_cancel(descendants, db) if self.engine else ({}, {})
                unconfirmed = [id for id, dead in decided.items() if not dead]
                for id in descendants:
                    child = nodes[id]
                    if child["state"] in {"done", "cancelled"}:
                        continue
                    if id in unconfirmed or id in stops:
                        # A node whose run is still to be stopped waits held:
                        # nothing admits it or its subtree, and a run reaped
                        # meanwhile cannot mark it done. The stop's outcome is
                        # announced once known (finish_cancel).
                        child.update(state="held", hold={"reason": "termination_unconfirmed"},
                                     revision=child["revision"] + 1)
                        if id in unconfirmed:
                            self.store.transition(db, "held", id, child["hold"])
                    else:
                        child.update(state="cancelled", hold=None, outcome=None,
                                     revision=child["revision"] + 1)
                        self.store.transition(db, "cancelled", id)
                if stops:
                    after.append({"target": node["id"], "unconfirmed": unconfirmed,
                                  "stops": {id: [run_id, nodes[id]["revision"]] for id, run_id in stops.items()}})
                if unconfirmed:
                    reply_node = nodes[unconfirmed[0]]
                transition = None
        # Configuration drift does not invalidate accepted assignments. Check
        # the live agent roster only for new or changed assignments, while
        # still validating the whole dependency and containment graph.
        check_agents = {id for id, record in nodes.items()
                        if id not in original or record.get("agent") != original[id].get("agent")}
        model.validate(nodes, config, check_agents=check_agents)
        for id, changed_node in nodes.items():
            if changed_node != original.get(id):
                self.store.save_node(db, changed_node)
        if op == "cancel_node":
            cancelled = model.subtree(nodes, node["id"])
            for id in cancelled:
                db.execute("UPDATE capabilities SET revoked=1 WHERE node_id=?", (id,))
        if transition:
            self.store.transition(db, transition, node["id"])
        self.store.set_meta(db, "plan_revision", plan_revision)
        return self.view(reply_node or node, plan_revision)

    def finish_cancel(self, principal, request_id, reply, pending):
        # Phase two of cancel_node: the request is durable and committed, so
        # the seconds a stop takes hold neither the write lock nor `changed`.
        results = self.engine.stop_runs({id: run for id, (run, _) in pending["stops"].items()})
        with self.changed:
            with self.store.transaction() as db:
                nodes = self.store.nodes(db)
                unconfirmed = list(pending["unconfirmed"])
                for id, (_, revision) in pending["stops"].items():
                    child = nodes[id]
                    # Only the interim hold this cancel set is resolved here;
                    # a node relaunched or closed meanwhile is left alone.
                    if (child["state"] != "held" or child["revision"] != revision
                            or (child.get("hold") or {}).get("reason") != "termination_unconfirmed"):
                        continue
                    if results.get(id):
                        child.update(state="cancelled", hold=None, outcome=None, revision=revision + 1)
                        self.store.save_node(db, child)
                        self.store.transition(db, "cancelled", id)
                    else:
                        unconfirmed.append(id)
                        self.store.transition(db, "held", id, child["hold"])
                shown = nodes[unconfirmed[0]] if unconfirmed else nodes[pending["target"]]
                reply = {**reply, "result": self.view(shown, int(self.store.meta(db, "plan_revision")))}
                db.execute("UPDATE requests SET reply=? WHERE subject=? AND request_id=?",
                           (encode(reply), principal["subject"], request_id))
            self.store.mirror()
            self.changed.notify_all()
        return reply

    def wait(self, request, principal, args):
        timeout = args.get("timeout", 0)
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout < 0:
            invalid("timeout: expected non-negative seconds")
        cursor = args.get("cursor")
        if "cursor" in args and (type(cursor) is not int or cursor < 0):
            invalid("cursor: expected non-negative integer")
        if "node_ids" in args and (not isinstance(args["node_ids"], list)
                                    or any(not isinstance(id, str) for id in args["node_ids"])):
            invalid("node_ids: expected node ids")
        end = time.monotonic() + timeout
        while True:
            with self.store.transaction(write=False) as db:
                # A wait cannot outlive revocation as an authenticated reader.
                principal = self.authenticate(db, request.get("token"))
                nodes = self.store.nodes(db)
                self.authorize(principal, "wait_for_nodes", args, nodes)
                after = (cursor if cursor is not None else
                         int(self.store.meta(db, "ack")) if principal["root"] else 0)
                rows = db.execute("SELECT record FROM notifications WHERE seq>? ORDER BY seq", (after,))
                transitions = [json.loads(row[0]) for row in rows]
                if not principal["root"]:
                    scope = model.subtree(nodes, principal["node_id"])
                    transitions = [t for t in transitions if t["node_id"] in scope]
                if "node_ids" in args:
                    transitions = [t for t in transitions if t["node_id"] in args["node_ids"]]
                top = db.execute("SELECT coalesce(max(seq), 0) FROM notifications").fetchone()[0]
            remaining = end - time.monotonic()
            if transitions or remaining <= 0 or self.stopping:
                return {"transitions": transitions, "next_cursor": max(after, top),
                        "capacity": {}, "scheduler": self.status(nodes)}
            self.changed.wait(min(remaining, 0.5))


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        self.connection.settimeout(60)
        while True:
            try:
                line = self.rfile.readline(1024 * 1024 + 1)
                if not line:
                    return
                if len(line) > 1024 * 1024:
                    reply = {"request_id": None, "ok": False,
                             "error": {"error": "invalid", "problems": ["request: line exceeds 1 MB"]}}
                    self.wfile.write(encode(reply).encode() + b"\n")
                    self.wfile.flush()
                    # Drain the remainder so a sender can finish sendall and
                    # receive the reply without a reset from unread bytes.
                    while line and not line.endswith(b"\n"):
                        line = self.rfile.readline(1024 * 1024 + 1)
                    return
                try:
                    request = json.loads(line)
                except (ValueError, UnicodeError, RecursionError):
                    request = None
                reply = self.server.service.request(request)
                self.wfile.write(encode(reply).encode() + b"\n")
                self.wfile.flush()
            except (OSError, TimeoutError):
                return


class Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    request_queue_size = 64
