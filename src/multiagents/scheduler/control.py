"""Root decisions on settled nodes, without changing their run history."""
from __future__ import annotations

from . import model, sessions
from .engine import attempts
from .model import Refused, invalid


def decide(service, db, op, args, nodes, config=None):
    allowed = {"outcome"} if op == "close_node" else {"max_rounds", "pins", "new_session", "retry", "task", "loop"}
    model.check_fields(args, allowed, {"id", "revision"})
    node = nodes.get(args.get("id"))
    if node is None:
        raise Refused("not_found")
    model.revision(args.get("revision"), node["revision"])
    scope = model.subtree(nodes, node["id"])
    if any(nodes[id].get("completion_pending") or nodes[id].get("git_operation")
           or nodes[id].get("disposal_pending") for id in scope):
        raise Refused("active")
    if any(a["node_id"] in scope and a["state"] in {"claimed", "launched", "captured"} for a in attempts(db).values()):
        raise Refused("active_runs" if op == "close_node" else "active")
    if op == "close_node":
        if not isinstance(args.get("outcome"), str) or args["outcome"] not in {"approved", "failed", "exhausted"}:
            invalid("outcome: expected approved, failed or exhausted")
        if node["state"] not in {"held", "open"}:
            invalid("state: close requires a held or open node")
        node["closed_by"] = "root"
        node["closure"] = {"by": "root", "outcome": args["outcome"]}
        if args["outcome"] == "approved" and node["generations"]:
            generation = node["generations"][-1]
            node["closure"]["generation"] = {k: generation[k] for k in ("seq", "run_id", "commit")}
        service.engine.complete(db, node, args["outcome"])
        return node
    if node.get("published"):
        raise Refused("published")
    if node.get("disposed"):
        raise Refused("disposed")
    if node["kind"] not in {"simple", "loop"} or node["state"] not in {"held", "done"}:
        invalid("state: relaunch requires a settled simple node or loop")
    if (node["kind"] == "loop" and node["state"] == "held"
            and (node["hold"] or {}).get("reason") not in {"loop_max", "unresolved_round"}):
        invalid("state: relaunch requires loop_max, unresolved_round or a done loop")
    config = config or service._config
    bindings = sessions.aliases(db)
    pins = args.get("pins", {})
    if not isinstance(pins, dict):
        invalid("pins: expected pins by child id")
    targets = {node["id"]: pins} if node["kind"] == "simple" else pins
    if any(id not in scope or nodes[id]["kind"] != "simple" for id in targets):
        invalid("pins: unknown simple child")
    new = args.get("new_session", [])
    if not isinstance(new, list) or any(not isinstance(id, str) or id not in scope or not nodes[id]["session"] for id in new):
        invalid("new_session: expected aliased child ids")
    new_keys = {sessions.alias_id(nodes[id], nodes) for id in new}
    for id, values in targets.items():
        if not isinstance(values, dict) or set(values) - {"model", "provider", "effort"}:
            invalid("pins: expected model, provider and effort")
        child = nodes[id]
        key = sessions.alias_id(child, nodes)
        bound = bindings.get(key)
        changed = bound and any(values.get(k, bound[k]) != bound[k] for k in ("model", "provider"))
        if changed and key not in new_keys:
            if values.get("provider", bound["provider"]) != bound["provider"] or not config.providers.get(bound["provider"], {}).get("session_model_change", False):
                invalid("pins: changing an alias binding requires new_session")
            bound["model"] = values.get("model", bound["model"])
            sessions.save_alias(db, bound)
        child["pins"].update(values)
        service.store.save_node(db, child)
    for key in new_keys:
        if any(a.get("alias_id") == key and a["state"] in {"claimed", "launched", "captured"} for a in attempts(db).values()):
            raise Refused("active_runs")
        # Keep the checkout and its dirty-file protection; only the provider
        # session/binding is renewed on its next launch.
        if key in bindings:
            bound = bindings[key]
            bound["session_id"] = None
            bound["renew"] = True
            sessions.save_alias(db, bound)
    if node["kind"] == "loop":
        if "loop" in args:
            invalid("loop: use max_rounds on relaunch")
        maximum = args.get("max_rounds", node["loop"]["max_rounds"])
        if type(maximum) is not int or maximum <= node["loop"]["rounds_rejected"]:
            invalid("max_rounds: must exceed the rejected counter")
        node["loop"]["max_rounds"] = maximum
        retry = args.get("retry", "verdict_child" if (node["hold"] or {}).get("reason") == "unresolved_round" else "round")
        if not isinstance(retry, str) or retry not in {"verdict_child", "round"}:
            invalid("retry: expected verdict_child or round")
        if retry == "verdict_child" and not node["generations"]:
            invalid("retry: no reviewed generation")
        service.engine.reset_round(db, node, nodes, verdict_only=retry == "verdict_child")
    else:
        if "max_rounds" in args or "loop" in args or "retry" in args:
            invalid("max_rounds/retry: only loops accept round controls")
        if "task" in args:
            node["task"] = args["task"]
        model.reset(node)
        service.store.save_node(db, node)
    model.validate(nodes, config, check_agents=set())
    for dependent in nodes.values():
        if any(ref["node"] == node["id"] for ref in dependent["depends_on"] + dependent["inputs"]) and dependent["runs"]:
            service.store.transition(db, "dependency_reopened", dependent["id"], {"node": node["id"]})
    service.store.transition(db, "reopened", node["id"])
    return node
