"""Harness for `tests/test_c3_prompt_transport*.py` (contract
`context/specs/c3-prompt-file-transport.md`, PF-R1..PF-R7).

Everything on the path is real: `Runner.start/steer/consult` -> `_launch` ->
the shipped provider block (or a custom one) -> the adapter (codex) ->
`agentwrap` -> a FAKE NATIVE BINARY. The executor is the real LocalExecutor, or
the real DockerExecutor talking to the executing fake `docker` of
`sp_harness` (it runs `docker exec` commands locally, so stdin, argv and files
reach the fake native exactly as a container would hand them).

The fake native is one script. Per invocation it appends a JSON record to a log:
its argv, ALL of stdin as bytes (`sys.stdin.buffer.read()`, base64), and the
bytes of every argv element that names a readable file *at the moment of the
call* (so a file transport is observed even if the file is later removed). It
then prints a minimal, valid end-of-turn event in the stream shape of the
provider family it stands in for, so a turn ends `done`.

Nothing in multiagents is patched except `budget.read_all` (the shipped
providers' budget readers shell out to real CLIs), as `test_h8_prompt_size`
already does, and `DockerExecutor.inside` (pinned False: this suite may itself
run in a container).

# ---------------------------------------------------------------------------
# The ONE place the tests name an implementer's choice
# ---------------------------------------------------------------------------
Config key / value / placeholder names are the implementer's choice
(PF-R1). Everything the suite must spell is in the block below and only there;
change these and nothing else if the implementation picked other names.
"""

from __future__ import annotations

import asyncio
import base64
import copy
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import c3_harness as h                                  # noqa: E402
import sp_harness as sp                                 # noqa: E402
from multiagents import budget as budget_mod            # noqa: E402
from multiagents.config import AgentSpec                # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
SHIPPED = ROOT / "src" / "multiagents" / "defaults" / "providers.yaml"

# ===== IMPLEMENTER'S CHOICE: names the suite must spell (PF-R1) ============
TRANSPORT_OPTION = "prompt_transport"   # the key, as the error of PF-R4 names it
TRANSPORT_ARGV, TRANSPORT_STDIN, TRANSPORT_FILE = "argv", "stdin", "file"
FILE_PLACEHOLDER = "{prompt_file}"      # in spawn args, replaced by the path


def declare_transport(block: dict, transport: str) -> None:
    """Make a provider block declare `transport` (a value above)."""
    block["spawn"][TRANSPORT_OPTION] = transport


def set_read_bound(project: dict, providers: dict, nbytes: int) -> None:
    """Configure the PF-R3 read bound, in UTF-8 bytes, for every provider."""
    project.setdefault("limits", {})["prompt_file_max_bytes"] = nbytes
# ===== end of the implementer's-choice block ===============================

# 200 KiB of mixed ASCII, accented and 4-byte characters, metacharacters
# included, with leading whitespace and trailing newlines. Over 128 KiB in
# bytes AND over it in characters-times-two, so any per-argument limit bites.
META = "$(touch /tmp/pf-pwn) `id` ; | & > < 'q' \"dq\" \\ * ? ! #x {prompt} {prompt_file} %s\n"
UNIT = "plain ascii é à ü ñ 日本語 😀🎉 " + META


CODEX_SESSION_ID = "0199c5a4-7e3b-7c10-8a52-3f6d2b9e41aa"


def session_id_for(kind: str) -> str:
    """The session id the fake native of `kind` emits."""
    return CODEX_SESSION_ID if kind == "codex" else "S-" + kind


def big_text(minimum_bytes: int = 200 * 1024) -> str:
    out, n = [], 0
    while n < minimum_bytes:
        out.append(UNIT)
        n += len(UNIT.encode())
    return "".join(out)


# ---------------------------------------------------------------------------
# the fake native
# ---------------------------------------------------------------------------

