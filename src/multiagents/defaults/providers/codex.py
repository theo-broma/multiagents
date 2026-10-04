#!/usr/bin/env python3
"""External multiagents provider/CLI bridge for Codex. Python 3.11+, standard library only."""
from __future__ import annotations

import argparse
import base64
import json
import os
import queue
import re
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote

# ---------------------------------------------------------------------- CX-C9 --

_AUTH_REFRESH_MARKERS = (
    "not logged in",
    "could not be refreshed",
    "log out and sign in again",
    "not authenticated",
    "please log in",
    "please sign in",
)

AUTH_LINE = "codex: not authenticated — run: multiagents auth login codex"
RESUME_FAILED = "codex: resume failed:"

# -------------------------------------------------------------------- CX-C11 --

MAX_FILES_PER_HOME = 8
TAIL_BYTES = 1024 * 1024
EXPIRY_MARGIN = 120.0
EXCHANGE_BOUND = 7.0
KILL_GRACE = 1.0
REAP_TIMEOUT = 1.0


def emit(value):
    print(json.dumps(value, ensure_ascii=False), flush=True)


# ------------------------------------------------------------- profiles (C8/C9) --

def _is_docker():
    return os.environ.get("MULTIAGENTS_EXECUTOR") == "docker"


def _user_home():
    """The real user's home: under the local executor an agent's $HOME is private."""
    return Path(os.environ.get("MULTIAGENTS_USER_HOME") or Path.home())


def _is_users_own_codex_home(path):
    real = os.path.realpath(path)
    return real in {os.path.realpath(_user_home() / ".codex"),
                    os.path.realpath(Path.home() / ".codex")}


def _host_profile():
    raw = os.environ.get("MULTIAGENTS_CODEX_PROFILE")
    if raw:
        candidate = Path(raw)
        if _is_users_own_codex_home(candidate):
            raise ValueError(
                "MULTIAGENTS_CODEX_PROFILE resolves to the user's own ~/.codex; refusing")
        return candidate
    return _user_home() / ".multiagents" / "profiles" / "codex"


def auth_profile():
    """Profile for `check`/`login`/`budget`/`models` (CX-C9 amended)."""
    if _is_docker() and os.environ.get("MULTIAGENTS_PROFILE") != "host":
        backing = os.environ.get("MULTIAGENTS_PRIVATE_BACKING")
        if not backing:
            raise ValueError("MULTIAGENTS_PRIVATE_BACKING is not set for a docker profile")
        return Path(backing)
    return _host_profile()


def run_profile():
    """Profile for an agent `run` (CX-C8 revised again): the private home, never $HOME/.codex."""
    if _is_docker():
        home = os.environ.get("MULTIAGENTS_PRIVATE_HOME")
        if not home:
            raise ValueError("MULTIAGENTS_PRIVATE_HOME is not set for a docker run")
        return Path(home)
    profile = _host_profile()
    # Only for an agent the engine launched (it names the real home): there an
    # empty profile is never a first run, it is a run that would call the API
    # unauthenticated. A hand-run adapter keeps creating its profile (CX-C8).
    if os.environ.get("MULTIAGENTS_USER_HOME") and not (profile / "auth.json").is_file():
        raise ValueError(f"no codex login at {profile} (auth.json missing); "
                         "run: multiagents auth login codex")
    return profile


def _ensure_profile(path):
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)


def codex_bin():
    # MULTIAGENTS_BIN is the resolved, absolute path to the native CLI (CX-C2).
    raw = os.environ.get("MULTIAGENTS_BIN")
    if not raw:
        raise ValueError(os.environ.get("MULTIAGENTS_BIN_ERROR") or
                         "MULTIAGENTS_BIN is not set; no native Codex CLI is configured")
    path = Path(raw)
    if not (path.is_file() and os.access(path, os.X_OK)):
        raise ValueError(f"native Codex CLI not found or not executable at {raw}")
    return str(path)


# ---------------------------------------------------------------- run (CX-C10) --

_TOML_SHORT_ESCAPES = {
    "\\": "\\\\", '"': '\\"', "\n": "\\n", "\r": "\\r", "\t": "\\t",
    "\b": "\\b", "\f": "\\f",
}


