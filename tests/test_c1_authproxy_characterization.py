"""Characterization of `authproxy.py` request handling — continuation.

`tests/test_char_c1_authproxy.py` already pins header/body/query forwarding,
the scrub-leaves-PII behaviour (F21), the no-event-on-unreachable-upstream
behaviour (F20's first half), redirect passthrough, malformed-JSON account
exhaustion, and large-body passthrough. This file does not repeat those; it
covers what `context/review/HANDOFF-c1-authproxy.md` flagged as still open:
account-credential edge cases, upstream status-code/header passthrough,
rate-limit retry/switch sequencing, the *ordinary* (non-429/529) HTTPError
no-event claim specifically, what `_scrub` does versus doesn't catch by key
name versus body shape, and the handoff's item #3 (a token's agent id is the
only identity there is — re-verified here against a stubbed upstream instead
of the real API it hit by accident).

Findings are in `context/review/C1-sandbox-authproxy.md`, F20 onwards.
"""

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))
import c1_harness as h


# ---------------------------------------------------------------------------
# Account-credential failure modes
# ---------------------------------------------------------------------------

def test_no_accounts_at_all_exhausts_immediately(tmp_path, monkeypatch):
    """No vault/accounts directory, no top-level .credentials.json: `labels()`
    returns [] and the very first `for_agent` call fails closed."""
    events = []
    with h.authproxy_server(tmp_path, monkeypatch, accounts=None,
                            upstream=h.closed_port_url(),
                            on_event=lambda k, f: events.append((k, f))) as proxy:
        req = urllib.request.Request(
            proxy.base_url + "/v1/messages", data=b"{}",
            headers={"Authorization": "Bearer " + proxy.mint("agent1")})
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req)
        assert exc.value.code == 429
        assert events == [("exhausted", {"agent": "agent1", "tried": []})]


def test_empty_credentials_file_is_treated_like_no_token(tmp_path, monkeypatch):
    """An empty `.credentials.json` (0 bytes): `json.loads("")` raises
    ValueError, caught by `Accounts.token`, which returns "" — same path as
    malformed JSON, marks the lone account limited and exhausts."""
    vault = tmp_path / "vault"
    d = vault / "accounts" / "acc"
    d.mkdir(parents=True)
    (d / ".credentials.json").write_text("")

    events = []
    with h.authproxy_server(tmp_path, monkeypatch, accounts=None,
                            upstream=h.closed_port_url(),
                            on_event=lambda k, f: events.append((k, f))) as proxy:
        req = urllib.request.Request(
            proxy.base_url + "/v1/messages", data=b"{}",
            headers={"Authorization": "Bearer " + proxy.mint("agent1")})
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req)
        assert exc.value.code == 429
        assert events == [("exhausted", {"agent": "agent1", "tried": ["acc"]})]


def test_credentials_file_without_access_token_key_exhausts(tmp_path, monkeypatch):
    """Valid JSON, but no block has a truthy `accessToken` — e.g. a stale
    OAuth blob with only a `refreshToken`. `Accounts.token` falls through its
    loop and returns "", same as missing/empty."""
    vault = tmp_path / "vault"
    d = vault / "accounts" / "acc"
    d.mkdir(parents=True)
    (d / ".credentials.json").write_text(
        json.dumps({"claudeAiOauth": {"refreshToken": "r-only", "accessToken": ""}}))

    events = []
    with h.authproxy_server(tmp_path, monkeypatch, accounts=None,
                            upstream=h.closed_port_url(),
                            on_event=lambda k, f: events.append((k, f))) as proxy:
        req = urllib.request.Request(
            proxy.base_url + "/v1/messages", data=b"{}",
            headers={"Authorization": "Bearer " + proxy.mint("agent1")})
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req)
        assert exc.value.code == 429
        assert events == [("exhausted", {"agent": "agent1", "tried": ["acc"]})]


def test_legacy_top_level_credentials_file_is_account_default(tmp_path, monkeypatch):
    """A vault with a top-level `.credentials.json` and no `accounts/`
    directory is treated as one account named "default" — the pre-multi-
    account layout, per `Accounts.labels`/`Accounts.path`."""
    vault = tmp_path / "vault"
    vault.mkdir(parents=True)
    (vault / ".credentials.json").write_text(
        json.dumps({"claudeAiOauth": {"accessToken": "legacy-token"}}))

    with h.fake_http_server(h.fixed_response_upstream(200, body=b'{"ok":true}')) as upstream_url:
        with h.authproxy_server(tmp_path, monkeypatch, accounts=None,
                                upstream=upstream_url) as proxy:
            req = urllib.request.Request(
                proxy.base_url + "/v1/messages", data=b"{}",
                headers={"Authorization": "Bearer " + proxy.mint("agent1")})
            resp = urllib.request.urlopen(req)
            assert resp.status == 200
            assert resp.read() == b'{"ok":true}'


