"""Node schema and whole-plan validation, independent of run launching."""
from __future__ import annotations

import re
import secrets

from ..scheduler_config import SchedulerConfigError, validate_setting
from ..tree import now

KINDS = {"simple", "sequence", "loop", "group"}
EDITABLE = {"task", "pins", "depends_on", "inputs", "urgent", "locks", "window",
            "children", "loop", "session"}
CREATABLE = EDITABLE | {"kind", "agent", "parent"}
OWNED = {"state", "hold", "outcome", "revision", "generations", "runs", "bindings",
         "created_at", "created_by", "attempts", "template", "eligible", "blocked",
         "ready", "ready_since", "eligible_since", "active_run", "published", "id"}


class Refused(Exception):
    def __init__(self, error: str, **detail):
        self.result = {"error": error, **detail}


def invalid(problem):
    raise Refused("invalid", problems=[problem])


def check_fields(args, allowed, control):
    for key in args:
        if key not in allowed | control | {"caller", "run_id"}:
            invalid(f"{key}: field is not client-writable")


def revision(value, current):
    if type(value) is not int or value < 0:
        invalid("revision: expected a non-negative integer")
    if value != current:
        raise Refused("conflict", current_revision=current)


def create_record(fields, subject):
    node = dict(id="nd-" + secrets.token_hex(4), parent=None, kind=None, pins={},
                session=None, children=[], loop=None, depends_on=[], inputs=[], urgent=False,
                locks=[], window=None, state="open", hold=None, outcome=None, revision=1,
                created_at=now(), created_by=subject, runs=[], generations=[], template=None)
    node.update({key: value for key, value in fields.items() if key in CREATABLE})
    if isinstance(node["loop"], dict):
        node["loop"] = {**node["loop"], "rounds_rejected": 0}
    return node


def attach(nodes, node, old_children=None):
    """Update both sides of parent/children, refusing adoption from another parent."""
    children = node["children"]
    if not isinstance(children, list) or any(not isinstance(x, str) for x in children):
        invalid("children: expected a list of node ids")
    if len(set(children)) != len(children):
        invalid("children: duplicate child")
    for id in children:
        if id not in nodes:
            invalid(f"children: unknown node {id}")
        child = nodes[id]
        if child["parent"] not in (None, node["id"]):
            invalid(f"children: {id} already has a parent")
        if child["state"] in {"done", "cancelled"} and child["parent"] != node["id"]:
            invalid(f"children: {id} is terminal")
        if child["parent"] != node["id"]:
            child["parent"] = node["id"]
            child["revision"] += 1
    for id in old_children or []:
        if id not in children:
            nodes[id]["parent"] = None
            nodes[id]["revision"] += 1
    parent = node["parent"]
    if parent is not None:
        if not isinstance(parent, str) or parent not in nodes:
            invalid("parent: unknown node")
        if nodes[parent]["state"] in {"done", "cancelled"}:
            invalid("parent: terminal node")
        if node["id"] not in nodes[parent]["children"]:
            nodes[parent]["children"].append(node["id"])
            nodes[parent]["revision"] += 1


def validate_window(window, timezone):
    if window is None:
        return
    if not isinstance(window, dict) or set(window) - {"timezone", "days", "ranges"}:
        invalid("window: expected timezone, days and ranges")
    try:
        validate_setting("timezone", window.get("timezone", timezone))
    except SchedulerConfigError as exc:
        invalid(f"window: {exc}")
    days, ranges = window.get("days"), window.get("ranges")
    if (not isinstance(days, list) or not days
            or any(not isinstance(d, str) or d not in {"mon", "tue", "wed", "thu", "fri", "sat", "sun"} for d in days)):
        invalid("window.days: expected non-empty weekdays")
    if not isinstance(ranges, list) or not ranges:
        invalid("window.ranges: expected non-empty hour ranges")
    for value in ranges:
        match = re.fullmatch(r"(\d{2}):(\d{2})-(\d{2}):(\d{2})", value) if isinstance(value, str) else None
        if not match:
            invalid("window.ranges: expected HH:MM-HH:MM")
        sh, sm, eh, em = map(int, match.groups())
        if sh > 23 or sm > 59 or eh > 24 or em > 59 or (eh == 24 and em != 0):
            invalid("window.ranges: invalid hour or minute")
        if (sh, sm) == (eh, em):
            invalid("window.ranges: start equals end")


def subtree(nodes, root):
    out, pending = set(), [root]
    while pending:
        id = pending.pop()
        if id in out or id not in nodes:
            continue
        out.add(id)
        pending.extend(nodes[id]["children"])
    return out


