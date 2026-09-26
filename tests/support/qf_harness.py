"""Test harness for quota freshness — `context/specs/quota-freshness.md`.

Every seam here stands at a process or network boundary, never inside the
code under test:

- **the usage endpoint.** `UsageEndpoint` replaces `urllib.request.urlopen`,
  the one call through which `https://api.anthropic.com/api/oauth/usage` is
  reached. It counts calls in a FILE, so two processes stubbed with the same
  file are counted together, and it refuses every other URL rather than let
  one leave the machine.
- **the clock.** `FakeClock` replaces `time.time` (and optionally
  `time.sleep`). Every reading in this codebase — `Budget.usable`,
  `reset_label`, the shared-file TTL, the tree's `now()` — asks `time.time`,
  so that is the clock. An implementation that reads `datetime.now()` instead
  would escape it; see `NEED_INFO(clock)` in the tester's report.
- **the CLI's own files.** `budget.CLAUDE_STATE` / `CLAUDE_CREDENTIALS` are
  bound from `Path.home()` at import time, so no environment variable moves
  them (see `conftest.py`). `ClaudeHome` repoints both at `tmp_path`.
- **provider scripts.** `Project` writes stub scripts into the project's own
  `providers/` layer, which outranks the shipped ones. `claude.sh` answers 64
  (unimplemented) to everything, which is what the shipped one does for
  `budget`, so the built-in reader runs; no real CLI is ever started.
- **the launch.** `driver._launch_agent` is replaced by a recorder. It is the
  point where `run` would exec a real CLI; the tests only need to know whether
  and when it was reached, and what the tree looked like at that moment.

The one thing here that is not a boundary is `seed_cooldown` / `seed_pause`,
which record a cooldown or pause WITH ITS CAUSE. The contract (QF-R4) says a
record must carry its cause from now on but does not name the field; this
harness assumes a `cause=` keyword on `Tree.set_cooldown` and `Tree.pause`,
with `"quota"` meaning the provider's quota. It is the only place that
assumption lives — change it here if the implementation names it otherwise.
"""

from __future__ import annotations

import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from email.message import Message
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from multiagents import budget as budget_mod                   # noqa: E402
from multiagents.tree import Tree                              # noqa: E402

USAGE_URL = budget_mod.CLAUDE_USAGE_URL
QUOTA = "quota"                     # the assumed cause value for a quota record
HERE = Path(__file__).resolve().parent


# ---------------------------------------------------------------- times ---

def iso(ts: float) -> str:
    """An aware UTC ISO stamp, as the providers send them."""
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


def naive(ts: float) -> str:
    """The same instant with its offset stripped — "no timezone"."""
    return datetime.fromtimestamp(ts, timezone.utc).replace(tzinfo=None) \
        .isoformat(timespec="seconds")


def epoch(stamp: Any) -> float:
    return datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).timestamp()


class FakeClock:
    """`time.time` under the test's control. Starts at the real time, whole
    seconds, so stamps written by a script and read by the code agree."""

    def __init__(self, monkeypatch, start: float | None = None,
                 patch_sleep: bool = False, sleep_limit: float | None = None):
        self.now = float(int(time.time() if start is None else start))
        self.start = self.now
        self.slept: list[float] = []
        self.sleep_limit = sleep_limit
        monkeypatch.setattr(time, "time", self)
        if patch_sleep:
            monkeypatch.setattr(time, "sleep", self.sleep)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> float:
        self.now += seconds
        return self.now

    def sleep(self, seconds: float) -> None:
        """Sleeping advances the clock. Past `sleep_limit` seconds after the
        start it raises KeyboardInterrupt — the only way out of a `--wait`
        loop, so an implementation that never proceeds ends instead of
        hanging the suite."""
        self.slept.append(seconds)
        self.now += max(0.0, float(seconds))
        if self.sleep_limit is not None and self.now - self.start > self.sleep_limit:
            raise KeyboardInterrupt


