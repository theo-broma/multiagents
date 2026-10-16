"""A scriptable fake `docker` CLI (slice 3, D-S3.3).

The Docker backend talks to Docker only through the `docker` CLI, found by a
configured path (default: `docker` on the scheduler's PATH). `FakeDocker`
writes an executable named `docker` into a private directory, puts that
directory first on PATH (`install`), and keeps all state in files, so it works
across the scheduler's processes and across xdist workers (one instance per
test, `tmp_path` only).

What it emulates, in Docker's own vocabulary (so a test asserts on what the
container IS, never on how the argv was spelled):

  create     parses the options into the `docker inspect` shape: Config
             (Image, User, Labels, Env, Cmd, Entrypoint, WorkingDir) and
             HostConfig (Binds, Mounts, NetworkMode, Memory, PidsLimit,
             CpuShares, NanoCpus, CapAdd, CapDrop, SecurityOpt, Privileged,
             PidMode, IpcMode, UsernsMode, GroupAdd, RestartPolicy, LogConfig,
             ReadonlyRootfs, Tmpfs). A name already in use is Docker's own
             conflict error. A missing image is Docker's own error.
  start      Created -> Running, then the scripted workload (below). Starting
             an Exited container runs it AGAIN (as Docker does): `start_count`
             counts the runs, so a test can prove an invocation ran once.
  inspect    JSON, `--format` with `{{json .X.Y}}` / `{{.X.Y}}`; a missing
             object is Docker's "No such object" (exit 1).
  logs       stdout on stdout, stderr on stderr; `--tail`, `--since`,
             `--timestamps`, `--follow`.
  stop/kill  Running -> Exited 143 (137 for a workload that ignores TERM, and
             for kill). No-op on Created/Exited. Missing -> error.
  rm         `-f` needed for a running one. Missing -> error.
  ps         `-a`, `-q`, `--no-trunc`, `--filter label=k[=v]|name=|status=`,
             `--format`.
  image inspect / info / version / wait / network|volume create (recorded).
  Anything else (`run`, `exec`, `cp`, ...) is recorded in `unsupported()` and
  fails with exit 125: the contract's protocol never needs it.

Scripted outcomes (`rule`): per subcommand prefix, a failure, a daemon error,
a hang, garbage output, or "the operation happens, then the CLI reports a
failure" (`after_effect`), each limited to `times` matches after `after`
skipped matches. `workload` says what the next started container does
(exit code, stdout, stderr, OOM, stays running, ignores TERM).

Every call is recorded (`calls`) before it is executed.
"""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import stat
import sys
import time
from pathlib import Path
from typing import Any

