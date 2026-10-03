"""Shared fixtures for Phase 0 contract B, P0-R8 (the orchestrator's context).

Contract: `context/specs/phase0-context-and-team.md` § P0-R8.

Three things live here because three test files need them:

- **`session_context`**, located rather than imported. The contract names its
  signature (`session_context(provider, cwd, session_id) -> int | None`) and
  leaves its module to the developer, so `find_session_context()` imports every
  module of the package and returns the first public callable of that name.
  When none exists yet the test fails with a sentence saying so, not with an
  ImportError at collection.
- **Transcript records** in the shape Claude Code writes to
  `~/.claude/projects/<slug>/<session_id>.jsonl`: assistant records carrying
  `message.usage` and a `requestId`, user records, and the manual compaction
  record quoted in BRIEF § R8. The provider in these tests is NOT named
  `claude`: the reading is required to be provider-agnostic, so it is exercised
  through a provider whose only claude-like property is the `transcript:` block
  it declares.
- **A fake provider script** (Python, run as itself — `scripts.script_argv`
  runs any non-`.sh` script directly) that implements `launch` and `compact`,
  logs every invocation with its argv, cwd and `MULTIAGENTS_*` environment,
  and does per turn whatever a JSON control file says. This is the seam the
  driver talks to: a real subprocess, reached through the real
  `scripts.exec_action`/`scripts.run_action`, standing in for a provider CLI.
"""

from __future__ import annotations

import importlib
import json
import os
import pkgutil
import stat
import sys
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import multiagents  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
SHIPPED = REPO / "src" / "multiagents" / "defaults"
PROVIDER_SCRIPTS = SHIPPED / "providers"

# Where the contract's reading most plausibly lands, searched first; every
# other module of the package is searched after them.
_CANDIDATES = ("transcripts", "runner", "server", "watchdog", "driver",
               "providers", "budget", "context", "session")


def find_session_context() -> Callable[..., Any] | None:
    """The contract's `session_context`, wherever the developer put it."""
    names = [f"multiagents.{n}" for n in _CANDIDATES]
    for info in pkgutil.walk_packages(multiagents.__path__, "multiagents."):
        if info.name not in names:
            names.append(info.name)
    for name in names:
        try:
            module = importlib.import_module(name)
        except Exception:
            continue
        fn = getattr(module, "session_context", None)
        if callable(fn):
            return fn
    return None


def slug(cwd: Path | str) -> str:
    """Claude Code's per-directory transcript folder name (see claude.sh launch)."""
    return str(cwd).replace("/", "-").replace(".", "-").replace("_", "-")


# ------------------------------------------------------------ transcripts --

_counter = [0]


def request(context: int, *, cache_read: int | None = None, output: int = 50,
            text: str = "ok", session: str = "s", request_id: str = "") -> dict:
    """One assistant record whose request carried `context` tokens.

    Split across the three fields `context_tokens` sums, so an implementation
    that reads only `input_tokens` gets the wrong answer.
    """
    _counter[0] += 1
    if cache_read is None:
        cache_read = context * 3 // 4
    create = (context - cache_read) // 2
    fresh = context - cache_read - create
    return {
        "type": "assistant",
        "requestId": request_id or f"req_{_counter[0]:06d}",
        "sessionId": session,
        "timestamp": "2026-09-23T10:00:00.000Z",
        "uuid": f"u-{_counter[0]}",
        "message": {
            "model": "claude-opus-5", "role": "assistant", "type": "message",
            "content": [{"type": "text", "text": text}],
            "usage": {"input_tokens": fresh,
                      "cache_read_input_tokens": cache_read,
                      "cache_creation_input_tokens": create,
                      "output_tokens": output},
        },
    }


def user(text: str = "go on", session: str = "s") -> dict:
    return {"type": "user", "sessionId": session,
            "timestamp": "2026-09-23T10:00:01.000Z",
            "message": {"role": "user", "content": text}}


def tool_result(session: str = "s") -> dict:
    return {"type": "user", "sessionId": session,
            "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "done"}]}}


def compaction(pre: int = 27729, post: int = 1607, trigger: str = "manual",
               session: str = "s") -> dict:
    """The record BRIEF § R8 quotes, verbatim in shape."""
    return {"type": "system", "subtype": "compact_boundary", "sessionId": session,
            "content": "Conversation compacted",
            "compactMetadata": {"trigger": trigger, "preTokens": pre,
                                "postTokens": post, "durationMs": 23771,
                                "cumulativeDroppedTokens": pre - post}}


def limit_message(text: str, session: str = "s") -> dict:
    """An assistant record the CLI writes when it stops for a usage limit."""
    return {"type": "assistant", "sessionId": session,
            "message": {"role": "assistant", "model": "<synthetic>",
                        "content": [{"type": "text", "text": text}]}}


def write_transcript(path: Path, records: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records))
    return path


