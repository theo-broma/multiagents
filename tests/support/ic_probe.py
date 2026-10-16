"""The IC-R13 probe workload (IC-C6), built on `sc_harness.FakeProvider`.

`ProbeProvider` is a FakeProvider (same opencode-shaped providers.yaml block,
same NDJSON event protocol) whose CLI is replaced by `PROBE_CLI`: a
self-contained python3 script that runs one probe *inside the launched
environment* and prints its result as a text event carrying one line
`IC-REPORT {json}`. Nothing is read from the host test tree or the
FakeProvider's control file at run time: the instruction travels in the prompt
(`IC-PROBE {json}`), which is how any provider receives its task. The script is
delivered like any provider binary (`bin` of the provider entry); it needs only
python3, git and coreutils in the image.

A probe never trusts its own claim of success: each step is reported with the
raw outcome (errors included) and the test decides.

Probes (`probe` in the instruction), all reporting `tag`, `cwd`, `home`, `tmp`:

  plant      plant markers carrying `nonce` in cwd, HOME, TMPDIR and the run
             directory; commit a secret blob on branch `ic-secret`; listen on a
             TCP port; then hold for `hold` seconds, re-reporting integrity
             every second (`phase: "hold"`), including whether anything named
             after `watch` (a rival's nonce) appeared in a cache directory.
  fs         walk the filesystem for `victim` nonce (by name and content); try
             to append to and delete every hit; report own markers (control).
  git        try the victim's blob `sha` and other git directories (cat-file,
             delete or move a ref named ic-secret); report own blob and refs (control).
  net        connect to the victim's `addrs` and `ports`; report own listener
             reachability (control).
  rpc        (also `victim_endpoint`) find the RPC endpoint and token in the environment, call it with
             the own token (control), the victim's token `victim_token`, and
             the own token naming the victim's node `victim_node`.
  cache      write `poison` into every cache directory it can find; report own
             private write/read (control) and where the poison landed.
  ro         attempt writes to the worktree and the Git metadata of a
             `readonly` attempt; report reads and the allowed temp write (control).
  handover   report whether `expect` (relative path) exists with `nonce` in the
             worktree, and whether any file carries the `unrelated` nonce.
  write      write `path` with `nonce` in the worktree and commit it.
"""
from __future__ import annotations

import json
import re
import stat
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import sc_harness as sc  # noqa: E402

