"""A scriptable workload for the slice 3 tests (IC-C15..C18): a FakeProvider
whose CLI does what the prompt's `IC-W {json}` says, deterministically, inside
the launched environment (a host process on the reference backend, the
container's main workload on Docker). It is delivered like any provider
binary (`bin` of the provider entry), needs only python3, and declares that it
needs no credentials, network or RPC (D-S3.2).

Ops, run in order (`{"ops": [...]}`):

  {"lines": N, "prefix": "L", "gap": 0.02}   N deterministic provider events
        (stdout, one JSON line each, text `<prefix>NNNN`), `gap` s apart. The
        bytes depend on nothing else: two runs print identical output.
  {"half": "text", "gap": 0.4}   write the first half of one event line, wait
        `gap`, then its second half and the newline (a partial line on the wire).
  {"err": N, "gap": 0.02}        N lines `E0001\n` on stderr.
  {"hold": S}                    sleep S seconds.
  {"exit": C}                    exit with code C (after flushing).
  {"forge": {"pid": P}}          write `wrapper.pid`, `container.pid`,
        `agent.pid` containing P into the cwd, HOME, TMPDIR and the run
        directory, wherever it can.
  {"setsid_child": S, "tag": T}  start a child in its OWN session that sleeps
        S seconds, after writing its pid into `$TMPDIR/child.pid`; its command
        line carries the string T, so a test can look for it.
  {"alloc_mb": M, "hold": S}     touch M MiB of memory, then sleep S.
  {"raw": "text"}                write `text` to stdout with no newline.
  {"commit": "path"}             write `<cwd>/path` and commit it on the checked-out branch.
  {"whoami": true}               report `WHOAMI {"uid", "gid", "groups", "umask", "pids"}`
        (pids: how many processes its /proc shows) as an event line.
  {"paths": [p, ...]}            report `PATHS {path: exists}` as an event line.
  {"net": "host:port"}           try to connect (2 s) and report `NET {"ok": bool}`.
  {"write": path, "mode": ...}   create the file with default umask and report its mode
        as `WROTE {"path", "mode", "dir_mode"}`.
  {"corrupt_ref": true}         overwrite the loose ref of the checked-out branch (the
        ref the host reads as the attempt's result) with junk, so result capture fails.
  {"marker": "name"}             create `$TMPDIR/name` (a file in the attempt's
        private temp) and `<cwd>/name` (in its repository).
"""
from __future__ import annotations

import json
import stat
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import sc_harness as sc  # noqa: E402