# ------------------------------------------------------------- payloads ---

def usage_payload(**windows: tuple[float, str | None]) -> dict:
    """The usage endpoint's shape: `five_hour=(99, stamp)` → the bucket."""
    out: dict[str, Any] = {}
    for name, (percent, resets_at) in windows.items():
        bucket: dict[str, Any] = {"utilization": percent}
        if resets_at is not None:
            bucket["resets_at"] = resets_at
        out[name] = bucket
    return out


def script_reading(windows: dict[str, tuple[float, str | None]],
                   source: str = "stubsrc", known: bool = True,
                   headroom: float | None = None,
                   resets_at: str | None = None) -> dict:
    """What a provider script's `budget` action prints, in the shape the
    shipped opencode.sh uses: headroom and resets_at are the WORST window's,
    and every window is listed under `windows`."""
    detail = {name: {"percent": float(p), "resets_at": r}
              for name, (p, r) in windows.items()}
    if headroom is None and detail:
        worst = max(detail, key=lambda n: detail[n]["percent"])
        headroom = round(1 - detail[worst]["percent"] / 100, 4)
        resets_at = resets_at if resets_at is not None else detail[worst]["resets_at"]
    data: dict[str, Any] = {"known": known, "source": source, "windows": detail}
    if headroom is not None:
        data["headroom"] = headroom
    if resets_at is not None:
        data["resets_at"] = resets_at
    return data


# ------------------------------------------------------------- endpoint ---

class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def _answer(response: dict, url: str):
    """Turn one response spec into what `urlopen` returns or raises.

    `{"payload": {...}}`            200 with that JSON body
    `{"status": 429, "retry_after": "600"}`   an HTTPError, header optional
    `{"unreachable": true}`         a URLError (offline, DNS, TLS)
    """
    if response.get("unreachable"):
        raise urllib.error.URLError("unreachable (test)")
    status = int(response.get("status", 200))
    if status != 200:
        headers = Message()
        if response.get("retry_after") is not None:
            headers["Retry-After"] = str(response["retry_after"])
        raise urllib.error.HTTPError(url, status, "stubbed", headers,
                                     io.BytesIO(b'{"error": {"type": "stub"}}'))
    return _Response(json.dumps(response.get("payload") or {}).encode())


class UsageEndpoint:
    """The usage endpoint, stubbed at `urlopen`. Calls are counted in a file."""

    def __init__(self, monkeypatch, calls_file: Path,
                 responder: Callable[[int], dict] | dict | None = None):
        self.calls_file = Path(calls_file)
        self.calls_file.parent.mkdir(parents=True, exist_ok=True)
        self.calls_file.touch()
        self.set(responder if responder is not None else {"unreachable": True})
        monkeypatch.setattr(urllib.request, "urlopen", self._urlopen)

    def set(self, responder: Callable[[int], dict] | dict) -> None:
        self.responder = responder

    @property
    def calls(self) -> int:
        return len([line for line in self.calls_file.read_text().splitlines() if line])

    def _urlopen(self, request, *args, **kwargs):
        url = getattr(request, "full_url", request)
        if url != USAGE_URL:
            raise urllib.error.URLError(f"network is disabled in tests ({url})")
        index = self.calls
        with self.calls_file.open("a") as handle:
            handle.write(f"{time.time()}\n")
        response = self.responder(index) if callable(self.responder) else self.responder
        return _answer(response, url)


# ----------------------------------------------------------- claude home ---

