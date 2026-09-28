"""Black-box harness for the codex provider contract (CX-C7..CX-C14).

The adapter is run the way the engine runs it: as an executable, with an
explicit environment, never imported. The native CLI it drives is a fake
`codex` written into a tmp dir and handed over through `MULTIAGENTS_BIN`
(CX-C2). The fake is deliberately NOT on PATH, so an adapter that looks for
`codex` on PATH instead of reading `MULTIAGENTS_BIN` finds nothing.

The fake reads its behaviour from `behaviour.json` beside itself rather than
from the environment, because the adapter is free to rebuild the environment
it hands the CLI. It appends one JSON record per invocation to `calls.jsonl`:
argv, cwd, stdin (for `exec` only), and the CODEX_HOME / HOME it was given.

Nothing here reaches the network, docker, or the real `codex`.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
DEFAULTS = ROOT / "src" / "multiagents" / "defaults"
ADAPTER = DEFAULTS / "providers" / "codex.py"
PROVIDERS_YAML = DEFAULTS / "providers.yaml"
PROJECT_YAML = DEFAULTS / "project.yaml"

# Printed by the fake on every channel an auth command has. It must never
# come back out of the adapter (CX-C9: `check` never prints the CLI's output).
SECRET = "sk-SECRET-never-print-4242"

# The exact stderr line of CX-C9 (amended) for an auth failure in a run.
AUTH_LINE = "codex: not authenticated — run: multiagents auth login codex"
RESUME_FAILED = "codex: resume failed:"

# ---------------------------------------------------------------------------
# CX-C10 (amended): "every invocation of the native CLI passes the option that
# disables its update check". The exact key is confirmed during implementation
# and recorded in the spec. THIS IS THE ONE PLACE TO CHANGE IT: a `-c` config
# override `key=value`, compared after TOML parsing. If it turns out to be a
# plain flag instead, change `update_check_disabled` below and nothing else.
UPDATE_CHECK_OFF = ("check_for_update_on_startup", False)


def update_check_disabled(argv: list[str]) -> bool:
    key, value = UPDATE_CHECK_OFF
    return lookup(native_config(argv), key) == value
# ---------------------------------------------------------------------------


FAKE_CODEX = r'''#!/usr/bin/env python3
import json, os, sys, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
B = json.loads((HERE / "behaviour.json").read_text())
SECRET = B["secret"]
argv = sys.argv[1:]
record = {"argv": argv, "cwd": os.getcwd(), "stdin": None,
          "codex_home": os.environ.get("CODEX_HOME"), "home": os.environ.get("HOME")}
if "exec" in argv:
    record["stdin"] = sys.stdin.read()
with open(HERE / "calls.jsonl", "a") as fh:
    fh.write(json.dumps(record) + "\n")

words = [a for a in argv if not a.startswith("-")]

if "mcp" in argv and "list" in argv:
    print(B.get("mcp_list", "[]"))
    sys.exit(B.get("mcp_list_exit", 0))

if "login" in argv and "status" in argv:
    status = B.get("status", "logged_in")
    if status == "logged_in":
        print("Logged in using ChatGPT " + SECRET)
        print("token " + SECRET, file=sys.stderr)
        sys.exit(0)
    if status == "not_logged_in":
        print("Not logged in " + SECRET)
        print(SECRET, file=sys.stderr)
        sys.exit(1)
    if status == "refresh_failed":
        print("Error: " + B["refresh_message"] + " " + SECRET, file=sys.stderr)
        sys.exit(1)
    if status == "hang":
        time.sleep(60)
        sys.exit(0)
    sys.exit(3)

if "login" in argv:
    print("Visit https://auth.example/device and enter code ABCD " + SECRET)
    sys.exit(B.get("login_exit", 0))

if "exec" in argv:
    events = B.get("events", [])
    if "resume" in argv:
        gone = B.get("resume_gone")
        if gone == "error":
            print("Error: no rollout found for thread id " + argv[argv.index("resume") + 1],
                  file=sys.stderr)
            sys.exit(1)
        # A live resume re-announces the same thread (NEED_INFO: assumed);
        # a gone one, in the "fresh" variant, silently starts a new thread.
        thread = "brand-new-thread" if gone == "fresh" else argv[argv.index("resume") + 1]
        events = [{"type": "thread.started", "thread_id": thread}] + [
            e for e in events if e.get("type") != "thread.started"]
    for event in events:
        print(json.dumps(event), flush=True)
    if B.get("stderr"):
        print(B["stderr"], file=sys.stderr)
    sys.exit(B.get("exit", 0))

sys.exit(0)
'''


class FakeCodex:
    """A fake native `codex` in its own directory, off PATH."""

    def __init__(self, root: Path):
        self.dir = root / "native"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "codex"
        self.path.write_text(FAKE_CODEX)
        self.path.chmod(0o755)
        self.behaviour: dict[str, Any] = {"secret": SECRET}
        self.save()

    def set(self, **values: Any) -> None:
        self.behaviour.update(values)
        self.save()

    def save(self) -> None:
        (self.dir / "behaviour.json").write_text(json.dumps(self.behaviour))

    def calls(self) -> list[dict[str, Any]]:
        log = self.dir / "calls.jsonl"
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text().splitlines() if line]

    def exec_calls(self) -> list[dict[str, Any]]:
        return [c for c in self.calls() if "exec" in c["argv"]]

    def reset_calls(self) -> None:
        (self.dir / "calls.jsonl").unlink(missing_ok=True)


def base_env(tmp: Path, fake: FakeCodex | None, **extra: str) -> dict[str, str]:
    """An explicit environment: nothing inherited from whoever runs the suite."""
    home = tmp / "home"
    home.mkdir(parents=True, exist_ok=True)
    env = {
        "PATH": f"{Path(sys.executable).parent}:/usr/bin:/bin",
        "HOME": str(home),
        "LANG": "C.UTF-8",
        "MULTIAGENTS_EXECUTOR": "local",
        "MULTIAGENTS_PROVIDER": "codex",
        "MULTIAGENTS_CODEX_PROFILE": str(tmp / "profile"),
    }
    if fake is not None:
        env["MULTIAGENTS_BIN"] = str(fake.path)
    env.update(extra)
    return {k: v for k, v in env.items() if v is not None}


def require_adapter() -> None:
    assert ADAPTER.is_file(), f"CX-C7: the codex adapter is not shipped at {ADAPTER}"
    assert os.access(ADAPTER, os.X_OK), f"CX-C7: {ADAPTER} is not executable"


def invoke(args: list[str], env: dict[str, str], *, cwd: Path | None = None,
           timeout: float = 60, preexec_fn=None) -> subprocess.CompletedProcess:
    """Run the adapter as the engine does: argv[0] is the adapter itself."""
    require_adapter()
    return subprocess.run([str(ADAPTER), *args], env=env, cwd=cwd, capture_output=True,
                          text=True, errors="replace", timeout=timeout,
                          stdin=subprocess.DEVNULL, preexec_fn=preexec_fn)


def block() -> dict[str, Any]:
    data = yaml.safe_load(PROVIDERS_YAML.read_text()) or {}
    providers = data.get("providers") or {}
    assert "codex" in providers, "CX-C7: no `codex:` block in defaults/providers.yaml"
    return providers["codex"]


def provider():
    from multiagents.providers import Provider, resolve_inheritance
    data = yaml.safe_load(PROVIDERS_YAML.read_text()) or {}
    raw = resolve_inheritance(data.get("providers") or {})
    assert "codex" in raw, "CX-C7: no `codex:` block in defaults/providers.yaml"
    return Provider.from_dict("codex", raw["codex"])


def events(prov, stdout: str) -> list:
    out = []
    for line in stdout.splitlines():
        event = prov.parse_line(line)
        if event is not None:
            out.append(event)
    return out


# ------------------------------------------------------------ -c overrides --

def _deep_merge(into: dict, new: dict) -> None:
    for key, value in new.items():
        if isinstance(value, dict) and isinstance(into.get(key), dict):
            _deep_merge(into[key], value)
        else:
            into[key] = value


def native_config(argv: list[str]) -> dict[str, Any]:
    """Every `-c key=value` override in a native argv, merged as Codex merges them.

    Codex parses each value as TOML and falls back to a literal string, and
    dotted keys address nested tables. So `-c mcp_servers={...}` and
    `-c mcp_servers.x.enabled=false` are both understood.
    """
    merged: dict[str, Any] = {}
    i = 0
    while i < len(argv):
        token = argv[i]
        item = None
        if token in ("-c", "--config") and i + 1 < len(argv):
            item = argv[i + 1]
            i += 1
        elif token.startswith("--config="):
            item = token.split("=", 1)[1]
        i += 1
        if item is None or "=" not in item:
            continue
        key, value = item.split("=", 1)
        try:
            parsed = tomllib.loads(f"{key} = {value}")
        except tomllib.TOMLDecodeError:
            try:
                parsed = tomllib.loads(f"{key} = {json.dumps(value)}")
            except tomllib.TOMLDecodeError:
                continue
        _deep_merge(merged, parsed)
    return merged


def lookup(config: dict, dotted: str) -> Any:
    node: Any = config
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def sandbox_mode(argv: list[str]) -> str | None:
    """The sandbox the native CLI was told to use, in either spelling."""
    found = lookup(native_config(argv), "sandbox_mode")
    for flag in ("--sandbox", "-s"):
        if flag in argv:
            found = argv[argv.index(flag) + 1]
    return found


def approval_policy(argv: list[str]) -> str | None:
    found = lookup(native_config(argv), "approval_policy")
    for flag in ("--ask-for-approval", "-a"):
        if flag in argv:
            found = argv[argv.index(flag) + 1]
    return found


# --------------------------------------------------------- rollout fixtures --

def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def window(percent: Any, minutes: Any, resets_at: Any) -> dict[str, Any]:
    return {"used_percent": percent, "window_minutes": minutes, "resets_at": resets_at}


def token_count_line(ts: float, primary: dict | None, secondary: dict | None,
                     **extra: Any) -> str:
    """One `token_count` event as Codex writes it into a rollout file."""
    limits: dict[str, Any] = {}
    if primary is not None:
        limits["primary"] = primary
    if secondary is not None:
        limits["secondary"] = secondary
    payload = {"type": "token_count",
               "info": {"total_token_usage": {"input_tokens": 1000, "output_tokens": 10},
                        "last_token_usage": {"input_tokens": 100, "output_tokens": 1}},
               "rate_limits": limits}
    payload.update(extra)
    return json.dumps({"timestamp": iso(ts), "type": "event_msg", "payload": payload})


def message_line(ts: float, text: str) -> str:
    return json.dumps({"timestamp": iso(ts), "type": "response_item",
                       "payload": {"type": "message", "role": "user",
                                   "content": [{"type": "input_text", "text": text}]}})


def meta_line(ts: float, cwd: str) -> str:
    return json.dumps({"timestamp": iso(ts), "type": "session_meta",
                       "payload": {"id": "0199a213-81c0-7800-8aa1-bbab2a035a53", "cwd": cwd,
                                   "instructions": "SYSTEM PROMPT BODY"}})


def write_rollout(home: Path, name: str, lines: list[str | bytes], *,
                  mtime: float | None = None, day: str = "2026/09/28") -> Path:
    directory = home / "sessions" / day
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"rollout-{name}.jsonl"
    with path.open("wb") as fh:
        for line in lines:
            fh.write(line if isinstance(line, bytes) else line.encode())
            fh.write(b"\n")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def parse_instant(value: Any) -> float:
    """An ISO 8601 timestamp WITH a UTC offset, as epoch seconds."""
    assert isinstance(value, str), f"expected an ISO 8601 string, got {value!r}"
    when = datetime.fromisoformat(value.replace("Z", "+00:00"))
    assert when.tzinfo is not None, f"{value!r} carries no timezone"
    assert when.utcoffset().total_seconds() == 0, f"{value!r} is not UTC"
    return when.timestamp()


# Loaded into the adapter's interpreter through PYTHONPATH. It records every
# path opened (builtins.open, io.open and os.open all raise the `open` audit
# event), so a test can count the rollout files a run touched without
# knowing how the adapter reads them.
SITECUSTOMIZE = r'''
import os, sys
_log = os.environ.get("CODEX_TEST_OPEN_LOG")
if _log:
    with open(_log, "a") as _fh:
        _fh.write("#loaded\n")
    def _hook(event, args, _log=_log):
        if event == "open" and args and isinstance(args[0], (str, bytes, os.PathLike)):
            path = os.fsdecode(args[0])
            if "rollout-" in path:
                with open(_log, "a") as fh:
                    fh.write(path + "\n")
    sys.addaudithook(_hook)
'''


# ================================================== app-server (CX-C11 rev.) ==
#
# The revised CX-C11 reads the quota live: `codex app-server` over stdio,
# `initialize` → `initialized` → `account/rateLimits/read`. The helpers below
# are ADDITIONS; nothing above changes behaviour. `FakeCodexAppServer` is the
# same fake as `FakeCodex` for every other subcommand (login status, exec, …)
# and additionally speaks the app-server exchange when argv holds `app-server`.
#
# ---------------------------------------------------------------------------
# THE WIRE ASSUMPTIONS, IN ONE PLACE. Taken from the 0.158.0 schema excerpts
# (context/codex-proposal/app-server-schema/) and NOT yet confirmed live (L5).
# If the live check disagrees, change the line here and nothing else:
#   - framing: "ndjson" (one JSON object per line) or "content-length"
#     (LSP-style `Content-Length: N\r\n\r\n<body>`), both implemented below;
#   - whether the fake's replies carry `"jsonrpc": "2.0"` (the schema's
#     JSONRPCResponse has only `id` and `result`/`error`);
#   - the handshake the fake insists on before it answers the read.
APP_SERVER_FRAMING = "ndjson"
APP_SERVER_JSONRPC_FIELD = False
APP_SERVER_HANDSHAKE = ("initialize", "initialized")   # request, then notification
RATE_LIMITS_METHOD = "account/rateLimits/read"
# ---------------------------------------------------------------------------

# Values that must never leave the adapter (revised CX-C11: "no accountId, no
# credits.balance, no upsell text").
ACCOUNT_ID = "acct-LEAK-7f3e9a"
CREDITS_BALANCE = "1234.56-LEAK"
UPSELL_TEXT = "UPSELL-LEAK Upgrade to Pro for more"

_APP_SERVER_BLOCK = r'''
if "app-server" in argv:
    import signal, subprocess
    A = B.get("app_server") or {}
    W = B.get("wire") or {}
    mode = A.get("mode", "ok")
    with open(HERE / "pids.txt", "a") as fh:
        fh.write(f"{os.getpid()}\n")
    if A.get("ignore_sigterm"):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    if mode.startswith("hang") or A.get("grandchild"):
        # A grandchild in the same process group, holding the same stdio.
        code = ("import signal, time\n"
                + ("signal.signal(signal.SIGTERM, signal.SIG_IGN)\n" if A.get("ignore_sigterm") else "")
                + "time.sleep(600)\n")
        child = subprocess.Popen([sys.executable, "-c", code])
        with open(HERE / "pids.txt", "a") as fh:
            fh.write(f"{child.pid}\n")
    if mode == "exit":
        print("fatal: app-server failed " + SECRET, file=sys.stderr, flush=True)
        sys.exit(A.get("exit", 2))
    if mode == "hang_silent":
        time.sleep(600)

    rin, rout = sys.stdin.buffer, sys.stdout.buffer
    framing = W.get("framing", "ndjson")

    def read_msg():
        if framing == "ndjson":
            while True:
                line = rin.readline()
                if not line:
                    return None
                if line.strip():
                    return line
        length = None
        while True:
            header = rin.readline()
            if not header:
                return None
            header = header.strip()
            if not header:
                if length is None:
                    continue
                return rin.read(length)
            name, _, value = header.decode("latin-1").partition(":")
            if name.strip().lower() == "content-length":
                length = int(value.strip())

    def send_raw(body):
        if framing == "ndjson":
            rout.write(body + b"\n")
        else:
            rout.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
        rout.flush()

    def send(obj):
        if W.get("jsonrpc_field"):
            obj = {"jsonrpc": "2.0", **obj}
        send_raw(json.dumps(obj).encode())

    def error(mid, code, message, data=None):
        err = {"code": code, "message": message}
        if data is not None:
            err["data"] = data
        send({"id": mid, "error": err})

    init_req, init_note = W.get("handshake", ["initialize", "initialized"])
    method_read = W.get("rate_limits_method", "account/rateLimits/read")
    init_done = initialized = False
    while True:
        raw = read_msg()
        if raw is None:
            if A.get("linger_after_eof"):
                time.sleep(600)
            sys.exit(A.get("eof_exit", 0))
        try:
            msg = json.loads(raw)
        except ValueError:
            msg = {"unparseable": raw.decode("utf-8", "replace")}
        with open(HERE / "appserver.jsonl", "a") as fh:
            fh.write(json.dumps(msg) + "\n")
        if not isinstance(msg, dict):
            continue
        method, mid = msg.get("method"), msg.get("id")
        if mode == "garbage":
            if mid is not None:
                rout.write(A.get("garbage", "not json at all " + SECRET).encode() + b"\n")
                rout.flush()
            continue
        if mid is None:
            if method == init_note and init_done:
                initialized = True
            continue
        if method == init_req:
            info = (msg.get("params") or {}).get("clientInfo")
            if not (isinstance(info, dict) and isinstance(info.get("name"), str)
                    and isinstance(info.get("version"), str)):
                error(mid, -32602, "Invalid request: missing clientInfo")
                continue
            if mode == "init_error":
                error(mid, -32603, "initialize failed", SECRET)
                continue
            init_done = True
            send({"id": mid, "result": {"userAgent": "codex_cli_rs/0.158.0 (fake)"}})
            continue
        if method == method_read:
            if not initialized:
                error(mid, -32002, "Not initialized")
                continue
            if mode == "hang_after_initialize":
                time.sleep(600)
            if A.get("notify_first", True):
                send({"method": "account/updated", "params": {"authMode": "chatgpt"}})
            if mode == "rpc_error":
                error(mid, -32603, "failed to fetch codex rate limits", SECRET)
            elif mode == "not_logged_in":
                error(mid, -32600,
                      "codex account authentication required to read rate limits", SECRET)
            else:
                send({"id": mid, "result": A.get("result")})
            continue
        error(mid, -32601, "Method not found")
'''

_MARK = "\nwords = [a for a in argv"
assert _MARK in FAKE_CODEX
FAKE_CODEX_APP_SERVER = FAKE_CODEX.replace(_MARK, "\n" + _APP_SERVER_BLOCK + _MARK, 1)


def rl_window(used: Any, minutes: Any, resets_at: Any) -> dict[str, Any]:
    """A `RateLimitWindow` as the app-server sends it (camelCase, Unix seconds)."""
    return {"usedPercent": used, "windowDurationMins": minutes, "resetsAt": resets_at}


def rl_snapshot(primary: dict | None, secondary: dict | None, *, limit_id: str | None = None,
                reached: str | None = None) -> dict[str, Any]:
    """A `RateLimitSnapshot`, with a credits block whose balance must not leak."""
    return {"limitId": limit_id, "limitName": None, "primary": primary,
            "secondary": secondary, "planType": "plus", "rateLimitReachedType": reached,
            "credits": {"hasCredits": True, "unlimited": False, "balance": CREDITS_BALANCE}}


def rate_limits_response(rate_limits: dict, by_limit_id: dict | None = None) -> dict[str, Any]:
    """A `GetAccountRateLimitsResponse`, carrying every field that must not leak."""
    return {"accountId": ACCOUNT_ID, "rateLimits": rate_limits,
            "rateLimitsByLimitId": by_limit_id,
            "rateLimitUpsell": {"title": UPSELL_TEXT, "body_text": UPSELL_TEXT},
            "ordinaryUsageAllowed": True,
            "rateLimitResetCredits": {"availableCount": 0, "credits": None}}


class FakeCodexAppServer(FakeCodex):
    """`FakeCodex`, plus `codex app-server` speaking the JSON-RPC exchange.

    Records, beside itself: `calls.jsonl` (argv and CODEX_HOME, as FakeCodex),
    `appserver.jsonl` (every message the adapter sent it), and `pids.txt` (its
    own pid, and a grandchild's in the hang modes).

    Modes: ok, rpc_error, not_logged_in, init_error, garbage, exit,
    hang_silent (never reads), hang_after_initialize. Options: `grandchild`
    (spawn one in the same process group, as the hang modes always do),
    `linger_after_eof` (keep running once stdin closes), `ignore_sigterm`.
    """

    def __init__(self, root: Path):
        super().__init__(root)
        self.path.write_text(FAKE_CODEX_APP_SERVER)
        self.behaviour["wire"] = {"framing": APP_SERVER_FRAMING,
                                  "jsonrpc_field": APP_SERVER_JSONRPC_FIELD,
                                  "handshake": list(APP_SERVER_HANDSHAKE),
                                  "rate_limits_method": RATE_LIMITS_METHOD}
        self.behaviour["app_server"] = {"mode": "ok", "result": None}
        self.save()

    def app_server(self, **values: Any) -> None:
        self.behaviour["app_server"].update(values)
        self.save()

    def app_server_calls(self) -> list[dict[str, Any]]:
        return [c for c in self.calls() if "app-server" in c["argv"]]

    def received(self) -> list[Any]:
        log = self.dir / "appserver.jsonl"
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text().splitlines() if line]

    def pids(self) -> list[int]:
        log = self.dir / "pids.txt"
        if not log.exists():
            return []
        return [int(line) for line in log.read_text().split()]


def process_gone(pid: int) -> bool:
    """True when `pid` no longer runs: absent, or a zombie awaiting its reaper."""
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except (FileNotFoundError, ProcessLookupError, IndexError):
        return True
    return state in ("Z", "X")