def test_token_agent_id_is_the_only_identity_no_separate_check(tmp_path, monkeypatch):
    """Re-verifies (with a stubbed upstream this time — the handoff's own
    attempt at this accidentally hit the real Anthropic API, per its safety
    note) that there is no notion of "the wrong agent" independent of the
    token itself: whichever agent id a validly-signed token names IS the
    identity `for_agent` pins against. Two tokens minted for two different
    agent ids, used standalone with no other context, each pin to their own
    account deterministically and independently — nothing compares "who is
    calling" against anything else."""
    with h.fake_http_server(h.fixed_response_upstream(200, body=b'{"ok":true}')) as upstream_url:
        with h.authproxy_server(tmp_path, monkeypatch,
                                accounts={"acc-a": "ta", "acc-b": "tb"},
                                upstream=upstream_url) as proxy:
            token_a = proxy.mint("agentA")
            token_b = proxy.mint("agentB")

            def send(token):
                req = urllib.request.Request(
                    proxy.base_url + "/v1/messages", data=b"{}",
                    headers={"Authorization": "Bearer " + token})
                return urllib.request.urlopen(req)

            assert send(token_a).status == 200
            assert send(token_b).status == 200
            # Same agent, same token again: still accepted, pinned to the
            # same account as before (fewest-pins tie-break already resolved
            # for it on the first call).
            assert send(token_a).status == 200


# ---------------------------------------------------------------------------
# Rate-limit retry / account-switch sequencing
# ---------------------------------------------------------------------------

def test_429_from_upstream_switches_then_exhausts_with_two_accounts(tmp_path, monkeypatch):
    """One agent, two accounts, upstream always 429s: `for_agent` picks one,
    `_forward` marks it limited and returns "limited", `do_POST`'s loop emits
    `switch` and tries the second account, which also gets marked limited —
    the third `for_agent` call finds nothing free and exhausts. Order of
    `tried` matches the fewest-agents-pinned-first tie-break in `for_agent`
    (alphabetical on a tie of zero pins each), so "acc-a" goes first."""
    events = []
    with h.fake_http_server(h.fixed_response_upstream(
            429, body=b"{}", headers={"Retry-After": "120"})) as upstream_url:
        with h.authproxy_server(tmp_path, monkeypatch,
                                accounts={"acc-a": "ta", "acc-b": "tb"},
                                upstream=upstream_url,
                                on_event=lambda k, f: events.append((k, f))) as proxy:
            req = urllib.request.Request(
                proxy.base_url + "/v1/messages", data=b"{}",
                headers={"Authorization": "Bearer " + proxy.mint("agent1")})
            with pytest.raises(urllib.error.HTTPError) as exc:
                urllib.request.urlopen(req)
            assert exc.value.code == 429
            # Both accounts get a `switch` event on the way down — the second
            # account also gets marked limited and switched away from before
            # the THIRD `for_agent` call (which finds nothing free) emits
            # `exhausted`. Three events, not two: `switch` fires even for the
            # account that turns out to be the last one available.
            assert events == [
                ("switch", {"agent": "agent1", "away_from": "acc-a"}),
                ("switch", {"agent": "agent1", "away_from": "acc-b"}),
                ("exhausted", {"agent": "agent1", "tried": ["acc-a", "acc-b"]}),
            ]


def test_529_from_upstream_is_treated_as_rate_limit_too(tmp_path, monkeypatch):
    """529 (Anthropic's "overloaded") takes the same `mark_limited` branch as
    429 in `_forward` — not just literally 429."""
    events = []
    with h.fake_http_server(h.fixed_response_upstream(529, body=b"{}")) as upstream_url:
        with h.authproxy_server(tmp_path, monkeypatch, accounts={"acc": "t"},
                                upstream=upstream_url,
                                on_event=lambda k, f: events.append((k, f))) as proxy:
            req = urllib.request.Request(
                proxy.base_url + "/v1/messages", data=b"{}",
                headers={"Authorization": "Bearer " + proxy.mint("agent1")})
            with pytest.raises(urllib.error.HTTPError) as exc:
                urllib.request.urlopen(req)
            assert exc.value.code == 429  # client always sees 429, never 529
            # A `switch` fires first (the one account is marked limited and
            # unpinned), THEN the retry finds nothing free and exhausts.
            assert events == [
                ("switch", {"agent": "agent1", "away_from": "acc"}),
                ("exhausted", {"agent": "agent1", "tried": ["acc"]}),
            ]


