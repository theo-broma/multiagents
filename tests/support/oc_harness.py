"""Shared fixtures for the OC suite (orchestrator-requested compaction).

Contract: `context/specs/orchestrator-compaction-request.md`.

Two surfaces, both public:

- the MCP tools `request_compaction` and `compaction_status`, called as the
  orchestrator's own server runs them (in process, with the environment the
  launched CLI gives its server);
- `driver._run_supervised`, the attached interactive path, holding a real child
  "CLI" (a fake provider script) exactly as the R8f / CW suites do.

`Oc` extends the R8f `Session`. Besides declaring the provider's compaction
capability and mirroring the provider into the project's config layer (the
server loads its providers from there, the driver gets them from `Session`),
it teaches the fake script a few things:

- every record logs its argv and pid (the pid tells a replaced CLI from the
  same one);
- `compact` may answer differently per call (`compacts`);
- any action the contract has not named (`control`, `deliver`, ...) is
  logged as `other` with its whole payload — argv, MULTIAGENTS_* environment,
  stdin and the content of any file an environment value names — and may fail
  on its first call carrying the return prompt (`deliver_fail_first`);
- the launched CLI may keep a child that looks like an unregistered root
  server of its session beneath it (`rogue_server`), which is what makes a
  safe point unreachable.

Nothing waits for real thresholds: `compact_idle_seconds` is large unless a
test says otherwise, so a compaction that happens at all was not the idle
rule's doing.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import sys
import threading
import time
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import p0_context_harness as ch  # noqa: E402
import test_phase0_interactive_compact as ic  # noqa: E402

from multiagents import driver, scripts, server  # noqa: E402
from multiagents.paths import global_config_dir  # noqa: E402
from multiagents.providers import Provider  # noqa: E402
from multiagents.tree import Node, now as tree_now  # noqa: E402

SID = ic.SID
OTHER_SID = "c0ffee00-0000-4000-8000-00000000f8f1"
OVER, UNDER, STOPPED_BY, GRACE = ic.OVER, ic.UNDER, ic.STOPPED_BY, ic.GRACE
FIGURES = ic.FIGURES
BOUND = 1.0              # the longest a test waits for a state it expects
LONG_IDLE = 600          # compact_idle_seconds when the idle rule must not be what fires

CLOSE = {"type": "system", "subtype": "turn_duration"}     # a closed turn (claude's vocabulary)
TURN_STATE = [
    {"when": {"type": "system", "subtype": "turn_duration"}, "idle": True},
    {"when": {"type": "assistant", "message.stop_reason": "end_turn"}, "idle": True},
    {"when": {"type": "assistant"}, "idle": False},
    {"when": {"type": "user"}, "idle": False},
]


def sent(text: str = "wait, one more thing") -> dict:
    """A message the user sent: a user record that is not a tool result."""
    return ch.user(text)


def opens() -> dict:
    """The record a turn starts with (a sent message)."""
    return ch.user("next")


def tool_call(name: str, **args):
    """Call an MCP tool the way the server's own process does."""
    fn = getattr(server, name)
    out = fn(**args)
    if inspect.isawaitable(out):
        out = asyncio.run(out)
    return out


def text(value) -> str:
    return json.dumps(value, default=str)


def declaration(mode) -> dict:
    return {"compaction": {"mode": mode}}


# ------------------------------------------------------- the fake provider --