PROBE_CLI = r'''#!/usr/bin/env python3
import json, os, re, socket, subprocess, sys, time, hashlib
prompt = sys.stdin.read()
m = re.search(r"IC-PROBE (\{.*\})", prompt)
ins = json.loads(m.group(1)) if m else {}
SES = "ses_ic_%d_%s" % (os.getpid(), __import__("uuid").uuid4().hex[:8])
def emit(kind, part):
    part = dict(part, sessionID=SES)
    print(json.dumps({"type": kind, "sessionID": SES, "part": part})); sys.stdout.flush()
def say(report):
    report.setdefault("tag", ins.get("tag"))
    report.setdefault("probe", ins.get("probe"))
    emit("text", {"id": "prt_%d_%d" % (os.getpid(), time.time_ns()), "type": "text",
                  "text": "IC-REPORT " + json.dumps(report, default=str)})
def sh(*a, cwd=None, env=None):
    try:
        p = subprocess.run(list(a), cwd=cwd, capture_output=True, text=True, timeout=20, env=env)
        return {"rc": p.returncode, "out": p.stdout[-500:], "err": p.stderr[-300:]}
    except Exception as e:
        return {"rc": -1, "err": repr(e)}
def attempt(fn):
    try:
        r = fn(); return {"ok": True, "v": r}
    except Exception as e:
        return {"ok": False, "err": repr(e)}
CWD = os.getcwd(); HOME = os.environ.get("HOME", "")
TMP = os.environ.get("TMPDIR") or "/tmp"
RUND = os.environ.get("MULTIAGENTS_RUN_DIR", "")
GITENV = dict(os.environ, GIT_AUTHOR_NAME="ic", GIT_AUTHOR_EMAIL="ic@example.invalid",
              GIT_COMMITTER_NAME="ic", GIT_COMMITTER_EMAIL="ic@example.invalid")
def common():
    return {"cwd": CWD, "home": HOME, "tmp": TMP, "run_dir": RUND}
def plant(nonce):
    spots = {}
    for label, d in (("cwd", CWD), ("home", HOME), ("tmp", TMP), ("run_dir", RUND)):
        if not d: continue
        p = os.path.join(d, "ic-marker-" + nonce)
        spots[label] = attempt(lambda p=p: open(p, "w").write(nonce) and p) | {"path": p}
    return spots
def markers_intact(nonce):
    out = {}
    for label, d in (("cwd", CWD), ("home", HOME), ("tmp", TMP), ("run_dir", RUND)):
        if not d: continue
        p = os.path.join(d, "ic-marker-" + nonce)
        try: out[label] = open(p).read() == nonce
        except OSError: out[label] = False
    return out
def secret_commit(nonce):
    sh("git", "checkout", "-q", "-B", "ic-secret", cwd=CWD, env=GITENV)
    f = os.path.join(CWD, "ic-secret.txt")
    open(f, "w").write("secret-" + nonce)
    sh("git", "add", "ic-secret.txt", cwd=CWD, env=GITENV)
    sh("git", "commit", "-q", "-m", "ic secret", cwd=CWD, env=GITENV)
    sha = sh("git", "rev-parse", "HEAD:ic-secret.txt", cwd=CWD)["out"].strip()
    return sha
def addrs():
    found = set()
    try:
        for fam, _, _, _, sa in socket.getaddrinfo(socket.gethostname(), None):
            if fam == socket.AF_INET: found.add(sa[0])
    except Exception: pass
    for line in sh("hostname", "-I")["out"].split(): found.add(line)
    return sorted(found)
FIRST = ["/tmp", "/home", "/root", "/work", "/workspace", "/var", "/mnt", "/srv", "/run", "/opt", "/data", "/cache"]
def walk(pred, limit=1500000, budget=45.0):
    hits, scanned, done = [], 0, set()
    deadline = time.time() + budget
    for start in FIRST + ["/"]:
        if not os.path.isdir(start): continue
        for base, dirs, files in os.walk(start, followlinks=False):
            dirs[:] = [d for d in dirs if os.path.join(base, d) not in ("/proc", "/sys", "/dev")
                       and (start != "/" or os.path.join(base, d) not in FIRST)]
            for n in files + dirs:
                scanned += 1
                if scanned > limit or (scanned % 512 == 0 and time.time() > deadline): return hits, scanned
                p = os.path.join(base, n)
                if pred(p, n): hits.append(p)
    return hits, scanned
def has_content(p, needle):
    try:
        if not p.startswith(("/tmp/", "/home/", "/root/", "/work/", "/workspace/", "/run/", "/mnt/", "/srv/", "/data/", "/cache/", "/var/tmp/")):
            return False
        if os.path.islink(p) or not os.path.isfile(p) or os.path.getsize(p) > 8192: return False
        return needle in open(p, errors="ignore").read()
    except OSError: return False
CACHE_ENV = ("UV_CACHE_DIR", "XDG_CACHE_HOME", "PUB_CACHE", "PIP_CACHE_DIR", "npm_config_cache", "CARGO_HOME")
def cache_dirs():
    dirs = {os.environ[k] for k in CACHE_ENV if os.environ.get(k)}
    dirs |= {os.path.join(HOME, ".cache"), "/root/.cache", "/var/cache", "/opt/cache", "/cache"}
    found, _ = walk(lambda p, n: os.path.isdir(p) and re.search(r"cache", n, re.I) is not None and
                    p.count("/") <= 5)
    dirs |= set(found)
    return sorted(d for d in dirs if os.path.isdir(d))
def cache_hits(needle):
    hits = []
    for d in cache_dirs():
        for base, _, files in os.walk(d):
            for n in files:
                if needle in n or has_content(os.path.join(base, n), needle): hits.append(os.path.join(base, n))
    return hits

probe = ins.get("probe")
emit("step_start", {"id": "prt_s_%d" % os.getpid(), "type": "step-start"})
if probe == "plant":
    nonce = ins["nonce"]
    srv = socket.socket(); srv.bind(("0.0.0.0", 0)); srv.listen(8); srv.settimeout(0.2)
    rep = dict(common(), nonce=nonce, spots=plant(nonce), blob=secret_commit(nonce),
               addrs=addrs(), port=srv.getsockname()[1], phase="planted",
               env_rpc={k: v for k, v in os.environ.items() if re.search(r"RPC|SCHED", k)})
    say(rep)
    end = time.time() + float(ins.get("hold", 60))
    while time.time() < end:
        try: srv.accept()[0].close()
        except OSError: pass
        blob = sh("git", "cat-file", "-p", rep["blob"], cwd=CWD)
        refs = sh("git", "for-each-ref", "--format=%(refname)", cwd=CWD)["out"].split()
        say(dict(common(), phase="hold", nonce=nonce, intact=markers_intact(nonce),
                 blob_ok=blob["rc"] == 0 and nonce in blob["out"], refs=refs,
                 seen_rival=cache_hits(ins["watch"]) if ins.get("watch") else [],
                 listening=True))
        time.sleep(1)
elif probe == "fs":
    own = ins["nonce"]; victim = ins["victim"]
    mine = plant(own)
    hits, scanned = walk(lambda p, n: victim in n or has_content(p, victim))
    acts = {}
    for p in hits[:20]:
        acts[p] = {"append": attempt(lambda p=p: open(p, "a").write("x")),
                   "delete": attempt(lambda p=p: os.remove(p))}
    own_found, _ = walk(lambda p, n: n == "ic-marker-" + own)
    say(dict(common(), scanned=scanned, hits=hits, attacks=acts, own=mine, own_found=own_found,
             own_intact=markers_intact(own)))
elif probe == "git":
    own = ins["nonce"]
    own_blob = secret_commit(own)
    mine = sh("git", "cat-file", "-p", own_blob, cwd=CWD)
    refs = sh("git", "for-each-ref", "--format=%(refname) %(objectname)", cwd=CWD)["out"]
    gitdir = os.path.realpath(sh("git", "rev-parse", "--git-dir", cwd=CWD)["out"].strip() or ".git")
    gitdir = gitdir if os.path.isabs(gitdir) else os.path.realpath(os.path.join(CWD, gitdir))
    others, _ = walk(lambda p, n: n == "HEAD" and os.path.isdir(os.path.join(os.path.dirname(p), "objects"))
                     and os.path.dirname(p) != gitdir)
    attacks = {}
    for g in others[:10]:
        d = os.path.dirname(g)
        attacks[d] = {"read_blob": sh("git", "--git-dir", d, "cat-file", "-p", ins["sha"]),
                      "delete_ref": sh("git", "--git-dir", d, "update-ref", "-d", "refs/heads/ic-secret"),
                      "move_ref": sh("git", "--git-dir", d, "update-ref", "refs/heads/ic-secret", "HEAD")}
    direct = sh("git", "cat-file", "-p", ins["sha"], cwd=CWD)
    say(dict(common(), own_blob=own_blob, own_read=mine, own_refs=refs, gitdir=gitdir,
             other_gitdirs=others, attacks=attacks, victim_read_here=direct,
             victim_alternates=sh("cat", os.path.join(gitdir, "objects", "info", "alternates"))))
elif probe == "net":
    srv = socket.socket(); srv.bind(("0.0.0.0", 0)); srv.listen(8); srv.settimeout(3)
    own_port = srv.getsockname()[1]
    def can(addr, port):
        try:
            c = socket.create_connection((addr, port), timeout=2); c.close(); return True
        except OSError as e: return repr(e)
    control = {a: can(a, own_port) for a in addrs()}
    gw = []
    try:
        for line in open("/proc/net/route").read().splitlines()[1:]:
            f = line.split()
            if f[1] == "00000000": gw.append(socket.inet_ntoa(bytes.fromhex(f[2])[::-1]))
    except OSError: pass
    tried = {}
    for a in ins.get("addrs", []) + gw + ["host.docker.internal"]:
        for p in ins.get("ports", []):
            tried["%s:%s" % (a, p)] = can(a, p)
    say(dict(common(), own_addrs=addrs(), control=control, tried=tried, gateway=gw))
elif probe == "rpc":
    env = {k: v for k, v in os.environ.items() if re.search(r"RPC|SCHED", k)}
    token = next((v for k, v in env.items() if "TOKEN" in k), None)
    target = next((v for k, v in env.items() if "TOKEN" not in k and v and
                   (os.path.exists(v) or re.match(r"^[\w.-]+:\d+$", v))), None)
    def call_op(tok, op, args, tgt=None):
        return call(tok, None, tgt, op=op, args=args)
    def call(tok, node, tgt=None, op="get_node", args=None):
        tgt = tgt or target
        if not tgt: return {"error": "no endpoint in env"}
        try:
            if os.path.exists(tgt):
                c = socket.socket(socket.AF_UNIX); c.settimeout(5); c.connect(tgt)
            else:
                h, p = tgt.rsplit(":", 1); c = socket.create_connection((h, int(p)), timeout=5)
            req = {"op": op, "args": args if args is not None else {"id": node}, "token": tok, "request_id": "ic-%d" % time.time_ns()}
            c.sendall(json.dumps(req).encode() + b"\n")
            buf = b""
            while b"\n" not in buf:
                d = c.recv(65536)
                if not d: break
                buf += d
            return json.loads(buf.split(b"\n")[0])
        except Exception as e: return {"error": repr(e)}
    own_node = ins.get("own_node") or next((v for k, v in os.environ.items() if re.search(r"NODE", k)), None)
    if not own_node and token:
        own_node = None
        try:
            r = call_op(token, "list_nodes", {})
            nodes = (r.get("result") or {}).get("nodes") or []
            own_node = nodes[0]["id"] if nodes else None
        except Exception: pass
    ins["own_node"] = own_node
    say(dict(common(), env_names=sorted(env), have_token=bool(token), endpoint=target,
             own=call(token, ins["own_node"]) if token else {"error": "no token"},
             victim_endpoint_own_token=(call(token, ins["own_node"], ins["victim_endpoint"])
                                        if ins.get("victim_endpoint") and token else {"error": "not tried"}),
             forged_token=call(ins["victim_token"], ins["victim_node"]),
             forged_token_as_own=call(ins["victim_token"], ins["own_node"]),
             own_token_victim_node=call(token, ins["victim_node"]) if token else {"error": "no token"}))
elif probe == "cache":
    poison = ins["poison"]; landed = []
    for d in cache_dirs():
        r = attempt(lambda d=d: open(os.path.join(d, "ic-poison-" + poison), "w").write(poison))
        if r["ok"]: landed.append(d)
    own = os.path.join(TMP, "ic-own-cache"); open(own, "w").write(poison)
    say(dict(common(), cache_dirs=cache_dirs(), landed=landed,
             own_private_ok=open(own).read() == poison,
             readable=cache_hits(poison)))
elif probe == "ro":
    tests = {
        "write_worktree": attempt(lambda: open(os.path.join(CWD, "ic-ro-file"), "w").write("x")),
        "write_gitdir": attempt(lambda: open(os.path.join(CWD, ".git", "ic-ro-file"), "w").write("x")),
        "write_hooks": attempt(lambda: open(os.path.join(CWD, ".git", "hooks", "pre-commit"), "w").write("#!/bin/sh\n")),
        "write_config": attempt(lambda: open(os.path.join(CWD, ".git", "config"), "a").write("\n# x\n")),
        "commit": sh("git", "commit", "-q", "--allow-empty", "-m", "ic", cwd=CWD, env=GITENV),
        "update_ref": sh("git", "update-ref", "refs/heads/ic-ro", "HEAD", cwd=CWD, env=GITENV),
    }
    say(dict(common(), writes=tests, read_ok=attempt(lambda: os.listdir(CWD)),
             status=sh("git", "status", "--porcelain", cwd=CWD),
             tmp_write=attempt(lambda: open(os.path.join(TMP, "ic-ok"), "w").write("x"))))
elif probe == "write":
    p = os.path.join(CWD, ins["path"]); os.makedirs(os.path.dirname(p), exist_ok=True)
    open(p, "w").write(ins["nonce"])
    sh("git", "add", "-A", cwd=CWD, env=GITENV)
    say(dict(common(), commit=sh("git", "commit", "-q", "-m", "ic write", cwd=CWD, env=GITENV)))
elif probe == "handover":
    p = os.path.join(CWD, ins["expect"])
    got = open(p).read() if os.path.exists(p) else None
    hits, scanned = walk(lambda q, n: ins["unrelated"] in n or has_content(q, ins["unrelated"]))
    say(dict(common(), expected_content=got, unrelated_hits=hits, scanned=scanned,
             log=sh("git", "log", "--format=%s", cwd=CWD)["out"]))
else:
    say(dict(common(), error="unknown probe"))
emit("step_finish", {"id": "prt_f_%d" % os.getpid(), "type": "step-finish", "reason": "stop", "cost": 0,
                    "tokens": {"input": 1, "output": 1, "reasoning": 0, "cache": {"read": 0, "write": 0}}})
emit("text", {"id": "prt_t_%d" % os.getpid(), "type": "text", "text": "IC-DONE"})
'''


