"""Fixtures for the D2 tmux viewer contract (context/specs/d2-tmux-viewer.md).

Everything runs through the CLI as a subprocess (`python -m multiagents.cli`),
against a tmp project and a tmp state root. tmux is a fake script on PATH that
records its argv and keeps a small model of one server per `-S` socket, so a
test can see what a correct caller would have caused without touching a real
tmux server.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

SRC = str(Path(__file__).resolve().parents[2] / "src")

FAKE_TMUX = r'''#!__PY__
import json, os, signal, subprocess, sys, time, fcntl

D = os.environ["FAKE_TMUX_DIR"]
argv = sys.argv[1:]
sock = None
i = 0
while i < len(argv) and argv[i].startswith("-"):
    if argv[i] in ("-S", "-L", "-f", "-c", "-T"):
        if argv[i] == "-S":
            sock = argv[i + 1]
        i += 2
    else:
        i += 1
cmd = argv[i] if i < len(argv) else ""
rest = argv[i + 1:]

def log(**extra):
    rec = {"argv": argv, "sock": sock, "cmd": cmd, "rest": rest,
           "sock_exists": bool(sock) and os.path.lexists(sock)}
    rec.update(extra)
    with open(os.path.join(D, "log.jsonl"), "a") as fh:
        fh.write(json.dumps(rec) + "\n")

lock = open(os.path.join(D, "lock"), "a")
fcntl.flock(lock, fcntl.LOCK_EX)
sp = os.path.join(D, "state.json")
state = json.load(open(sp)) if os.path.exists(sp) else {}

def save():
    json.dump(state, open(sp, "w"))

def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False

def die(msg, code=1):
    sys.stderr.write(msg + "\n")
    sys.exit(code)

log()
if sock is None:
    die("fake tmux: no -S given (would use the default server)", 99)
if cmd in ("attach", "attach-session", "a", "at"):
    die("fake tmux: attach is never expected from a headless caller", 97)

srv = state.get(sock)
if srv and not alive(srv["pid"]):
    srv = None
    state.pop(sock, None)

def parse(args, valued):
    flags, pos, j = {}, [], 0
    while j < len(args):
        a = args[j]
        if a.startswith("-") and len(a) > 1:
            if a[1] in valued and len(a) == 2:
                flags[a[1]] = args[j + 1]
                j += 2
                continue
            flags[a[1]] = True
        else:
            pos.append(a)
        j += 1
    return flags, pos

def target(t):
    t = t.lstrip("=")
    if ":" in t:
        s, w = t.split(":", 1)
        return s.lstrip("="), w.lstrip("=")
    return t, None

def drop_server():
    s = state.pop(sock, None)
    if s:
        try:
            os.kill(s["pid"], signal.SIGTERM)
        except OSError:
            pass

if cmd in ("new-session", "new", "new-sessionn"):
    f, pos = parse(rest, "snxycFte")
    if srv is None:
        if os.path.lexists(sock):
            die("error creating %s (Address already in use)" % sock)
        code = ("import socket,os,sys,time\n"
                "s=socket.socket(socket.AF_UNIX);s.bind(sys.argv[1]);s.listen(4)\n"
                "end=time.time()+600\n"
                "while os.path.exists(sys.argv[1]) and time.time()<end: time.sleep(0.2)\n")
        p = subprocess.Popen([sys.executable, "-c", code, sock], start_new_session=True,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        for _ in range(50):
            if os.path.lexists(sock):
                break
            time.sleep(0.05)
        srv = state[sock] = {"pid": p.pid, "sessions": {}}
    name = f.get("s") or str(len(srv["sessions"]))
    if name in srv["sessions"]:
        die("duplicate session: %s" % name)
    srv["sessions"][name] = [{"name": f.get("n") or "sh", "cmd": pos}]
    save()
    sys.exit(0)

if srv is None:
    die("no server running on %s" % sock)

if cmd in ("has-session", "has"):
    f, _ = parse(rest, "t")
    s, _w = target(f.get("t", ""))
    sys.exit(0 if s in srv["sessions"] else 1)

if cmd in ("list-sessions", "ls"):
    for s in srv["sessions"]:
        print(s)
    sys.exit(0)

if cmd in ("list-windows", "lsw"):
    f, _ = parse(rest, "tF")
    s, _w = target(f.get("t", ""))
    if s not in srv["sessions"]:
        die("can't find session: %s" % s)
    for n, w in enumerate(srv["sessions"][s]):
        print(w["name"] if "F" in f else "%d: %s* (1 panes)" % (n, w["name"]))
    sys.exit(0)

if cmd in ("new-window", "neww"):
    f, pos = parse(rest, "tncFe")
    s, _w = target(f.get("t", ""))
    if s not in srv["sessions"]:
        die("can't find session: %s" % s)
    srv["sessions"][s].append({"name": f.get("n") or "sh", "cmd": pos})
    save()
    sys.exit(0)

if cmd in ("select-window", "selectw"):
    f, _ = parse(rest, "t")
    s, w = target(f.get("t", ""))
    if s not in srv["sessions"] or not any(x["name"] == w for x in srv["sessions"][s]):
        die("can't find window: %s" % w)
    sys.exit(0)

def after_removal():
    for s in [k for k, v in srv["sessions"].items() if not v]:
        del srv["sessions"][s]
    if not srv["sessions"]:
        drop_server()
    else:
        pass
    save()

if cmd in ("kill-window", "killw"):
    f, _ = parse(rest, "t")
    s, w = target(f.get("t", ""))
    if s not in srv["sessions"]:
        die("can't find session: %s" % s)
    before = len(srv["sessions"][s])
    srv["sessions"][s] = [x for x in srv["sessions"][s] if x["name"] != w]
    if len(srv["sessions"][s]) == before:
        die("can't find window: %s" % w)
    after_removal()
    sys.exit(0)

if cmd in ("kill-session",):
    f, _ = parse(rest, "t")
    s, _w = target(f.get("t", ""))
    if s not in srv["sessions"]:
        die("can't find session: %s" % s)
    srv["sessions"][s] = []
    after_removal()
    sys.exit(0)

if cmd == "kill-server":
    drop_server()
    save()
    sys.exit(0)

save()
sys.exit(0)
'''


class FakeTmux:
    def __init__(self, root: Path):
        self.dir = root / "faketmux"
        self.bin = self.dir / "bin"
        self.bin.mkdir(parents=True)
        script = self.bin / "tmux"
        script.write_text(FAKE_TMUX.replace("__PY__", sys.executable))
        script.chmod(0o755)

    @property
    def calls(self) -> list[dict]:
        path = self.dir / "log.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line]

    def calls_of(self, *cmds: str) -> list[dict]:
        return [c for c in self.calls if c["cmd"] in cmds]

    def state(self) -> dict:
        path = self.dir / "state.json"
        return json.loads(path.read_text()) if path.exists() else {}

    def windows(self, sock, session) -> list[str]:
        server = self.state().get(str(sock)) or {}
        return [w["name"] for w in (server.get("sessions") or {}).get(session, [])]

    def sessions(self, sock) -> list[str]:
        return list(((self.state().get(str(sock)) or {}).get("sessions") or {}))

    def created_windows(self) -> list[dict]:
        """Every window-creating call, as (name, command) pairs."""
        out = []
        for c in self.calls_of("new-session", "new", "new-window", "neww"):
            name = cmdline = None
            r = c["rest"]
            valued = "snxycFte"
            j = 0
            pos = []
            while j < len(r):
                if r[j].startswith("-") and len(r[j]) == 2 and r[j][1] in valued:
                    if r[j] == "-n":
                        name = r[j + 1]
                    j += 2
                    continue
                if not r[j].startswith("-"):
                    pos.append(r[j])
                j += 1
            out.append({"call": c, "name": name, "command": " ".join(pos)})
        return out

    def cleanup(self):
        for sock, server in self.state().items():
            try:
                os.kill(server["pid"], signal.SIGTERM)
            except OSError:
                pass


def not_implemented(text: str, args=()):
    """argparse rejects an unknown subcommand with exit 2 — the same code some
    contract errors use — so a missing command must fail loudly here, or every
    'exits 2' test would pass for the wrong reason."""
    if "invalid choice" in text and "argument command" in text:
        raise AssertionError(f"subcommand not implemented yet: {' '.join(args)}\n{text}")


class Follower:
    """A running `multiagents view` whose output can be waited on."""

    def __init__(self, proc: subprocess.Popen):
        self.proc = proc
        self.chunks: list[bytes] = []
        self._t = threading.Thread(target=self._read, daemon=True)
        self._t.start()

    def _read(self):
        while True:
            data = os.read(self.proc.stdout.fileno(), 65536)
            if not data:
                return
            self.chunks.append(data)

    @property
    def out(self) -> str:
        return b"".join(self.chunks).decode("utf-8", "replace")

    def wait_for(self, needle: str, timeout: float = 12.0) -> bool:
        end = time.time() + timeout
        while time.time() < end:
            not_implemented(self.out)
            if needle in self.out:
                return True
            if self.proc.poll() is not None and needle not in self.out:
                time.sleep(0.2)
                return needle in self.out
            time.sleep(0.05)
        return needle in self.out

    def alive(self) -> bool:
        not_implemented(self.out)
        return self.proc.poll() is None

    def wait_exit(self, timeout: float) -> int | None:
        try:
            code = self.proc.wait(timeout)
        except subprocess.TimeoutExpired:
            return None
        self._t.join(2)
        not_implemented(self.out)
        return code

    def stop(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self._t.join(2)


def agent_id(n: int = 1, suffix: str | None = None) -> str:
    return f"ag-{0xabc000 + n:06x}" + (f"-{suffix}" if suffix else "")


class D2Project:
    def __init__(self, tmp_path: Path, monkeypatch):
        # Short, because the fake server binds a real AF_UNIX socket at
        # <state>/host-authority/<slug>/tmux/sock and sun_path is ~108 bytes.
        self.state = Path(tempfile.mkdtemp(prefix="d2s", dir="/tmp")).resolve()
        self.home = tmp_path / "home"
        self.home.mkdir()
        self.config_dir = tmp_path / "cfg"
        self.root = (tmp_path / "p").resolve()
        self.root.mkdir()
        monkeypatch.setenv("MULTIAGENTS_STATE_DIR", str(self.state))
        monkeypatch.setenv("MULTIAGENTS_CONFIG_DIR", str(self.config_dir))
        from multiagents.paths import ProjectPaths
        self.paths = ProjectPaths(self.root)
        self.paths.ensure()
        self.tmux = FakeTmux(tmp_path)
        self.tmux_dir_of_bin = str(self.tmux.bin)

    # ---------------------------------------------------------------- paths --
    @property
    def slug(self) -> str:
        return self.paths.slug

    @property
    def session(self) -> str:
        return f"ma-{self.slug}"

    @property
    def sock_dir(self) -> Path:
        return self.state / "host-authority" / self.slug / "tmux"

    @property
    def sock(self) -> Path:
        return self.sock_dir / "sock"

    def attach_command(self, aid: str) -> str:
        return f"tmux -S {self.sock} attach -r -t {self.session}:{aid}"

    def run_dir(self, aid: str) -> Path:
        return self.paths.run_dir(aid)

    def stream(self, aid: str) -> Path:
        return self.run_dir(aid) / "stream.jsonl"

    # --------------------------------------------------------------- agents --
    def add_agent(self, aid: str, status: str = "running", events=None,
                  raw_lines: list[bytes] | None = None, pid=None) -> Path:
        from multiagents.tree import Node, Tree
        Tree(self.paths.tree_file, self.paths.events_file).add(
            Node(id=aid, agent="worker", provider="fake", model="m",
                 parent=None, depth=1, status=status, pid=pid))
        self.run_dir(aid).mkdir(parents=True, exist_ok=True)
        if events is not None or raw_lines is not None:
            data = b""
            for e in events or []:
                data += json.dumps(e).encode() + b"\n"
            for line in raw_lines or []:
                data += line
            self.stream(aid).write_bytes(data)
        return self.stream(aid)

    def set_status(self, aid: str, status: str):
        from multiagents.tree import Tree
        Tree(self.paths.tree_file, self.paths.events_file).set_status(aid, status)

    def append(self, aid: str, *events, raw: bytes = b""):
        with open(self.stream(aid), "ab") as fh:
            for e in events:
                fh.write(json.dumps(e).encode() + b"\n")
            fh.write(raw)

    # ------------------------------------------------------------- running --
    def env(self, tmux: bool = True, path: str | None = None) -> dict:
        env = {
            "PATH": path if path is not None else (self.tmux_dir_of_bin if tmux else ""),
            "PYTHONPATH": SRC,
            "PYTHONUNBUFFERED": "1",
            "HOME": str(self.home),
            "MULTIAGENTS_STATE_DIR": str(self.state),
            "MULTIAGENTS_CONFIG_DIR": str(self.config_dir),
            "FAKE_TMUX_DIR": str(self.tmux.dir),
            "LANG": "C.UTF-8",
        }
        return env

    def argv(self, *args: str) -> list[str]:
        return [sys.executable, "-m", "multiagents.cli", "--path", str(self.root), *args]

    def run(self, *args: str, tmux: bool = True, path: str | None = None,
            timeout: float = 30, cwd=None, env=None) -> subprocess.CompletedProcess:
        cp = subprocess.run(self.argv(*args), capture_output=True, timeout=timeout,
                            env=env or self.env(tmux, path), cwd=cwd or self.root)
        not_implemented(cp.stderr.decode("utf-8", "replace"), args)
        return cp

    def follow(self, *args: str, cwd=None) -> Follower:
        proc = subprocess.Popen(self.argv(*args), stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, env=self.env(False),
                                cwd=cwd or self.root, stdin=subprocess.DEVNULL)
        return Follower(proc)

    # ------------------------------------------------------------- helpers --
    def snapshot_of_files(self, base: Path) -> dict:
        out = {}
        for p in sorted(base.rglob("*")):
            st = p.lstat()
            key = str(p.relative_to(base))
            if stat.S_ISREG(st.st_mode):
                out[key] = (p.read_bytes(), st.st_mtime_ns, st.st_size)
            else:
                out[key] = (stat.S_IFMT(st.st_mode),)
        return out

    def cleanup(self):
        self.tmux.cleanup()
        shutil.rmtree(self.state, ignore_errors=True)


def sock_of(call: dict) -> str | None:
    return call["sock"]


def out_text(cp) -> str:
    return cp.stdout.decode("utf-8", "replace")


def err_text(cp) -> str:
    return cp.stderr.decode("utf-8", "replace")


def visible_escape(text: str, code: int) -> bool:
    """Whether control character `code` shows up as a visible escape.

    Accepts the usual conventions rather than pinning one: \\x1b, \\u001b,
    0x1b, <1b>, octal, caret notation, Unicode control pictures, and the
    short C names. The contract says "escaped visibly", not how.
    """
    forms = [f"x{code:02x}", f"X{code:02X}", f"u{code:04x}", f"U+{code:04X}",
             f"\\{code:o}", f"<{code:02x}>", f"<{code:02X}>", f"&#{code};"]
    if code < 0x20:
        forms += [f"^{chr(code ^ 0x40)}", chr(0x2400 + code)]
    if code == 0x7f:
        forms += ["^?", chr(0x2421)]
    names = {0x1b: ["\\e", "ESC"], 0x0d: ["\\r", "CR"], 0x08: ["\\b", "BS"],
             0x07: ["\\a", "BEL"], 0x9b: ["CSI"]}
    forms += names.get(code, [])
    return any(f in text for f in forms)


def has_raw_control(data: bytes) -> list[int]:
    """C0 (except \\n, \\t), DEL, and C1 as UTF-8, in `data`."""
    bad = [b for b in data if (b < 0x20 and b not in (0x0a, 0x09)) or b == 0x7f]
    text = data.decode("utf-8", "replace")
    bad += [ord(ch) for ch in text if 0x80 <= ord(ch) <= 0x9f]
    return bad


import pytest  # noqa: E402


@pytest.fixture
def proj(tmp_path, monkeypatch):
    p = D2Project(tmp_path, monkeypatch)
    try:
        yield p
    finally:
        p.cleanup()