def patched_fake(source: str) -> str:
    """The R8f fake provider script, taught what the OC suite needs."""
    def swap(old: str, new: str) -> None:
        nonlocal source
        assert old in source, f"the R8f fake changed shape: {old!r}"
        source = source.replace(old, new, 1)

    swap('"cwd": os.getcwd(),', '"cwd": os.getcwd(), "argv": sys.argv, "pid": os.getpid(),')
    # per-call answers for `compact`
    swap('    record("compact")\n    c = ctl.get("compact") or {}',
         '    _n = previous("compact")\n    record("compact")\n'
         '    c = (ctl["compacts"][min(_n, len(ctl["compacts"]) - 1)] if ctl.get("compacts")\n'
         '         else ctl.get("compact")) or {}')
    # a child that looks like an unregistered root server, and its cleanup
    swap('    def on_term(*_):\n        record("sigterm", n=n + 1)\n',
         '    rogue = None\n'
         '    if me.get("rogue_server"):\n'
         '        import subprocess\n'
         '        rogue = subprocess.Popen([sys.executable, "-c",\n'
         '            "import time; time.sleep(30)", "multiagents.server"],\n'
         '            env={**os.environ, "MULTIAGENTS_SESSION_ID": os.environ.get("MULTIAGENTS_SESSION_ID", "")})\n'
         '    def on_term(*_):\n        record("sigterm", n=n + 1)\n'
         '        if rogue: rogue.kill()\n')
    swap('    record("exit", n=n + 1)\n',
         '    if rogue: rogue.kill()\n    record("exit", n=n + 1)\n')
    # everything the contract does not name
    swap('\nsys.exit(64)\n',
         '''
if action not in ("check", "prepare", "launch", "compact"):
    payload = {"argv": sys.argv[1:], "env": dict(os.environ)}
    try:
        import select
        if not sys.stdin.isatty() and select.select([sys.stdin], [], [], 0.2)[0]:
            payload["stdin"] = sys.stdin.read()
    except Exception:
        pass
    files = {}
    for k, v in os.environ.items():
        if k.startswith("MULTIAGENTS_") and os.path.isfile(v) and os.path.getsize(v) < 200000:
            try:
                files[k] = open(v).read()
            except OSError:
                pass
    payload["files"] = files
    blob = json.dumps(payload)
    marks = ctl.get("return_marks") or []
    carries = any(m in blob for m in marks)
    seen = sum(1 for l in log.read_text().splitlines()
               if l.strip() and json.loads(l)["action"] == "other" and json.loads(l).get("carries")) if log.is_file() else 0
    record("other", name=action, payload=blob, carries=carries)
    if carries and ctl.get("deliver_fail_first") and seen == 0:
        sys.exit(1)
    sys.exit(0)
sys.exit(64)
''')
    return source


