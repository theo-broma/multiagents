"""Root decisions on settled nodes, without changing their run history."""
from __future__ import annotations

from . import model, sessions
from .engine import attempts
from .model import Refused, invalid


ACTIVE_ATTEMPTS = {"claimed", "launched", "captured", "suspended"}


def active(attempt):
    # TF: a numeric resume deadline is an internal backoff marker, not a
    # pending request — on a finished attempt it blocks nothing. A recorded
    # resume REQUEST (a dict) still holds the node until it is answered.
    return (attempt["state"] in ACTIVE_ATTEMPTS
            or isinstance(attempt.get("resume_pending"), dict))


def recovery(node, journal, enabled):
    """RR-R3: retain an unconsumed source even when its launch failed."""
    pending = node.get("recovery")
    source = pending if pending and pending.get("pending") else None
    if not source and node["runs"]:
        newest = journal.get(node["runs"][-1]["attempt_id"])
        result = (newest or {}).get("result", {})
        if result.get("status") != "done" and result.get("commit") and not result.get("capture_error"):
            source = {"attempt": newest["attempt_id"], "commit": result["commit"],
                      "input_commit": newest["input_commit"], "pending": True}
    node.pop("recovered_from", None)
    if source:
        node["recovery"] = dict(source, pending=enabled)
        if enabled:
            node["recovered_from"] = {k: source[k] for k in ("attempt", "commit")}
        else:
            node["recovery"]["opted_out"] = True


def reopened_round(node, nodes):
    rounds = {}
    parent = nodes.get(node["parent"])
    while parent:
        if parent["kind"] == "loop":
            rounds[parent["id"]] = parent["loop"]["rounds_rejected"]
        parent = nodes.get(parent["parent"])
    if rounds:
        node["relaunch_rounds"] = rounds


