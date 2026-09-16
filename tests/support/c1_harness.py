"""Shared test harness for C1 — sandbox and egress boundary.

Builders and seams for `executor/docker.py`, `executor/base.py`,
`executor/local.py`, `executor/__init__.py` and `authproxy.py`, so a
characterizer can construct the objects and reach the entry points without a
docker daemon, a tinyproxy binary, or the real model API.

**What is NOT reachable through this harness.** Actually spawning a container
(`DockerExecutor.ensure_running` / `ensure_proxy` / `ensure_auth_proxy` calling
real `docker run`, `docker network create`, ...) needs the `docker` binary,
which is not on PATH in this environment (`docker_available()` returns False
here — the same reason three tests in `test_core.py` already guard themselves
with `if shutil.which("docker") is None: pytest.skip(...)`). Do not work around
that: no docker-in-docker, no socket mount. Everything else in this module is
real production code, exercised directly rather than mocked:

- argv construction (`run_args`), mount planning (`mounts`, `private_state`),
  and the proxy config generator (`write_proxy_config`) are pure — no
  subprocess, no daemon — and are exercised as-is.
- the auth proxy (`authproxy.serve`) is started for real, on a real socket, in
  a real thread, exactly as `docker.py:ensure_auth_proxy` starts it in a
  container. Only its upstream (`https://api.anthropic.com`) is swapped for a
  local fake, via the module-level `authproxy.UPSTREAM` constant — the proxy
  code that decides what to do with a response is untouched.
"""

from __future__ import annotations

import contextlib
import json
import re
import shutil
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from multiagents import authproxy                              # noqa: E402
from multiagents.executor import get_executor, executor_for    # noqa: E402
from multiagents.executor.docker import DockerExecutor         # noqa: E402
from multiagents.executor.local import LocalExecutor           # noqa: E402
from multiagents.executor.base import Executor, build_env, prepare_home  # noqa: E402
from multiagents.paths import ProjectPaths                     # noqa: E402

__all__ = [
    "docker_available", "make_docker_executor", "filter_patterns",
    "allowlist_admits", "seed_account", "authproxy_server", "closed_port_url",
    "fake_http_server", "fixed_response_upstream",
    "get_executor", "executor_for", "DockerExecutor", "LocalExecutor",
    "Executor", "build_env", "prepare_home", "ProjectPaths",
]


def docker_available() -> bool:
    return shutil.which("docker") is not None


# ---------------------------------------------------------------------------
# 1. Construction — the executors
# ---------------------------------------------------------------------------

def make_docker_executor(tmp_path: Path, providers: dict | None = None,
                         **config) -> DockerExecutor:
    """A `DockerExecutor` with sensible defaults. Building one does no I/O and
    touches neither the filesystem nor the docker daemon — only calling one of
    its methods (`run_args`, `mounts`, `write_proxy_config`, ...) does."""
    config.setdefault("image", "img")
    config.setdefault("network", "allowlist")
    return DockerExecutor(config, ProjectPaths(tmp_path), providers or {}, tmp_path)


# ---------------------------------------------------------------------------
# 2. The allowlist decision — no network, no tinyproxy binary
# ---------------------------------------------------------------------------

def filter_patterns(tmp_path: Path, allowlist: list) -> list[str]:
    """The exact ERE lines `DockerExecutor.write_proxy_config` hands to
    tinyproxy for this allowlist. Calls the real production method — nothing
    about pattern generation is reimplemented here. A malformed entry (not a
    string) raises AttributeError from inside that real method, same as it
    would on a live `docker up`; this does not catch it for you."""
    ex = make_docker_executor(tmp_path, egress_allowlist=allowlist)
    target = ex.write_proxy_config(tmp_path / "proxy")
    return [line for line in (target / "filter").read_text().splitlines() if line.strip()]


def allowlist_admits(tmp_path: Path, allowlist: list, host: str) -> bool:
    """Would tinyproxy's generated filter let `host` through?

    tinyproxy is configured `FilterType ere`, `FilterCaseSensitive Off`,
    `FilterDefaultDeny Yes`, `FilterURLs Off` (`Dockerfile.proxy` +
    `write_proxy_config`) — each filter line is a case-insensitive POSIX
    extended regex matched against the bare destination host, and a host
    matching none of them is refused. There is no tinyproxy binary in this
    sandbox, so this evaluates the SAME lines tinyproxy would load, using
    Python's `re` module standing in for tinyproxy's `regexec` — equivalent
    POSIX ERE semantics for the constructs these patterns actually use
    (anchors, escaped literals, alternation). See F1 in C1-sandbox.md: that
    equivalence is exactly what makes a crafted allowlist entry dangerous for
    the real proxy too, not just for this stand-in.
    """
    patterns = filter_patterns(tmp_path, allowlist)
    return any(re.search(p, host, re.IGNORECASE) for p in patterns)