FAKE_NATIVE = r'''#!{python}
import base64, json, os, sys
HERE = os.path.dirname(os.path.abspath(__file__))
KIND = {kind!r}
argv = sys.argv[1:]
if KIND == "codex" and "exec" not in argv:
    if "mcp" in argv and "list" in argv:
        print("[]")
    sys.exit(0)
stdin = sys.stdin.buffer.read()
files = {{}}
for a in argv:
    for cand in (a, a.split("=", 1)[-1], a[1:] if a.startswith("@") else a):
        try:
            if os.path.isfile(cand):
                files[a] = base64.b64encode(open(cand, "rb").read()).decode()
                break
        except OSError:
            pass
try:
    behaviour = json.load(open(os.path.join(HERE, "behaviour.json")))
except OSError:
    behaviour = {{}}
n = 0
try:
    n = len(open(os.path.join(HERE, "calls.jsonl")).read().splitlines())
except OSError:
    pass
paths = []
for a in argv:
    for cand in (a, a.split("=", 1)[-1]):
        if os.path.isfile(cand):
            paths.append(cand)
            break
rec = {{"argv": argv, "stdin": base64.b64encode(stdin).decode(), "files": files,
       "paths": paths, "cwd": os.getcwd(), "n": n}}
with open(os.path.join(HERE, "calls.jsonl"), "a") as fh:
    fh.write(json.dumps(rec) + "\n")
sid = "S-" + KIND
if KIND == "codex":
    sid = {codex_sid!r}   # the adapter accepts only UUID session ids
def emit(o):
    sys.stdout.write(json.dumps(o) + "\n"); sys.stdout.flush()
if behaviour.get("hold_first") and n == 0 and paths:
    # A predecessor that is still READING its prompt file after it was told to
    # stop: on TERM it waits, re-reads the file and logs what it now holds.
    import signal, time
    emit({{"type": "step", "session_id": sid}})
    def on_term(*_a):
        time.sleep(behaviour.get("drain_seconds", 1.0))
        with open(os.path.join(HERE, "reread.jsonl"), "a") as fh:
            fh.write(json.dumps({{"path": paths[0], "bytes": base64.b64encode(
                open(paths[0], "rb").read()).decode()}}) + "\n")
        sys.exit(0)
    signal.signal(signal.SIGTERM, on_term)
    time.sleep(60)
def emit(o):
    sys.stdout.write(json.dumps(o) + "\n"); sys.stdout.flush()
sys_exit = behaviour.get("exit_by_call", {{}}).get(str(n), behaviour.get("exit", 0))
if sys_exit != 0 and behaviour.get("silent_failure", True):
    sys.exit(sys_exit)                  # a silent failure: no output at all
if behaviour.get("touch"):
    open(os.path.join(os.getcwd(), behaviour["touch"]), "w").write("x" * n)
if KIND == "claude":
    emit({{"type": "system", "subtype": "init", "session_id": sid}})
    emit({{"type": "assistant", "message": {{"id": "m%d" % n, "model": "x", "content": [
        {{"type": "text", "text": "ok"}}]}}, "session_id": sid}})
    emit({{"type": "result", "subtype": "success", "result": "ok", "session_id": sid,
          "usage": {{}}, "total_cost_usd": 0}})
elif KIND == "opencode":
    emit({{"type": "step_start", "sessionID": sid, "part": {{}}}})
    emit({{"type": "text", "sessionID": sid, "part": {{"text": "ok"}}}})
    emit({{"type": "step_finish", "sessionID": sid,
          "part": {{"reason": "stop", "id": "prt_%d" % n, "tokens": {{"input": 1, "output": 1}}, "cost": 0}}}})
elif KIND == "codex":
    emit({{"type": "thread.started", "thread_id": sid}})
    emit({{"type": "turn.started"}})
    emit({{"type": "item.completed", "item": {{"id": "i", "type": "agent_message", "text": "ok"}}}})
    emit({{"type": "turn.completed", "usage": {{"input_tokens": 1, "output_tokens": 1}}}})
elif KIND == "agy":
    emit({{"event": "init", "init": {{"conversation_id": sid}}}})
    emit({{"event": "result", "result": {{"status": "success", "response": "ok",
          "conversation_id": sid, "usage": {{}}}}}})
else:
    emit({{"type": "result", "subtype": "success", "result": "ok", "session_id": sid}})
sys.exit(sys_exit)
'''