def test_retry_after_header_is_read_as_a_duration_not_an_epoch(tmp_path, monkeypatch):
    """`_retry_after` treats a small `Retry-After` value as a duration in
    seconds (its `value > 1e9` epoch check does not trigger). With one
    account and a single request, the account is marked limited for that
    duration but this request itself just sees the 429 straight through —
    the limit only matters to the *next* request, which is out of scope for
    a single-request test. This pins that no exception is raised on parsing
    a small Retry-After."""
    with h.fake_http_server(h.fixed_response_upstream(
            429, body=b"{}", headers={"Retry-After": "5"})) as upstream_url:
        with h.authproxy_server(tmp_path, monkeypatch, accounts={"acc": "t"},
                                upstream=upstream_url) as proxy:
            req = urllib.request.Request(
                proxy.base_url + "/v1/messages", data=b"{}",
                headers={"Authorization": "Bearer " + proxy.mint("agent1")})
            with pytest.raises(urllib.error.HTTPError) as exc:
                urllib.request.urlopen(req)
            assert exc.value.code == 429


# ---------------------------------------------------------------------------
# Ordinary (non-429/529) upstream errors: status, headers, and events
# ---------------------------------------------------------------------------

def test_ordinary_500_passthrough_preserves_status_and_emits_no_event(tmp_path, monkeypatch):
    """The handoff flagged this exact case as unconfirmed: a genuine HTTPError
    from upstream (not 429/529, so it never reaches `mark_limited`/`_event`
    at all — `_forward`'s `except HTTPError` branch for a non-limit code has
    no `self._event(...)` call anywhere on it). Confirmed here with a live
    500, distinct from the unreachable-upstream 502 case already pinned in
    `test_char_c1_authproxy.py` (that one takes the bare `except Exception`
    branch, not `except HTTPError`).

    Note: `_scrub` round-trips every JSON error body through
    `json.loads`/`json.dumps` even when nothing is redacted, so the bytes are
    not preserved verbatim (whitespace is renormalized) even though the
    content is unchanged — compared here by parsed value, not raw bytes."""
    events = []
    body = b'{"type":"error","error":{"type":"internal_server_error"}}'
    with h.fake_http_server(h.fixed_response_upstream(500, body=body)) as upstream_url:
        with h.authproxy_server(tmp_path, monkeypatch, accounts={"acc": "t"},
                                upstream=upstream_url,
                                on_event=lambda k, f: events.append((k, f))) as proxy:
            req = urllib.request.Request(
                proxy.base_url + "/v1/messages", data=b"{}",
                headers={"Authorization": "Bearer " + proxy.mint("agent1")})
            with pytest.raises(urllib.error.HTTPError) as exc:
                urllib.request.urlopen(req)
            assert exc.value.code == 500
            assert json.loads(exc.value.read()) == json.loads(body)
            assert events == []


def test_ordinary_401_passthrough_drops_all_upstream_headers(tmp_path, monkeypatch):
    """F22: the error-passthrough branch of `_forward` (authproxy.py:320-324,
    the `except urllib.error.HTTPError` case for a non-429/529 code) sends
    only its own `content-type: application/json` and `content-length` — it
    does NOT loop over `exc.headers` the way the success branch loops over
    `upstream.headers` (line 337). A custom header the upstream sent (tested
    here; also true for something like `WWW-Authenticate` or a `Retry-After`
    on a plain 400) is silently dropped on this path, unlike on success. A
    plain 401 from upstream (e.g. an expired access token) still passes
    through with its own status code, distinct from the proxy's OWN 401 for
    an unsigned/missing token (a fixed `_fail(401, ...)` body on a different
    path entirely, not this one)."""
    with h.fake_http_server(h.fixed_response_upstream(
            401, body=b'{"error":"expired"}',
            headers={"X-Upstream-Marker": "yes"})) as upstream_url:
        with h.authproxy_server(tmp_path, monkeypatch, accounts={"acc": "t"},
                                upstream=upstream_url) as proxy:
            req = urllib.request.Request(
                proxy.base_url + "/v1/messages", data=b"{}",
                headers={"Authorization": "Bearer " + proxy.mint("agent1")})
            with pytest.raises(urllib.error.HTTPError) as exc:
                urllib.request.urlopen(req)
            assert exc.value.code == 401
            assert json.loads(exc.value.read()) == {"error": "expired"}
            assert exc.value.headers.get("X-Upstream-Marker") is None
            assert exc.value.headers.get("Content-Type") == "application/json"


# ---------------------------------------------------------------------------
# What `_scrub` actually catches
# ---------------------------------------------------------------------------

