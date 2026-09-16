"""Proof that the C1 harness (tests/support/c1_harness.py) runs.

Not a characterization suite — one or two tests per entry point, enough to
show the seams actually reach real production code. The next phase's
characterizers own the full suite.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import c1_harness as h  # noqa: E402
from multiagents.config import AgentSpec  # noqa: E402


# ---------------------------------------------------------------------------
# Entry point 1 — the allowlist decision
# ---------------------------------------------------------------------------

def test_allowlist_admits_exact_and_subdomain_hosts_and_refuses_others(tmp_path):
    allowlist = ["api.anthropic.com"]
    assert h.allowlist_admits(tmp_path, allowlist, "api.anthropic.com")
    assert h.allowlist_admits(tmp_path, allowlist, "sub.api.anthropic.com")
    assert not h.allowlist_admits(tmp_path, allowlist, "evilapi.anthropic.com")
    assert not h.allowlist_admits(tmp_path, allowlist, "anthropic.com")

    # An empty allowlist denies everything — FilterDefaultDeny with no lines
    # to match is a deny-all, not an allow-all.
    assert not h.allowlist_admits(tmp_path, [], "api.anthropic.com")

    # A malformed (non-string) entry blows up inside the real production
    # method, same as it would on a live `docker up` — the harness does not
    # paper over that.
    with pytest.raises(AttributeError):
        h.filter_patterns(tmp_path, [{"not": "a string"}])


def test_an_unescaped_pipe_in_an_allowlist_entry_admits_every_host():
    """F1: `write_proxy_config` only escapes literal dots, so an entry
    containing a bare `|` turns the WHOLE allowlist into an allow-all via ERE
    alternation's low precedence — proven here against the real method, not a
    reimplementation of it."""
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        tmp_path = Path(d)
        allowlist = ["evil.com|.*"]
        for host in ("attacker.example", "steal-creds.io", "pastebin.com"):
            assert h.allowlist_admits(tmp_path, allowlist, host), host


# ---------------------------------------------------------------------------
# Entry point 2 — the proxy's request/response and failure paths
# ---------------------------------------------------------------------------

def test_authproxy_refuses_a_caller_without_a_signed_token(tmp_path, monkeypatch):
    with h.authproxy_server(tmp_path, monkeypatch) as proxy:
        req = urllib.request.Request(
            proxy.base_url + "/v1/messages", data=b"{}", method="POST")
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(req, timeout=10)
        assert exc_info.value.code == 401


def test_authproxy_forwards_to_the_upstream_and_streams_the_body(tmp_path, monkeypatch):
    upstream_handler = h.fixed_response_upstream(200, b'{"hello":"world"}')
    with h.fake_http_server(upstream_handler) as upstream_url:
        with h.authproxy_server(
            tmp_path, monkeypatch,
            accounts={"only": "real-token"}, upstream=upstream_url,
        ) as proxy:
            token = proxy.mint("ag-1")
            req = urllib.request.Request(
                proxy.base_url + "/v1/messages", data=b"{}", method="POST")
            req.add_header("authorization", f"Bearer {token}")
            with urllib.request.urlopen(req, timeout=10) as resp:
                assert resp.status == 200
                assert json.loads(resp.read())["hello"] == "world"


def test_authproxy_refuses_with_429_when_every_account_is_rate_limited(tmp_path, monkeypatch):
    upstream_handler = h.fixed_response_upstream(
        429, b'{"type":"error"}', {"retry-after": "1"})
    with h.fake_http_server(upstream_handler) as upstream_url:
        with h.authproxy_server(
            tmp_path, monkeypatch,
            accounts={"only": "tok"}, upstream=upstream_url,
        ) as proxy:
            token = proxy.mint("ag-1")
            req = urllib.request.Request(
                proxy.base_url + "/v1/messages", data=b"{}", method="POST")
            req.add_header("authorization", f"Bearer {token}")
            with pytest.raises(urllib.error.HTTPError) as exc_info:
                urllib.request.urlopen(req, timeout=10)
            assert exc_info.value.code == 429


def test_authproxy_switches_accounts_after_a_rate_limit_and_serves_the_retry(tmp_path, monkeypatch):
    """The entire point of pinning an agent to more than one account: a 429
    from the pinned account must not reach the agent if another account is
    free to retry on."""
    seen_tokens = []

    class Upstream(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            seen_tokens.append(self.headers.get("authorization", ""))
            if self.headers.get("authorization") == "Bearer token-one":
                body = b'{"type":"error"}'
                self.send_response(429)
                self.send_header("retry-after", "5")
            else:
                body = b'{"ok":true}'
                self.send_response(200)
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    with h.fake_http_server(Upstream) as upstream_url:
        with h.authproxy_server(
            tmp_path, monkeypatch,
            accounts={"one": "token-one", "two": "token-two"},
            upstream=upstream_url,
        ) as proxy:
            token = proxy.mint("ag-1")
            req = urllib.request.Request(
                proxy.base_url + "/v1/messages", data=b"{}", method="POST")
            req.add_header("authorization", f"Bearer {token}")
            with urllib.request.urlopen(req, timeout=10) as resp:
                assert resp.status == 200
                assert resp.read() == b'{"ok":true}'
    assert seen_tokens == ["Bearer token-one", "Bearer token-two"], seen_tokens


def test_authproxy_reports_502_when_upstream_is_unreachable(tmp_path, monkeypatch):
    with h.authproxy_server(
        tmp_path, monkeypatch,
        accounts={"only": "tok"}, upstream=h.closed_port_url(),
    ) as proxy:
        token = proxy.mint("ag-1")
        req = urllib.request.Request(
            proxy.base_url + "/v1/messages", data=b"{}", method="POST")
        req.add_header("authorization", f"Bearer {token}")
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(req, timeout=10)
        assert exc_info.value.code == 502


# ---------------------------------------------------------------------------
# Entry point 3 — executor selection
# ---------------------------------------------------------------------------

def test_get_executor_dispatches_by_kind(tmp_path):
    assert isinstance(h.get_executor("local"), h.LocalExecutor)
    docker_ex = h.get_executor(
        "docker", {"image": "img"}, h.ProjectPaths(tmp_path), {}, tmp_path)
    assert isinstance(docker_ex, h.DockerExecutor)
    with pytest.raises(ValueError):
        h.get_executor("windows-sandbox")


def test_executor_for_honors_a_per_agent_executor_override(tmp_path):
    # executor_for's build() takes a PROVIDER name and looks for an agent
    # pinned to that provider with its own executor override.
    config = SimpleNamespace(
        executor="local",
        agents={
            "reviewer": AgentSpec("reviewer", "agy", "m", executor="docker"),
            "researcher": AgentSpec("researcher", "opencode", "m"),
        },
        project={"executor": {"docker": {"image": "img"}}},
    )
    build = h.executor_for(h.ProjectPaths(tmp_path), config, {})
    assert isinstance(build("agy"), h.DockerExecutor)       # pinned to docker
    assert isinstance(build("opencode"), h.LocalExecutor)   # falls back to config.executor