# provider name -> (stream family of the fake, model the shipped block allows)
SHIPPED_PROVIDERS = {
    "claude": ("claude", "sonnet"),
    "codex": ("codex", "gpt-5"),
    "agy": ("agy", "gemini-3.8-flash-low"),
    "opencode": ("opencode", "opencode-go/test-model"),
    # `extends` variants
    "opencode-zai": ("opencode", "zai-coding-plan/glm"),
    "opencode-deepinfra": ("opencode", "deepinfra/test-model"),
    "agy-partner": ("agy", "claude-sonnet-x"),
}


class Native:
    """A fake native binary in its own directory, with its call log."""

    def __init__(self, root: Path, kind: str, name: str = "native"):
        self.dir = root / f"fake-{name}"
        # a versioned layout (<dir>/versions/v1/bin/native): codex's
        # `bin_versions_depth: 3` needs the versions root to be three up.
        bindir = self.dir / "versions" / "v1" / "bin"
        bindir.mkdir(parents=True, exist_ok=True)
        self.path = bindir / "native"
        self.dir = bindir
        self.path.write_text(FAKE_NATIVE.format(python=sys.executable, kind=kind, codex_sid=CODEX_SESSION_ID))
        self.path.chmod(self.path.stat().st_mode | stat.S_IEXEC)
        self.behave()

    def behave(self, **values: Any) -> None:
        (self.dir / "behaviour.json").write_text(json.dumps(values))

    def calls(self) -> list[dict]:
        log = self.dir / "calls.jsonl"
        if not log.is_file():
            return []
        out = []
        for line in log.read_text().splitlines():
            rec = json.loads(line)
            rec["stdin"] = base64.b64decode(rec["stdin"])
            rec["files"] = {k: base64.b64decode(v) for k, v in rec["files"].items()}
            out.append(rec)
        return out


def rereads(native: "Native") -> list[dict]:
    log = native.dir / "reread.jsonl"
    if not log.is_file():
        return []
    out = []
    for line in log.read_text().splitlines():
        rec = json.loads(line)
        rec["bytes"] = base64.b64decode(rec["bytes"])
        out.append(rec)
    return out


# ---------------------------------------------------------------------------
# what reached the native, whatever the stdin framing
# ---------------------------------------------------------------------------

def delivered_by_stdin(call: dict, provider: str) -> list[bytes]:
    """The candidate prompt payloads on stdin. For agy the framing is
    `--input-format stream-json`: every string leaf of every JSON line is a
    candidate (PF-R1a: "the decoded message content must equal the prompt").
    For the others the payload is the raw stdin."""
    raw = call["stdin"]
    if provider.split("-")[0] != "agy" and provider != "agy-partner":
        return [raw]
    leaves: list[bytes] = []

    def walk(v: Any) -> None:
        if isinstance(v, str):
            leaves.append(v.encode("utf-8"))
        elif isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)

    for line in raw.decode("utf-8", "replace").splitlines():
        try:
            walk(json.loads(line))
        except ValueError:
            continue
    return leaves


def delivered_anywhere(call: dict, provider: str = "") -> bytes:
    """Everything the native was handed, however it was handed: stdin
    candidates, the bytes of any file named in argv, and argv itself. For tests
    about WHICH text arrived (PF-R7), not about how."""
    parts = [*delivered_by_stdin(call, provider), *call["files"].values(),
             *(a.encode("utf-8", "surrogateescape") for a in call["argv"])]
    return b"\n\0".join(parts)