def decide(service, db, op, args, nodes, config=None):
    allowed = {"outcome"} if op == "close_node" else {
        "max_rounds", "pins", "new_session", "retry", "task", "loop", "children", "recover_wip"}
    model.check_fields(args, allowed, {"id", "revision"})
    node = nodes.get(args.get("id"))
    if node is None:
        raise Refused("not_found")
    model.revision(args.get("revision"), node["revision"])
    scope = model.subtree(nodes, node["id"])
    journal = attempts(db)
    children = args.get("children", {})
    if op == "relaunch_node":
        if "children" in args and node["kind"] != "loop":
            invalid("children: only loops accept child patches; use task, pins and new_session")
        if node["kind"] == "loop" and "task" in args:
            invalid("task: use children to patch a loop's simple descendants")
        if not isinstance(children, dict):
            invalid("children: expected patches by simple child id")
        for id, patch in children.items():
            if id not in scope or nodes[id]["kind"] != "simple":
                invalid("children: unknown simple descendant")
            if not isinstance(patch, dict) or set(patch) - {"revision", "task", "pins", "new_session"}:
                invalid("children: expected revision, task, pins and new_session")
            model.revision(patch.get("revision"), nodes[id]["revision"])
            if any(a["node_id"] == id and active(a) for a in journal.values()) or nodes[id].get("pending_resume"):
                invalid("children: child has an active attempt")
            if "new_session" in patch and type(patch["new_session"]) is not bool:
                invalid("children.new_session: expected boolean")
    if any(nodes[id].get("completion_pending") or nodes[id].get("git_operation")
           or nodes[id].get("disposal_pending") for id in scope):
        raise Refused("active")
    if (any(a["node_id"] in scope and active(a) for a in journal.values())
            or any(nodes[id].get("pending_resume") for id in scope)):
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
        for id in scope - {node["id"]}:
            child = nodes[id]
            if child["state"] in {"held", "open"}:
                child["closure"] = {"by": "root", "via": node["id"], "outcome": args["outcome"]}
                service.engine.complete(db, child, args["outcome"], approve_generation=False)
                service.store.transition(db, "closed_by_ancestor", id, child["closure"])
        service.engine.complete(db, node, args["outcome"])
        return node
    if node.get("published"):
        raise Refused("published")
    if node.get("disposed"):
        raise Refused("disposed")
    # TF-R2: a pending automatic retry is not a settled node, but a root
    # relaunch wins over it and cancels it — so it is relaunchable too.
    pending_retry = node["kind"] == "simple" and node["state"] == "open" and (
        node.get("retry_pending") or node.get("retry_route") or node.get("launch_failure"))
    if node["kind"] not in {"simple", "loop"} or node["state"] not in {"held", "done"} and not pending_retry:
        invalid("state: relaunch requires a settled simple node or loop")
    if (node["kind"] == "loop" and node["state"] == "held"
            and (node["hold"] or {}).get("reason") not in {"loop_max", "unresolved_round", "run_failed"}):
        invalid("state: relaunch requires loop_max, unresolved_round, run_failed or a done loop")
    recover = args.get("recover_wip", True)
    if type(recover) is not bool:
        invalid("recover_wip: expected boolean")
    failed_loop = node if (node["kind"] == "loop" and node["state"] == "held"
                          and (node["hold"] or {}).get("reason") == "run_failed") else None
    if node["kind"] == "simple":
        parent = nodes.get(node.get("parent"))
        while parent and parent["kind"] != "loop":
            parent = nodes.get(parent.get("parent"))
        if (parent and parent["state"] == "held"
                and (parent["hold"] or {}).get("reason") == "run_failed"
                and parent["hold"].get("detail") == node["id"]):
            failed_loop = parent
    failed_child = None
    if failed_loop:
        failed_child = nodes.get(failed_loop["hold"].get("detail"))
        if (not failed_child or failed_child["kind"] != "simple"
                or failed_child["id"] not in model.subtree(nodes, failed_loop["id"])
                or failed_child["state"] != "done" or failed_child["outcome"] != "failed"):
            invalid("state: run_failed must name a failed child in the current round")
    config = config or service._config
    bindings = sessions.aliases(db)
    pins = args.get("pins", {})
    if not isinstance(pins, dict):
        invalid("pins: expected pins by child id")
    targets = {node["id"]: pins} if node["kind"] == "simple" else dict(pins)
    if any(id not in scope or nodes[id]["kind"] != "simple" for id in targets):
        invalid("pins: unknown simple child")
    new = args.get("new_session", [])
    if not isinstance(new, list) or any(not isinstance(id, str) or id not in scope or not nodes[id]["session"] for id in new):
        invalid("new_session: expected aliased child ids")
    if set(children) & (set(targets) | set(new)):
        invalid("children: a child cannot also appear in pins or new_session")
    new = list(new)
    patched = set()
    for id, patch in children.items():
        child = nodes[id]
        if "task" in patch:
            child["task"] = patch["task"]
        if "pins" in patch:
            targets[id] = patch["pins"]
        if patch.get("new_session"):
            if not child["session"]:
                invalid("children.new_session: expected an aliased child")
            new.append(id)
        patched.add(id)
    new_keys = {sessions.alias_id(nodes[id], nodes) for id in new}
    if node["kind"] == "simple" and not failed_loop:
        key = sessions.alias_id(node, nodes)
        if key:
            new_keys.add(key)
    changed_bindings = set()
    for id, values in targets.items():
        if (not isinstance(values, dict) or set(values) - {"model", "provider", "effort"}
                or any(not isinstance(v, str) or not v for v in values.values())):
            invalid("pins: expected model, provider and effort strings")
        child = nodes[id]
        key = sessions.alias_id(child, nodes)
        bound = bindings.get(key)
        changed = bound and any(values.get(k, bound[k]) != bound[k] for k in ("model", "provider"))
        if changed and key not in new_keys:
            if values.get("provider", bound["provider"]) != bound["provider"] or not config.providers.get(bound["provider"], {}).get("session_model_change", False):
                invalid("pins: changing an alias binding requires new_session")
            bound["model"] = values.get("model", bound["model"])
            changed_bindings.add(key)
        child["pins"].update(values)
        patched.add(id)
    for key in new_keys:
        if any(a.get("alias_id") == key and active(a) for a in journal.values()):
            raise Refused("active_runs")
        # Keep the checkout and dirty-file protection; renew only the binding.
        if key in bindings:
            bindings[key].update(session_id=None, renew=True)
            changed_bindings.add(key)
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
        reset_ids = ({failed_child["id"]} if failed_loop else
                     {id for child in ([node["loop"]["verdict_child"]] if retry == "verdict_child" else node["children"])
                      for id in model.subtree(nodes, child)})
    else:
        if "max_rounds" in args or "loop" in args or "retry" in args:
            invalid("max_rounds/retry: only loops accept round controls")
        if "task" in args:
            node["task"] = args["task"]
        reset_ids = {node["id"]}
    # RR-R2: all argument, alias and whole-plan validation precedes writes.
    model.validate(nodes, config, check_agents=set())
    for id in reset_ids:
        child = nodes[id]
        if child["kind"] == "simple":
            recovery(child, journal, recover)
            reopened_round(child, nodes)
            # TF-R2: a root relaunch resets the retry budget and cancels any
            # pending automatic retry, held quota release or launch failure.
            for field in ("tf_retries", "retry_pending", "retry_route",
                          "launch_failure"):
                child.pop(field, None)
            quota = child.pop("quota_hold", None)
            if quota and quota.get("provider"):
                # An explicit relaunch outranks the account cooldown the
                # exhaustion placed; the budget guards the rest.
                service.engine.runner.tree.clear_quota({quota["provider"]})
    for key in changed_bindings:
        sessions.save_alias(db, bindings[key])
    for id in patched - reset_ids:
        nodes[id]["revision"] += 1
        service.store.save_node(db, nodes[id])
    if failed_loop:
        service.engine.resume_failed(db, failed_loop, failed_child)
    elif node["kind"] == "loop":
        service.engine.reset_round(db, node, nodes, verdict_only=retry == "verdict_child")
    else:
        model.reset(node)
        service.store.save_node(db, node)
    if node["kind"] == "loop":
        sources = [nodes[id].get("recovered_from") for id in reset_ids if nodes[id].get("recovered_from")]
        node.pop("recovered_from", None)
        if sources:
            node["recovered_from"] = sources[0]
        service.store.save_node(db, node)
    for dependent in nodes.values():
        if any(ref["node"] == node["id"] for ref in dependent["depends_on"] + dependent["inputs"]) and dependent["runs"]:
            service.store.transition(db, "dependency_reopened", dependent["id"], {"node": node["id"]})
    if not failed_loop:
        service.store.transition(db, "reopened", node["id"])
    return node
