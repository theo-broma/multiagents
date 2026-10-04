"""The fixture agent of NC-R70: a deterministic fake provider CLI.

Test infrastructure for the Phase 7 part 1 tests (`tests/test_nc_*`), never
shipped. It stands in for an agent CLI (`bin:` of a provider block copied from
the shipped `opencode` block, so spawn args, resume args, stream rules and the
MCP registration are the product's own). What it does is scripted by its TASK:
one line `FX {json}` anywhere in the prompt.

Directives (all optional):

    tag        str    identifies this run in the calls log (`FixtureProvider.by_tag`)
    session    str    the session id to emit (default `ses_<pid>`); a resumed run
                      (`-s <id>` in argv) always keeps the id it was given
    gate       str    wait until `World.open_gate(name)` / `FixtureProvider.open_gate`
    gate_after str    like `gate`, but only after the final text has been printed
                      (a run that "said its piece" and has not exited yet)
    sleep      float  sleep this long before finishing
    write      {path: text}   write files in the working directory
    commit     str    `git add -A && git commit -m <msg>` in the working directory
    text       str    the final message (default "done")
    exit       int    exit code after the final text (default 0)
    crash      bool   die with exit 1 right after the opening events, no final text
    hang       bool   after the opening events, sleep until a signal ends it
    ignore_term bool  ignore SIGTERM (a stop is only confirmed by SIGKILL)
    watch_pid  int    record whether this pid is alive at the moment we start
    verdict    obj    (stub for M4) recorded in the calls log, nothing is sent

Every invocation appends one JSON line to `calls.jsonl`:

    {tag, pid, argv, prompt, cwd, t, resume (the -s id or null), session,
     mcp_env (the environment of the MCP registration, e.g. MULTIAGENTS_RPC_TOKEN),
     watch_alive}

and, when it reaches its natural end, one line to `done.jsonl` with the same
tag and pid. A run that is stopped never writes a done line.
"""
from __future__ import annotations

import json
import os
import stat
import sys
import time
from pathlib import Path
from typing import Any

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from multiagents.paths import shipped_defaults_dir  # noqa: E402

_SCRIPT = r'''#!{python}
import json, os, re, signal, subprocess, sys, time
base = {base!r}
argv = sys.argv[1:]
prompt = sys.stdin.read()
m = re.search(r"^FX (\{{.*\}})\s*$", prompt, re.M)
fx = json.loads(m.group(1)) if m else {{}}
resume = argv[argv.index("-s") + 1] if "-s" in argv else None
session = resume or fx.get("session") or "ses_%d" % os.getpid()
mcp_env = {{}}
cfg = os.environ.get("OPENCODE_CONFIG")
if cfg and os.path.isfile(cfg):
    try:
        servers = json.load(open(cfg)).get("mcp", {{}})
        mcp_env = dict(servers.get("multiagents", {{}}).get("environment") or {{}})
    except Exception:
        pass
watch = fx.get("watch_pid")
watch_alive = None
if watch:
    try:
        os.kill(watch, 0)
        stat = open("/proc/%d/stat" % watch).read().split(")")[-1].split()[0]
        watch_alive = stat != "Z"
    except (ProcessLookupError, FileNotFoundError):
        watch_alive = False
    except PermissionError:
        watch_alive = True
with open(base + "/calls.jsonl", "a") as f:
    f.write(json.dumps({{"tag": fx.get("tag"), "pid": os.getpid(), "argv": argv,
                        "prompt": prompt, "cwd": os.getcwd(), "t": time.time(),
                        "resume": resume, "session": session, "mcp_env": mcp_env,
                        "watch_alive": watch_alive, "verdict": fx.get("verdict"),
                        "env_token": os.environ.get("MULTIAGENTS_RPC_TOKEN")}}) + "\n")
if fx.get("ignore_term"):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
def emit(kind, part):
    ev = {{"type": kind, "sessionID": session, "part": dict(part, sessionID=session)}}
    print(json.dumps(ev)); sys.stdout.flush()
def wait_gate(name):
    path = os.path.join(base, "gate." + name)
    while not os.path.exists(path):
        time.sleep(0.05)
emit("step_start", {{"id": "prt_s%d" % os.getpid(), "type": "step-start"}})
emit("step_finish", {{"id": "prt_f%d" % os.getpid(), "type": "step-finish",
                     "reason": "tool-calls", "cost": 0,
                     "tokens": {{"input": 1, "output": 1, "reasoning": 0,
                                "cache": {{"read": 0, "write": 0}}}}}})
emit("text", {{"id": "prt_w%d" % os.getpid(), "type": "text", "text": "working"}})
if fx.get("crash"):
    os._exit(1)
if fx.get("hang"):
    while True:
        time.sleep(0.5)
if fx.get("gate"):
    wait_gate(fx["gate"])
if fx.get("sleep"):
    time.sleep(fx["sleep"])
for rel, text in (fx.get("write") or {{}}).items():
    os.makedirs(os.path.dirname(os.path.join(os.getcwd(), rel)) or ".", exist_ok=True)
    open(os.path.join(os.getcwd(), rel), "w").write(text)
if fx.get("commit"):
    env = dict(os.environ, GIT_AUTHOR_NAME="fx", GIT_AUTHOR_EMAIL="fx@example.invalid",
               GIT_COMMITTER_NAME="fx", GIT_COMMITTER_EMAIL="fx@example.invalid")
    subprocess.run(["git", "add", "-A"], env=env, check=False)
    subprocess.run(["git", "commit", "-q", "-m", fx["commit"]], env=env, check=False)
emit("text", {{"id": "prt_t%d" % os.getpid(), "type": "text", "text": fx.get("text", "done")}})
if fx.get("gate_after"):
    wait_gate(fx["gate_after"])
with open(base + "/done.jsonl", "a") as f:
    f.write(json.dumps({{"tag": fx.get("tag"), "pid": os.getpid(), "t": time.time()}}) + "\n")
sys.exit(fx.get("exit", 0))
'''


