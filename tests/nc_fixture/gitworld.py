"""M3 test infrastructure: a fixture provider that can also run shell
commands, a `World` whose providers are those, and host-git read helpers.

The extra directive of `ShellFixtureProvider`:

    shell      [str]  `sh -c` each command in the run's working directory, after
                      `write` and before `commit`. Environment: FX_DIR (the
                      provider's private dir, outside every repository), FX_TAG.
    verdict_rpc obj   {"verdict": "approved"|"rejected"} -- send `give_verdict`
                      over the NC-R8 socket with the run's own token. The
                      generation under review is scraped from the prompt
                      (a node id, a 40-hex commit, a `generation` number); this
                      is a GUESS about NC-R66's prompt wording and only the
                      loop-based tests use it.

Nothing here imports a scheduler internal.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nc_fixture import agent as _agent  # noqa: E402
from nc_fixture.agent import FixtureProvider  # noqa: E402
from nc_fixture.world import WAIT_TIMEOUT, World  # noqa: E402

_SHELL = r'''
for cmd in (fx.get("shell") or []):
    subprocess.run(["sh", "-c", cmd], cwd=os.getcwd(), check=False,
                   env=dict(os.environ, FX_DIR=base, FX_TAG=str(fx.get("tag")),
                            GIT_AUTHOR_NAME="fx", GIT_AUTHOR_EMAIL="fx@example.invalid",
                            GIT_COMMITTER_NAME="fx", GIT_COMMITTER_EMAIL="fx@example.invalid"))
vr = fx.get("verdict_rpc")
if vr:
    import socket
    nodes = re.findall(r"nd-[0-9a-f]{{8}}", prompt)
    commits = re.findall(r"\b[0-9a-f]{{40}}\b", prompt)
    seqs = re.findall(r"generation[^0-9]{{0,24}}(\d+)", prompt, re.I)
    args = {{"verdict": vr["verdict"], "findings": vr.get("findings", []),
            "node_id": nodes[0] if nodes else None,
            "generation_seq": int(seqs[0]) if seqs else 1,
            "commit": commits[0] if commits else None}}
    req = {{"op": "give_verdict", "args": args, "request_id": "v-%d" % os.getpid(),
           "token": os.environ.get("MULTIAGENTS_RPC_TOKEN") or mcp_env.get("MULTIAGENTS_RPC_TOKEN")}}
    sock_path = os.environ.get("MULTIAGENTS_RPC_SOCKET") or mcp_env.get("MULTIAGENTS_RPC_SOCKET")
    if sock_path:
        s = socket.socket(socket.AF_UNIX); s.connect(sock_path)
        s.sendall((json.dumps(req) + "\n").encode()); s.recv(65536)
'''

_MARK = '\nif fx.get("commit"):'


class ShellFixtureProvider(FixtureProvider):
    def __init__(self, tmp: Path, name: str, **extra: Any):
        super().__init__(tmp, name, **extra)
        text = _agent._SCRIPT.replace(_MARK, _SHELL + _MARK, 1)
        assert text != _agent._SCRIPT
        script = self.dir / "agent.py"
        script.write_text(text.format(python=sys.executable, base=str(self.dir)))


class GitWorld(World):
    """A `World` whose providers run shell directives; agents `coder` (writes)
    and `guard` (writes, `protected.txt` is read-only for it) exist."""

    def __init__(self, tmp_path: Path, monkeypatch, **kw: Any):
        super().__init__(tmp_path, monkeypatch, **kw)
        self.agent("coder", "fx", writes=True)
        self.agent("guard", "fx", writes=True, readonly_paths=["protected.txt"])
        self.base = self.git("symbolic-ref", "--short", "HEAD")

    def provider(self, name: str, **extra: Any) -> FixtureProvider:
        fx = ShellFixtureProvider(self.tmp, name, **extra)
        self.providers[name] = fx
        return fx

    # ------------------------------------------------------------- host git
    def git(self, *args: str, check: bool = True) -> str:
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
                   GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")
        res = subprocess.run(["git", "-C", str(self.root), *args], capture_output=True,
                             text=True, env=env)
        if check and res.returncode != 0:
            raise AssertionError(f"git {args}: {res.stderr}")
        return res.stdout.strip() if res.returncode == 0 else ""

    def commit_on_main(self, path: str, text: str, msg: str = "main change") -> str:
        p = self.root / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", msg)
        return self.main_tip()

    def main_tip(self) -> str:
        return self.git("rev-parse", self.base)

    def nodes_ref(self, node_id: str) -> str:
        return f"refs/heads/nodes/{node_id}"

    def tip(self, node_id: str) -> str | None:
        return self.git("rev-parse", "--verify", "-q", self.nodes_ref(node_id), check=False) or None

    def files(self, ref: str) -> set[str]:
        out = self.git("ls-tree", "-r", "--name-only", ref, check=False)
        return set(out.splitlines())

    def blob(self, ref: str, path: str) -> str | None:
        res = self.git("show", f"{ref}:{path}", check=False)
        return res if path in self.files(ref) else None

    def is_ancestor(self, a: str, b: str) -> bool:
        res = subprocess.run(["git", "-C", str(self.root), "merge-base", "--is-ancestor", a, b])
        return res.returncode == 0

    def all_refs(self) -> dict[str, str]:
        out = self.git("for-each-ref", "--format=%(refname) %(objectname)")
        return {l.split()[0]: l.split()[1] for l in out.splitlines() if l.strip()}

    def exists(self, sha: str) -> bool:
        return subprocess.run(["git", "-C", str(self.root), "cat-file", "-e", sha + "^{commit}"],
                              capture_output=True).returncode == 0

    def fx_file(self, name: str) -> str:
        return (self.fx.dir / name).read_text()

    # ------------------------------------------------------------ node helpers
    def done(self, node_id: str, timeout: float = WAIT_TIMEOUT) -> dict:
        return self.wait_state(node_id, "done", timeout)

    def coder(self, tag: str, files: dict[str, str] | None = None, *, agent: str = "coder",
              commit: bool = True, **fields: Any) -> str:
        fx = fields.pop("fx", {})
        if files:
            fx.setdefault("write", files)
        if commit:
            fx.setdefault("commit", f"{tag} work")
        return self.simple(tag, agent, fx=fx, **fields)

    def group(self, children: list[str], **fields: Any) -> str:
        reply = self.create({"kind": "group", "children": children, **fields})
        assert reply.get("ok"), reply
        from nc_fixture.world import unwrap
        return unwrap(reply["result"])["id"]

    def generations(self, node_id: str) -> list[dict]:
        return list(self.get(node_id).get("generations") or [])
