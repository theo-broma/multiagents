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