# ---------------------------------------------------------------------------
# 3. The auth proxy — real HTTP, real threads, a fake upstream
# ---------------------------------------------------------------------------

def seed_account(vault: Path, label: str, access_token: str) -> None:
    d = vault / "accounts" / label
    d.mkdir(parents=True, exist_ok=True)
    (d / ".credentials.json").write_text(
        json.dumps({"claudeAiOauth": {"accessToken": access_token}}))


@contextlib.contextmanager
def authproxy_server(tmp_path: Path, monkeypatch, accounts: dict[str, str] | None = None,
                     upstream: str | None = None, on_event: Callable | None = None):
    """Start the real `authproxy.serve()` on an ephemeral localhost port.

    `accounts` seeds `<vault>/accounts/<label>/.credentials.json` for each
    label -> access-token pair before the server starts. `upstream`, if given,
    monkeypatches `authproxy.UPSTREAM` for the life of the `with` block — the
    seam onto the proxy's model-API destination, so `_forward` talks to a
    `fake_http_server()` (or `closed_port_url()`, for "unreachable") instead of
    `https://api.anthropic.com`. Requires a `monkeypatch` fixture whenever
    `upstream` is given, so the swap is undone even if the test fails.

    Yields `SimpleNamespace(base_url, vault, secret, mint(agent_id), server)`.
    `mint(agent_id)` returns a bearer token this proxy will accept — what a
    characterizer sends as `Authorization`.

    Teardown: shuts the server down and closes its socket on exit. Nothing
    outlives the `with` block except the files under `tmp_path`, which is
    pytest's own responsibility.

    NOTE: `authproxy.Handler` is a module-level class, and `serve()` sets
    `Handler.accounts` / `.secret` / `.on_event` as CLASS attributes — the same
    global-class-attribute pattern `tests/test_core.py` already uses for
    `monitor.server.Handler`. Do not run two `authproxy_server()` blocks
    concurrently in the same process; sequential (the normal case under
    pytest, including under `pytest-randomly`) is fine because each call
    reassigns those attributes before its own server starts accepting.
    """
    vault = tmp_path / "vault"
    vault.mkdir(parents=True, exist_ok=True)
    for label, token in (accounts or {}).items():
        seed_account(vault, label, token)
    if upstream is not None:
        monkeypatch.setattr(authproxy, "UPSTREAM", upstream)
    server = authproxy.serve(vault, host="127.0.0.1", port=0, on_event=on_event)
    secret = authproxy.Handler.secret
    try:
        yield SimpleNamespace(
            base_url=f"http://127.0.0.1:{server.server_address[1]}",
            vault=vault, secret=secret,
            mint=lambda agent_id: authproxy.mint_token(agent_id, secret),
            server=server,
        )
    finally:
        server.shutdown()
        server.server_close()


def closed_port_url() -> str:
    """A `http://127.0.0.1:<port>` nothing is listening on — for exercising
    the "upstream unreachable" branch of `_forward` (connection refused) without
    depending on the real network or a real TLS handshake failure. `_forward`
    catches this the same way it catches a DNS failure or a TLS error: a bare
    `except Exception` around `urlopen`, so this one case stands in for all
    three from the proxy's point of view."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return f"http://127.0.0.1:{port}"


@contextlib.contextmanager
def fake_http_server(handler_cls):
    """A minimal stand-in server for whatever a characterizer needs on the
    other end of an HTTP call — here, `api.anthropic.com`. Same idiom the
    project already uses for `monitor.server.Handler` in
    `test_the_api_refuses_a_caller_without_the_token`: a bare
    `BaseHTTPRequestHandler` subclass served by a real `ThreadingHTTPServer` on
    an ephemeral port, torn down on exit."""
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}"
    finally:
        httpd.shutdown()
        httpd.server_close()


def fixed_response_upstream(status: int, body: bytes = b"{}",
                            headers: dict[str, str] | None = None):
    """A handler class that answers every request the same way: a 429 with a
    Retry-After, a bare 400, whatever one failure path needs. Build a fresh one
    per test — it carries no state, so reuse is safe, but a fresh class keeps
    tests independent of each other by construction."""
    class _Fixed(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _reply(self):
            self.send_response(status)
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = _reply
        do_POST = _reply

    return _Fixed