SCRIPT = r'''#!{python}
import base64, fcntl, hashlib, json, os, re, sys, time
from datetime import datetime, timezone

DIR = {state!r}
ARGV = sys.argv[1:]

def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

def lock():
    f = open(os.path.join(DIR, "lock"), "a")
    fcntl.flock(f, fcntl.LOCK_EX)
    return f

def load():
    with open(os.path.join(DIR, "state.json")) as f:
        return json.load(f)

def save(state):
    tmp = os.path.join(DIR, "state.json.tmp")
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, os.path.join(DIR, "state.json"))

def b64(b):
    return base64.b64encode(b).decode()

def unb64(s):
    return base64.b64decode(s)

GLOBAL_VALUE = {{"-H", "--host", "--context", "-c", "--config", "-l", "--log-level",
                "--tlscacert", "--tlscert", "--tlskey"}}
GLOBAL_FLAG = {{"-D", "--debug", "--tls", "--tlsverify", "-v", "--version"}}

def split_globals(argv):
    i = 0
    while i < len(argv) and argv[i].startswith("-") and argv[i] != "--":
        if "=" in argv[i]:
            i += 1
        elif argv[i] in GLOBAL_VALUE:
            i += 2
        else:
            i += 1
    return argv[:i], argv[i:]

GLOBALS, CMD = split_globals(ARGV)

def fail(message, code=1, out=""):
    if out:
        sys.stdout.write(out)
    sys.stderr.write(message + "\n")
    sys.exit(code)

# ------------------------------------------------------------------ calls log
SUBS = {{"container", "image", "network", "volume", "system", "builder", "compose"}}
def command_tokens():
    if not CMD:
        return []
    if CMD[0] in SUBS and len(CMD) > 1:
        # `docker container rm` is `docker rm`
        if CMD[0] == "container":
            return CMD[1:]
        return CMD[:2] + CMD[2:]
    return CMD

def log_call(seq):
    with open(os.path.join(DIR, "calls.jsonl"), "a") as f:
        f.write(json.dumps({{"seq": seq, "argv": ARGV, "globals": GLOBALS, "cmd": CMD,
                            "cwd": os.getcwd(), "at": time.time()}}) + "\n")

# ------------------------------------------------------------------- parsing
VALUE = {{"--name", "--label", "-l", "-v", "--volume", "--mount", "--user", "-u", "--network",
         "--net", "--memory", "-m", "--memory-swap", "--memory-reservation", "--pids-limit",
         "--cpu-shares", "-c", "--cpus", "--cap-drop", "--cap-add", "--security-opt",
         "--log-driver", "--log-opt", "--restart", "--pid", "--ipc", "--uts", "--userns", "-w",
         "--workdir", "-e", "--env", "--env-file", "--entrypoint", "--tmpfs", "--hostname", "-h",
         "--group-add", "--ulimit", "--shm-size", "--stop-timeout", "--stop-signal",
         "--oom-score-adj", "--cgroup-parent", "--cpuset-cpus", "--device", "--add-host", "--dns",
         "--platform", "--pull", "--runtime", "--cgroupns", "--label-file", "--cidfile",
         "--cpu-period", "--cpu-quota", "--mac-address", "--ip", "--expose", "-p", "--publish",
         "--volumes-from", "--storage-opt", "--sysctl", "--health-cmd", "--annotation",
         "--blkio-weight", "--kernel-memory", "--memory-swappiness", "--cpu-rt-period",
         "--cpu-rt-runtime", "--isolation", "--link", "--domainname", "--gpus"}}

def parse_create(args):
    opts = {{}}
    multi = {{}}
    unknown = []
    flags = set()
    i = 0
    image = None
    while i < len(args):
        a = args[i]
        if image is None and a.startswith("-") and a != "-":
            if a.startswith("--") and "=" in a:
                k, v = a.split("=", 1)
                multi.setdefault(k, []).append(v)
                i += 1
            elif a in VALUE:
                if i + 1 >= len(args):
                    fail("flag needs an argument: " + a, 125)
                multi.setdefault(a, []).append(args[i + 1])
                i += 2
            elif a.startswith("-") and not a.startswith("--") and len(a) > 2 and a[:2] in VALUE:
                multi.setdefault(a[:2], []).append(a[2:])
                i += 1
            else:
                flags.add(a)
                i += 1
            continue
        if image is None:
            image = a
        else:
            multi.setdefault("__cmd__", []).append(a)
        i += 1
    return image, multi, flags

def first(multi, *names):
    for n in names:
        if n in multi:
            return multi[n][-1]
    return None

def allof(multi, *names):
    out = []
    for n in names:
        out.extend(multi.get(n, []))
    return out

def size(text):
    if text is None:
        return 0
    t = str(text).strip().lower()
    m = re.fullmatch(r"(-?(?:\d+(?:\.\d*)?|\.\d+)) ?([kmgtp])?i?b?", t)
    if not m:
        fail("invalid size: " + str(text), 125)
    unit = 1024 ** (" kmgtp".index(m.group(2)) if m.group(2) else 0)
    return int(float(m.group(1)) * unit)

def parse_mount(spec):
    parts = dict(p.split("=", 1) if "=" in p else (p, "true") for p in spec.split(","))
    kind = parts.get("type", "volume")
    return {{"Type": kind, "Source": parts.get("source", parts.get("src", "")),
            "Destination": parts.get("destination", parts.get("dst", parts.get("target", ""))),
            "RW": not (parts.get("readonly", parts.get("ro", "false")) in ("true", "1"))}}

def parse_bind(spec):
    parts = spec.split(":")
    src, dst = parts[0], parts[1] if len(parts) > 1 else ""
    mode = parts[2] if len(parts) > 2 else ""
    kind = "bind" if src.startswith("/") else "volume"
    return {{"Type": kind, "Source": src, "Destination": dst, "RW": "ro" not in mode.split(",")}}

def build_config(image, multi, flags, name):
    labels = {{}}
    for item in allof(multi, "--label", "-l"):
        k, _, v = item.partition("=")
        labels[k] = v
    binds = allof(multi, "-v", "--volume")
    mounts_opt = allof(multi, "--mount")
    log_opts = {{}}
    for item in allof(multi, "--log-opt"):
        k, _, v = item.partition("=")
        log_opts[k] = v
    driver = first(multi, "--log-driver")
    cpus = first(multi, "--cpus")
    return {{
        "Config": {{"Image": image, "User": first(multi, "--user", "-u") or "",
                   "Labels": labels, "Env": allof(multi, "-e", "--env"),
                   "Cmd": multi.get("__cmd__", []) or None,
                   "Entrypoint": ([first(multi, "--entrypoint")] if first(multi, "--entrypoint") is not None else None),
                   "WorkingDir": first(multi, "--workdir", "-w") or "",
                   "Hostname": first(multi, "--hostname", "-h") or ""}},
        "HostConfig": {{
            "Binds": binds or None,
            "Mounts": [parse_mount(m) for m in mounts_opt] or None,
            "Tmpfs": {{t.split(":")[0]: (t.split(":", 1) + [""])[1] for t in allof(multi, "--tmpfs")}} or None,
            "NetworkMode": first(multi, "--network", "--net") or "default",
            "Memory": size(first(multi, "--memory", "-m")),
            "MemorySwap": size(first(multi, "--memory-swap")) if first(multi, "--memory-swap") not in (None, "-1") else (-1 if first(multi, "--memory-swap") == "-1" else 0),
            "PidsLimit": int(first(multi, "--pids-limit") or 0) or None,
            "CpuShares": int(first(multi, "--cpu-shares", "-c") or 0),
            "NanoCpus": int(float(cpus) * 1e9) if cpus is not None else 0,
            "CapAdd": allof(multi, "--cap-add") or None,
            "CapDrop": allof(multi, "--cap-drop") or None,
            "SecurityOpt": allof(multi, "--security-opt") or None,
            "Privileged": "--privileged" in flags,
            "PidMode": first(multi, "--pid") or "",
            "IpcMode": first(multi, "--ipc") or "private",
            "UTSMode": first(multi, "--uts") or "",
            "UsernsMode": first(multi, "--userns") or "",
            "GroupAdd": allof(multi, "--group-add") or None,
            "ReadonlyRootfs": "--read-only" in flags,
            "AutoRemove": "--rm" in flags,
            "Init": True if "--init" in flags else None,
            "Devices": allof(multi, "--device") or None,
            "RestartPolicy": {{"Name": (first(multi, "--restart") or "no").split(":")[0], "MaximumRetryCount": 0}},
            "LogConfig": {{"Type": driver or "json-file", "Config": log_opts}},
            "LogConfigPinned": driver is not None,
        }},
    }}

# --------------------------------------------------------------------- state
def find(state, ref):
    cs = state["containers"]
    if ref in cs:
        return cs[ref]
    named = [c for c in cs.values() if c["Name"].lstrip("/") == ref.lstrip("/")]
    if named:
        return named[0]
    pref = [c for c in cs.values() if len(ref) >= 1 and c["Id"].startswith(ref)]
    if len(pref) == 1:
        return pref[0]
    return None

def view(c):
    out = {{k: v for k, v in c.items() if not k.startswith("_")}}
    return out

def apply_workload(state, c):
    queue = state.get("workload_queue") or []
    w = queue.pop(0) if queue else dict(state.get("default_workload") or {{}})
    state["workload_queue"] = queue
    c["_stdout"] = b64(unb64(c.get("_stdout", "")) + unb64(w.get("stdout", "")))
    c["_stderr"] = b64(unb64(c.get("_stderr", "")) + unb64(w.get("stderr", "")))
    c["_ignore_term"] = bool(w.get("ignore_term"))
    c["State"].update({{"Status": "running", "Running": True, "Paused": False, "Restarting": False,
                      "OOMKilled": False, "Dead": False, "ExitCode": 0, "Pid": 4242 + c["StartCount"],
                      "StartedAt": now(), "FinishedAt": "0001-01-01T00:00:00Z", "Error": ""}})
    if not w.get("runs"):
        finish(c, w.get("exit_code", 0), bool(w.get("oom")))

def finish(c, code, oom=False):
    c["State"].update({{"Status": "exited", "Running": False, "ExitCode": code, "OOMKilled": oom,
                      "Pid": 0, "FinishedAt": now()}})

DAEMON_DOWN = ("Cannot connect to the Docker daemon at unix:///var/run/docker.sock. "
               "Is the docker daemon running?")

def render(fmt, obj):
    def sub(m):
        as_json, path = m.group(1), m.group(2)
        cur = obj
        for part in [p for p in path.split(".") if p]:
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                cur = None
                break
        if as_json:
            return json.dumps(cur)
        if cur is None:
            return "<no value>"
        if isinstance(cur, (dict, list)):
            return json.dumps(cur)
        if isinstance(cur, bool):
            return "true" if cur else "false"
        return str(cur)
    return re.sub(r"\{{\{{\s*(json\s+)?(\.[A-Za-z0-9_.]*)\s*\}}\}}", sub, fmt)

def opt_values(args, names, flags=()):
    """(values for value options, remaining positionals, set of flags seen)."""
    vals, rest, seen = {{}}, [], set()
    i = 0
    while i < len(args):
        a = args[i]
        if a.startswith("--") and "=" in a and a.split("=", 1)[0] in names:
            k, v = a.split("=", 1)
            vals.setdefault(k, []).append(v)
        elif a in names:
            vals.setdefault(a, []).append(args[i + 1] if i + 1 < len(args) else "")
            i += 1
        elif a.startswith("-") and a != "-":
            seen.add(a)
        else:
            rest.append(a)
        i += 1
    return vals, rest, seen

def canon(names, vals):
    for n in names:
        if n in vals:
            return vals[n][-1]
    return None

# ------------------------------------------------------------------ commands
def cmd_info(state):
    info = dict(state.get("info") or {{}})
    fmt = None
    vals, _, _ = opt_values(CMD[1:], {{"--format", "-f"}})
    fmt = canon(("--format", "-f"), vals)
    if fmt:
        print(render(fmt, info))
    else:
        print(json.dumps(info))

def cmd_version(state):
    v = {{"Client": {{"Version": "27.0.0"}}, "Server": {{"Version": (state.get("info") or {{}}).get("ServerVersion", "27.0.0")}}}}
    vals, _, _ = opt_values(CMD[1:], {{"--format", "-f"}})
    fmt = canon(("--format", "-f"), vals)
    print(render(fmt, v) if fmt else json.dumps(v))

def cmd_image_inspect(state, args):
    vals, rest, _ = opt_values(args, {{"--format", "-f"}})
    images = state.get("images")
    out, missing = [], []
    for ref in rest:
        if images is None or ref in images:
            out.append({{"Id": "sha256:" + hashlib.sha256(ref.encode()).hexdigest(), "RepoTags": [ref]}})
        else:
            missing.append(ref)
    fmt = canon(("--format", "-f"), vals)
    if fmt:
        for o in out:
            print(render(fmt, o))
    else:
        print(json.dumps(out))
    if missing:
        fail("Error response from daemon: No such image: " + missing[0], 1)

def cmd_create(state, args):
    image, multi, flags = parse_create(args)
    if image is None:
        fail("docker create requires at least 1 argument", 125)
    images = state.get("images")
    if images is not None and image not in images:
        fail("Unable to find image '%s' locally\ndocker: Error response from daemon: pull access denied for %s, repository does not exist or may require 'docker login'." % (image, image), 125)
    name = first(multi, "--name")
    state["seq"] += 1
    if name:
        for c in state["containers"].values():
            if c["Name"].lstrip("/") == name:
                fail('docker: Error response from daemon: Conflict. The container name "/%s" is already in use by container "%s". You have to remove (or rename) that container to be able to reuse that name.' % (name, c["Id"]), 125)
    cid = hashlib.sha256(("%d-%s" % (state["seq"], name)).encode()).hexdigest()
    name = name or "fake_%d" % state["seq"]
    cfg = build_config(image, multi, flags, name)
    # What Docker would find on the host when the container is created: a
    # missing `--mount` source is an error; a missing `-v` source is created
    # by the daemon, as root (the F31 hazard), which is recorded here.
    sources = []
    for m in [parse_mount(x) for x in allof(multi, "--mount")] + [parse_bind(x) for x in allof(multi, "-v", "--volume")]:
        if m["Type"] != "bind":
            continue
        try:
            st = os.stat(m["Source"])
            sources.append({{"path": m["Source"], "exists": True, "uid": st.st_uid, "gid": st.st_gid,
                            "mode": st.st_mode & 0o7777, "is_dir": os.path.isdir(m["Source"])}})
        except OSError:
            sources.append({{"path": m["Source"], "exists": False}})
    for m, src in zip([x for x in [parse_mount(y) for y in allof(multi, "--mount")] if x["Type"] == "bind"], sources):
        if not src["exists"]:
            fail('docker: Error response from daemon: invalid mount config for type "bind": bind source path does not exist: %s' % m["Source"], 125)
    c = {{"Id": cid, "Name": "/" + name, "Created": now(), "Image": "sha256:" + hashlib.sha256(image.encode()).hexdigest(),
         "State": {{"Status": "created", "Running": False, "Paused": False, "Restarting": False,
                   "OOMKilled": False, "Dead": False, "Pid": 0, "ExitCode": 0, "Error": "",
                   "StartedAt": "0001-01-01T00:00:00Z", "FinishedAt": "0001-01-01T00:00:00Z"}},
         "StartCount": 0, "CreateArgv": list(args), "UnknownOptions": [], "HostSources": sources,
         "_stdout": "", "_stderr": ""}}
    c.update(cfg)
    c["NetworkSettings"] = {{"Networks": {{}}}}
    mode = cfg["HostConfig"]["NetworkMode"]
    if mode in ("none", "host"):
        c["NetworkSettings"]["Networks"][mode] = {{"NetworkID": "", "Aliases": []}}
    elif mode not in ("default", "bridge"):
        net = net_find(state, mode)
        if net is None:
            fail("docker: Error response from daemon: network %s not found" % mode, 125)
        attach(state, net, c, allof(multi, "--network-alias"))
    state["containers"][cid] = c
    print(cid)

def check_log_size(c):
    """The logging driver is initialised at start and at read time; a max-size
    that is not a size (`-1`) makes both fail, as in Docker."""
    log = c["HostConfig"]["LogConfig"]
    if log["Type"] in ("json-file", "local"):
        v = (log.get("Config") or {{}}).get("max-size")
        if v is not None and not re.fullmatch(r"(?:\d+(?:\.\d+)?|\.\d+) ?[kKmMgGtTpP]?[bB]?", v):
            sys.stderr.write("Error response from daemon: failed to initialize logging driver: invalid size: '%s'\n" % v)
            sys.exit(1)

def cmd_start(state, args):
    vals, rest, _ = opt_values(args, {{"--detach-keys"}})
    bad = 0
    for ref in rest:
        c = find(state, ref)
        if c is None:
            sys.stderr.write("Error response from daemon: No such container: %s\n" % ref)
            bad = 1
            continue
        if not c["State"]["Running"]:
            check_log_size(c)
            # What the container is attached to at the moment it starts (slice 4B).
            c.setdefault("StartSnapshots", []).append({{
                n["Name"]: {{cid: bool(state["containers"].get(cid, {{}}).get("State", {{}}).get("Running"))
                             for cid in sorted(n["Containers"])}}
                for n in state.get("networks", {{}}).values() if c["Id"] in n["Containers"]}})
            c["StartCount"] += 1
            apply_workload(state, c)
        print(ref)
    if bad:
        save(state)
        sys.exit(1)

def cmd_stop(state, args, kill=False):
    vals, rest, _ = opt_values(args, {{"-t", "--time", "-s", "--signal"}})
    bad = 0
    for ref in rest:
        c = find(state, ref)
        if c is None:
            sys.stderr.write("Error response from daemon: No such container: %s\n" % ref)
            bad = 1
            continue
        if c["State"]["Running"]:
            sig = canon(("-s", "--signal"), vals) or ("KILL" if kill else "TERM")
            c.setdefault("Signals", []).append(sig)
            hard = kill or c.get("_ignore_term") or sig.upper().lstrip("SIG") in ("KILL", "9")
            finish(c, 137 if hard else 143, False)
        print(ref)
    if bad:
        save(state)
        sys.exit(1)

def cmd_rm(state, args):
    vals, rest, flags = opt_values(args, set())
    force = "-f" in flags or "--force" in flags
    bad = 0
    for ref in rest:
        c = find(state, ref)
        if c is None:
            sys.stderr.write("Error response from daemon: No such container: %s\n" % ref)
            bad = 1
            continue
        if c["State"]["Running"] and not force:
            sys.stderr.write("Error response from daemon: cannot remove container \"%s\": container is running: stop the container before removing or force remove\n" % ref)
            bad = 1
            continue
        for net in state.setdefault("networks", {{}}).values():
            net["Containers"].pop(c["Id"], None)
        del state["containers"][c["Id"]]
        print(ref)
    if bad:
        save(state)
        sys.exit(1)

def cmd_inspect(state, args):
    vals, rest, _ = opt_values(args, {{"--format", "-f", "--type"}})
    fmt = canon(("--format", "-f"), vals)
    found, missing = [], []
    for ref in rest:
        c = find(state, ref)
        (found if c else missing).append(c or ref)
    if fmt:
        for c in found:
            print(render(fmt, view(c)))
    else:
        print(json.dumps([view(c) for c in found], indent=4))
    if missing:
        fail("Error: No such object: " + missing[0], 1)

def cmd_logs(state, args):
    vals, rest, flags = opt_values(args, {{"--tail", "-n", "--since", "--until", "--details"}})
    if not rest:
        fail("docker logs requires exactly 1 argument", 1)
    c = find(state, rest[0])
    if c is None:
        fail("Error response from daemon: No such container: " + rest[0], 1)
    check_log_size(c)
    follow = "-f" in flags or "--follow" in flags
    stamps = "-t" in flags or "--timestamps" in flags
    tail = canon(("--tail", "-n"), vals)
    def emit(stream_name, data, out):
        lines = data.split(b"\n")
        if lines and lines[-1] == b"":
            lines.pop()
            tailing = b"\n"
        else:
            tailing = b""
        if tail not in (None, "all"):
            lines = lines[-int(tail):] if int(tail) else []
        body = b"\n".join((now().encode() + b" " + l) if stamps else l for l in lines)
        if lines:
            out.write(body + (tailing if (not lines or tailing) else b""))
            out.flush()
    out_b, err_b = unb64(c.get("_stdout", "")), unb64(c.get("_stderr", ""))
    emit("stdout", out_b, sys.stdout.buffer)
    emit("stderr", err_b, sys.stderr.buffer)
    if follow and c["State"]["Running"]:
        seen_o, seen_e = len(out_b), len(err_b)
        deadline = time.time() + 30
        while time.time() < deadline:
            time.sleep(0.05)
            f = lock()
            try:
                cur = find(load(), rest[0])
            finally:
                f.close()
            if cur is None:
                break
            o, e = unb64(cur.get("_stdout", "")), unb64(cur.get("_stderr", ""))
            if len(o) > seen_o:
                sys.stdout.buffer.write(o[seen_o:]); sys.stdout.buffer.flush(); seen_o = len(o)
            if len(e) > seen_e:
                sys.stderr.buffer.write(e[seen_e:]); sys.stderr.buffer.flush(); seen_e = len(e)
            if not cur["State"]["Running"]:
                break

def labels_text(c):
    return ",".join("%s=%s" % kv for kv in sorted(c["Config"]["Labels"].items()))

def cmd_ps(state, args):
    vals, rest, flags = opt_values(args, {{"--filter", "-f", "--format", "-n", "--last"}})
    show_all = "-a" in flags or "--all" in flags or "-aq" in flags
    quiet = "-q" in flags or "--quiet" in flags or "-aq" in flags
    trunc = "--no-trunc" not in flags
    fmt = canon(("--format",), vals)
    filters = vals.get("--filter", []) + vals.get("-f", [])
    rows = []
    for c in sorted(state["containers"].values(), key=lambda c: c["Created"]):
        if not show_all and not c["State"]["Running"]:
            continue
        ok = True
        for flt in filters:
            k, _, v = flt.partition("=")
            if k == "label":
                have = c["Config"]["Labels"]
                if "=" in v:
                    lk, _, lv = v.partition("=")
                    ok = ok and have.get(lk) == lv
                else:
                    ok = ok and v in have
            elif k == "name":
                ok = ok and re.search(v, c["Name"].lstrip("/")) is not None
            elif k == "status":
                ok = ok and c["State"]["Status"] == v
            elif k == "id":
                ok = ok and c["Id"].startswith(v)
        if ok:
            rows.append(c)
    for c in rows:
        cid = c["Id"][:12] if trunc else c["Id"]
        if quiet:
            print(cid)
        elif fmt:
            obj = {{"ID": cid, "Names": c["Name"].lstrip("/"), "Image": c["Config"]["Image"],
                   "Labels": labels_text(c), "State": c["State"]["Status"],
                   "Status": c["State"]["Status"], "CreatedAt": c["Created"]}}
            print(render(fmt, obj))
        else:
            print(cid, c["Name"].lstrip("/"), c["State"]["Status"])

def cmd_wait(state, args):
    for ref in [a for a in args if not a.startswith("-")]:
        c = find(state, ref)
        if c is None:
            fail("Error response from daemon: No such container: " + ref, 1)
        print(c["State"]["ExitCode"])

def cmd_resource_create(state, args, kind):
    state["seq"] += 1
    vals, rest, _ = opt_values(args, {{"--label"}})
    state.setdefault("resources", []).append({{"kind": kind, "argv": args, "name": rest[-1] if rest else ""}})
    print(rest[-1] if rest else "x")

# ------------------------------------------------------------- networks (4B)
def net_find(state, ref):
    nets = state.setdefault("networks", {{}})
    if ref in nets:
        return nets[ref]
    named = [n for n in nets.values() if n["Name"] == ref]
    if named:
        return named[0]
    pref = [n for n in nets.values() if len(ref) >= 4 and n["Id"].startswith(ref)]
    return pref[0] if len(pref) == 1 else None

def attach(state, net, c, aliases):
    net["Containers"][c["Id"]] = {{"Name": c["Name"].lstrip("/"), "EndpointID": hashlib.sha256((net["Id"] + c["Id"]).encode()).hexdigest(),
                                  "IPv4Address": "10.%d.0.%d/16" % (len(net["Containers"]) % 200 + 1, len(net["Containers"]) + 2)}}
    c.setdefault("NetworkSettings", {{"Networks": {{}}}}).setdefault("Networks", {{}})[net["Name"]] = {{
        "NetworkID": net["Id"], "Aliases": list(aliases)}}

def detach(state, net, c):
    net["Containers"].pop(c["Id"], None)
    c.get("NetworkSettings", {{}}).get("Networks", {{}}).pop(net["Name"], None)

def cmd_network_create(state, args):
    vals, rest, flags = opt_values(args, {{"--label", "--driver", "-d", "--subnet", "--opt", "-o", "--ip-range", "--gateway"}})
    name = rest[-1] if rest else ""
    nets = state.setdefault("networks", {{}})
    if not name:
        fail("\"docker network create\" requires exactly 1 argument.", 1)
    if any(n["Name"] == name for n in nets.values()):
        fail("Error response from daemon: network with name %s already exists" % name, 1)
    labels = {{}}
    for item in vals.get("--label", []):
        k, _, v = item.partition("=")
        labels[k] = v
    state["seq"] += 1
    nid = hashlib.sha256(("net-%d-%s" % (state["seq"], name)).encode()).hexdigest()
    nets[nid] = {{"Id": nid, "Name": name, "Created": now(), "Driver": canon(("--driver", "-d"), vals) or "bridge",
                 "Internal": "--internal" in flags, "Labels": labels, "Containers": {{}}, "Options": {{}}}}
    state.setdefault("resources", []).append({{"kind": "network", "argv": args, "name": name}})
    print(nid)

def cmd_network_connect(state, args):
    vals, rest, flags = opt_values(args, {{"--alias", "--ip", "--link"}})
    if len(rest) != 2:
        fail("\"docker network connect\" requires exactly 2 arguments.", 1)
    net, c = net_find(state, rest[0]), find(state, rest[1])
    if net is None:
        fail("Error response from daemon: network %s not found" % rest[0], 1)
    if c is None:
        fail("Error response from daemon: No such container: " + rest[1], 1)
    if c["Id"] in net["Containers"]:
        fail("Error response from daemon: endpoint with name %s already exists in network %s" % (c["Name"].lstrip("/"), net["Name"]), 1)
    attach(state, net, c, vals.get("--alias", []))

def cmd_network_disconnect(state, args):
    vals, rest, flags = opt_values(args, set())
    if len(rest) != 2:
        fail("\"docker network disconnect\" requires exactly 2 arguments.", 1)
    net, c = net_find(state, rest[0]), find(state, rest[1])
    if net is None:
        fail("Error response from daemon: network %s not found" % rest[0], 1)
    if c is None:
        fail("Error response from daemon: No such container: " + rest[1], 1)
    if c["Id"] not in net["Containers"]:
        fail("Error response from daemon: container %s is not connected to network %s" % (c["Id"], net["Name"]), 1)
    detach(state, net, c)

def net_view(n):
    return dict(n)

def cmd_network_inspect(state, args):
    vals, rest, _ = opt_values(args, {{"--format", "-f", "--type"}})
    fmt = canon(("--format", "-f"), vals)
    found, missing = [], []
    for ref in rest:
        n = net_find(state, ref)
        (found if n else missing).append(n or ref)
    if fmt:
        for n in found:
            print(render(fmt, net_view(n)))
    else:
        print(json.dumps([net_view(n) for n in found], indent=4))
    if missing:
        fail("Error response from daemon: network %s not found" % missing[0], 1)

def cmd_network_ls(state, args):
    vals, rest, flags = opt_values(args, {{"--filter", "-f", "--format"}})
    quiet = "-q" in flags or "--quiet" in flags
    trunc = "--no-trunc" not in flags
    fmt = canon(("--format",), vals)
    rows = []
    for n in sorted(state.setdefault("networks", {{}}).values(), key=lambda n: (n["Created"], n["Id"])):
        ok = True
        for flt in vals.get("--filter", []) + vals.get("-f", []):
            k, _, v = flt.partition("=")
            if k == "label":
                lk, eq, lv = v.partition("=")
                ok = ok and lk in n["Labels"] and (not eq or n["Labels"][lk] == lv)
            elif k == "name":
                ok = ok and re.search(v, n["Name"]) is not None
            elif k == "id":
                ok = ok and n["Id"].startswith(v)
            elif k == "driver":
                ok = ok and n["Driver"] == v
        if ok:
            rows.append(n)
    for n in rows:
        nid = n["Id"][:12] if trunc else n["Id"]
        if quiet:
            print(nid)
        elif fmt:
            print(render(fmt, {{"ID": nid, "Name": n["Name"], "Driver": n["Driver"], "Internal": n["Internal"],
                               "Labels": ",".join("%s=%s" % kv for kv in sorted(n["Labels"].items())),
                               "CreatedAt": n["Created"]}}))
        else:
            print(nid, n["Name"], n["Driver"])

def cmd_network_rm(state, args):
    vals, rest, flags = opt_values(args, set())
    bad = 0
    for ref in rest:
        n = net_find(state, ref)
        if n is None:
            sys.stderr.write("Error response from daemon: network %s not found\n" % ref)
            bad = 1
        elif n["Containers"]:
            sys.stderr.write("Error response from daemon: error while removing network: network %s has active endpoints\n" % n["Name"])
            bad = 1
        else:
            del state["networks"][n["Id"]]
            print(ref)
    if bad:
        save(state)
        sys.exit(1)

def cmd_run(state, args):
    # `docker run -d`: create, then start; the id is printed once.
    import io
    keep, sys.stdout = sys.stdout, io.StringIO()
    try:
        cmd_create(state, [a for a in args if a not in ("-d", "--detach")])
        cid = sys.stdout.getvalue().strip()
        sys.stdout = io.StringIO()
        cmd_start(state, [cid])
    finally:
        sys.stdout = keep
    print(cid)

# ------------------------------------------------------------------ the rules
def matches(rule, tokens):
    m = rule.get("match") or []
    if tokens[:len(m)] != m:
        return False
    sub = rule.get("contains")
    return sub is None or sub in " ".join(ARGV)

def main():
    f = lock()
    state = load()
    state["calls_seen"] = state.get("calls_seen", 0) + 1
    seq = state["calls_seen"]
    log_call(seq)
    tokens = command_tokens()
    effect_after = None
    for rule in state.get("rules", []):
        if not matches(rule, tokens):
            continue
        rule["seen"] = rule.get("seen", 0) + 1
        if rule["seen"] <= rule.get("after", 0):
            continue
        times = rule.get("times")
        if times is not None and rule.get("fired", 0) >= times:
            continue
        rule["fired"] = rule.get("fired", 0) + 1
        effect = rule.get("effect", "fail")
        if effect == "hang":
            save(state); f.close()
            time.sleep(rule.get("seconds", 60))
            sys.exit(rule.get("code", 1))
        if effect in ("after_effect", "hang_after"):
            effect_after = rule
            break
        save(state)
        if effect == "daemon_down":
            fail(DAEMON_DOWN, 1)
        if effect == "garbage":
            sys.stdout.write(rule.get("stdout", "\x00not json{{"))
            sys.exit(rule.get("code", 0))
        if effect == "empty":
            sys.exit(rule.get("code", 0))
        fail(rule.get("stderr", "fake docker: scripted failure"), rule.get("code", 1),
             rule.get("stdout", ""))
    code = dispatch(state, tokens)
    save(state)
    if effect_after is not None and effect_after.get("effect") == "hang_after":
        # The operation happened; the CLI then stalls (a crash window after the effect).
        sys.stdout.flush()
        f.close()
        time.sleep(effect_after.get("seconds", 5))
        sys.exit(code or 0)
    if effect_after is not None:
        fail(effect_after.get("stderr", "fake docker: connection lost after the operation"),
             effect_after.get("code", 1))
    sys.exit(code or 0)

def dispatch(state, t):
    if not t:
        print("fake docker"); return 0
    head = t[0]
    if head == "info" or t[:2] == ["system", "info"]:
        cmd_info(state)
    elif head == "version":
        cmd_version(state)
    elif t[:2] == ["image", "inspect"]:
        cmd_image_inspect(state, t[2:])
    elif head == "create":
        cmd_create(state, t[1:])
    elif head == "start":
        cmd_start(state, t[1:])
    elif head == "stop":
        cmd_stop(state, t[1:])
    elif head == "kill":
        cmd_stop(state, t[1:], kill=True)
    elif head == "rm":
        cmd_rm(state, t[1:])
    elif head == "inspect":
        cmd_inspect(state, t[1:])
    elif head == "logs":
        cmd_logs(state, t[1:])
    elif head in ("ps", "ls") or t[:2] == ["container", "ls"]:
        cmd_ps(state, t[1:])
    elif head == "wait":
        cmd_wait(state, t[1:])
    elif t[:2] == ["network", "create"]:
        cmd_network_create(state, t[2:])
    elif t[:2] == ["network", "connect"]:
        cmd_network_connect(state, t[2:])
    elif t[:2] == ["network", "disconnect"]:
        cmd_network_disconnect(state, t[2:])
    elif t[:2] == ["network", "inspect"]:
        cmd_network_inspect(state, t[2:])
    elif t[:2] in (["network", "ls"], ["network", "list"]):
        cmd_network_ls(state, t[2:])
    elif t[:2] in (["network", "rm"], ["network", "remove"]):
        cmd_network_rm(state, t[2:])
    elif t[:2] == ["volume", "create"]:
        cmd_resource_create(state, t[2:], t[0])
    elif head == "run":
        cmd_run(state, t[1:])
    else:
        state.setdefault("unsupported", []).append(t)
        sys.stderr.write("fake docker: unsupported subcommand: %s\n" % " ".join(t[:2]))
        return 125
    return 0

main()
'''