class ProbeProvider(sc.FakeProvider):
    """A FakeProvider whose CLI is the probe script."""

    def __init__(self, tmp: Path, name: str, **extra):
        super().__init__(tmp, name, **extra)
        # K17.2: network/RPC probes attempt forbidden access; neither is required to launch.
        self.entry["needs"] = {"credentials": False, "network": False, "rpc": False}
        script = Path(self.entry["bin"])
        script.write_text(PROBE_CLI)
        script.chmod(script.stat().st_mode | stat.S_IEXEC)


def instruction(probe: str, **fields) -> str:
    """The task text that tells a probe what to do."""
    return "IC-PROBE " + json.dumps({"probe": probe, **fields})


REPORT = re.compile(r"IC-REPORT (\{.*?\})(?=\\n|\"|$|\s*IC-REPORT)", re.S)


def parse_reports(text: str) -> list[dict]:
    """Every IC-REPORT in `text`, in order. The text may be JSON-escaped
    (a result embedded in a JSON view): both forms are tried."""
    out = []
    for blob in (text, text.replace('\\"', '"').replace("\\\\", "\\")):
        for m in re.finditer(r"IC-REPORT (\{.*?\})(?=\s*(?:IC-REPORT|IC-DONE|\"|\\n|$))", blob, re.S):
            try:
                out.append(json.loads(m.group(1)))
            except ValueError:
                continue
        if out:
            return out
    return out
