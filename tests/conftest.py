"""Keep the suite off the developer's machine.

`state_root()` and `global_config_dir()` are machine-wide by design — one
`~/.multiagents` holding every project's worktrees and agent homes, one
`~/.config/multiagents` holding the editable defaults. Tests build a
`ProjectPaths` over a `tmp_path`, which makes the *slug* per-test but leaves
the *root* pointing at the real home, so `ProjectPaths.ensure()` has been
doing `mkdir` under it on nearly every test, and `register_project()` has been
appending to the real `projects.json`. Measured before this file existed:

    ~/.multiagents/worktrees        14,231 dirs, 13,886 written by tests
    ~/.multiagents/homes            14,232 dirs, 13,887 written by tests
    ~/.config/multiagents/projects.json   2,167 entries, 2,165 naming
                                          /tmp/pytest-of-*/ paths since deleted

That is the visible half. The half that cost time is that it makes the suite
non-idempotent: pytest recycles its `pytest-of-<user>/pytest-N` directory
names, the slug hashes the absolute path, so a later run collides with its own
leftovers and `git worktree add` fails with "already exists". A refactor is
then indistinguishable from a stale directory, which is exactly the wrong
thing to be guessing about while changing code the suite is supposed to be
guarding.

Both redirects already existed and about seven tests set the state one
themselves; nothing set them globally because there was no conftest.

The root is deliberately NOT under `tmp_path`. Most tests use `tmp_path` as the
project directory and some commit it, so a state root nested inside it would
sweep worktrees and agent homes into the repository under test.

What this does NOT cover, checked rather than assumed. `budget.py` binds
`CLAUDE_STATE` and `CLAUDE_CREDENTIALS` from `Path.home()` at import time, so
an environment variable set in a fixture cannot move them; a test that called
those readers unmocked would reach the real home and has to mock them instead.
Shipped scripts are different: they run as subprocesses and read `HOME` and
`XDG_*` when they start, so `opencode.sh budget` (auth store plus a live usage
endpoint) is kept off the real credentials by clearing `XDG_*` and pointing
`HOME` at a per-test directory (2026-10-01; full suite showed no test depending
on the real HOME).
"""

from __future__ import annotations

import inspect
import os
import urllib.error
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _caller_environment_is_invisible(monkeypatch):
    """Start every test with no MULTIAGENTS_* or CLAUDE_* variables.

    The suite is run by people and by agents, and an agent's environment
    carries its own identity: MULTIAGENTS_AGENT_ID, _DEPTH, _CAN_SPAWN,
    CLAUDE_CONFIG_DIR and the rest. The code under test reads those, so the
    same commit gave different results depending on who ran it — measured
    2026-09-23, about eighteen CAN_SPAWN tests and four doctor /
    container-profile tests failed from inside an agent and passed from a
    clean shell. That is a leak, not a regression, and it made a real
    regression impossible to tell apart from one.

    A test that needs one of these sets it itself with `monkeypatch.setenv`,
    which runs after this. `_machine_state_is_disposable` requests this
    fixture so its two redirects are set after the clear, not wiped by it.
    """
    for name in list(os.environ):
        if name.startswith(("MULTIAGENTS_", "CLAUDE_")):
            monkeypatch.delenv(name, raising=False)
    for name in ("XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME",
                 "XDG_STATE_HOME"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _machine_state_is_disposable(_caller_environment_is_invisible,
                                 tmp_path_factory, monkeypatch):
    """Point the machine-wide roots at a directory belonging to this test.

    `mktemp` numbers each call, so this is per-test rather than per-session.
    Per-test on purpose: a shared root lets one test read what another wrote
    and makes the suite's result depend on collection order. Cross-project
    behaviour belongs in a test that creates two projects in one root and says
    so, not in accidental bleed between unrelated tests.
    """
    root = tmp_path_factory.mktemp("machine")
    monkeypatch.setenv("MULTIAGENTS_STATE_DIR", str(root / "state"))
    monkeypatch.setenv("MULTIAGENTS_CONFIG_DIR", str(root / "config"))
    # A shipped budget script (`opencode.sh budget`) reads its auth store from
    # ${XDG_DATA_HOME:-$HOME/.local/share} and curls the usage endpoint; with
    # the XDG variables gone, HOME is the only way left to the real one.
    home = root / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))


@pytest.fixture(autouse=True)
def _no_catalog_fetch(monkeypatch):
    """Keep the suite off the network.

    `cmd_init` reports the model catalog, which downloads
    https://models.opencode.ai/api.json with a 45-SECOND timeout. Nothing
    stubbed it, so every init test made a live request: measured 2026-09-15,
    one of them took 43-57s on its own and the six of them dominated the whole
    run. It also makes the suite fail differently offline, and CI has no
    network at all.

    Raising URLError is not a special case — it is the outage path
    `catalog.check` already documents and returns `ok: False` for, so the tests
    exercise real behaviour rather than a stub. A test that wants a successful
    fetch patches `fetch_remote` back for itself.
    """
    from multiagents import catalog

    def refuse(*_args, **_kwargs):
        raise urllib.error.URLError("network is disabled in tests")

    monkeypatch.setattr(catalog, "fetch_remote", refuse)