class FakeDocker:
    """One scriptable fake `docker` CLI, owned by one test."""

    def __init__(self, tmp: Path, *, images: list[str] | None = None, userns: str = ""):
        self.dir = Path(tmp) / "fake-docker"
        self.bin_dir = self.dir / "bin"
        self.state_dir = self.dir / "state"
        self.bin_dir.mkdir(parents=True)
        self.state_dir.mkdir(parents=True)
        self.docker = self.bin_dir / "docker"
        self.docker.write_text(SCRIPT.format(python=sys.executable, state=str(self.state_dir)))
        self.docker.chmod(self.docker.stat().st_mode | stat.S_IEXEC)
        security = ["name=seccomp,profile=builtin"] + ([f"name={userns}"] if userns else [])
        self._write({"seq": 0, "containers": {}, "rules": [], "images": images,
                     "workload_queue": [], "default_workload": {"exit_code": 0, "runs": False},
                     "info": {"ServerVersion": "27.0.0", "LoggingDriver": "json-file",
                              "SecurityOptions": security, "OSType": "linux",
                              "Rootless": userns == "rootless", "ID": "FAKE"}})
        (self.state_dir / "lock").touch()
        (self.state_dir / "calls.jsonl").touch()

    # -- wiring -------------------------------------------------------------
    def install(self, monkeypatch) -> "FakeDocker":
        """Put the fake first on PATH: it is `docker` for this test's processes."""
        monkeypatch.setenv("PATH", str(self.bin_dir) + os.pathsep + os.environ.get("PATH", ""))
        return self

    # -- state access (file locked, so safe next to a running scheduler) ----
    def _lock(self):
        import fcntl
        handle = open(self.state_dir / "lock", "a")
        fcntl.flock(handle, fcntl.LOCK_EX)
        return handle

    def _read(self) -> dict:
        return json.loads((self.state_dir / "state.json").read_text())

    def _write(self, state: dict) -> None:
        tmp = self.state_dir / "state.json.tmp"
        tmp.write_text(json.dumps(state))
        os.replace(tmp, self.state_dir / "state.json")

    def _mutate(self, fn) -> Any:
        handle = self._lock()
        try:
            state = self._read()
            result = fn(state)
            self._write(state)
            return result
        finally:
            handle.close()

    def state(self) -> dict:
        handle = self._lock()
        try:
            return self._read()
        finally:
            handle.close()

    # -- what happened ------------------------------------------------------
    def calls(self) -> list[list[str]]:
        """Every argv the fake was run with (without the program name), in order."""
        rows = [json.loads(line) for line in (self.state_dir / "calls.jsonl").read_text().splitlines() if line]
        return [r["argv"] for r in sorted(rows, key=lambda r: r["seq"])]

    def commands(self) -> list[list[str]]:
        """Calls with the global options dropped and `docker container X` folded to `X`."""
        rows = [json.loads(line) for line in (self.state_dir / "calls.jsonl").read_text().splitlines() if line]
        out = []
        for r in sorted(rows, key=lambda r: r["seq"]):
            cmd = r["cmd"]
            out.append(cmd[1:] if cmd[:1] == ["container"] and len(cmd) > 1 else cmd)
        return out

    def calls_of(self, *prefix: str) -> list[list[str]]:
        n = len(prefix)
        return [c for c in self.commands() if c[:n] == list(prefix)]

    def unsupported(self) -> list[list[str]]:
        return self.state().get("unsupported", [])

    def resources_created(self) -> list[dict]:
        """Networks and volumes the backend created (a container needs none)."""
        return self.state().get("resources", [])

    def containers(self) -> dict[str, dict]:
        return {cid: c for cid, c in self.state()["containers"].items()}

    def container(self, ref: str) -> dict | None:
        for cid, c in self.containers().items():
            if ref in (cid, c["Name"], c["Name"].lstrip("/")):
                return c
        return None

    def by_label(self, key: str, value: str | None = None) -> list[dict]:
        out = []
        for c in self.containers().values():
            labels = c["Config"]["Labels"]
            if key in labels and (value is None or labels[key] == value):
                out.append(c)
        return out

    def start_count(self, ref: str) -> int:
        c = self.container(ref)
        return c["StartCount"] if c else 0

    def stdout_of(self, ref: str) -> bytes:
        c = self.container(ref)
        return base64.b64decode(c["_stdout"]) if c else b""

    def stderr_of(self, ref: str) -> bytes:
        c = self.container(ref)
        return base64.b64decode(c["_stderr"]) if c else b""

    # -- scripting ----------------------------------------------------------
    def rule(self, *match: str, effect: str = "fail", times: int | None = None, after: int = 0,
             stderr: str | None = None, code: int = 1, contains: str | None = None, **extra) -> None:
        """Script an outcome for commands starting with `match`.
        effects: fail | daemon_down | hang(seconds=) | garbage | empty | after_effect | hang_after(seconds=)."""
        rule = {"match": list(match), "effect": effect, "after": after, "code": code, **extra}
        if times is not None:
            rule["times"] = times
        if stderr is not None:
            rule["stderr"] = stderr
        if contains is not None:
            rule["contains"] = contains
        self._mutate(lambda s: s["rules"].append(rule))

    def clear_rules(self) -> None:
        self._mutate(lambda s: s.update(rules=[]))

    def daemon_down(self, down: bool = True) -> None:
        """Every command reports that the daemon is unreachable (or stop doing so)."""
        if down:
            self.rule(effect="daemon_down")
        else:
            self.clear_rules()

    def set_images(self, images: list[str] | None) -> None:
        """None: every image exists. A list: only these do."""
        self._mutate(lambda s: s.update(images=images))

    def set_info(self, **fields) -> None:
        self._mutate(lambda s: s["info"].update(fields))

    def workload(self, exit_code: int = 0, stdout: bytes = b"", stderr: bytes = b"", oom: bool = False,
                 runs: bool = False, ignore_term: bool = False, queue: bool = False) -> None:
        """What a started container does. `runs`: stays running until `finish`
        or stop. `queue`: only for the next start (otherwise the default)."""
        w = {"exit_code": exit_code, "stdout": base64.b64encode(stdout).decode(),
             "stderr": base64.b64encode(stderr).decode(), "oom": oom, "runs": runs,
             "ignore_term": ignore_term}
        if queue:
            self._mutate(lambda s: s["workload_queue"].append(w))
        else:
            self._mutate(lambda s: s.update(default_workload=w))

    def append_output(self, ref: str, stdout: bytes = b"", stderr: bytes = b"") -> None:
        def go(s):
            c = self._find(s, ref)
            c["_stdout"] = base64.b64encode(base64.b64decode(c["_stdout"]) + stdout).decode()
            c["_stderr"] = base64.b64encode(base64.b64decode(c["_stderr"]) + stderr).decode()
        self._mutate(go)

    def finish(self, ref: str, code: int = 0, oom: bool = False, stdout: bytes = b"", stderr: bytes = b"") -> None:
        """The container's workload ends (as Docker would record it)."""
        def go(s):
            c = self._find(s, ref)
            c["_stdout"] = base64.b64encode(base64.b64decode(c["_stdout"]) + stdout).decode()
            c["_stderr"] = base64.b64encode(base64.b64decode(c["_stderr"]) + stderr).decode()
            c["State"].update(Status="exited", Running=False, ExitCode=code, OOMKilled=oom, Pid=0,
                              FinishedAt=time.strftime("%Y-%m-%dT%H:%M:%S.000000Z", time.gmtime()))
        self._mutate(go)

    def _find(self, state: dict, ref: str) -> dict:
        for cid, c in state["containers"].items():
            if ref in (cid, c["Name"], c["Name"].lstrip("/")) or cid.startswith(ref):
                return c
        raise KeyError(ref)

    def inject(self, name: str, labels: dict[str, str] | None = None, image: str = "multiagents/workspace:latest",
               status: str = "exited") -> str:
        """A container that exists in Docker but that no attempt record names
        (an orphan, or another project's), made behind the backend's back."""
        import hashlib
        def go(s):
            s["seq"] += 1
            cid = hashlib.sha256(f"injected-{s['seq']}-{name}".encode()).hexdigest()
            created = time.strftime("%Y-%m-%dT%H:%M:%S.000000Z", time.gmtime())
            s["containers"][cid] = {
                "Id": cid, "Name": "/" + name, "Created": created, "Image": "sha256:x",
                "State": {"Status": status, "Running": status == "running", "Paused": False,
                          "Restarting": False, "OOMKilled": False, "Dead": False, "Pid": 0,
                          "ExitCode": 0, "Error": "", "StartedAt": created,
                          "FinishedAt": created if status != "running" else "0001-01-01T00:00:00Z"},
                "StartCount": 1, "CreateArgv": [], "UnknownOptions": [],
                "_stdout": "", "_stderr": "",
                "Config": {"Image": image, "User": "", "Labels": dict(labels or {}), "Env": [],
                           "Cmd": None, "Entrypoint": None, "WorkingDir": "", "Hostname": ""},
                "HostConfig": {"Binds": None, "Mounts": None, "NetworkMode": "default", "Memory": 0,
                               "PidsLimit": None, "CpuShares": 0, "NanoCpus": 0, "CapAdd": None,
                               "CapDrop": None, "SecurityOpt": None, "Privileged": False,
                               "PidMode": "", "IpcMode": "private", "UsernsMode": "",
                               "RestartPolicy": {"Name": "no", "MaximumRetryCount": 0},
                               "LogConfig": {"Type": "json-file", "Config": {}}, "LogConfigPinned": False}}
            return cid
        return self._mutate(go)

    # -- networks (slice 4B) --------------------------------------------------
    def networks(self) -> dict[str, dict]:
        return dict(self.state().get("networks", {}))

    def network(self, ref: str) -> dict | None:
        for nid, n in self.networks().items():
            if ref in (nid, n["Name"]):
                return n
        return None

    def members(self, ref: str) -> list[str]:
        """Container ids attached to the network `ref` (id or name)."""
        n = self.network(ref)
        return sorted(n["Containers"]) if n else []

    def networks_of(self, ref: str) -> dict[str, dict]:
        """`NetworkSettings.Networks` of a container, by network name."""
        c = self.container(ref)
        return dict((c or {}).get("NetworkSettings", {}).get("Networks", {}))

    def inject_network(self, name: str, labels: dict[str, str] | None = None, *, internal: bool = True,
                       members: list[str] | None = None, created: str | None = None) -> str:
        """A network that exists in Docker behind the backend's back."""
        import hashlib
        def go(s):
            s["seq"] += 1
            nid = hashlib.sha256(f"injected-net-{s['seq']}-{name}".encode()).hexdigest()
            stamp = created or time.strftime("%Y-%m-%dT%H:%M:%S.000000Z", time.gmtime())
            net = {"Id": nid, "Name": name, "Created": stamp, "Driver": "bridge", "Internal": internal,
                   "Labels": dict(labels or {}), "Containers": {}, "Options": {}}
            for ref in members or []:
                c = self._find(s, ref)
                net["Containers"][c["Id"]] = {"Name": c["Name"].lstrip("/"), "EndpointID": "x", "IPv4Address": "10.0.0.2/16"}
                c.setdefault("NetworkSettings", {"Networks": {}}).setdefault("Networks", {})[name] = {
                    "NetworkID": nid, "Aliases": []}
            s.setdefault("networks", {})[nid] = net
            return nid
        return self._mutate(go)

    def attach(self, network: str, container: str, aliases: list[str] | None = None) -> None:
        """Attach a container to a network behind the backend's back."""
        def go(s):
            net = next(n for n in s["networks"].values() if network in (n["Id"], n["Name"]))
            c = self._find(s, container)
            net["Containers"][c["Id"]] = {"Name": c["Name"].lstrip("/"), "EndpointID": "x", "IPv4Address": "10.9.0.2/16"}
            c.setdefault("NetworkSettings", {"Networks": {}}).setdefault("Networks", {})[net["Name"]] = {
                "NetworkID": net["Id"], "Aliases": list(aliases or [])}
        self._mutate(go)

    def remove_behind_its_back(self, ref: str) -> None:
        def go(s):
            c = self._find(s, ref)
            del s["containers"][c["Id"]]
        self._mutate(go)