def _toml_escape_string(value):
    out = []
    for ch in value:
        if ch in _TOML_SHORT_ESCAPES:
            out.append(_TOML_SHORT_ESCAPES[ch])
        elif ch == "\x7f" or ord(ch) < 0x20:
            out.append(f"\\u{ord(ch):04x}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def toml(value):
    """Serialize the small JSON subset accepted by MCP configuration as TOML."""
    if isinstance(value, str):
        return _toml_escape_string(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return "[" + ", ".join(toml(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{ " + ", ".join(toml(str(k)) + " = " + toml(v)
                                  for k, v in value.items()) + " }"
    raise ValueError("unsupported MCP configuration value")


def mcp_flags(filename, cwd, env):
    # Codex deep-merges -c tables: mcp_servers={} does NOT remove servers.
    # Listing definitions is local and does not start an MCP server.
    listing = subprocess.run(
        [codex_bin(), "-c", "check_for_update_on_startup=false", "mcp", "list", "--json"],
        cwd=cwd, env=env, capture_output=True, text=True, timeout=15)
    if listing.returncode:
        raise ValueError("cannot enumerate inherited MCP servers; refusing launch")
    inherited = json.loads(listing.stdout)
    if not isinstance(inherited, list):
        raise ValueError("unexpected Codex MCP listing")
    servers = {}
    for server in inherited:
        if not isinstance(server, dict):
            raise ValueError("unexpected Codex MCP listing")
        name = server.get("name")
        if not isinstance(name, str):
            raise ValueError("MCP server has no valid name")
        transport = server.get("transport") or {}
        if not isinstance(transport, dict):
            raise ValueError("unsupported inherited MCP transport")
        # Keep a minimal valid transport even when --ignore-user-config later
        # removes its original definition. Never copy auth headers or tokens.
        if transport.get("command"):
            servers[name] = {"command": transport["command"], "enabled": False}
        elif transport.get("url"):
            # The inherited URL may carry a token or userinfo, and argv is
            # world-readable: a placeholder is enough to keep the entry valid.
            servers[name] = {"url": "http://disabled.invalid/", "enabled": False}
        else:
            raise ValueError("unsupported inherited MCP transport")
    if filename:
        data = json.loads(Path(filename).read_text())
        entry = data["mcpServers"]["multiagents"]
        if not isinstance(entry.get("command"), str) or not entry["command"]:
            raise ValueError("MCP command must be a nonempty string")
        servers["multiagents"] = {
            "command": entry["command"], "args": entry.get("args", []),
            "env": entry.get("env", {}), "enabled": True, "required": True,
        }
    # Explicitly re-enable only the server supplied by the orchestrator.
    return ["-c", "mcp_servers=" + toml(servers),
            "-c", "features.multi_agent=false"]


_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def command(opts, env):
    if opts.session and not _UUID_RE.match(opts.session):
        raise ValueError(f"session id is not UUID-shaped: {opts.session!r}")
    if env.get("MULTIAGENTS_EXECUTOR") == "docker":
        # Interim (L3 pending): Codex's own sandbox is not yet verified to
        # initialise correctly inside our container, so every profile maps to
        # full access there rather than silently under-sandboxing.
        permission = "danger-full-access"
    else:
        permission = {"readonly": "read-only", "sandbox": "workspace-write",
                      "full": "danger-full-access"}[opts.permission]
    argv = [codex_bin(), "-c", "check_for_update_on_startup=false",
            "-c", 'approval_policy="never"',
            "-c", "sandbox_mode=" + toml(permission),
            *mcp_flags(opts.mcp_config, opts.workdir, env)]
    if opts.effort:
        argv += ["-c", "model_reasoning_effort=" + toml(opts.effort)]
    argv += ["exec"]
    if opts.session:
        argv += ["resume", opts.session]
    argv += ["--json", "--ignore-user-config"]
    if opts.model:
        argv += ["--model", opts.model]
    # Prompt through stdin: leading dashes/braces/newlines remain literal.
    return [*argv, "-"]


def _nonneg_int(value):
    """Best-effort, never-raising coercion to a non-negative int."""
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):  # NaN or +/-inf
            return 0
        try:
            return max(0, int(value))
        except (OverflowError, ValueError):
            return 0
    if isinstance(value, str):
        try:
            return max(0, int(value))
        except ValueError:
            return 0
    return 0


class Normalizer:
    def __init__(self):
        self.session = ""
        self.turn = 0
        self.prefix = uuid.uuid4().hex
        self.seen_tools = set()
        self.terminal = False
        self.failed = False

    def event(self, obj):
        kind = obj.get("type")
        # Only the event type is carried, never the raw object (CX-C26).
        out = {"kind": "raw", "type": kind if isinstance(kind, str) else ""}
        if kind == "thread.started":
            thread_id = obj.get("thread_id")
            if isinstance(thread_id, str):
                self.session = thread_id
            out["kind"] = "step"
        elif kind == "turn.started":
            self.turn += 1
            self.terminal = False
            self.seen_tools.clear()
            out["kind"] = "step"
        elif kind in {"turn.completed", "turn.failed"}:
            self.terminal = True
            failed = kind == "turn.failed"
            self.failed |= failed
            out.update(kind="result", status="failed" if failed else "success")
            if failed:
                error = obj.get("error")
                if isinstance(error, dict):
                    message = error.get("message")
                    out["text"] = message if isinstance(message, str) and message else "Codex turn failed"
                elif error:
                    out["text"] = str(error)
                else:
                    out["text"] = "Codex turn failed"
            else:
                usage = obj.get("usage")
                usage = usage if isinstance(usage, dict) else {}
                inp = _nonneg_int(usage.get("input_tokens", 0))
                cache = min(inp, _nonneg_int(usage.get("cached_input_tokens", 0)))
                output = _nonneg_int(usage.get("output_tokens", 0))
                out["tokens"] = {"input_tokens": inp - cache,
                                 "cache_read_input_tokens": cache,
                                 "output_tokens": output, "total_tokens": inp + output}
        elif kind == "error":
            # Can be a retry notice; only turn.failed/exit status is terminal.
            out.update(kind="error", text=str(obj.get("message", "Codex error")))
        elif kind in {"item.started", "item.updated", "item.completed"}:
            item = obj.get("item") or {}
            typ = item.get("type")
            out.update(kind="step", state=item.get("status", ""))
            if typ == "agent_message" and kind == "item.completed":
                out.update(kind="text", text=item.get("text", ""))
                if item.get("id") is not None:
                    out["block"] = str(item["id"])
            elif typ in {"command_execution", "mcp_tool_call", "file_change", "web_search"}:
                ident = item.get("id")
                # Missing ids remain raw: never invent repeat signatures.
                if ident is None:
                    out["kind"] = "raw"
                elif ident not in self.seen_tools:
                    self.seen_tools.add(ident)
                    name, args = typ, {}
                    if typ == "command_execution":
                        args = {"command": item.get("command", "")}
                    elif typ == "mcp_tool_call":
                        name = "mcp__" + str(item.get("server", "")) + "__" + str(item.get("tool", ""))
                        args = item.get("arguments") or {}
                    elif typ == "file_change":
                        args = {"changes": item.get("changes", [])}
                    else:
                        args = {"query": item.get("query", "")}
                    out.update(kind="tool", name=name, args=args if isinstance(args, dict) else {"_": args})
        return {**out, "session_id": self.session, "turn": f"{self.prefix}:{self.turn}"}



def read_prompt(path, limit, run_dir=None):
    """Snapshot regular UTF-8 input, without following links, including growth.

    Used by the executors too: a tempfile keeps large input out of pipes and
    survives the server's departure without depending on an asynchronous feed.
    """
    if limit <= 0:
        raise ValueError("prompt_file_max_bytes must be positive")
    path = os.path.abspath(path)
    root = os.path.abspath(run_dir or os.path.dirname(path))
    parts = os.path.relpath(path, root).split(os.sep)
    if any(part in ("", ".", "..") for part in parts):
        raise ValueError("prompt file must be inside its run directory")
    # Workspace ancestors may be symlinks (/tmp on macOS, for example).
    # Resolve that trusted boundary once; no link below it is followed.
    dfd = os.open(os.path.realpath(root), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=dfd)
            os.close(dfd)
            dfd = child
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW,
                     dir_fd=dfd)
    finally:
        os.close(dfd)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise ValueError("prompt file must be a regular file")
        if st.st_size > limit:
            raise ValueError("prompt file exceeds prompt_file_max_bytes (%d bytes)" % limit)
        chunks, size = [], 0
        while size <= limit:
            chunk = os.read(fd, min(1024 * 1024, limit + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        if size > limit:
            raise ValueError("prompt file exceeds prompt_file_max_bytes (%d bytes)" % limit)
        data = b"".join(chunks)
        data.decode("utf-8")
        return data
    finally:
        os.close(fd)


def run(opts, env):
    if os.environ.get("MULTIAGENTS_CAN_SPAWN") == "1" and not opts.mcp_config:
        raise ValueError("spawn-enabled agent has no multiagents MCP configuration")
    normalizer = Normalizer()
    auth_printed = False
    # A regular temporary file avoids pipe deadlock for very large prompts.
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as prompt:
        if getattr(opts, "prompt_file", None):
            data = read_prompt(opts.prompt_file, int(env.get(
                "MULTIAGENTS_PROMPT_MAX_BYTES", 16 * 1024 * 1024)),
                               env.get("MULTIAGENTS_PROMPT_RUN_DIR"))
            prompt.write(data.decode("utf-8"))
        else:
            prompt.write(opts.prompt)
        prompt.seek(0)
        child = None
        previous = {}

        def forward(signum, _frame):
            if child is not None and child.poll() is None:
                child.send_signal(signum)

        # Installed before Popen so there is no window in which a signal kills
        # only this wrapper. agentwrap kills the whole process group, so a
        # signal arriving before the child exists needs no forwarding.
        for sig in (signal.SIGTERM, signal.SIGINT):
            previous[sig] = signal.signal(sig, forward)
        try:
            child = subprocess.Popen(command(opts, env), cwd=opts.workdir, env=env, stdin=prompt,
                                     stdout=subprocess.PIPE, text=True, encoding="utf-8",
                                     errors="replace")  # stderr inherited; no buffering deadlock
            for line in child.stdout:
                try:
                    value = json.loads(line)
                    if not isinstance(value, dict):
                        raise ValueError("not an object")
                    event = normalizer.event(value)
                except (ValueError, TypeError, AttributeError):
                    event = {"kind": "raw", "line": line.rstrip("\n"),
                             "session_id": normalizer.session,
                             "turn": f"{normalizer.prefix}:{normalizer.turn}"}
                emit(event)
                kind = event.get("kind")
                text = event.get("text")
                if kind == "result" and event.get("status") == "failed":
                    # Existing runner sniffs quota/auth errors from stderr.
                    print(f"codex: {text or 'Codex failed'}", file=sys.stderr, flush=True)
                # A retryable `error` event never decides the run: only the
                # final result (or the exit status) does.
                if (kind == "result" and isinstance(text, str) and not auth_printed
                        and any(marker in text.lower() for marker in _AUTH_REFRESH_MARKERS)):
                    print(AUTH_LINE, file=sys.stderr, flush=True)
                    auth_printed = True
                    normalizer.failed = True
            code = child.wait()
        finally:
            if child is not None:
                if child.poll() is None:
                    child.kill()
                    child.wait()
                child.stdout.close()
            for sig, handler in previous.items():
                signal.signal(sig, handler)
    if code or not normalizer.terminal:
        emit({"kind": "result", "status": "failed", "session_id": normalizer.session,
              "text": f"Codex exit={code}; terminal event={normalizer.terminal}",
              "turn": f"{normalizer.prefix}:{normalizer.turn}"})
    mismatch = bool(opts.session) and normalizer.session != opts.session
    if mismatch:
        detail = f"{RESUME_FAILED} requested {opts.session!r}, observed {normalizer.session!r}"
        print(detail, file=sys.stderr, flush=True)
        emit({"kind": "result", "status": "session_lost",
              "requested_session": opts.session, "session_id": normalizer.session,
              "text": detail, "turn": f"{normalizer.prefix}:{normalizer.turn}"})
        normalizer.failed = True
    if code > 0:
        return code
    if code < 0:
        return 128 - code
    return int(normalizer.failed or not normalizer.terminal or mismatch)


def run_action(opts):
    profile = run_profile()
    _ensure_profile(profile)
    env = {**os.environ, "CODEX_HOME": str(profile)}
    return run(opts, env)


# --------------------------------------------------------------- check/login (C9) --

def check_action():
    profile = auth_profile()
    _ensure_profile(profile)
    bin_path = codex_bin()
    env = {**os.environ, "CODEX_HOME": str(profile)}
    argv = [bin_path, "-c", "check_for_update_on_startup=false", "login", "status"]
    result = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=15)
    if result.returncode == 0:
        print("Codex authenticated")
        return 0
    # Don't print CLI output: auth status text may contain part of a token.
    print("Codex authentication unavailable")
    combined = (result.stdout + result.stderr).lower()
    if any(marker in combined for marker in _AUTH_REFRESH_MARKERS):
        return 10
    return 20


def login_action():
    profile = auth_profile()
    _ensure_profile(profile)
    bin_path = codex_bin()
    env = {**os.environ, "CODEX_HOME": str(profile)}
    # Flushed: exec replaces the process, and a piped stdout is block-buffered.
    executor = os.environ.get("MULTIAGENTS_EXECUTOR") or "local"
    print(f"Codex sign-in writes the credential store at {profile} "
          f"(chosen by the {executor} executor).", flush=True)
    print("Codex sign-in: follow the device link and code.", flush=True)
    argv = [bin_path, "-c", "check_for_update_on_startup=false", "login", "--device-auth"]
    os.execvpe(argv[0], argv, env)


# ------------------------------------------------------------------- models (C12) --

def models_action():
    profile = auth_profile()
    path = profile / "models_cache.json"
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        print(f"codex: cannot read the model cache ({type(exc).__name__})",
              file=sys.stderr)
        return 1
    if not isinstance(data, dict) or not isinstance(data.get("models"), list):
        print("codex: cache is not in the expected shape", file=sys.stderr)
        return 1
    for item in data["models"]:
        if not isinstance(item, dict):
            continue
        slug = item.get("slug")
        if slug and item.get("visibility") == "list":
            slug = str(slug).replace("\t", " ").replace("\n", " ")
            name = str(item.get("display_name", slug)).replace("\t", " ").replace("\n", " ")
            print(f"{slug}\t{name}")
    return 0


# -------------------------------------------------------- budget: rollout (C11) --

def _window_name(minutes):
    if minutes is None or isinstance(minutes, bool) or not isinstance(minutes, (int, float)):
        return "window"
    if isinstance(minutes, float) and (minutes != minutes or minutes in (float("inf"), float("-inf"))):
        return "window"
    if minutes == 300:
        return "5h"
    if minutes == 10080:
        return "weekly"
    try:
        return f"{int(minutes)}m"
    except (TypeError, ValueError, OverflowError):
        return "window"


def _named_windows(entries):
    """Name windows from (side, minutes, percent, resets_at); shared by both paths.

    Windows whose durations map to the same name (null, or equal) are kept
    apart by a `-primary` / `-secondary` suffix instead of overwriting.
    """
    names = [_window_name(minutes) for _, minutes, _, _ in entries]
    if len(set(names)) < len(names):
        names = [f"{name}-{side}" for name, (side, _, _, _) in zip(names, entries)]
    return {name: {"percent": percent, "resets_at": resets, "span_minutes": minutes}
            for name, (_, minutes, percent, resets) in zip(names, entries)}


def _iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_iso(epoch):
    try:
        return _iso(epoch)
    except (OverflowError, OSError, ValueError):
        return None


def _parse_ts(value):
    if not isinstance(value, str):
        return None
    try:
        when = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        return None
    return when.timestamp()


def _valid_window(raw):
    if not isinstance(raw, dict):
        return None
    if "window_minutes" not in raw:
        return None
    minutes = raw["window_minutes"]
    if isinstance(minutes, bool) or not isinstance(minutes, (int, float)):
        return None
    if isinstance(minutes, float) and (minutes != minutes or minutes in (float("inf"), float("-inf"))):
        return None
    used = raw.get("used_percent")
    resets = raw.get("resets_at")
    if not isinstance(used, (int, float)) or isinstance(used, bool):
        return None
    if not (0 <= used <= 100):
        return None
    if not isinstance(resets, (int, float)) or isinstance(resets, bool):
        return None
    if _safe_iso(resets) is None:
        return None
    return {"used_percent": float(used), "window_minutes": minutes,
            "resets_at": float(resets)}


def _valid_event(obj):
    if not isinstance(obj, dict):
        return None
    ts = _parse_ts(obj.get("timestamp"))
    if ts is None:
        return None
    payload = obj.get("payload")
    if not isinstance(payload, dict) or payload.get("type") != "token_count":
        return None
    limits = payload.get("rate_limits")
    if not isinstance(limits, dict):
        return None
    entries = []
    for side in ("primary", "secondary"):
        raw = limits.get(side)
        if raw is None:
            continue
        valid = _valid_window(raw)
        if valid is None:
            return None
        entries.append((side, valid["window_minutes"], valid["used_percent"],
                        valid["resets_at"]))
    if not entries:
        return None
    windows = _named_windows(entries)
    return {"timestamp": ts, "windows": windows}


def _rollout_files(home):
    root = home / "sessions"
    if not root.is_dir():
        return []
    stamped = []
    for p in root.glob("**/rollout-*.jsonl"):
        try:
            if p.is_file():
                stamped.append((p.stat().st_mtime, p))
        except OSError:
            continue  # vanished between the listing and the stat
    stamped.sort(key=lambda item: item[0], reverse=True)
    return [p for _, p in stamped[:MAX_FILES_PER_HOME]]


def _read_tail_lines(path):
    with path.open("rb") as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        start = max(0, size - TAIL_BYTES)
        fh.seek(start)
        data = fh.read()
    if start > 0:
        # Discard the first (possibly truncated) partial line after the seek.
        nl = data.find(b"\n")
        data = data[nl + 1:] if nl != -1 else b""
    return data.splitlines()


def _scan_homes(homes):
    best = None
    for home in homes:
        for path in _rollout_files(home):
            try:
                lines = _read_tail_lines(path)
            except OSError:
                continue
            for raw in lines:
                if not raw.strip():
                    continue
                try:
                    obj = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                event = _valid_event(obj)
                if event is None:
                    continue
                if best is None or event["timestamp"] > best["timestamp"]:
                    best = event
    return best


def _extra_quota_homes():
    raw = os.environ.get("MULTIAGENTS_CODEX_QUOTA_HOMES", "")
    homes = []
    for part in raw.split(":"):
        part = part.strip()
        if part:
            homes.append(Path(part))
    return homes


def _build_result(raw_windows, *, source, stale_seconds, note=None, force_zero=False):
    now = time.time()
    kept = {}
    for name, w in raw_windows.items():
        iso = _safe_iso(w["resets_at"])
        if iso is None:
            continue
        if w["resets_at"] < now - EXPIRY_MARGIN:
            continue
        kept[name] = {"percent": w["percent"], "resets_at": w["resets_at"], "iso": iso,
                      "span_minutes": w.get("span_minutes")}
    if not kept:
        return {"known": False, "note": note or "Codex quota: no rate-limit reading available."}
    worst_name = max(kept, key=lambda n: kept[n]["percent"])
    worst = kept[worst_name]
    headroom = 0.0 if force_zero else max(0.0, min(1.0, 1 - worst["percent"] / 100.0))
    result = {
        "known": True,
        "source": source,
        "windows": {n: {"percent": w["percent"], "resets_at": w["iso"],
                        "span_minutes": w["span_minutes"]}
                    for n, w in kept.items()},
        "headroom": headroom,
        "resets_at": worst["iso"],
        "stale_seconds": stale_seconds,
    }
    if note:
        result["note"] = note
    return result


# --------------------------------------------------- budget: live app-server (C11 rev) --

class _ExchangeFailed(Exception):
    """The live read failed; `kind` is one of the CX-C24 note classes."""

    def __init__(self, kind):
        super().__init__(kind)
        self.kind = kind


def _rpc_error_kind(msg):
    error = msg.get("error")
    text = str(error.get("message", "")) if isinstance(error, dict) else ""
    text = text.lower()
    if "authentication" in text or any(m in text for m in _AUTH_REFRESH_MARKERS):
        return "not-logged-in"
    return "jsonrpc-error"


def _snapshot_windows(snapshot, prefix=None):
    entries = []
    for side in ("primary", "secondary"):
        w = snapshot.get(side)
        if not isinstance(w, dict):
            continue
        used = w.get("usedPercent")
        minutes = w.get("windowDurationMins")
        resets = w.get("resetsAt")
        if not isinstance(used, (int, float)) or isinstance(used, bool):
            continue
        if not (0 <= used <= 100):
            continue
        if not isinstance(resets, (int, float)) or isinstance(resets, bool):
            continue
        entries.append((side, minutes, float(used), float(resets)))
    named = _named_windows(entries)
    if prefix:
        named = {f"{prefix}-{name}": w for name, w in named.items()}
    return named


def _rate_limits_windows(result):
    if not isinstance(result, dict):
        return None
    by_id = result.get("rateLimitsByLimitId")
    windows = {}
    reached = False
    if isinstance(by_id, dict) and by_id:
        for key, snap in by_id.items():
            if not isinstance(snap, dict):
                return None
            if snap.get("rateLimitReachedType") is not None:
                reached = True
            windows.update(_snapshot_windows(snap, prefix=str(key)))
    else:
        snap = result.get("rateLimits")
        if not isinstance(snap, dict):
            return None
        if snap.get("rateLimitReachedType") is not None:
            reached = True
        windows.update(_snapshot_windows(snap))
    if not windows:
        return None
    return windows, reached


def _shutdown(proc, thread):
    try:
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except (OSError, ProcessLookupError):
                pass
            deadline = time.monotonic() + KILL_GRACE
            while time.monotonic() < deadline and proc.poll() is None:
                time.sleep(0.05)
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (OSError, ProcessLookupError):
                    pass
        try:
            proc.wait(timeout=REAP_TIMEOUT)
        except subprocess.TimeoutExpired:
            pass
    finally:
        # Join first: closing a pipe under a blocked reader is undefined; the
        # process is dead (or abandoned) by now, so the reader sees EOF.
        thread.join(timeout=1)
        for stream in (proc.stdin, proc.stdout):
            try:
                stream.close()
            except (OSError, ValueError):
                pass


def _live_rate_limits(profile, timeout=EXCHANGE_BOUND):
    """`initialize` -> `initialized` -> `account/rateLimits/read`, over stdio.

    Returns (windows, reached) on success. Any expected failure (bad JSON, RPC
    error, timeout, broken pipe, exit before a reply) raises _ExchangeFailed
    with its class, and the caller falls back to the rollout reading.
    """
    try:
        bin_path = codex_bin()
    except ValueError:
        raise _ExchangeFailed("exit")
    env = {**os.environ, "CODEX_HOME": str(profile)}
    try:
        proc = subprocess.Popen(
            [bin_path, "-c", "check_for_update_on_startup=false", "app-server"],
            cwd=str(profile), env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        raise _ExchangeFailed("exit")

    deadline = time.monotonic() + timeout
    q: queue.Queue = queue.Queue()
    eof = object()

    def reader():
        try:
            for line in proc.stdout:
                q.put(line)
        except (OSError, ValueError):
            pass
        finally:
            q.put(eof)

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()

    def recv():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _ExchangeFailed("timeout")
        try:
            item = q.get(timeout=remaining)
        except queue.Empty:
            raise _ExchangeFailed("timeout")
        if item is eof:
            raise _ExchangeFailed("exit")
        try:
            return json.loads(item)
        except ValueError:
            raise _ExchangeFailed("unparseable")

    def send(obj):
        try:
            proc.stdin.write(json.dumps(obj).encode() + b"\n")
            proc.stdin.flush()
        except (OSError, ValueError):
            raise _ExchangeFailed("exit")

    try:
        send({"id": 1, "method": "initialize",
              "params": {"clientInfo": {"name": "multiagents", "version": "1"}}})
        while True:
            msg = recv()
            if not isinstance(msg, dict):
                continue
            if msg.get("id") == 1:
                if "error" in msg:
                    raise _ExchangeFailed(_rpc_error_kind(msg))
                if "result" not in msg:
                    raise _ExchangeFailed("unparseable")
                break
        send({"method": "initialized", "params": {}})
        send({"id": 2, "method": "account/rateLimits/read", "params": {}})
        while True:
            msg = recv()
            if not isinstance(msg, dict):
                continue
            if msg.get("id") == 2:
                if "error" in msg:
                    raise _ExchangeFailed(_rpc_error_kind(msg))
                parsed = _rate_limits_windows(msg.get("result"))
                if parsed is None:
                    raise _ExchangeFailed("unparseable")
                return parsed
    finally:
        _shutdown(proc, thread)


def budget_action():
    now = time.time()
    try:
        profile = auth_profile()
    except ValueError:
        profile = None

    if profile is not None:
        try:
            _ensure_profile(profile)
        except OSError:
            profile = None

    live = None
    failure = None
    if profile is not None:
        try:
            live = _live_rate_limits(profile)
        except _ExchangeFailed as exc:
            failure = exc.kind
        except Exception:
            # A programming error in the live read is not an unavailable
            # server: say so. `budget` must still exit 0 with valid JSON.
            failure = "internal"

    if live is not None:
        windows, reached = live
        result = _build_result(windows, source="app-server", stale_seconds=0, force_zero=reached)
        if result.get("known"):
            print(json.dumps(result, ensure_ascii=False))
            return 0

    reason = f" ({failure})" if failure else ""
    try:
        return _budget_from_rollouts(profile, now, reason)
    except Exception:
        print(json.dumps({"known": False,
                          "note": "Codex quota: no rate-limit reading available (internal)."},
                         ensure_ascii=False))
        return 0


def _budget_from_rollouts(profile, now, reason):
    # A docker instance is a separate account. CX-C11's host/extra history
    # fallback belongs to plain codex, never to a second provider's reading.
    if _is_docker() and os.environ.get("MULTIAGENTS_PROVIDER", "codex") != "codex":
        homes = [profile] if profile is not None else []
    else:
        try:
            host = _host_profile()
        except ValueError:
            host = None
        homes = [host] if host is not None else []
        if profile is not None and profile != host:
            homes.append(profile)
        homes += _extra_quota_homes()
    best = _scan_homes(homes)
    if best is None:
        print(json.dumps({"known": False,
                          "note": "Codex quota: no rate-limit reading available."},
                         ensure_ascii=False))
        return 0
    stale = max(0.0, now - best["timestamp"])
    note = ("Codex quota: read from local session history; "
            f"the live app-server reading was unavailable{reason}.")
    result = _build_result(best["windows"], source="rollout", stale_seconds=stale, note=note)
    if not result.get("known"):
        result = {"known": False, "note": note}
    print(json.dumps(result, ensure_ascii=False))
    return 0


# ------------------------------------------------------------------------ dispatch --

def _identity_json(root, name):
    """Trust the resolved profile root; refuse links in everything below it."""
    relative = Path(name)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("invalid profile path")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    directory = os.open(os.path.realpath(root), flags)
    try:
        for part in relative.parts[:-1]:
            child = os.open(part, flags, dir_fd=directory)
            os.close(directory)
            directory = child
        fd = os.open(relative.parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                     dir_fd=directory)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError("invalid profile file")
            with os.fdopen(fd) as handle:
                fd = None
                return json.load(handle)
        finally:
            if fd is not None:
                os.close(fd)
    finally:
        os.close(directory)


def _identity_normalized(value):
    value = "".join(value.split()).casefold()
    for _ in range(3):
        normalized = "".join(unquote(value).split()).casefold()
        if normalized == value:
            break
        value = normalized
    return value


def _identity_credential_key(key):
    key = key.casefold()
    # Claude's native camelCase names denote the same credential fields.
    return (key in {"token", "secret", "password", "key", "accesstoken",
                    "refreshtoken", "idtoken", "apikey", "primaryapikey",
                    "sessionkey", "clientsecret"}
            or key.endswith(("_token", "_key", "_secret")))


def _identity_secrets(value, credential=False):
    if isinstance(value, str):
        if (credential and len(value) <= 8192
                and len(value.encode("utf-8", errors="surrogatepass")) <= 8192):
            normalized = _identity_normalized(value)
            if len(normalized) >= 16:
                yield normalized
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _identity_secrets(item, credential or _identity_credential_key(key))
    elif isinstance(value, list):
        for item in value:
            yield from _identity_secrets(item, credential)


def identity_action():
    """Only the selected account's email claim may leave the auth profile."""
    try:
        if os.environ.get("MULTIAGENTS_EXECUTOR", "local") not in ("local", "docker"):
            return 64
        # Identity follows the executor even when an ambient host scope is set.
        if _is_docker():
            backing = os.environ.get("MULTIAGENTS_PRIVATE_BACKING")
            if not backing:
                return 64
            profile = Path(backing)
        else:
            profile = auth_profile()
        data = _identity_json(profile, "auth.json")
        token = data["tokens"]["id_token"]
        parts = token.split(".")
        if len(parts) != 3 or not parts[1]:
            return 64
        raw = base64.b64decode(parts[1] + "=" * (-len(parts[1]) % 4),
                               altchars=b"-_", validate=True)
        claims = json.loads(raw)
        email = claims.get("email") if isinstance(claims, dict) else None
        if (not isinstance(email, str) or len(email) > 254
                or not re.fullmatch(r"[^@\s]+@[^@\s]+", email)
                or any(ord(c) < 32 or ord(c) == 127 for c in email)
                or re.search(r"(?i)(?:sk-|rt-|bearer\s|eyJ[A-Za-z0-9_-]*\.)", email)):
            return 64
        normalized = _identity_normalized(email)
        if any(secret in normalized or normalized in secret for secret in _identity_secrets(data)):
            return 64
    except Exception:
        return 64
    emit({"identity": email, "kind": "email"})
    return 0


def action(name):
    if name == "identity":
        return identity_action()
    if name in {"check", "login"} and not os.environ.get("MULTIAGENTS_BIN"):
        print(os.environ.get("MULTIAGENTS_BIN_ERROR") or "MULTIAGENTS_BIN is not set",
              file=sys.stderr)
        return 20
    if name == "check":
        return check_action()
    if name == "login":
        return login_action()
    if name == "budget":
        return budget_action()
    if name == "models":
        return models_action()
    # `launch` is withdrawn for this phase (CX-C13); `compact`, `usage` and
    # `prepare` are not yet implemented. Zero side effects either way.
    print(f"codex: {name!r} is not implemented.", file=sys.stderr)
    return 64


def main():
    if len(sys.argv) < 2:
        return action("check")
    if sys.argv[1] != "run":
        return action(sys.argv[1])
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["run"])
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--prompt-file")
    inputs.add_argument("--prompt")
    parser.add_argument("--model", default="")
    parser.add_argument("--workdir", required=True)
    parser.add_argument("--session", default="")
    parser.add_argument("--permission", choices=["full", "sandbox", "readonly"], default="readonly")
    parser.add_argument("--effort")
    parser.add_argument("--mcp-config")
    # The existing command builder passes whole tokens. argparse otherwise
    # treats a task starting with '--' as another option rather than its value.
    raw = sys.argv[1:]
    valued = {flag for act in parser._actions if act.nargs != 0
              for flag in act.option_strings}
    fixed = []
    i = 0
    while i < len(raw):
        if raw[i] in valued and i + 1 < len(raw):
            fixed.append(raw[i] + "=" + raw[i + 1])
            i += 2
        else:
            fixed.append(raw[i])
            i += 1
    opts = parser.parse_args(fixed)
    return run_action(opts)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, KeyError, subprocess.TimeoutExpired) as exc:
        print(f"codex: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(20)