CLI = r'''#!/usr/bin/env python3
import json, os, re, subprocess, sys, time
prompt = sys.stdin.read()
try:
    open(@CALLS@, "a").write(json.dumps({"argv": sys.argv[1:], "cwd": os.getcwd()}) + "\n")
except OSError:
    pass        # inside a container the host's call log is not there
m = re.search(r"IC-W (\{.*\})", prompt)
spec = json.loads(m.group(1)) if m else {"ops": []}
SES = "ses_ic_w"
n = [0]
def out(text):
    sys.stdout.write(text); sys.stdout.flush()
def event(text):
    n[0] += 1
    return json.dumps({"type": "text", "sessionID": SES,
                       "part": {"id": "prt_w_%d" % n[0], "type": "text", "text": text, "sessionID": SES}}) + "\n"
code = 0
for op in spec["ops"]:
    if "lines" in op:
        for i in range(1, op["lines"] + 1):
            out(event("%s%04d" % (op.get("prefix", "L"), i)))
            time.sleep(op.get("gap", 0.02))
    elif "half" in op:
        line = event(op["half"])
        cut = len(line) // 2
        out(line[:cut]); time.sleep(op.get("gap", 0.4)); out(line[cut:])
    elif "err" in op:
        for i in range(1, op["err"] + 1):
            sys.stderr.write("E%04d\n" % i); sys.stderr.flush()
            time.sleep(op.get("gap", 0.02))
    elif "hold" in op and "alloc_mb" not in op:
        time.sleep(op["hold"])
    elif "exit" in op:
        code = op["exit"]; break
    elif "forge" in op:
        for d in (os.getcwd(), os.environ.get("HOME", ""), os.environ.get("TMPDIR", ""),
                  os.environ.get("MULTIAGENTS_RUN_DIR", "")):
            for name in ("wrapper.pid", "container.pid", "agent.pid"):
                try:
                    if d:
                        open(os.path.join(d, name), "w").write("%d\n" % op["forge"]["pid"])
                except OSError:
                    pass
    elif "setsid_child" in op:
        tmp = os.environ.get("TMPDIR") or "/tmp"
        code_ = ("import os,time;open(%r,'w').write(str(os.getpid()));time.sleep(%d);%r" %
                 (os.path.join(tmp, "child.pid"), op["setsid_child"], op.get("tag", "")))
        subprocess.Popen([sys.executable, "-c", code_], start_new_session=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(0.3)
    elif "alloc_mb" in op:
        blob = bytearray(op["alloc_mb"] * 1024 * 1024)
        for i in range(0, len(blob), 4096):
            blob[i] = 1
        time.sleep(op.get("hold", 0))
    elif "raw" in op:
        out(op["raw"])
    elif "commit" in op:
        env = dict(os.environ, GIT_AUTHOR_NAME="ic", GIT_AUTHOR_EMAIL="ic@example.invalid",
                   GIT_COMMITTER_NAME="ic", GIT_COMMITTER_EMAIL="ic@example.invalid")
        open(os.path.join(os.getcwd(), op["commit"]), "w").write("committed by the workload\n")
        subprocess.run(["git", "add", op["commit"]], env=env, check=False)
        subprocess.run(["git", "commit", "-q", "-m", "workload: " + op["commit"]], env=env, check=False)
    elif "whoami" in op:
        um = os.umask(0); os.umask(um)
        out(event("WHOAMI " + json.dumps({"uid": os.getuid(), "gid": os.getgid(), "groups": os.getgroups(),
                                          "umask": um, "pids": sum(1 for d in os.listdir("/proc") if d.isdigit())})))
    elif "paths" in op:
        out(event("PATHS " + json.dumps({p: os.path.exists(p) for p in op["paths"]})))
    elif "net" in op:
        import socket
        host, _, port = op["net"].rpartition(":")
        try:
            socket.create_connection((host, int(port)), timeout=2).close()
            ok = True
        except Exception:
            ok = False
        out(event("NET " + json.dumps({"ok": ok})))
    elif "write" in op:
        path = op["write"]
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, "w").write("x")
        out(event("WROTE " + json.dumps({"path": path, "mode": os.stat(path).st_mode & 0o7777,
                                         "dir_mode": os.stat(os.path.dirname(path)).st_mode & 0o7777})))
    elif op.get("corrupt_ref"):
        head = open(".git/HEAD").read().strip()
        assert head.startswith("ref: refs/heads/"), head
        open(".git/" + head[len("ref: "):], "w").write("x")
        out(event("CORRUPTED " + head))
    elif "marker" in op:
        for d in (os.environ.get("TMPDIR") or "/tmp", os.getcwd()):
            open(os.path.join(d, op["marker"]), "w").write("x")
sys.stdout.flush()
sys.exit(code)
'''

NEEDS_KEY = "needs"      # K17.2 provider needs declaration


class WorkloadProvider(sc.FakeProvider):
    def __init__(self, tmp: Path, name: str, **extra):
        super().__init__(tmp, name, **extra)
        script = Path(self.entry["bin"])
        script.write_text(CLI.replace("@CALLS@", repr(str(self.calls))))
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        self.entry[NEEDS_KEY] = {"credentials": False, "network": False, "rpc": False}


def guest_runnable(entry: dict) -> dict:
    """Make a script provider runnable in a container guest. The entry's `bin`
    is a host path (in the test's tmp dir) that is not mounted into the guest,
    where only the attempt's checkout, HOME and TMPDIR exist. The interpreter
    is in the image, so run it with the script's text: `python3 -c SCRIPT ...`
    (the script sees the same `sys.argv[1:]`, the prompt arrives on stdin)."""
    text = Path(entry["bin"]).read_text()
    spawn = dict(entry["spawn"])
    spawn["args"] = ["-c", text, *spawn["args"]]
    entry["bin"], entry["spawn"] = "python3", spawn
    return entry


def task(*ops: dict) -> str:
    """The task text that tells the workload what to do."""
    return "IC-W " + json.dumps({"ops": list(ops)})


def reports(text: str, tag: str) -> list[dict]:
    """The `<tag> {json}` event lines a workload printed, in order."""
    out = []
    for line in text.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        said = (event.get("part") or {}).get("text", "")
        if said.startswith(tag + " "):
            out.append(json.loads(said[len(tag) + 1:]))
    return out


def lines_stdout(n: int, prefix: str = "L") -> bytes:
    """What `{"lines": n}` prints, byte for byte."""
    out = b""
    for i in range(1, n + 1):
        out += (json.dumps({"type": "text", "sessionID": "ses_ic_w",
                            "part": {"id": "prt_w_%d" % i, "type": "text", "text": "%s%04d" % (prefix, i),
                                     "sessionID": "ses_ic_w"}}) + "\n").encode()
    return out