# --------------------------------------------------------------- semantic views

def mounts_of(container: dict) -> list[dict]:
    """Every mount of a container, whichever option made it:
    [{"type", "source", "destination", "rw"}] (tmpfs as type "tmpfs")."""
    host = container["HostConfig"]
    out = []
    for bind in host.get("Binds") or []:
        parts = bind.split(":")
        mode = parts[2] if len(parts) > 2 else ""
        out.append({"type": "bind" if parts[0].startswith("/") else "volume", "source": parts[0],
                    "destination": parts[1] if len(parts) > 1 else "", "rw": "ro" not in mode.split(",")})
    for m in host.get("Mounts") or []:
        out.append({"type": m["Type"], "source": m["Source"], "destination": m["Destination"], "rw": m["RW"]})
    for dest in (host.get("Tmpfs") or {}):
        out.append({"type": "tmpfs", "source": "", "destination": dest, "rw": True})
    return out


def memory_bytes(text: str) -> int:
    t = text.strip().lower()
    m = re.fullmatch(r"(-?(?:\d+(?:\.\d*)?|\.\d+)) ?([kmgtp])?i?b?", t)
    if not m:
        raise ValueError("invalid size: " + text)
    return int(float(m.group(1)) * 1024 ** (" kmgtp".index(m.group(2)) if m.group(2) else 0))


def may_lose_log_data(container: dict) -> bool:
    """True when the container's logging could silently drop output: a driver
    `docker logs` cannot read back, or a rotation/size cap that discards."""
    log = container["HostConfig"]["LogConfig"]
    driver, cfg = log["Type"], log.get("Config") or {}
    if driver not in ("json-file", "local", "journald"):
        return True
    size = cfg.get("max-size")
    if driver == "local":
        return size != "-1"                         # local defaults to a 20m x 5 rotation
    if driver == "json-file":
        return size not in (None, "-1", "0")
    return False
