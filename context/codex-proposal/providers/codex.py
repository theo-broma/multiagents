#!/usr/bin/env python3
"""External multiagents provider/CLI bridge. Python 3.11+, standard library only."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import uuid


def emit(value):
    print(json.dumps(value, ensure_ascii=False), flush=True)


def codex_bin():
    # MULTIAGENTS_BIN points to THIS bridge, never to the native CLI.
    name = os.environ.get("MULTIAGENTS_CODEX_BIN", "codex")
    found = shutil.which(name)
    if not found or Path(found).resolve() == Path(__file__).resolve():
        raise ValueError("native Codex CLI missing; set MULTIAGENTS_CODEX_BIN")
    return found


def profile(action="run"):
    if (action in {"check", "login", "budget"}
            and os.environ.get("MULTIAGENTS_PROFILE") != "host"
            and os.environ.get("MULTIAGENTS_EXECUTOR") == "docker"):
        backing = os.environ.get("MULTIAGENTS_PRIVATE_BACKING")
        if not backing:
            raise ValueError("Docker private profile is missing")
        return Path(backing)
    return Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).expanduser()


def toml(value):
    """Serialize the small JSON subset accepted by MCP configuration as TOML."""
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return "[" + ", ".join(toml(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{ " + ", ".join(toml(str(k)) + " = " + toml(v)
                                  for k, v in value.items()) + " }"
    raise ValueError("unsupported MCP configuration value")


def mcp_flags(filename=None, cwd=None):
    # Codex deep-merges -c tables: mcp_servers={} does NOT remove servers.
    # Listing definitions is local and does not start an MCP server.
    listing = subprocess.run([codex_bin(), "mcp", "list", "--json"],
                             cwd=cwd, capture_output=True, text=True, timeout=15)
    if listing.returncode:
        raise ValueError("cannot enumerate inherited MCP servers; refusing launch")
    inherited = json.loads(listing.stdout)
    if not isinstance(inherited, list):
        raise ValueError("unexpected Codex MCP listing")
    servers = {}
    for server in inherited:
        name = server.get("name")
        if not isinstance(name, str):
            raise ValueError("MCP server has no valid name")
        transport = server.get("transport") or {}
        # Keep a minimal valid transport even when --ignore-user-config later
        # removes its original definition. Never copy auth headers or tokens.
        if transport.get("command"):
            servers[name] = {"command": transport["command"], "enabled": False}
        elif transport.get("url"):
            servers[name] = {"url": transport["url"], "enabled": False}
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


def command(opts):
    permission = {"readonly": "read-only", "sandbox": "workspace-write",
                  "full": "danger-full-access"}[opts.permission]
    argv = [codex_bin(), "-c", 'cli_auth_credentials_store="file"', "-c", 'approval_policy="never"',
            "-c", "sandbox_mode=" + toml(permission), *mcp_flags(opts.mcp_config, opts.workdir)]
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
        out = {"kind": "raw", "codex": obj}
        if kind == "thread.started":
            self.session = obj.get("thread_id", "")
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
                out["text"] = str((obj.get("error") or {}).get("message", "Codex turn failed"))
            else:
                usage = obj.get("usage") or {}
                inp = max(0, int(usage.get("input_tokens", 0)))
                cache = min(inp, max(0, int(usage.get("cached_input_tokens", 0))))
                output = max(0, int(usage.get("output_tokens", 0)))
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


def run(opts, session_file=None):
    if os.environ.get("MULTIAGENTS_CAN_SPAWN") == "1" and not opts.mcp_config:
        raise ValueError("spawn-enabled agent has no multiagents MCP configuration")
    normalizer = Normalizer()
    # A regular temporary file avoids pipe deadlock for very large prompts.
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as prompt:
        prompt.write(opts.prompt)
        prompt.seek(0)
        child = subprocess.Popen(command(opts), cwd=opts.workdir, stdin=prompt,
                                 stdout=subprocess.PIPE, text=True, encoding="utf-8",
                                 errors="replace")  # stderr inherited; no buffering deadlock
        previous = {}
        def forward(signum, _frame):
            if child.poll() is None:
                child.send_signal(signum)
        for sig in (signal.SIGTERM, signal.SIGINT):
            previous[sig] = signal.signal(sig, forward)
        try:
            for line in child.stdout:
                try:
                    value = json.loads(line)
                    if not isinstance(value, dict):
                        raise ValueError("not an object")
                    event = normalizer.event(value)
                except (ValueError, TypeError, AttributeError):
                    event = {"kind": "raw", "line": line.rstrip("\n")}
                emit(event)
                if event.get("kind") == "result" and event.get("status") == "failed":
                    # Existing runner sniffs quota/auth errors from stderr.
                    print(event.get("text", "Codex failed"), file=sys.stderr, flush=True)
                if session_file and normalizer.session:
                    atomic_write(session_file, normalizer.session)
            code = child.wait()
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()
            child.stdout.close()
            for sig, handler in previous.items():
                signal.signal(sig, handler)
    if code or not normalizer.terminal:
        emit({"kind": "result", "status": "failed", "session_id": normalizer.session,
              "text": f"Codex exit={code}; terminal event={normalizer.terminal}"})
    return code if code > 0 else (128 - code if code < 0 else
                                  int(normalizer.failed or not normalizer.terminal))


def atomic_write(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(text)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def sessions(root, cwd):
    """Read metadata only; do not expose conversation bodies or credentials."""
    found = {}
    for path in (root / "sessions").glob("**/*.jsonl"):
        try:
            with path.open() as handle:
                row = json.loads(handle.readline())
            meta = row.get("payload", {})
            if row.get("type") == "session_meta" and meta.get("cwd") == str(Path(cwd).resolve()):
                found[str(meta["id"])] = path
        except (OSError, ValueError, KeyError):
            continue
    return found


def launch():
    state = Path(os.environ["MULTIAGENTS_LAUNCH_STATE"])
    role = os.environ.get("MULTIAGENTS_ROLE", "orchestrator")
    if role not in {"orchestrator", "initializer"}:
        raise ValueError("invalid launch role")
    marker = state / f"codex-{role}-session"
    cwd = os.environ["MULTIAGENTS_PROJECT"]
    existing = sessions(profile(), cwd)
    sid = marker.read_text().strip() if marker.exists() else ""
    if os.environ.get("MULTIAGENTS_RESUME") != "1" or sid not in existing:
        if os.environ.get("MULTIAGENTS_RESUME") == "1":
            print("No verified Codex role session; starting fresh.", file=sys.stderr)
        sid = ""
    brief = Path(os.environ["MULTIAGENTS_PROMPT_FILE"]).read_text()
    prompt = os.environ.get("MULTIAGENTS_RESUME_PROMPT", "")
    prompt = "\n\n".join(p for p in (brief, prompt, os.environ.get("MULTIAGENTS_NUDGE", "")) if p)
    opts = argparse.Namespace(prompt=prompt, model=os.environ.get("MULTIAGENTS_MODEL", ""),
                              workdir=cwd, permission="sandbox", session=sid, effort=None,
                              mcp_config=os.environ["MULTIAGENTS_MCP_CONFIG"])
    if os.environ.get("MULTIAGENTS_UNATTENDED") == "1":
        return run(opts, marker)
    argv = [codex_bin(), "-c", 'cli_auth_credentials_store="file"', *mcp_flags(opts.mcp_config, cwd)]
    if sid:
        argv += ["resume", sid]
    if opts.model:
        argv += ["--model", opts.model]
    argv += ["--", prompt]
    code = subprocess.call(argv, cwd=cwd)
    fresh = set(sessions(profile(), cwd)) - set(existing)
    if not sid and len(fresh) == 1:
        atomic_write(marker, fresh.pop())
    elif not sid:
        # Do not resume a stranger's session via --last.
        marker.unlink(missing_ok=True)
        print("Codex role session could not be identified uniquely; next launch starts fresh.", file=sys.stderr)
    return code


def action(name):
    if name == "compact":
        return 64  # Same non-mutating answer for MULTIAGENTS_COMPACT_CHECK=1.
    if name == "budget":
        emit({"known": False, "note": "Codex quota unavailable through this CLI adapter; tokens are not quota."})
        return 0
    if name == "usage":
        budget = json.loads(os.environ.get("MULTIAGENTS_BUDGET") or "{}")
        print("Codex quota: unknown")
        print(str(budget.get("note", "No quota reading")).replace("\n", " "))
        print("Cost: not reported by codex exec")
        return 0
    if name == "models":
        # Optional local cache; no invented CLI `models` command or model ids.
        path = profile() / "models_cache.json"
        data = json.loads(path.read_text()) if path.exists() else {}
        for item in data.get("models", []):
            slug = item.get("slug")
            if slug and item.get("visibility", "list") == "list":
                print(str(slug) + "\t" + str(item.get("display_name", slug)).replace("\t", " ").replace("\n", " "))
        if not data.get("models"):
            print("No local model cache; declare models: in providers.yaml.", file=sys.stderr)
            return 64
        return 0
    if name in {"check", "login"}:
        env = {**os.environ, "CODEX_HOME": str(profile(name))}
        argv = [codex_bin(), "-c", 'cli_auth_credentials_store="file"', "login"]
        if name == "login":
            print("Codex sign-in: follow the device link and code. Profile: " + env["CODEX_HOME"], flush=True)
            Path(env["CODEX_HOME"]).mkdir(parents=True, exist_ok=True)
            os.execvpe(argv[0], [*argv, "--device-auth"], env)
        result = subprocess.run([*argv, "status"], env=env, capture_output=True, text=True, timeout=15)
        print("Codex authenticated" if result.returncode == 0 else "Codex authentication unavailable")
        # Don't print CLI output: API-key status may contain part of the key.
        if result.returncode == 0:
            return 0
        return 10 if "not logged in" in (result.stdout + result.stderr).lower() else 20
    if name == "prepare":
        mcp_flags(os.environ["MULTIAGENTS_MCP_CONFIG"], os.environ.get("MULTIAGENTS_PROJECT"))
        print("Codex MCP configuration validated; passed at launch without global registration.")
        return 0
    if name == "launch":
        return launch()
    return 64


def main():
    if len(sys.argv) < 2 or sys.argv[1] != "run":
        return action(sys.argv[1] if len(sys.argv) > 1 else "check")
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["run"])
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--model", default="")
    parser.add_argument("--workdir", required=True)
    parser.add_argument("--session", default="")
    parser.add_argument("--permission", choices=["full", "sandbox", "readonly"], default="readonly")
    parser.add_argument("--effort")
    parser.add_argument("--mcp-config")
    # The existing command builder passes whole tokens. argparse otherwise
    # treats a task starting with '--' as another option rather than its value.
    raw = sys.argv[1:]
    fixed = []
    i = 0
    valued = {"--prompt", "--model", "--workdir", "--session", "--permission",
              "--effort", "--mcp-config"}
    while i < len(raw):
        if raw[i] in valued and i + 1 < len(raw):
            fixed.append(raw[i] + "=" + raw[i + 1])
            i += 2
        else:
            fixed.append(raw[i])
            i += 1
    return run(parser.parse_args(fixed))


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, KeyError, subprocess.TimeoutExpired) as exc:
        print(f"Codex adapter: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(20)