def task(tag: str, prose: str = "do the work", **directives: Any) -> str:
    """A task text carrying the scripted behaviour."""
    return f"{prose} [{tag}]\nFX {json.dumps({'tag': tag, **directives})}"


def shipped_opencode() -> dict[str, Any]:
    raw = yaml.safe_load((shipped_defaults_dir() / "providers.yaml").read_text())
    return json.loads(json.dumps(raw["providers"]["opencode"]))


class FixtureProvider:
    """One fixture provider. `.entry` is its `providers.yaml` block."""

    def __init__(self, tmp: Path, name: str, **extra: Any):
        self.name = name
        self.dir = tmp / f"fx-{name}"
        self.dir.mkdir(parents=True)
        (self.dir / "calls.jsonl").write_text("")
        script = self.dir / "agent.py"
        script.write_text(_SCRIPT.format(python=sys.executable, base=str(self.dir)))
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        entry = shipped_opencode()
        for key in ("auth", "home_links", "bin_search", "models_cmd", "models_parse", "notes"):
            entry.pop(key, None)
        entry["bin"] = str(script)
        entry["models_include"] = [f"{name}/*"]
        entry.update(extra)
        self.entry = entry

    # -- scripting -----------------------------------------------------------
    def open_gate(self, name: str) -> None:
        (self.dir / f"gate.{name}").write_text("open")

    def open_all_gates(self, names) -> None:
        for n in names:
            self.open_gate(n)

    # -- observing -----------------------------------------------------------
    def calls(self) -> list[dict]:
        out = []
        for line in (self.dir / "calls.jsonl").read_text().splitlines():
            if line.strip():
                out.append(json.loads(line))
        return out

    def by_tag(self, tag: str) -> list[dict]:
        return [c for c in self.calls() if c.get("tag") == tag]

    def spawns(self) -> int:
        return len(self.calls())

    def done(self) -> list[dict]:
        path = self.dir / "done.jsonl"
        if not path.is_file():
            return []
        return [json.loads(x) for x in path.read_text().splitlines() if x.strip()]

    def done_tags(self) -> list[str]:
        return [d["tag"] for d in self.done()]

    def pids(self) -> list[int]:
        return [c["pid"] for c in self.calls()]


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        return Path(f"/proc/{pid}/stat").read_text().split(")")[-1].split()[0] != "Z"
    except OSError:
        return False