def argv_bytes(call: dict) -> int:
    return sum(len(a.encode("utf-8", "surrogateescape")) for a in call["argv"])


# ---------------------------------------------------------------------------
# the project / runner
# ---------------------------------------------------------------------------

def shipped_blocks() -> dict[str, Any]:
    return yaml.safe_load(SHIPPED.read_text())["providers"]


class Rig:
    """A Runner over a throwaway project, one agent per provider (named after
    it), every provider's `bin` pointing at that provider's fake native."""

    def __init__(self, tmp_path: Path, monkeypatch, *, executor: str = "local",
                 names: tuple[str, ...] = (), custom: dict[str, dict] | None = None,
                 project_extra: dict | None = None, bound: int | None = None,
                 mutate=None, conversational: tuple[str, ...] = ()):
        self.tmp = tmp_path
        self.monkeypatch = monkeypatch
        self.executor = executor
        monkeypatch.setattr(budget_mod, "read_all", lambda *a, **k: {})
        raw = shipped_blocks()
        providers: dict[str, Any] = {}
        self.natives: dict[str, Native] = {}
        self.models: dict[str, str] = {}
        for name in names:
            kind, model = SHIPPED_PROVIDERS[name]
            native = Native(tmp_path, kind, name)
            self.natives[name] = native
            self.models[name] = model
        # shipped blocks resolved through `extends`, each given its fake bin
        raw_sel: dict[str, Any] = {}
        need = set(names)
        for name in list(need):
            base = raw[name].get("extends")
            if base:
                need.add(base)
        for name in need:
            blk = copy.deepcopy(raw[name])
            blk["enabled"] = True
            if name in self.natives:
                blk["bin"] = str(self.natives[name].path)
            elif name in raw and "bin" in raw[name]:
                kind = SHIPPED_PROVIDERS[name][0]
                self.natives.setdefault(name, Native(tmp_path, kind, name))
                blk["bin"] = str(self.natives[name].path)
            raw_sel[name] = blk
        for name, blk in (custom or {}).items():
            raw_sel[name] = copy.deepcopy(blk)
        if mutate:
            mutate(raw_sel)
        provs = raw_sel
        agents = {}
        for name in (*names, *(custom or {})):
            model = self.models.get(name, "m1")
            agents[name] = AgentSpec.from_dict(
                name, {"provider": name, "model": model, "can_spawn": False,
                       "description": "t", "instructions": "",
                       "conversational": name in conversational})
        project: dict[str, Any] = {
            "team": "",
            "limits": {"provider_failure_threshold": 100,
                       "startup_failure_threshold": 100,
                       "provider_down_cooldown_seconds": 0.15},
        }
        if executor == "docker":
            project["executor"] = {"kind": "docker", "docker": {"network": "bridge"}}
            self.docker_log = sp.install_fake_docker(tmp_path, monkeypatch)
        else:
            self.docker_log = tmp_path / "docker.log"
        if bound is not None:
            set_read_bound(project, raw_sel, bound)
        for k, v in (project_extra or {}).items():
            if isinstance(v, dict):
                project.setdefault(k, {}).update(v)
            else:
                project[k] = v
        self.runner = h.make_runner(tmp_path / "project", monkeypatch,
                                    providers=provs, agents=agents, project=project)
        # the run dirs of an in-process docker executor must exist as the host
        # sees them; the fake docker runs commands locally.
        if executor == "docker":
            monkeypatch.setattr(type(self.runner.executor(agents[next(iter(agents))])),
                                "inside", lambda self: False, raising=False)
        self.agents = agents
        if "codex" in names:
            # a logged-in codex profile, as the adapter requires for a run
            profile = Path(os.path.expanduser("~")) / ".multiagents" / "profiles" / "codex"
            profile.mkdir(parents=True, exist_ok=True)
            (profile / "auth.json").write_text("{}")

    # -- driving ---------------------------------------------------------

    def run_async(self, coro):
        return asyncio.run(coro)

    async def _settle(self, agent_id: str, timeout: float = 30):
        run = self.runner.runs.get(agent_id)
        if run is not None:
            await asyncio.wait_for(run.done.wait(), timeout)
        # a free retry replaces the Run: wait until the node stops changing
        for _ in range(100):
            await asyncio.sleep(0.05)
            run2 = self.runner.runs.get(agent_id)
            if run2 is run or run2 is None:
                break
            run = run2
            await asyncio.wait_for(run.done.wait(), timeout)

    def start(self, agent: str, task: str, settle: bool = True) -> dict:
        async def go():
            result = await self.runner.start(agent, task)
            if settle and isinstance(result, dict) and result.get("agent_id"):
                await self._settle(result["agent_id"])
            return result
        return self.run_async(go())

    def ensure_transcripts(self) -> None:
        """A provider that declares where its sessions live has them there, as
        the real CLI would have written them; a steer/consult refuses to resume
        a session with no transcript."""
        from multiagents.transcripts import session_transcript
        for nid in list((self.runner.tree.read().get("nodes") or {})):
            node = self.runner.tree.get(nid)
            if node is None or not node.session_id or not node.worktree:
                continue
            provider = self.runner.providers.get(node.provider)
            spec = self.agents.get(node.agent)
            if provider is None or spec is None:
                continue
            path = session_transcript(provider, Path(node.worktree), node.session_id,
                                      self.runner.executor(spec))
            if path is not None and not path.is_file():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps({"type": "user",
                                            "sessionId": node.session_id}) + "\n")

    def steer(self, agent_id: str, message: str) -> dict:
        self.ensure_transcripts()

        async def go():
            try:
                result = await self.runner.steer(agent_id, message)
            except Exception as exc:        # a launch that failed in the core
                return {"agent_id": agent_id, "steered": False, "raised": True,
                        "error": f"{type(exc).__name__}: {exc}"}
            await self._settle(agent_id)
            return result
        return self.run_async(go())

    def consult(self, agent: str, message: str, timeout: int = 60) -> dict:
        self.ensure_transcripts()

        async def go():
            return await self.runner.consult(agent, message, timeout)
        return self.run_async(go())

    def run_dir(self, agent_id: str) -> Path:
        return self.runner.paths.run_dir(agent_id)

    def prompt_md(self, agent_id: str) -> bytes:
        return (self.run_dir(agent_id) / "prompt.md").read_bytes()

    def run_files(self, agent_id: str) -> list[Path]:
        return sorted(p for p in self.run_dir(agent_id).iterdir())

    def node(self, agent_id: str):
        return self.runner.tree.get(agent_id)

    def seed_session(self, agent_id: str, session: str) -> None:
        self.runner.tree.update(agent_id, session_id=session)


def custom_rig(tmp_path: Path, monkeypatch, transport: str | None, args: list[str], *,
               executor: str = "local", name: str = "cust", **kw) -> "Rig":
    block, native = fake_custom(tmp_path, name, transport, args=args)
    rig = Rig(tmp_path, monkeypatch, executor=executor, custom={name: block}, **kw)
    rig.natives[name] = native
    rig.name = name
    return rig


def fake_custom(tmp_path: Path, name: str, transport: str | None, *,
                args: list[str]) -> tuple[dict, Native]:
    """A user's custom provider whose native is the fake, declaring `transport`."""
    native = Native(tmp_path, "custom", name)
    block: dict[str, Any] = {
        "bin": str(native.path), "family": name,
        "spawn": {"args": args, "resume": ["--resume", "{session_id}"]},
        "stream": {"format": "ndjson",
                   "session_id_paths": ["session_id"],
                   "rules": [{"match": {"type": "result"}, "as": "result",
                              "fields": {"status": "subtype", "text": "result"}},
                             {"match": {"type": "step"}, "as": "step", "fields": {}}]},
    }
    if transport is not None:
        declare_transport(block, transport)
    return block, native
