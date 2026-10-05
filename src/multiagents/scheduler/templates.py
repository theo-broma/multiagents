"""Host template registry and whole-value parameter expansion (NC-R41/R42)."""
from __future__ import annotations

import copy
import hashlib
import uuid

import yaml

from ..paths import global_config_dir, shipped_defaults_dir
from . import model
from .model import Refused, invalid
from .store import encode


def definition(text):
    try:
        value = yaml.safe_load(text)
    except (yaml.YAMLError, TypeError, ValueError) as exc:
        invalid(f"template: {exc}")
    if (not isinstance(value, dict) or set(value) != {"template", "version", "params", "root"}
            or not isinstance(value["template"], str) or not value["template"].strip()
            or type(value["version"]) is not int or value["version"] < 1
            or not isinstance(value["params"], dict)):
        invalid("template: expected name, positive version, params and root")
    for name, param in value["params"].items():
        if (not isinstance(name, str) or not isinstance(param, dict)
                or set(param) - {"type", "default"}
                or not isinstance(param.get("type"), str)
                or param["type"] not in {"string", "text", "agent", "model", "int", "bool"}):
            invalid("params: unknown parameter type")
        if "default" in param:
            check_value(name, param, param["default"])
    keys = set()
    def check(node):
        if (not isinstance(node, dict) or set(node) - {"key", "kind", "agent", "task", "session",
                "pins", "children", "verdict_child", "max_rounds", "depends_on", "inputs", "locks", "urgent", "window"}
                or not isinstance(node.get("key"), str) or not node["key"]
                or node["key"] in keys or not isinstance(node.get("kind"), str) or node["kind"] not in model.KINDS):
            invalid("root: invalid node or duplicate local key")
        keys.add(node["key"])
        children = node.get("children", [])
        if not isinstance(children, list):
            invalid("children: expected node definitions")
        for child in children:
            check(child)
    check(value["root"])
    def refs(item):
        if isinstance(item, dict):
            if "param" in item:
                if set(item) != {"param"} or not isinstance(item["param"], str) or item["param"] not in value["params"]:
                    invalid("param: unknown parameter reference")
            else:
                for child in item.values():
                    refs(child)
        elif isinstance(item, list):
            for child in item:
                refs(child)
    refs(value["root"])
    return value


def check_value(name, param, value, config=None):
    kind = param["type"]
    expected = int if kind == "int" else bool if kind == "bool" else str
    if type(value) is not expected:
        invalid(f"params.{name}: expected {kind}")
    if kind == "agent" and config is not None and value not in config.agents:
        invalid(f"params.{name}: unknown agent")


# The fields a client may set on an instance's root, as it sets them on a
# composite `create_node` (context/specs/template-instantiation.md, TI-R2/R3).
ROOT_FIELDS = frozenset({"urgent", "window", "depends_on", "inputs", "locks"})


def host_registry():
    result = {}
    # Project files are deliberately absent: only the host registry is used.
    for directory in (shipped_defaults_dir() / "node-templates", global_config_dir() / "node-templates"):
        for path in sorted(directory.glob("*.yaml")):
            template = definition(path.read_text())
            result[template["template"]] = template
    return result


def registry(db, host=None):
    result = dict(host_registry() if host is None else host)
    import json
    for name, raw in db.execute("SELECT name, record FROM templates"):
        result[name] = json.loads(raw)
    return result


def register(db, text, host=None):
    template = definition(text)
    previous = registry(db, host).get(template["template"])
    if previous and template["version"] <= previous["version"]:
        raise Refused("conflict", current_version=previous["version"])
    db.execute("INSERT OR REPLACE INTO templates VALUES (?, ?)", (template["template"], encode(template)))
    return {"name": template["template"], "version": template["version"]}


def materialize(template, supplied, config):
    if not isinstance(supplied, dict) or set(supplied) - set(template["params"]):
        invalid("params: unknown parameter")
    bindings = {}
    for name, param in template["params"].items():
        if name not in supplied and "default" not in param:
            invalid(f"params.{name}: required")
        value = supplied.get(name, param.get("default"))
        check_value(name, param, value, config)
        bindings[name] = value
    def substitute(item):
        if isinstance(item, dict):
            if set(item) == {"param"}:
                return copy.deepcopy(bindings[item["param"]])
            return {k: substitute(v) for k, v in item.items()}
        if isinstance(item, list):
            return [substitute(v) for v in item]
        return item
    return substitute(template["root"]), bindings


def expand(template, supplied, config, subject, nodes, parent=None, root_fields=None):
    expanded, bindings = materialize(template, supplied, config)
    local, made = {}, []
    def create(item, parent):
        fields = {k: copy.deepcopy(v) for k, v in item.items() if k in model.CREATABLE - {"children", "loop"}}
        fields["parent"] = parent
        node = model.create_record(fields, subject)
        while node["id"] in nodes:
            node = model.create_record(fields, subject)
        nodes[node["id"]] = node
        local[item["key"]] = node["id"]
        made.append((node, item))
        node["children"] = [create(c, node["id"])["id"] for c in item.get("children", [])]
        return node
    root = create(expanded, parent)
    root["template"] = {"name": template["template"], "version": template["version"],
                        "sha256": hashlib.sha256(encode(template).encode()).hexdigest(),
                        "instance": uuid.uuid4().hex, "bindings": bindings, "definition": expanded}
    def resolve(key):
        if not isinstance(key, str) or key not in local:
            invalid("template: unknown local node reference")
        return local[key]
    for node, item in made:
        for field in ("depends_on", "inputs"):
            refs = node[field]
            if not isinstance(refs, list):
                invalid(f"{field}: expected references")
            node[field] = [{"node": resolve(r)} if isinstance(r, str) else
                           {**r, "node": resolve(r.get("node"))} if isinstance(r, dict) else
                           invalid(f"{field}: invalid reference") for r in refs]
        if node["kind"] == "loop":
            node["loop"] = {"verdict_child": resolve(item.get("verdict_child")),
                            "max_rounds": item.get("max_rounds"), "rounds_rejected": 0}
    # Client-supplied root fields name nodes of the plan, not template-local
    # keys, so they are applied after expansion and validated with the rest.
    root.update({k: copy.deepcopy(v) for k, v in (root_fields or {}).items() if k in ROOT_FIELDS})
    model.attach(nodes, root)
    model.validate(nodes, config, check_agents={node["id"] for node, _ in made})
    return root