class Oc(ic.Session):
    """The R8f session with a declared compaction capability and the server
    side of the same project."""

    def __init__(self, tmp_path, monkeypatch, capsys, *, declare=None,
                 limits: dict | None = None, compact_at: int = 8000,
                 transcript_block: bool = True, **kw):
        for tool in ("request_compaction", "compaction_status"):
            assert callable(getattr(server, tool, None)), (
                f"the MCP tool {tool} is not implemented in multiagents.server")
        merged = {"compact_idle_seconds": LONG_IDLE, "compact_grace_seconds": GRACE,
                  **(limits or {})}
        super().__init__(tmp_path, monkeypatch, capsys, compact_at=compact_at,
                         limits=merged, transcript_block=transcript_block, **kw)
        self.script.write_text(patched_fake(self.script.read_text()))
        data = self.config.providers["fakeprov"]
        if transcript_block:
            data["transcript"]["turn_state"] = TURN_STATE
        if declare is not None:
            data.update(declare)
        self.provider = Provider.from_dict("fakeprov", data)
        self.threads: list[threading.Thread] = []
        self.boxes: list[dict] = []
        self.global_dir = tmp_path / "global"
        self.global_dir.mkdir()
        monkeypatch.setenv("MULTIAGENTS_CONFIG_DIR", str(self.global_dir))
        # The server's side of the same project: its providers and roster come
        # from the config layer, not from `Session`'s in-memory Config.
        cfg = self.paths.config
        (cfg / "providers.yaml").write_text(yaml.safe_dump({"providers": {"fakeprov": data}}))
        (cfg / "agents.yaml").write_text(yaml.safe_dump({"agents": {"orchestrator": {
            "provider": "fakeprov", "model": "m", "launch": True, "role": "orchestrator",
            "instructions": []}}}))
        (cfg / "project.yaml").write_text(yaml.safe_dump({"limits": merged}))
        monkeypatch.setenv("MULTIAGENTS_SESSION_ID", SID)
        monkeypatch.setenv("MULTIAGENTS_ROLE", "orchestrator")
        monkeypatch.delenv("MULTIAGENTS_AGENT_ID", raising=False)
        # A live driver, as `driver.run` registers it before it supervises.
        driver._write_pid(self.paths, "orchestrator", os.getpid())
        server._reset()
        server.runner()       # built now: a first call from the helper thread must be fast

    # ------------------------------------------------------------- tools --

    def call(self, tool: str, **kw):
        return tool_call(tool, **kw)

    def request(self, reason: str = "context is stale", **kw) -> dict:
        return self.call("request_compaction", reason=reason, **kw)

    def status(self, request_id: str = "") -> dict:
        return self.call("compaction_status", request_id=request_id)

    def later(self, fn, *, after_launches: int = 1, delay: float = 0.2) -> dict:
        """Run `fn` on a thread once the Nth launch has happened."""
        box: dict = {}

        def go():
            deadline = time.monotonic() + 10
            while len(self.calls("launch")) < after_launches and time.monotonic() < deadline:
                time.sleep(0.02)
            time.sleep(delay)
            try:
                box["value"] = fn()
            except BaseException as exc:          # surfaced by the test
                box["error"] = exc
        self.boxes.append(box)
        thread = threading.Thread(target=go, daemon=True)
        thread.start()
        self.threads.append(thread)
        return box

    def go(self, launches, *, compacts=None, probe: int = 0, **ctl) -> tuple[int, str]:
        """`run`, with the fake's extra controls; joins the helper threads."""
        self.ctl.write_text(json.dumps({
            "transcript": str(self.transcript), "events": str(self.paths.events_file),
            "launches": launches, "probe": probe,
            "compacts": compacts or [{"exit": 0, "stdout": FIGURES + "\n"}],
            "return_marks": ["compacted by the driver"], **ctl}))
        argv, env = scripts.exec_action("fakeprov", self.provider, object(), "launch",
                                        global_config_dir(), self.paths.config,
                                        extra_env=dict(self.context))
        began = time.monotonic()
        code = driver._run_supervised(self.paths, self.config, "orchestrator",
                                      self.spec, self.provider, object(),
                                      dict(self.context), argv, env)
        self.elapsed = time.monotonic() - began
        for thread in self.threads:
            thread.join(15)
        out = self.capsys.readouterr()
        for box in self.boxes:
            if "error" in box:
                raise box["error"]
        return code, out.out + out.err

    # ----------------------------------------------------------- observe --

    def prompt(self, n: int = 1) -> str:
        launches = self.calls("launch")
        assert len(launches) > n, f"launch #{n + 1} never happened: {self.seq()}"
        return launches[n]["env"].get("MULTIAGENTS_RESUME_PROMPT", "")

    def other(self) -> list[dict]:
        return self.calls("other")

    def delivered(self) -> list[dict]:
        return [e for e in self.other() if e.get("carries")]

    def phases(self) -> dict[str, list[dict]]:
        """The driver's audit events, by phase word (D10)."""
        out: dict[str, list[dict]] = {}
        for event in ch.events(self.paths.events_file):
            kind = str(event.get("kind", ""))
            if "compact" not in kind or kind.startswith("compact_"):
                continue
            for word in ("requested", "started", "compacted", "completed", "failed"):
                if word in kind:
                    out.setdefault(word, []).append(event)
        return out

    def wait_status(self, request_id: str, want: str, bound: float = BOUND) -> dict:
        deadline = time.monotonic() + bound
        got = self.status(request_id)
        while got.get("status") != want and time.monotonic() < deadline:
            time.sleep(0.05)
            got = self.status(request_id)
        return got


def agent_running(s: Oc) -> str:
    return s.node("running")
