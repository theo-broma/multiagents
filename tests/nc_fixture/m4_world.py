"""World for the M4 tests: `nc_fixture.world.World` whose providers are
`M4Provider`s (queue + verdict through the RPC), plus plan-building helpers.

Assumptions where the contract is silent (loose on purpose):
- `register_template` takes its YAML text under one of `yaml`/`text`/`template`.
- `instantiate_template` returns the top-level node (or `{node: ...}`, or
  `{root: id}`); children are read with `get_node(...)["children"]`.
- a node op's reply carries the node; an `update_node`/`relaunch_node`/
  `close_node` needs the node's current `revision`.
- the `list_templates` result mentions templates by name somewhere in it.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nc_fixture.m4_agent import M4Provider  # noqa: E402
from nc_fixture.world import World, blocked_codes, err_code, unwrap  # noqa: E402

__all__ = ["M4World", "blocked_codes", "err_code", "unwrap", "M4Provider"]


class M4World(World):
    def provider(self, name, **extra):
        fx = M4Provider(self.tmp, name, self.sock, **extra)
        self.providers[name] = fx
        return fx

    # ------------------------------------------------------------ templates
    def register(self, text: str) -> dict:
        last = {}
        for key in ("yaml", "text", "template"):
            last = self.rpc("register_template", {key: text})
            if last.get("ok") or err_code(last) not in (None, "invalid"):
                return last
        return last

    def register_ok(self, text: str) -> None:
        reply = self.register(text)
        assert reply.get("ok"), reply

    def instantiate(self, name: str, params: dict | None = None, **kw) -> dict:
        return self.rpc("instantiate_template", {"name": name, "params": params or {}, **kw})

    def instantiate_ok(self, name: str, params: dict | None = None, **kw) -> str:
        reply = self.instantiate(name, params, **kw)
        assert reply.get("ok"), reply
        top = unwrap(reply["result"])
        return top["id"] if "id" in top else top["root"]

    # --------------------------------------------------------------- plans
    def comp(self, kind: str, kids: list[str], **fields) -> str:
        reply = self.create({"kind": kind, "children": kids, **fields})
        assert reply.get("ok"), reply
        return unwrap(reply["result"])["id"]

    def mkloop(self, max_rounds: int, worker: str = "wk", reviewer: str = "rv",
               **fields) -> tuple[str, str, str]:
        wk = self.simple("W", worker, prose="do the work")
        rv = self.simple("R", reviewer, prose="review the work")
        loop = self.comp("loop", [wk, rv], loop={"verdict_child": rv, "max_rounds": max_rounds},
                         **fields)
        return loop, wk, rv

    def children(self, node_id: str) -> list[str]:
        return list(self.get(node_id)["children"])

    def by_agent(self, top: str, agent: str) -> list[str]:
        out = []
        for c in self.children(top):
            n = self.get(c)
            if n["kind"] == "simple":
                if n["agent"] == agent:
                    out.append(c)
            else:
                out += self.by_agent(c, agent)
        return out

    def wait_held(self, node_id: str, reason: str, timeout: float = 60) -> dict:
        return self.until(lambda: (n := self.get(node_id))["state"] == "held"
                          and (n["hold"] or {}).get("reason") == reason and n,
                          timeout, what=f"{node_id} held for {reason}")

    def root_op(self, op: str, node_id: str, **args) -> dict:
        rev = self.get(node_id)["revision"]
        return self.rpc(op, {"id": node_id, "revision": rev, **args})

    # ----------------------------------------------------------------- git
    def git(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-C", str(self.root), *args], capture_output=True, text=True)

    def show(self, rev_path: str) -> str | None:
        r = self.git("show", rev_path)
        return r.stdout if r.returncode == 0 else None

    def token_of(self, fx: M4Provider, tag: str) -> str:
        call = self.wait_spawn(tag, fx)
        return call["mcp_env"].get("MULTIAGENTS_RPC_TOKEN") or call["env_token"]

    def kinds_of(self, node_id: str) -> list[str]:
        return self.transitions(node_id)


def commit_entry(path: str, text: str, msg: str = "work", **more) -> dict:
    return {"write": {path: text}, "commit": msg, **more}


def verdict_entry(verdict: str, findings=None, **more) -> dict:
    v = {"verdict": verdict, "findings": findings or []}
    v.update(more.pop("v", {}))
    return {"verdict": v, **more}


def finding(summary: str, severity: str = "major") -> dict:
    return {"summary": summary, "severity": severity}
