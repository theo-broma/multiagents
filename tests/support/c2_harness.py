"""Shared test harness for C2 — provider seam, quota and failover.

Builders and seams for `scripts.py`, `budget.py`, `providers.py`, `auth.py`,
`catalog.py` and the shipped `providers.yaml` + `providers/*.sh`, so a
characterizer can drive the plugin seam against a controlled script instead of
a real CLI, and read budgets without a network call or a real subscription.

**What is real, unmodified production code here.** Everything except the
scripts a characterizer writes for itself:

- `scripts.run_action` / `exec_action` / `resolve` / `find_script` /
  `script_argv` / `build_env` — the whole plugin seam, run against whatever
  script a test writes with `write_script` / `case_script`.
- `budget.read_provider` / `read_all` / `choose_provider` / `pick_instance` —
  real dispatch and routing logic, fed a script's real stdout/stderr/exit code.
- `providers.load_providers` / `resolve_inheritance` — real config folding,
  exercised against both the real shipped `providers.yaml` (`shipped_providers`)
  and whatever raw dict a test hands it.
- `auth.check` / `check_all` / `login_command` / `looks_like_auth_failure` —
  thin wrappers over the same seam; nothing here reimplements them.
- The three shipped shell scripts (`run_shipped_script`) — run for real under
  `sh`, exactly as `scripts.script_argv` would invoke them.

**What is stubbed, and what a test can no longer observe through it.**

- `FakeExecutor` stands in for `DockerExecutor` when a test only needs
  `build_env`'s docker branch (`MULTIAGENTS_CONTAINER`, `_PRIVATE_HOME`,
  `_PRIVATE_BACKING`, `_PRIVATE_VAULT`, `_AUTH_PROXY`) and does not care how
  those paths were computed. It is a dict wearing the attributes `build_env`
  reads — `private_state()`/`vault_state()` do no filesystem work and return
  exactly what they were built with. A test about *how* a container's private
  home is laid out (one path per provider, never the first one found) needs
  the REAL `DockerExecutor` — use `make_docker_executor` from
  `tests/support/c1_harness.py` for that; do not duplicate C1's coverage here.
- `read_claude` / `fetch_claude_usage` talk to `https://api.anthropic.com` and
  to `Path.home()` when given no `config_dir`. This harness never calls them
  with `config_dir=None` — see `claude_profile_dir` below — and
  `fake_claude_fetch` replaces `budget.fetch_claude_usage` itself rather than
  standing up a fake HTTP server, so a test can supply the payload without
  caring how `_shared_usage`'s file lock or cross-process cache works. A test
  ABOUT that lock/cache mechanism needs to drive `_shared_usage` directly and
  is out of scope for this harness.
- `supervisor.Supervisor` needs no seam at all — it is pure (no I/O, no
  subprocess) and is exercised directly against synthetic `providers.Event`s.

**A real hazard this harness works around.** `budget.py` keeps `_cache` as a
plain module-level dict keyed by provider name, refreshed at most once every
`_CACHE_TTL` (60s) real seconds. Nothing before this file cleared it between
tests, so under `pytest-randomly` a test using provider name "claude" could
read another test's cached `Budget` for "claude" purely by collection order —
the exact hazard `conftest.py`'s other three autouse fixtures already guard
against for state dirs, the network and provider subprocesses. `conftest.py`
now clears it before every test (`_reset_budget_cache`), so nothing here
needs to call `invalidate_cache()` defensively — but a test that wants to
prove the cache itself (`test_budget_is_cached_...`) still calls it explicitly
between phases, same as before.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from multiagents import auth as auth_mod                        # noqa: E402
from multiagents import budget as budget_mod                    # noqa: E402
from multiagents import catalog                                 # noqa: E402
from multiagents import scripts as scripts_mod                  # noqa: E402
from multiagents.providers import (                             # noqa: E402
    Provider, Event, load_providers, resolve_inheritance, families,
)
from multiagents.paths import shipped_defaults_dir               # noqa: E402

__all__ = [
    "make_provider", "raw_shipped_providers", "shipped_providers",
    "write_script", "case_script", "FakeExecutor",
    "shipped_script_path", "run_shipped_script",
    "claude_profile_dir", "fake_claude_fetch",
    "run_action", "exec_action", "build_env", "resolve", "find_script",
    "script_argv", "read_provider", "read_all", "invalidate_cache", "Budget",
    "choose_provider", "pick_instance", "reserved_providers", "resets_soon",
    "check", "check_all", "login_command", "looks_like_auth_failure", "HOST",
    "load_providers", "resolve_inheritance", "families", "Provider", "Event",
    "catalog",
]

# Real production callables, re-exported so a characterizer imports one module.
run_action = scripts_mod.run_action
exec_action = scripts_mod.exec_action
build_env = scripts_mod.build_env
resolve = scripts_mod.resolve
find_script = scripts_mod.find_script
script_argv = scripts_mod.script_argv

read_provider = budget_mod.read_provider
read_all = budget_mod.read_all
invalidate_cache = budget_mod.invalidate_cache
Budget = budget_mod.Budget
choose_provider = budget_mod.choose_provider
pick_instance = budget_mod.pick_instance
reserved_providers = budget_mod.reserved_providers
resets_soon = budget_mod.resets_soon

check = auth_mod.check
check_all = auth_mod.check_all
login_command = auth_mod.login_command
looks_like_auth_failure = auth_mod.looks_like_auth_failure
HOST = auth_mod.HOST


# ---------------------------------------------------------------------------
# 1. Construction — providers, from a raw dict or from the shipped yaml
# ---------------------------------------------------------------------------

def make_provider(name: str = "p", **overrides: Any) -> Provider:
    """A `Provider` with sensible defaults for exercising the script seam.

    Goes through the real `Provider.from_dict`, not a hand-built stub, so a
    field added to the dataclass later cannot silently go missing here — the
    same reasoning `test_core.py`'s own `_provider_double` already uses.
    `overrides` are `providers.yaml` keys: `script`, `bin`, `env`, `family`,
    `extends`, `enabled`, `container_private_home`, etc.
    """
    overrides.setdefault("bin", name)
    return Provider.from_dict(name, overrides)


def raw_shipped_providers() -> dict[str, Any]:
    """The `providers:` block of the shipped `providers.yaml`, unparsed.

    For exercising `load_providers` / `resolve_inheritance` against the real
    shipped config rather than a hand-written fixture — entry point 3.
    """
    import yaml

    text = (shipped_defaults_dir() / "providers.yaml").read_text()
    return (yaml.safe_load(text) or {}).get("providers", {}) or {}


def shipped_providers() -> dict[str, Provider]:
    """What the shipped `providers.yaml` parses into, right now.

    `claude`, `opencode`, `agy`, `codex` — the four real `Provider` objects, built the
    same way `config.load()` builds them (`load_providers` over the raw dict),
    so a characterizer can assert on the real spawn args / stream rules /
    script names without re-typing the yaml.
    """
    return load_providers(raw_shipped_providers())


# ---------------------------------------------------------------------------
# 2. The plugin seam — a controlled script instead of a real CLI
# ---------------------------------------------------------------------------

def write_script(config_dir: Path, name: str, body: str) -> Path:
    """A provider script at `<config_dir>/providers/<name>`, body verbatim.

    `body` is shell (or anything with its own shebang) written AFTER a
    `#!/bin/sh` line is prepended only if `body` does not already start with
    `#!`. Executable. Use this when a test wants to control the WHOLE script,
    including a non-.sh name to exercise `script_argv`'s "runs itself" branch.
    """
    d = Path(config_dir) / "providers"
    d.mkdir(parents=True, exist_ok=True)
    path = d / name
    text = body if body.startswith("#!") else f"#!/bin/sh\n{body}"
    path.write_text(text)
    path.chmod(0o755)
    return path


def case_script(config_dir: Path, name: str, arms: str) -> Path:
    """A provider script built from `case "$1" in ... esac` ARMS.

    `arms` is one or more case arms, e.g. `'budget) printf "{}"; exit 0 ;;'`.
    Missing actions fall through to `*)`, which this wraps to exit 64
    (UNIMPLEMENTED) rather than a bare shell parse of nothing — the same
    "no arm matched" a hand-rolled script would need to state explicitly, so a
    test's arms list does not have to repeat it every time.
    """
    body = f'#!/bin/sh\ncase "$1" in\n{arms}\n*) exit 64 ;;\nesac\n'
    return write_script(config_dir, name, body)


class FakeExecutor:
    """Stands in for an executor when a test only needs what `build_env`
    reads off it: `kind`, `container`, `private_state(name)`,
    `vault_state(name)`, `auth_proxy_enabled()`. Does no filesystem work — see
    the module docstring for when this is NOT enough and the real
    `DockerExecutor` (via `c1_harness.make_docker_executor`) is needed instead.
    """

    def __init__(self, kind: str = "local", container: str = "",
                 private: dict[str, str] | None = None,
                 vault: dict[str, str] | None = None,
                 auth_proxy: bool = False):
        self.kind = kind
        self.container = container
        self._private = private or {}
        self._vault = vault or {}
        self._auth_proxy = auth_proxy

    def private_state(self, name: str = "") -> dict[str, str]:
        return dict(self._private)

    def vault_state(self, name: str = "") -> dict[str, str]:
        return dict(self._vault)

    def auth_proxy_enabled(self) -> bool:
        return self._auth_proxy


# ---------------------------------------------------------------------------
# 3. The three shipped provider scripts, invoked by name
# ---------------------------------------------------------------------------

# The variables `scripts.build_env` sets, plus the ones the shipped scripts
# read directly (`claude.sh`'s MULTIAGENTS_PROFILE/MODEL/MCP_CONFIG/...,
# `budget.py`'s MULTIAGENTS_BUDGET passed by the caller of `usage`). Stripped
# from the base environment before an override is applied, so a shipped
# script under test never inherits a stray value the ambient shell happens to
# carry, and never sees the developer's own CLAUDE_CONFIG_DIR / XDG dirs.
_SCRIPT_ENV_KEYS = (
    "MULTIAGENTS_PROVIDER", "MULTIAGENTS_BIN", "MULTIAGENTS_EXECUTOR",
    "MULTIAGENTS_UID", "MULTIAGENTS_GID", "MULTIAGENTS_CONTAINER",
    "MULTIAGENTS_PRIVATE_HOME", "MULTIAGENTS_PRIVATE_BACKING",
    "MULTIAGENTS_PRIVATE_VAULT", "MULTIAGENTS_AUTH_PROXY",
    "MULTIAGENTS_PROFILE", "MULTIAGENTS_MODEL", "MULTIAGENTS_MCP_CONFIG",
    "MULTIAGENTS_MCP_COMMAND", "MULTIAGENTS_MCP_ARGS",
    "MULTIAGENTS_LAUNCH_STATE", "MULTIAGENTS_PROJECT",
    "MULTIAGENTS_PROMPT_FILE", "MULTIAGENTS_SESSION_ID",
    "MULTIAGENTS_RESUME", "MULTIAGENTS_RESUME_PROMPT",
    "MULTIAGENTS_UNATTENDED", "MULTIAGENTS_NUDGE", "MULTIAGENTS_BUDGET",
    "MULTIAGENTS_ACCOUNT",
    "CLAUDE_CONFIG_DIR", "XDG_DATA_HOME", "XDG_CONFIG_HOME",
)


def shipped_script_path(name: str) -> Path:
    """`src/multiagents/defaults/providers/<name>.sh`, resolved."""
    return (shipped_defaults_dir() / "providers" / f"{name}.sh").resolve()


def run_shipped_script(name: str, action: str, env: dict[str, str] | None = None,
                       timeout: int = 20) -> subprocess.CompletedProcess:
    """`sh <shipped script> <action>`, with a clean, controlled environment.

    Starts from the real process environment (PATH matters — `python3` is
    invoked by name inside these scripts) with every variable in
    `_SCRIPT_ENV_KEYS` removed, then applies `env` on top. That is what makes
    this safe to call from a developer's own machine: without the strip, a
    real `CLAUDE_CONFIG_DIR` or a real `~/.claude` on PATH-adjacent state could
    leak into what looks like a fully-isolated test.
    """
    base = {k: v for k, v in os.environ.items() if k not in _SCRIPT_ENV_KEYS}
    base.update(env or {})
    return subprocess.run(
        ["sh", str(shipped_script_path(name)), action],
        env=base, capture_output=True, text=True, timeout=timeout,
    )


# ---------------------------------------------------------------------------
# 4. budget.read_claude — a profile directory instead of the developer's home
# ---------------------------------------------------------------------------

def claude_profile_dir(tmp_path: Path, *, access_token: str | None = None,
                       expires_at_ms: float | None = None,
                       cached_usage: dict[str, Any] | None = None) -> Path:
    """A `CLAUDE_CONFIG_DIR`-shaped directory: `.credentials.json` and
    `.claude.json` side by side, as the real CLI keeps them when that variable
    is set — never `~/.claude.json` / `~/.claude/.credentials.json`.

    `read_claude` / `_claude_token` read `Path.home()` directly when handed
    `config_dir=None` (bound at import time — see the module docstring), so a
    characterizer MUST always pass the directory this returns as
    `config_dir`, never `None`, or the test touches the developer's real
    account state.

    Passing neither `access_token` nor `cached_usage` gives a profile that
    exists but answers nothing — the "run Claude Code once" case.
    """
    import json
    import time as _time

    d = Path(tmp_path) / "claude-profile"
    d.mkdir(parents=True, exist_ok=True)
    if access_token is not None:
        oauth = {"accessToken": access_token}
        if expires_at_ms is not None:
            oauth["expiresAt"] = expires_at_ms
        (d / ".credentials.json").write_text(json.dumps({"claudeAiOauth": oauth}))
    if cached_usage is not None:
        (d / ".claude.json").write_text(json.dumps({
            "cachedUsageUtilization": {
                "fetchedAtMs": _time.time() * 1000,
                "utilization": cached_usage,
            },
        }))
    return d


def fake_claude_fetch(monkeypatch, payload: dict[str, Any] | None,
                      note: str = "") -> None:
    """Replace `budget.fetch_claude_usage` so `read_claude(fetch=True, ...)`
    never makes a real request to `api.anthropic.com`.

    Works because `_shared_usage` looks `fetch_claude_usage` up from the
    module's own globals at call time (`fetch = lambda: fetch_claude_usage(...)`
    closes over the NAME, not the function object) — patching the module
    attribute is enough; no need to stand up a fake HTTP server the way
    `c1_harness.authproxy_server` does for the auth proxy's upstream.
    """
    monkeypatch.setattr(budget_mod, "fetch_claude_usage",
                        lambda config_dir=None: (payload, note))