def append_transcript(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")


def transcript_block(root: Path) -> dict:
    """A provider `transcript:` block rooted in a scratch directory."""
    return {"dir": str(root) + "/{slug}", "glob": "*.jsonl"}


# Claude's shipped usage vocabulary (C12-R1a): a provider's transcript block
# declares where the usage lives and which fields make up the context size;
# without both, nothing is read.
CLAUDE_USAGE_DECLARATION = {
    "usage_path": "message.usage",
    "context_fields": ["input_tokens", "cache_read_input_tokens",
                       "cache_creation_input_tokens"],
}


def claude_transcript_block(root: Path) -> dict:
    """`transcript_block` plus the usage declaration Claude's config ships."""
    return {**transcript_block(root), **CLAUDE_USAGE_DECLARATION}


# ------------------------------------------------- the fake provider script --

FAKE_PROVIDER = r'''#!{python}
"""A provider script standing in for a real one. Logs, then obeys FAKE_CTL."""
import json, os, pathlib, sys, time

action = sys.argv[1] if len(sys.argv) > 1 else "check"
log = pathlib.Path(os.environ["FAKE_LOG"])
ctl_path = pathlib.Path(os.environ["FAKE_CTL"])
ctl = json.loads(ctl_path.read_text()) if ctl_path.is_file() else {}

previous = []
if log.is_file():
    previous = [json.loads(l) for l in log.read_text().splitlines() if l.strip()]
entry = {"action": action, "argv": sys.argv, "cwd": os.getcwd(), "t": time.time(),
         "env": {k: v for k, v in os.environ.items() if k.startswith("MULTIAGENTS_")}}
with log.open("a") as fh:
    fh.write(json.dumps(entry) + "\n")

def append(path, records):
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")

if action == "check":
    print("logged in"); sys.exit(0)
if action == "prepare":
    sys.exit(0)
if action == "launch":
    n = sum(1 for e in previous if e["action"] == "launch")
    turns = ctl.get("turns") or [{}]
    turn = turns[min(n, len(turns) - 1)]
    if turn.get("append"):
        append(ctl["transcript"], turn["append"])
    if turn.get("activity"):
        append(ctl["events"], [{"t": time.time(), "agent": "fake",
                                "kind": "fake_activity", "turn": n + 1}])
    sys.exit(int(turn.get("exit", 0)))
if action == "compact":
    c = ctl.get("compact") or {}
    if c.get("sleep"):
        time.sleep(float(c["sleep"]))
    if c.get("stdout"):
        sys.stdout.write(c["stdout"]); sys.stdout.flush()
    if c.get("stderr"):
        sys.stderr.write(c["stderr"]); sys.stderr.flush()
    sys.exit(int(c.get("exit", 0)))
sys.exit(64)
'''


class FakeProvider:
    """The fake script installed in a project's config layer, plus its log."""

    def __init__(self, config_dir: Path, scratch: Path, name: str = "fakeprov.py"):
        self.script = config_dir / "providers" / name
        self.script.parent.mkdir(parents=True, exist_ok=True)
        self.script.write_text(FAKE_PROVIDER.replace("{python}", sys.executable))
        self.script.chmod(self.script.stat().st_mode | stat.S_IEXEC)
        self.log = scratch / "fake-provider.log"
        self.ctl = scratch / "fake-provider.ctl.json"
        self.name = name

    def control(self, **ctl: Any) -> None:
        self.ctl.write_text(json.dumps(ctl))

    def calls(self, action: str | None = None) -> list[dict]:
        if not self.log.is_file():
            return []
        out = [json.loads(line) for line in self.log.read_text().splitlines() if line.strip()]
        return [e for e in out if action is None or e["action"] == action]

    def actions(self) -> list[str]:
        """The sequence of launch/compact actions, in order."""
        return [e["action"] for e in self.calls() if e["action"] in ("launch", "compact")]


def events(path: Path, kind: str | None = None) -> list[dict]:
    if not path.is_file():
        return []
    out = []
    for line in path.read_text().splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if kind is None or record.get("kind") == kind:
            out.append(record)
    return out


# ------------------------------------------------ observing file reads --

class ReadCounter:
    """Counts bytes read from one file through Python's file objects.

    Installed over `builtins.open`, `io.open` and `os.open` with monkeypatch.
    `opens` counts every open of the path; `bytes` counts what was read through
    the returned file object (`read`, `readline`, `readlines`, `readinto`,
    iteration). A reader that goes around Python's file objects is
    under-counted, never over-counted — so this can only fail an implementation
    that demonstrably read the file.
    """

    def __init__(self, target: Path):
        self.target = os.path.realpath(target)
        self.opens = 0
        self.bytes = 0

    def matches(self, file: Any) -> bool:
        try:
            return os.path.realpath(os.fspath(file)) == self.target
        except TypeError:
            return False

    def install(self, monkeypatch) -> "ReadCounter":
        import builtins
        import io

        real_open = builtins.open
        real_io_open = io.open
        real_os_open = os.open
        counter = self

        def wrap(real):
            def opener(file, *args, **kwargs):
                handle = real(file, *args, **kwargs)
                if counter.matches(file):
                    counter.opens += 1
                    return _Counting(handle, counter)
                return handle
            return opener

        def os_opener(path, *args, **kwargs):
            if counter.matches(path):
                counter.opens += 1
            return real_os_open(path, *args, **kwargs)

        monkeypatch.setattr(builtins, "open", wrap(real_open))
        monkeypatch.setattr(io, "open", wrap(real_io_open))
        monkeypatch.setattr(os, "open", os_opener)
        return self

    def reset(self) -> None:
        self.opens = 0
        self.bytes = 0


class _Counting:
    def __init__(self, handle, counter: ReadCounter):
        self._h = handle
        self._c = counter

    def _n(self, data):
        if data:
            self._c.bytes += len(data)
        return data

    def read(self, *a):
        return self._n(self._h.read(*a))

    def readline(self, *a):
        return self._n(self._h.readline(*a))

    def readlines(self, *a):
        lines = self._h.readlines(*a)
        self._c.bytes += sum(len(line) for line in lines)
        return lines

    def readinto(self, buf):
        n = self._h.readinto(buf)
        self._c.bytes += n or 0
        return n

    def __iter__(self):
        return self

    def __next__(self):
        return self._n(next(self._h))

    def __enter__(self):
        self._h.__enter__()
        return self

    def __exit__(self, *a):
        return self._h.__exit__(*a)

    def __getattr__(self, name):
        return getattr(self._h, name)
