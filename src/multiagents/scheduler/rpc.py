"""Authenticated node operations and the newline JSON Unix socket service."""
from __future__ import annotations

import copy
from contextlib import nullcontext
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
from . import model, effects, windows
from .model import Refused, invalid
from .store import Store, encode, token_hash

OPS = {"create_node", "update_node", "cancel_node", "get_node", "list_nodes",
       "instantiate_template", "register_template", "list_templates", "wait_for_nodes",
       "ack_nodes", "give_verdict", "relaunch_node", "close_node", "merge_node",
       "dispose_node", "scheduler_status", "start_agent", "admit_run", "admit_agent", "steer_run", "steer_result", "stop_run", "admit_window_resume"}
MUTATING = OPS - {"get_node", "list_nodes", "list_templates", "wait_for_nodes", "scheduler_status", "admit_agent", "steer_result", "admit_window_resume"}
ROOT_ONLY = {"register_template", "ack_nodes", "relaunch_node", "close_node", "merge_node", "dispose_node", "admit_window_resume"}


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

    def status(self, nodes, db=None, at=None, scope=None):
        # `at`, the scheduler's clock, is read before any store transaction:
        # given `db`, the caller read it; else it is read here, before ours.
        if self.engine and db is None and at is None:
            at = self.engine.instant()
        counts = {s: 0 for s in ("open", "running", "suspended", "held", "done", "cancelled")}
        for node in nodes.values():
            counts[node["state"]] += 1
        extra = {}
        if self.engine:
            from .engine import attempts
            if db is not None:
                extra = self.engine.status(nodes, attempts(db), db, at)
            else:
                with self.store.transaction(write=False) as connection:
                    extra = self.engine.status(nodes, attempts(connection), connection, at)
        if scope is not None and "windows" in extra:
            extra["windows"] = [window for window in extra["windows"] if window["node_id"] in scope]
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

    def authorize(self, principal, op, args, nodes, db=None):
        if principal["root"]:
            if op == "give_verdict":
                raise Refused("forbidden")
            return
        subject = principal["subject"]
        if op in {"admit_run", "steer_run", "steer_result", "stop_run"} and args.get("run_id") != subject:
            run = self.engine.runner.tree.get(args.get("run_id")) if self.engine else None
            from .engine import attempts
            if db is None:
                with self.store.transaction(write=False) as connection:
                    journal = attempts(connection)
            else:
                journal = attempts(db)
            target = next((a["node_id"] for a in journal.values()
                           if a["run_id"] == args.get("run_id")), None)
            recorded = self.engine.runner.authority.get(run.id) if run and self.engine.runner.authority else None
            if target not in model.subtree(nodes, principal["node_id"]) and not (
                    recorded and recorded.get("parent") == subject):
                raise Refused("forbidden")
        scope = model.subtree(nodes, principal["node_id"])
        permissions = principal["permissions"]
        if op in ROOT_ONLY:
            raise Refused("forbidden")
        if op == "give_verdict":
            if "verdict" not in permissions:
                raise Refused("forbidden")
            from .engine import attempts
            if db is None:
                with self.store.transaction(write=False) as connection:
                    journal = attempts(connection)
            else:
                journal = attempts(db)
            activation = next((a for a in journal.values()
                               if a["run_id"] == subject and a["state"] in {"claimed", "launched"}), None)
            review = (activation or {}).get("review")
            if not review:
                raise Refused("forbidden")
            scope.add(review["node_id"])
        if op in {"create_node", "start_agent", "admit_agent", "steer_run", "stop_run", "instantiate_template", "update_node", "cancel_node"} and "delegate" not in permissions:
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
            deposited = []
            clock = self.engine.instant() if self.engine else None
            config = self.configuration()
            host_templates, window_template, expanded = None, None, None
            if isinstance(op, str) and op in {"register_template", "instantiate_template", "list_templates"}:
                from . import templates
                host_templates = templates.host_registry()
            with self.store.transaction(write=False) as snapshot:
                window_nodes = self.store.nodes(snapshot)
                if op == "instantiate_template" and isinstance(args, dict) and isinstance(args.get("name"), str):
                    window_template = templates.registry(snapshot, host_templates).get(args["name"])
            if window_template:
                try:
                    expanded, _ = templates.materialize(window_template, args.get("params", {}), config)
                except Refused:
                    # Preserve authentication and argument-error precedence.
                    pass
            windows.prepare(config.project["scheduler"].get("timezone", "Europe/Paris"), [window_nodes, args, expanded])
            # Reads use one SQLite snapshot and never wait behind a writer
            # holding the notification condition while reserving the store.
            with self.changed if isinstance(op, str) and op in MUTATING else nullcontext():
                with self.store.transaction(write=isinstance(op, str) and op in MUTATING) as db:
                    principal = self.authenticate(db, request.get("token"))
                    if not isinstance(op, str) or not isinstance(args, dict):
                        invalid("request: expected op string and args object")
                    validate_arguments(args)
                    principal["request_id"] = request_id
                    principal["host_templates"] = host_templates
                    principal["window_template"] = window_template
                    nodes = self.store.nodes(db)
                    self.authorize(principal, op, args, nodes, db)
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
                        before = set(nodes)
                        result = self.dispatch(db, principal, op, args, nodes, after, clock, config)
                        deposited = [id for id in nodes if id not in before]
                        reply.update(ok=True, result=result)
                        if op in MUTATING:
                            db.execute("INSERT INTO requests VALUES (?, ?, ?, ?)",
                                       (principal["subject"], request_id, payload, encode(reply)))
                if op == "wait_for_nodes":
                    reply.update(ok=True, result=self.wait(request, principal, args))
                elif op in MUTATING:
                    self.store.mirror()
                    self.changed.notify_all()
            if self.engine and op in MUTATING:
                effects.finish_completions(self.engine)
                effects.finish_operations(self.engine)
                self.store.mirror()
                with self.store.transaction(write=False) as db:
                    saved = db.execute("SELECT reply FROM requests WHERE subject=? AND request_id=?",
                                       (principal["subject"], request_id)).fetchone()
                if saved:
                    reply = json.loads(saved[0])
                if reply.get("ok"):
                    self.engine.observe(deposited)
            for pending in after:
                if pending.get("kind") == "spawn":
                    self.engine.spawn(pending["attempt"])
                elif pending.get("kind") == "window_admission":
                    reply["result"] = self.engine.window_launch_admission(pending["run_id"], pending["instant"])
                elif pending.get("kind") == "admit_agent":
                    reply["result"] = self.engine.agent_admission(args, principal)
                else:
                    reply = self.finish_cancel(principal, request_id, reply, pending)
        except Refused as exc:
            reply = {"request_id": reply["request_id"], "ok": False, "error": exc.result}
        except Exception:
            logging.getLogger(__name__).exception("scheduler request failed: %s", request_id)
            reply = {"request_id": reply["request_id"], "ok": False, "error": {"error": "internal"}}
        return reply

    def dispatch(self, db, principal, op, args, nodes, after=None, clock=None, config=None):
        config = config or self._config
        plan_revision = int(self.store.meta(db, "plan_revision"))
        def view(node):
            if self.engine and op in {"get_node", "list_nodes"}:
                from .engine import attempts
                return {**self.engine.view(node, nodes, attempts(db), db, clock), "plan_revision": plan_revision}
            return self.view(node, plan_revision)
        if op == "stop_run":
            from .suspension import operator_stop
            model.check_fields(args, {"run_id"}, set())
            return operator_stop(self.store, db, args.get("run_id"))
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
            admission = self.engine.resume_admission(args.get("run_id"), db, config=config)
            if admission.get("error") or admission.get("blocked"):
                return admission
            attempt = attempts(db)[attempt["attempt_id"]]
            command_id = uuid.uuid4().hex
            attempt.setdefault("steer_commands", {})[command_id] = {"message": args["message"]}
            save_attempt(db, attempt)
            after.append({"kind": "spawn", "attempt": attempt})
            return {"command_id": command_id}
        if op == "admit_agent":
            after.append({"kind": "admit_agent"})
            return {}
        if op == "admit_window_resume":
            model.check_fields(args, {"run_id"}, set())
            after.append({"kind": "window_admission", "run_id": args.get("run_id"), "instant": clock})
            return {}
        if op == "admit_run":
            return self.engine.resume_admission(args.get("run_id"), db, config=config)
        if op == "start_agent":
            allowed = {"agent", "task", "urgent", "model", "timeout", "workdir", "verifies", "budget_tag", "budget_tokens"}
            model.check_fields(args, allowed, set())
            fields = {"kind": "simple", "agent": args.get("agent"), "task": args.get("task"),
                      "urgent": args.get("urgent", False), "plan_revision": plan_revision}
            if args.get("model"):
                fields["pins"] = {"model": args["model"]}
            made = self.dispatch(db, principal, "create_node", fields, nodes, config=config)
            node = nodes[made["id"]]
            node["launch"] = {k: v for k, v in args.items() if k in allowed - {"agent", "task", "urgent", "model"}}
            self.store.save_node(db, node)
            return self.view(node, plan_revision + 1)
        if op == "scheduler_status":
            result = self.status(nodes, db, clock, scope=None if principal["root"] else model.subtree(nodes, principal["node_id"]))
            result["launch_context"] = principal["root"] or bool(
                self.engine and self.engine.runner.tree.get(principal["subject"]))
            return result
        if op == "list_templates":
            from .templates import registry
            return {"templates": [{"name": t["template"], "version": t["version"]} for t in registry(db, principal.get("host_templates")).values()]}
        if op in {"register_template", "instantiate_template"}:
            from . import templates
            if op == "register_template":
                model.check_fields(args, {"yaml"}, set())
                return {**templates.register(db, args.get("yaml"), principal.get("host_templates")), "plan_revision": plan_revision}
            model.check_fields(args, {"name", "params", "parent"}, {"plan_revision"})
            if "plan_revision" in args:
                model.revision(args["plan_revision"], plan_revision)
            name = args.get("name")
            if not isinstance(name, str):
                invalid("name: expected template name")
            template = templates.registry(db, principal.get("host_templates")).get(name)
            if template is None:
                raise Refused("not_found")
            if "window_template" in principal and template != principal["window_template"]:
                raise Refused("conflict", current_version=template["version"])
            original = copy.deepcopy(nodes)
            parent = args.get("parent") or (None if principal["root"] else principal["node_id"])
            root = templates.expand(template, args.get("params", {}), config, principal["subject"], nodes, parent)
            for id, node in nodes.items():
                if node != original.get(id):
                    self.store.save_node(db, node)
                    if id not in original:
                        self.store.transition(db, "created", id)
            plan_revision += 1
            self.store.set_meta(db, "plan_revision", plan_revision)
            return self.view(root, plan_revision)
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
        if op in {"relaunch_node", "close_node"}:
            from .control import decide
            node = decide(self, db, op, args, nodes, config)
            if node.get("completion_pending"):
                node["completion_pending"]["request"] = [principal["subject"], principal["request_id"]]
                self.store.save_node(db, node)
            return view(node)
        if op in {"merge_node", "dispose_node", "give_verdict"}:
            from .engine import attempts
            from ..tree import now
            node = nodes.get(args.get("node_id" if op == "give_verdict" else "id"))
            if node is None:
                raise Refused("not_found")
            if op == "give_verdict":
                journal = attempts(db)
                active = next((a for a in journal.values() if a["run_id"] == principal["subject"]
                               and a["state"] in {"claimed", "launched"}), None)
                review = (active or {}).get("review")
                if not review or nodes[active["node_id"]]["parent"] != node["id"] or node["loop"]["verdict_child"] != active["node_id"]:
                    raise Refused("forbidden")
                if any(args.get(k) != review[k] for k in ("node_id", "generation_seq", "commit")):
                    raise Refused("forbidden")
                if not isinstance(args.get("verdict"), str) or args["verdict"] not in {"approved", "rejected"}:
                    invalid("verdict: expected approved or rejected")
                if node.get("pending_verdict"):
                    raise Refused("conflict")
                model.check_fields(args, {"node_id", "generation_seq", "commit", "verdict", "findings"}, set())
                findings = args.get("findings", [])
                if not isinstance(findings, list) or any(not isinstance(f, dict) or not isinstance(f.get("summary"), str)
                        or not isinstance(f.get("severity"), str) for f in findings):
                    invalid("findings: expected summary and severity records")
                reviewed = next((g for g in node["generations"] if g["seq"] == review["generation_seq"] and g["commit"] == review["commit"]), None)
                if reviewed is None:
                    raise Refused("forbidden")
                # Receipt records the proposal; only successful activation
                # completion can apply it to the reviewed generations.
                node["pending_verdict"] = {"verdict": args["verdict"], "findings": findings,
                                           "attempt_id": active["attempt_id"], **review}
                self.store.transition(db, "verdict", node["id"], {**review, **node["pending_verdict"]})
            elif op == "merge_node":
                model.check_fields(args, {"force"}, {"id", "revision"})
                if "revision" in args:
                    model.revision(args["revision"], node["revision"])
                if node["parent"]:
                    raise Refused("not_top_level")
                if node["state"] != "done":
                    raise Refused("not_done")
                if node["outcome"] not in {"completed", "approved"} and not args.get("force"):
                    raise Refused("not_approved")
                if node.get("published"):
                    return view(node)
                if node.get("disposed"):
                    raise Refused("disposed")
                if node.get("git_operation") or node.get("completion_pending"):
                    raise Refused("active")
                node["git_operation"] = {"kind": "publish", "request": [principal["subject"], principal["request_id"]]}
                self.store.save_node(db, node)
                return view(node)
            else:
                model.check_fields(args, set(), {"id", "revision"})
                model.revision(args.get("revision"), node["revision"])
                scope = model.subtree(nodes, node["id"])
                if any(a["node_id"] in scope and a["state"] in {"claimed", "launched", "captured"}
                       for a in attempts(db).values()):
                    raise Refused("active")
                if op == "dispose_node":
                    from .sessions import alias_id
                    def alias_key(record):
                        return alias_id(record, nodes)
                    aliases = {alias_key(nodes[id]) for id in scope} - {None}
                    if any(n["id"] not in scope and (any(r["node"] in scope for r in n["inputs"])
                           or alias_key(n) in aliases) for n in nodes.values() if not n.get("disposed")):
                        raise Refused("referenced")
                    if any(nodes[id].get("completion_pending") or nodes[id].get("git_operation")
                           or nodes[id].get("disposal_pending") for id in scope):
                        raise Refused("active")
                    refs = set()
                    for id in scope:
                        child = nodes[id]
                        if child.get("branch"):
                            refs.add(child["branch"])
                        refs.update(f"refs/heads/node-generations/{id}/{g['seq']}" for g in child["generations"])
                        child["disposal_pending"] = node["id"]
                        self.store.save_node(db, child)
                    journal = attempts(db)
                    for child in (nodes[id] for id in scope):
                        for run in child["runs"]:
                            for namespace in ("inputs", "results"):
                                refs.add("refs/heads/node-" + namespace + "/" + run["attempt_id"])
                    for activation in journal.values():
                        if activation["node_id"] in scope:
                            for namespace in ("inputs", "results"):
                                refs.add("refs/heads/node-" + namespace + "/" + activation["attempt_id"])
                            refs.update(ref for ref, _ in activation.get("integration", {}).get("refs", []))
                    node["git_operation"] = {"kind": "dispose", "scope": sorted(scope), "refs": sorted(refs),
                        "aliases": sorted(aliases), "request": [principal["subject"], principal["request_id"]]}
                    self.store.save_node(db, node)
                    return view(node)
            node["revision"] += 1
            self.store.save_node(db, node)
            return view(node)
        if op not in {"create_node", "update_node", "cancel_node"}:
            raise Refused("not_implemented")
        if not config.project["scheduler"]["enabled"]:
            raise Refused("scheduler_disabled", pending_nodes=len(self.store.pending()))
        if op == "update_node" and nodes.get(args.get("id"), {}).get("completion_pending"):
            raise Refused("active")
        if any(n.get("disposal_pending") and (n["id"] == args.get("id") or n["id"] == args.get("parent")
               or any(ref["node"] == n["id"] for ref in args.get("inputs", []))) for n in nodes.values()):
            raise Refused("active")
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
                    maximum = args["loop"]["max_rounds"]
                    if type(maximum) is not int or maximum <= node["loop"]["rounds_rejected"]:
                        invalid("loop.max_rounds: must exceed the rejected counter")
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
                if any(nodes[id]["kind"] == "simple" and nodes[id]["state"] in {"running", "suspended"} and id not in managed
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
        if op in {"create_node", "update_node"}:
            from . import sessions
            from .engine import attempts
            sessions.validate_attachments(nodes, original, attempts(db), sessions.aliases(db))
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
                self.authorize(principal, "wait_for_nodes", args, nodes, db)
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
                        "capacity": {}, "scheduler": self.status(nodes, scope=None if principal["root"] else scope)}
            if self.changed.acquire(timeout=min(remaining, 0.5)):
                try:
                    self.changed.wait(min(max(0, end - time.monotonic()), 0.5))
                finally:
                    self.changed.release()


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