def validate(nodes, config, *, check_agents=None):
    """Check references, containment, sequence edges and inherited dependencies."""
    graph = {id: set() for id in nodes}
    gates = {id: set() for id in nodes}
    ancestry = {}
    aliases = {}
    for id, node in nodes.items():
        kind = node["kind"]
        if not isinstance(kind, str) or kind not in KINDS:
            invalid("kind: unknown node kind")
        if kind == "simple":
            agent, task = node.get("agent"), node.get("task")
            if (not isinstance(agent, str) or not agent
                    or (check_agents is None or id in check_agents) and agent not in config.agents):
                invalid("agent: unknown or missing agent")
            if not isinstance(task, str) or not task.strip():
                invalid("task: a non-empty task is required")
        elif "agent" in node or "task" in node or node["pins"] or node["session"]:
            invalid("agent/task/pins/session: only valid on simple nodes")
        if type(node["urgent"]) is not bool:
            invalid("urgent: expected boolean")
        if not isinstance(node["locks"], list) or any(not isinstance(x, str) or not x for x in node["locks"]):
            invalid("locks: expected lock names")
        pins = node["pins"]
        if (not isinstance(pins, dict) or set(pins) - {"model", "effort", "provider"}
                or any(not isinstance(v, str) or not v for v in pins.values())):
            invalid("pins: expected model, effort, provider strings")
        validate_window(node["window"], config.project["scheduler"]["timezone"])
        for field in ("depends_on", "inputs"):
            refs = node[field]
            if not isinstance(refs, list):
                invalid(f"{field}: expected a list")
            for ref in refs:
                if not isinstance(ref, dict) or not isinstance(ref.get("node"), str) or ref["node"] not in nodes:
                    invalid(f"{field}: unknown node")
                allowed = {"node", "require"} if field == "depends_on" else {"node", "generation"}
                if set(ref) - allowed:
                    invalid(f"{field}: unknown reference field")
                if field == "depends_on":
                    if not isinstance(ref.get("require", "success"), str) or ref.get("require", "success") not in {"success", "approved", "finished"}:
                        invalid("depends_on.require: unknown requirement")
                    ref.setdefault("require", "success")
                elif "generation" in ref and (type(ref["generation"]) is not int or ref["generation"] < 1):
                    invalid("inputs.generation: expected a positive integer")
                gates[id].add(ref["node"])
        parent = node["parent"]
        if parent is not None and (parent not in nodes or id not in nodes[parent]["children"]):
            invalid("parent: inconsistent containment")
        for child in node["children"]:
            if child not in nodes or nodes[child]["parent"] != id:
                invalid("children: inconsistent containment")
            graph[id].add(child)  # A composite finishes only after its descendants.
        # Check containment before following it for dependency inheritance.
        ancestors, cur = set(), parent
        while cur is not None:
            if cur in ancestors or cur == id:
                invalid("parent: containment cycle")
            ancestors.add(cur)
            cur = nodes[cur]["parent"]
        ancestry[id] = ancestors
        if kind in {"sequence", "loop"}:
            for previous, following in zip(node["children"], node["children"][1:]):
                gates[following].add(previous)
        loop = node["loop"]
        if kind == "loop":
            if not isinstance(loop, dict) or set(loop) - {"verdict_child", "max_rounds", "rounds_rejected"}:
                invalid("loop: invalid specification")
            if not node["children"] or loop.get("verdict_child") != node["children"][-1]:
                invalid("loop.verdict_child: must be the last child")
            maximum = loop.get("max_rounds")
            if type(maximum) is not int or maximum < 1 or maximum <= loop["rounds_rejected"]:
                invalid("loop.max_rounds: must exceed the rejected counter")
        elif loop is not None:
            invalid("loop: only valid on loop nodes")
        alias = node["session"]
        if alias is not None:
            if not isinstance(alias, str) or not alias:
                invalid("session: expected an alias name")
            top = node
            while top["parent"] is not None:
                top = nodes[top["parent"]]
            template = top["template"]
            if not template or not template.get("instance"):
                invalid("session: outside a template instance")
            agent = config.agents.get(node["agent"])
            provider = pins.get("provider") or (agent.provider if agent is not None else None)
            # An existing assignment can outlive its configured agent. Without
            # a declared provider, its family cannot be resolved until M2
            # handles the unavailable assignment; it must remain editable.
            if provider is not None:
                family = config.providers.get(provider, {}).get("family", provider)
                key = (template["instance"], alias)
                if key in aliases and aliases[key] != family:
                    invalid("session: incompatible provider families")
                aliases[key] = family
    # Build all explicit and implicit launch prerequisites before inheriting
    # them: an ancestor may receive its sequence edge later in node order.
    # Completion edges to children are deliberately not inherited, since a
    # composite finishes only after the descendants it gates have finished.
    for id in graph:
        graph[id].update(gates[id])
        for ancestor in ancestry[id]:
            graph[id].update(gates[ancestor])

    active, done = set(), set()
    for root in graph:
        if root in done:
            continue
        active.add(root)
        pending = [(root, iter(graph[root]))]
        while pending:
            id, edges = pending[-1]
            dep = next(edges, None)
            if dep is None:
                pending.pop()
                active.remove(id)
                done.add(id)
            elif dep in active:
                invalid("depends_on: dependency cycle")
            elif dep not in done:
                active.add(dep)
                pending.append((dep, iter(graph[dep])))