@pytest.fixture(autouse=True)
def _no_provider_subprocesses(request, monkeypatch):
    """Keep the monitor's snapshot from shelling out to real agent CLIs.

    `providers_view` runs each provider's `budget` and `usage` script, and the
    shipped providers are the real ones: `claude auth status`, an HTTPS call
    from opencode.sh, and since 2026-09-14 a genuine `agy -p "/usage"` that
    takes about four seconds. The two monitor HTTP tests give their client a
    ten-second deadline and then serve `/api/state`, so on a loaded machine the
    request outran the client and the test failed on a socket timeout — three
    times in a row here, while passing in isolation.

    Only the provider ROWS are stubbed. Everything those tests are named for —
    the token gate, the Host check, the page — is untouched. A test that is
    actually about the rows marks itself `@pytest.mark.real_providers`.
    """
    if request.node.get_closest_marker("real_providers"):
        return
    from multiagents.monitor import snapshot

    monkeypatch.setattr(snapshot, "providers_view", lambda *a, **k: [])


@pytest.fixture(autouse=True)
def _no_host_cli_budget(request, monkeypatch, tmp_path_factory):
    """Keep `budget` and `models` from running a shipped provider script
    against the developer's own CLI.

    `Runner.start` reads every enabled provider's budget, and the shipped
    providers are the real ones: `agy.sh budget` runs the `agy` it finds on
    PATH as `agy -p /usage`, and `codex.py budget` talks to the real codex.
    Measured 2026-10-01 (TS Run B): on a machine with agy installed but not
    answering, twelve tests paid its 10 s script timeout each, and which twelve
    depended on the order tests landed on an xdist worker, because
    `budget._cache` is per process with a 60 s life. That is the host leak the
    docstring at the top of this file describes for the budget readers. The
    same run measured `init` listing models through the real `opencode models`
    and `agy models`, about 7 s per init.

    Where the `budget` or `models` action would run a SHIPPED script with a CLI that the
    test did not build under the basetemp, the script still runs, but as on a
    machine without that CLI: `MULTIAGENTS_BIN` empty, `MULTIAGENTS_BIN_ERROR`
    saying why. Every shipped script reads its CLI only from there, so what
    comes back is that script's own no-CLI answer (an unknown reading, or 64
    for claude's built-in reader). A test that points a shipped script at a
    fake CLI of its own is untouched, and so is one marked `real_providers`.
    """
    if request.node.get_closest_marker("real_providers"):
        return
    from multiagents import scripts
    from multiagents.paths import shipped_defaults_dir

    real = scripts.run_action
    basetemp = tmp_path_factory.getbasetemp().resolve()
    shipped = shipped_defaults_dir() / "providers"

    def effective(value, cwd, search_path):
        """The file `value` names when exec'd from `cwd` (None: this process's
        directory) with `search_path` as PATH. A relative path, and a relative
        or empty PATH entry, resolve against that directory, not ours (review
        ag-7236bf). None when a bare name is found nowhere."""
        base = Path.cwd() / cwd if cwd is not None else Path.cwd()
        if "/" in value:
            return base / value
        for entry in search_path.split(os.pathsep):
            candidate = base / entry / value
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return candidate
        return None

    def host_cli(name, provider, executor, config_dir, project_config, extra_env, cwd):
        """The CLI the shipped script would run, when it is the host's: read
        from MULTIAGENTS_BIN as the script will see it, after the provider's
        own `env:`/`credential_env` and the caller's extra_env (review
        ag-dc89fc), not from the provider's `bin`."""
        script = scripts.resolve(name, provider, config_dir, project_config)
        twin = shipped / script.name if script is not None else None
        if twin is None or not twin.is_file() or script.read_bytes() != twin.read_bytes():
            return None
        try:
            env = scripts.build_env(name, provider, executor, extra_env)
        except Exception:                                    # noqa: BLE001
            return None             # run_action reports it as it always has
        value = env.get("MULTIAGENTS_BIN") or ""
        if not value:
            return None
        binary = effective(value, cwd, env.get("PATH", ""))
        if binary is None or binary.resolve().is_relative_to(basetemp):
            return None
        return binary

    signature = inspect.signature(real)

    def run_action(*args, **kwargs):
        bound = signature.bind(*args, **kwargs)
        bound.apply_defaults()
        call = bound.arguments
        if call["action"] in ("budget", "models"):
            binary = host_cli(call["provider_name"], call["provider"], call["executor"],
                              call["config_dir"], call["project_config"], call["extra_env"],
                              call["cwd"])
            if binary is not None:
                call["extra_env"] = {**(call["extra_env"] or {}),
                                     "MULTIAGENTS_BIN": "",
                                     "MULTIAGENTS_BIN_ERROR":
                                         f"tests do not run the host's {binary}"}
        return real(*bound.args, **bound.kwargs)

    monkeypatch.setattr(scripts, "run_action", run_action)

    # `models_cmd` (opencode, agy) is run by models.py itself, not by a
    # script: the same host CLIs, refused as an exec on a machine without them
    # would be.
    import errno
    import subprocess
    import yaml
    from multiagents import models

    names = {str(block.get("bin")) for block in (yaml.safe_load(
        (shipped_defaults_dir() / "providers.yaml").read_text()).get("providers") or {}
    ).values() if isinstance(block, dict) and block.get("bin")}

    class Subprocess:
        def __getattr__(self, name):
            return getattr(subprocess, name)

        def run(self, command, *args, **kwargs):
            env = kwargs.get("env")
            exe = effective(str(command[0]), kwargs.get("cwd"),
                            (os.environ if env is None else env).get("PATH", ""))
            if (exe is not None and exe.name in names
                    and not exe.resolve().is_relative_to(basetemp)):
                raise FileNotFoundError(errno.ENOENT,
                                        "tests do not run the host's CLI", str(exe))
            return subprocess.run(command, *args, **kwargs)

    monkeypatch.setattr(models, "subprocess", Subprocess())


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "real_providers: this test is about providers_view itself, so let it run")
