"""M4 extension of the fixture agent (NC-R70): scripted by a per-provider QUEUE
as well as by the task, and able to give a verdict through the scheduler RPC.

Why a queue: the shipped templates fix some tasks (the reviewer of `implement`
has no task parameter), so a run cannot always be scripted by its task. Each
invocation pops the first line of `queue.jsonl` (a JSON directive object, same
vocabulary as `nc_fixture.agent`) and merges it over the task's `FX` line.

Extra directives:
    verdict   {"verdict": "approved"|"rejected", "findings": [...],
               "twice": bool, "seq_offset": int, "omit_commit": bool}
              after the work, call `give_verdict` over the RPC socket with the
              run's own token (MULTIAGENTS_RPC_TOKEN), naming the generation
              found in the prompt. ASSUMPTION (contract silent on the prompt's
              spelling, NC-R31/R66): the prompt states `generation_seq <n>`,
              the 40-hex commit and the node id(s) (`nd-xxxxxxxx`); any
              spelling matching those patterns works. Every `nd-` id in the
              prompt is tried as `node_id` until one is accepted.
    leave     {path: text}  untracked files written AFTER the commit (dirty)

Observations, one JSON line each: `calls.jsonl` (adds `head`, `files`, `model`),
`verdicts.jsonl` ({tag, replies: [reply, ...]}).
"""
from __future__ import annotations

import json
import stat
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from nc_fixture.agent import FixtureProvider, task  # noqa: E402,F401

_SCRIPT = r'''#!{python}
import fcntl, json, os, re, signal, socket, subprocess, sys, time
base = {base!r}
sock_path = {sock!r}
argv = sys.argv[1:]
prompt = sys.stdin.read()
m = re.search(r"^FX (\{{.*\}})\s*$", prompt, re.M)
fx = json.loads(m.group(1)) if m else {{}}
qpath = base + "/queue.jsonl"
if os.path.exists(qpath):
    with open(qpath, "r+") as q:
        fcntl.flock(q, fcntl.LOCK_EX)
        lines = [l for l in q.read().splitlines() if l.strip()]
        if lines:
            fx.update(json.loads(lines[0]))
            q.seek(0); q.truncate(); q.write("".join(l + "\n" for l in lines[1:]))
resume = argv[argv.index("-s") + 1] if "-s" in argv else None
model = argv[argv.index("-m") + 1] if "-m" in argv else None
session = resume or fx.get("session") or "ses_%d" % os.getpid()
mcp_env = {{}}
cfg = os.environ.get("OPENCODE_CONFIG")
if cfg and os.path.isfile(cfg):
    try:
        mcp_env = dict(json.load(open(cfg)).get("mcp", {{}}).get("multiagents", {{}}).get("environment") or {{}})
    except Exception:
        pass
token = mcp_env.get("MULTIAGENTS_RPC_TOKEN") or os.environ.get("MULTIAGENTS_RPC_TOKEN")
def git(*a):
    r = subprocess.run(["git"] + list(a), capture_output=True, text=True)
    return r.stdout.strip()
files = sorted(f for f in os.listdir(".") if f != ".git")
with open(base + "/calls.jsonl", "a") as f:
    f.write(json.dumps({{"tag": fx.get("tag"), "pid": os.getpid(), "argv": argv,
        "prompt": prompt, "cwd": os.getcwd(), "t": time.time(), "resume": resume,
        "session": session, "mcp_env": mcp_env, "env_token": token, "model": model,
        "head": git("rev-parse", "HEAD"), "files": files, "verdict": fx.get("verdict")}}) + "\n")
if fx.get("ignore_term"):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
def emit(kind, part):
    print(json.dumps({{"type": kind, "sessionID": session, "part": dict(part, sessionID=session)}})); sys.stdout.flush()
def wait_gate(name):
    while not os.path.exists(os.path.join(base, "gate." + name)):
        time.sleep(0.05)
emit("step_start", {{"id": "prt_s%d" % os.getpid(), "type": "step-start"}})
emit("step_finish", {{"id": "prt_f%d" % os.getpid(), "type": "step-finish", "reason": "tool-calls", "cost": 0,
    "tokens": {{"input": 1, "output": 1, "reasoning": 0, "cache": {{"read": 0, "write": 0}}}}}})
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
for rel in (fx.get("delete") or []):
    try: os.remove(rel)
    except OSError: pass
if fx.get("commit"):
    env = dict(os.environ, GIT_AUTHOR_NAME="fx", GIT_AUTHOR_EMAIL="fx@example.invalid",
               GIT_COMMITTER_NAME="fx", GIT_COMMITTER_EMAIL="fx@example.invalid")
    subprocess.run(["git", "add", "-A"], env=env); subprocess.run(["git", "commit", "-q", "-m", fx["commit"]], env=env)
for rel, text in (fx.get("leave") or {{}}).items():
    open(rel, "w").write(text)
v = fx.get("verdict")
if v:
    seqs = re.findall(r"generation[_ ]seq\w*\D{{0,6}}(\d+)", prompt)
    commits = re.findall(r"\b[0-9a-f]{{40}}\b", prompt)
    nodes = list(dict.fromkeys(re.findall(r"nd-[0-9a-f]{{8}}", prompt))) or [None]
    seq = (int(seqs[-1]) if seqs else 0) + int(v.get("seq_offset", 0))
    def call(node):
        args = {{"generation_seq": seq, "verdict": v["verdict"], "findings": v.get("findings", [])}}
        if node: args["node_id"] = node
        if commits and not v.get("omit_commit"): args["commit"] = commits[-1]
        req = {{"op": "give_verdict", "token": token, "args": args, "request_id": "v-%s-%s" % (os.getpid(), time.time())}}
        s = socket.socket(socket.AF_UNIX); s.settimeout(20); s.connect(sock_path)
        s.sendall((json.dumps(req) + "\n").encode()); buf = b""
        while not buf.endswith(b"\n"):
            c = s.recv(65536)
            if not c: break
            buf += c
        return json.loads(buf)
    replies = []
    for node in nodes:
        r = call(node); replies.append(r)
        if r.get("ok"): break
    if v.get("twice"):
        replies.append(call(nodes[0]))
    with open(base + "/verdicts.jsonl", "a") as f:
        f.write(json.dumps({{"tag": fx.get("tag"), "replies": replies}}) + "\n")
emit("text", {{"id": "prt_t%d" % os.getpid(), "type": "text", "text": fx.get("text", "done")}})
if fx.get("gate_after"):
    wait_gate(fx["gate_after"])
with open(base + "/done.jsonl", "a") as f:
    f.write(json.dumps({{"tag": fx.get("tag"), "pid": os.getpid(), "t": time.time()}}) + "\n")
sys.exit(fx.get("exit", 0))
'''


class M4Provider(FixtureProvider):
    def __init__(self, tmp: Path, name: str, sock: Path, **extra):
        super().__init__(tmp, name, **extra)
        script = self.dir / "agent.py"
        script.write_text(_SCRIPT.format(python=sys.executable, base=str(self.dir), sock=str(sock)))
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        (self.dir / "queue.jsonl").write_text("")

    def queue(self, *directives: dict) -> None:
        with open(self.dir / "queue.jsonl", "a") as f:
            for d in directives:
                f.write(json.dumps(d) + "\n")

    def verdicts(self) -> list[dict]:
        p = self.dir / "verdicts.jsonl"
        return [json.loads(x) for x in p.read_text().splitlines() if x.strip()] if p.is_file() else []

    def first_reply(self, tag: str) -> dict:
        return next(v for v in self.verdicts() if v["tag"] == tag)["replies"][-1]