def test_scrub_masks_a_key_named_like_a_secret_but_not_its_sibling_pii(tmp_path, monkeypatch):
    """`_scrub` -> `redact.scrub` drops a dict VALUE wholesale when its KEY
    matches `_SECRET_KEYS` (e.g. "api_key", "access_token"), regardless of
    the value's shape. A sibling key like "organization" or "email" is not
    dropped — it only survives redaction if its VALUE happens to match one of
    the shape patterns (JWT, sk-..., ghp_..., bearer ..., or "key: value"
    with a secret-looking key *inside the string*), which plain PII text does
    not. This pins the boundary precisely: key-based scrubbing works,
    unstructured PII in the same body does not benefit from it at all."""
    body = json.dumps({
        "error": {
            "message": "token invalid",
            "api_key": "sk-should-be-masked-1234567890",
            "organization": "MyCorp Inc",
            "email": "admin@mycorp.com",
        }
    }).encode()
    with h.fake_http_server(h.fixed_response_upstream(400, body=body)) as upstream_url:
        with h.authproxy_server(tmp_path, monkeypatch, accounts={"acc": "t"},
                                upstream=upstream_url) as proxy:
            req = urllib.request.Request(
                proxy.base_url + "/v1/messages", data=b"{}",
                headers={"Authorization": "Bearer " + proxy.mint("agent1")})
            with pytest.raises(urllib.error.HTTPError) as exc:
                urllib.request.urlopen(req)
            got = json.loads(exc.value.read())
            assert got["error"]["api_key"] == "[redacted]"
            assert got["error"]["organization"] == "MyCorp Inc"
            assert got["error"]["email"] == "admin@mycorp.com"
            assert got["error"]["message"] == "token invalid"


def test_scrub_falls_back_to_original_bytes_on_non_json_error_body(tmp_path, monkeypatch):
    """`_scrub` wraps `json.loads`/`scrub`/`json.dumps` in a bare `except
    Exception: return payload` — a non-JSON error body from upstream (plain
    text, HTML) is returned completely unscrubbed, byte for byte. If such a
    body ever contained a bearer token in a shape `_scrub`'s own regexes
    would normally catch, that protection is bypassed entirely because the
    body never reaches `redact.scrub` at all."""
    body = b"<html>Bad Gateway: token=sk-live-1234567890abcdef leaked here</html>"
    with h.fake_http_server(h.fixed_response_upstream(502, body=body)) as upstream_url:
        with h.authproxy_server(tmp_path, monkeypatch, accounts={"acc": "t"},
                                upstream=upstream_url) as proxy:
            req = urllib.request.Request(
                proxy.base_url + "/v1/messages", data=b"{}",
                headers={"Authorization": "Bearer " + proxy.mint("agent1")})
            with pytest.raises(urllib.error.HTTPError) as exc:
                urllib.request.urlopen(req)
            assert exc.value.code == 502
            assert exc.value.read() == body


# ---------------------------------------------------------------------------
# Body edge cases and the GET/POST alias
# ---------------------------------------------------------------------------

def test_empty_body_passes_through_as_empty(tmp_path, monkeypatch):
    with _echo_upstream() as upstream_url:
        with h.authproxy_server(tmp_path, monkeypatch, accounts={"acc": "t"},
                                upstream=upstream_url) as proxy:
            req = urllib.request.Request(
                proxy.base_url + "/v1/messages", data=b"",
                headers={"Authorization": "Bearer " + proxy.mint("agent1")})
            resp = urllib.request.urlopen(req)
            data = json.loads(resp.read())
            assert data["body"] == ""


def test_get_request_is_forwarded_upstream_as_post(tmp_path, monkeypatch):
    """`Handler.do_GET = do_POST` (authproxy.py:299) is a straight alias, and
    `_forward` hardcodes `method="POST"` on the `urllib.request.Request` it
    builds regardless of `self.command`. A client sending a bare GET to the
    proxy therefore reaches the upstream as a POST, not a GET — confirmed
    directly rather than assumed from reading the alias."""
    with _echo_upstream() as upstream_url:
        with h.authproxy_server(tmp_path, monkeypatch, accounts={"acc": "t"},
                                upstream=upstream_url) as proxy:
            req = urllib.request.Request(
                proxy.base_url + "/v1/messages",
                headers={"Authorization": "Bearer " + proxy.mint("agent1")},
                method="GET")
            resp = urllib.request.urlopen(req)
            data = json.loads(resp.read())
            assert data["method"] == "POST"


import contextlib
from http.server import BaseHTTPRequestHandler


class _EchoWithMethod(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _handle(self):
        length = int(self.headers.get("content-length", 0) or 0)
        body = self.rfile.read(length) if length else b""
        resp = json.dumps({"method": self.command, "body": body.decode()}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(resp)))
        self.end_headers()
        self.wfile.write(resp)

    do_GET = _handle
    do_POST = _handle


@contextlib.contextmanager
def _echo_upstream():
    with h.fake_http_server(_EchoWithMethod) as url:
        yield url