class ClaudeHome:
    """The CLI's `~/.claude.json` and credential, under `tmp_path`."""

    def __init__(self, monkeypatch, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.state = self.root / ".claude.json"
        self.credentials = self.root / ".claude" / ".credentials.json"
        monkeypatch.setattr(budget_mod, "CLAUDE_STATE", self.state)
        monkeypatch.setattr(budget_mod, "CLAUDE_CREDENTIALS", self.credentials)

    def login(self) -> None:
        self.credentials.parent.mkdir(parents=True, exist_ok=True)
        self.credentials.write_text(json.dumps({"claudeAiOauth": {
            "accessToken": "qf-test-token-not-a-secret",
            "expiresAt": (time.time() + 30 * 86400) * 1000}}))

    def logout(self) -> None:
        if self.credentials.exists():
            self.credentials.unlink()

    def cache(self, utilization: dict, fetched_at: float) -> None:
        """What the CLI itself last wrote to `cachedUsageUtilization`."""
        self.state.write_text(json.dumps({"cachedUsageUtilization": {
            "fetchedAtMs": fetched_at * 1000, "utilization": utilization}}))


# --------------------------------------------------------------- project ---

STUB_SCRIPT = """#!/bin/sh
case "$1" in
budget)
    echo x >> "{calls}"
    if [ -f "{code}" ]; then cat "{reading}"; exit "$(cat "{code}")"; fi
    cat "{reading}"; exit 0 ;;
check) exit 0 ;;
*) exit 64 ;;
esac
"""


class Project:
    """A project on disk, driven through `multiagents` itself.

    `script_providers` get a stub script whose `budget` answer is whatever
    `reading()` last wrote; claude keeps its shipped definition with a script
    that answers 64, so the built-in reader runs against `ClaudeHome`.
    opencode and agy are disabled: their shipped scripts reach real services.
    """

    def __init__(self, tmp_path: Path, monkeypatch, orchestrator: str = "qfp",
                 script_providers: tuple[str, ...] = ("qfp",),
                 limits: dict | None = None):
        from multiagents import driver
        from multiagents.paths import ProjectPaths

        self.root = Path(tmp_path) / "proj"
        self.config = self.root / ".multiagents" / "config"
        (self.config / "providers").mkdir(parents=True)
        self.work = Path(tmp_path) / "qf-work"
        self.work.mkdir()
        self.script_providers = script_providers

        project = "executor:\n  kind: local\n"
        if limits:
            project += "limits:\n" + "".join(f"  {k}: {v}\n" for k, v in limits.items())
        (self.config / "project.yaml").write_text(project)
        lines = ["providers:", "  opencode:", "    enabled: false",
                 "  agy:", "    enabled: false"]
        for name in script_providers:
            lines += [f"  {name}:", "    bin: sh", f"    script: {name}.sh"]
            script = STUB_SCRIPT.format(calls=self.work / f"{name}.calls",
                                        reading=self.work / f"{name}.json",
                                        code=self.work / f"{name}.code")
            path = self.config / "providers" / f"{name}.sh"
            path.write_text(script)
            path.chmod(0o755)
            self.reading(name, {"known": False, "note": "no reading written"})
        (self.config / "providers.yaml").write_text("\n".join(lines) + "\n")
        (self.config / "providers" / "claude.sh").write_text("#!/bin/sh\nexit 64\n")
        (self.config / "agents.yaml").write_text(
            f"agents:\n  orchestrator:\n    provider: {orchestrator}\n    model: m\n")

        self.paths = ProjectPaths(self.root)
        self.paths.ensure()
        self.launches: list[dict] = []

        def launch(paths, config, role, *args, **kwargs):
            self.launches.append({"role": role, "at": time.time(),
                                  "pause": self.tree.pause_state()})
            return 0

        monkeypatch.setattr(driver, "_launch_agent", launch)

    # -- readings ------------------------------------------------------------

    def reading(self, name: str, data: dict | str, exit_code: int | None = None) -> None:
        text = data if isinstance(data, str) else json.dumps(data)
        (self.work / f"{name}.json").write_text(text + "\n")
        code_file = self.work / f"{name}.code"
        if exit_code is None:
            code_file.unlink(missing_ok=True)
        else:
            code_file.write_text(str(exit_code))

    def script_calls(self, name: str) -> int:
        path = self.work / f"{name}.calls"
        return len(path.read_text().splitlines()) if path.exists() else 0

    # -- state ---------------------------------------------------------------

    @property
    def tree(self) -> Tree:
        return Tree(self.paths.tree_file, self.paths.events_file)

    def config_snapshot(self) -> dict[str, bytes]:
        return {str(p.relative_to(self.config)): p.read_bytes()
                for p in sorted(self.config.rglob("*")) if p.is_file()}

    # -- the command line ----------------------------------------------------

    def cli(self, capsys, *argv: str) -> tuple[int, str]:
        """`multiagents --path <root> <argv>`; returns (exit code, stdout+stderr).

        argparse's own exit is caught and returned, so an unknown subcommand
        comes back as code 2 with its message rather than ending the test.
        """
        from multiagents import cli

        capsys.readouterr()
        try:
            code = cli.main(["--path", str(self.root), *argv])
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 1
        captured = capsys.readouterr()
        return int(code or 0), captured.out + captured.err


def seed_cooldown(tree: Tree, provider: str, until: float, reason: str,
                  cause: str | None = None, needs_login: bool = False) -> None:
    """A cooldown record, with its cause when one is given (see module doc)."""
    kwargs: dict[str, Any] = {}
    if cause is not None:
        kwargs["cause"] = cause
    if needs_login:
        kwargs["needs_login"] = True
    tree.set_cooldown(provider, until, reason, **kwargs)


def seed_pause(tree: Tree, providers: list[str], until: float, reason: str,
               cause: str | None = None) -> None:
    kwargs: dict[str, Any] = {}
    if cause is not None:
        kwargs["cause"] = cause
    tree.pause(until, reason, providers, **kwargs)


def paused_for(tree: Tree, provider: str) -> bool:
    return provider in (tree.pause_state().get("providers") or [])


# ------------------------------------------------------ relative/absolute ---

def mentions_absolute(text: str, ts: float) -> bool:
    """The reset as a clock time: the ISO stamp, or HH:MM local or UTC."""
    local = datetime.fromtimestamp(ts).astimezone()
    utc = datetime.fromtimestamp(ts, timezone.utc)
    return (iso(ts) in text or iso(ts)[:16] in text
            or local.strftime("%H:%M") in text or utc.strftime("%H:%M") in text)


def mentions_relative(text: str, seconds: float, slack: float = 180) -> bool:
    """The reset as a countdown, in any of the usual spellings, within slack."""
    low, high = seconds - slack, seconds + slack
    for h, m in re.findall(r"(\d+)\s*h\s*(\d{1,2})\s*m", text):
        if low <= int(h) * 3600 + int(m) * 60 <= high:
            return True
    for n in re.findall(r"(\d+)\s*(?:min|minutes|m)\b", text):
        if low <= int(n) * 60 <= high:
            return True
    for n in re.findall(r"(\d+)\s*(?:s|sec|secs|seconds)\b", text):
        if low <= int(n) <= high:
            return True
    return False


# ----------------------------------------------------- a second process ---

def run_reader(spec: dict, timeout: float = 60) -> dict:
    """Read claude's budget in a separate Python process — see qf_reader.py."""
    import subprocess

    spec_file = Path(spec["work"]) / f"reader-{time.monotonic_ns()}.json"
    spec_file.write_text(json.dumps(spec))
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("MULTIAGENTS_", "CLAUDE_"))}
    env["PYTHONPATH"] = os.pathsep.join(
        [str(HERE.parents[1] / "src"), str(HERE), env.get("PYTHONPATH", "")])
    done = subprocess.run([sys.executable, str(HERE / "qf_reader.py"), str(spec_file)],
                          capture_output=True, text=True, timeout=timeout, env=env)
    if done.returncode != 0:
        raise AssertionError(f"reader process failed:\n{done.stdout}\n{done.stderr}")
    return json.loads(done.stdout.strip().splitlines()[-1])
