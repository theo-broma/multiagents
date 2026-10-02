"""DK: claude authenticates under the docker executor (bug-07d880).

Contract: `context/specs/docker-claude-auth.md` (DK-R1..R6, R2a, R3a, R4a,
R4b, and the precise R5 from its revision section).

Everything here drives `authproxy.py` in-process, on a real socket, against a
fake upstream that answers 200, 401 or 429 per access token. No docker, no
network, no real claude, and no real credential file is read: every vault is a
temp dir holding fake tokens.

Pins: the contract does not fix how a pin reaches the sidecar or how the host
signs the claim. The ONE place that assumption lives is `start_proxy` /
`mint` below. Assumed shape (NEED_INFO, to be confirmed against the
implementation, then changed in these two helpers only):
    authproxy.serve(vault, host, port, on_event, pins={provider: label})
    authproxy.mint_token(agent_id, secret, provider=<provider name>)
The pin key's name in provider config (`container_account`, as the contract
suggests) is never used by these tests.

Not expressible here (see the final report): DK-R3 renewal and per-account
`check`, DK-R4 profile dir and `docker login --account`, DK-R5 budget. Their
public surface is not fixed by the contract.
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from multiagents import authproxy  # noqa: E402

SRC = Path(__file__).resolve().parents[1] / "src" / "multiagents"


# --------------------------------------------------------------------------
# Vault and fake upstream
# --------------------------------------------------------------------------

def write_login(path: Path, token: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"claudeAiOauth": {
        "accessToken": token, "refreshToken": "fake-refresh-" + token,
        "expiresAt": 4102444800000}}))


def make_vault(tmp_path: Path, top: str | None = None, **accounts: str) -> Path:
    vault = tmp_path / "vault"
    vault.mkdir(parents=True, exist_ok=True)
    if top is not None:
        write_login(vault / ".credentials.json", top)
    for label, token in accounts.items():
        write_login(vault / "accounts" / label / ".credentials.json", token)
    return vault


class Upstream:
    """Answers per access token and records every token it was asked with."""

    def __init__(self, answers: dict[str, int], default: int = 200) -> None:
        self.answers = dict(answers)
        self.default = default
        self.hits: list[str] = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _reply(self):
                n = int(self.headers.get("content-length") or 0)
                if n:
                    self.rfile.read(n)
                token = self.headers.get("authorization", "").replace("Bearer ", "")
                outer.hits.append(token)
                status = outer.answers.get(token, outer.default)
                body = json.dumps({"served_by": token}).encode()
                self.send_response(status)
                if status == 429:
                    self.send_header("retry-after", "120")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = do_POST = _reply

        self.handler = H

    def count(self, token: str) -> int:
        return self.hits.count(token)


class Rig:
    def __init__(self, tmp_path, monkeypatch):
        self.tmp_path = tmp_path
        self.monkeypatch = monkeypatch
        self.events: list[tuple[str, dict]] = []
        self.servers: list = []
        self.httpds: list = []

    def upstream(self, answers=None, default=200, handler=None) -> Upstream:
        up = Upstream(answers or {}, default)
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler or up.handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.httpds.append(httpd)
        self.monkeypatch.setattr(authproxy, "UPSTREAM",
                                 f"http://127.0.0.1:{httpd.server_port}")
        return up

    def start_proxy(self, vault: Path, pins: dict | None = None) -> str:
        kwargs = {"pins": pins} if pins else {}   # assumption, see module doc
        server = authproxy.serve(vault, host="127.0.0.1", port=0,
                                 on_event=lambda k, f: self.events.append((k, f)),
                                 **kwargs)
        self.servers.append(server)
        self.secret = authproxy.Handler.secret
        return f"http://127.0.0.1:{server.server_address[1]}"

    def mint(self, agent: str, provider: str | None = None, secret=None) -> str:
        secret = secret or self.secret
        if provider is None:
            return authproxy.mint_token(agent, secret)
        return authproxy.mint_token(agent, secret, provider=provider)  # assumption

    def close(self):
        for s in self.servers:
            s.shutdown()
            s.server_close()
        for h in self.httpds:
            h.shutdown()
            h.server_close()

    def kinds(self, kind: str) -> list[dict]:
        return [f for k, f in self.events if k == kind]


@pytest.fixture
def rig(tmp_path, monkeypatch):
    r = Rig(tmp_path, monkeypatch)
    yield r
    r.close()


def post(base: str, bearer: str, path: str = "/v1/messages") -> tuple[int, str]:
    req = urllib.request.Request(base + path, data=b"{}",
                                 headers={"Authorization": "Bearer " + bearer})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


def served_by(body: str) -> str:
    try:
        return json.loads(body).get("served_by", "")
    except ValueError:
        return ""


# --------------------------------------------------------------------------
# DK-R1: every vault login is in the pool
# --------------------------------------------------------------------------

def test_dk_r1_fresh_top_level_login_is_served_when_accounts_exist(rig, tmp_path):
    vault = make_vault(tmp_path, top="tok-top-fresh", a="tok-a-expired")
    up = rig.upstream({"tok-a-expired": 401})
    base = rig.start_proxy(vault)
    for i in range(4):
        status, body = post(base, rig.mint(f"agent{i}"))
        assert status == 200, body
        assert served_by(body) == "tok-top-fresh"


def test_dk_r1_top_level_login_alone_is_served(rig, tmp_path):
    vault = make_vault(tmp_path, top="tok-top")
    rig.upstream()
    base = rig.start_proxy(vault)
    status, body = post(base, rig.mint("agent"))
    assert (status, served_by(body)) == (200, "tok-top")


def test_dk_r1_accounts_without_a_top_level_login_are_served(rig, tmp_path):
    vault = make_vault(tmp_path, a="tok-a")
    rig.upstream()
    base = rig.start_proxy(vault)
    status, body = post(base, rig.mint("agent"))
    assert (status, served_by(body)) == (200, "tok-a")


def test_dk_r1_every_account_and_the_default_share_the_load(rig, tmp_path):
    """The pool really contains all three: with enough fresh agents each one
    is used (agents spread to the least-loaded account)."""
    vault = make_vault(tmp_path, top="tok-top", a="tok-a", b="tok-b")
    up = rig.upstream()
    base = rig.start_proxy(vault)
    for i in range(9):
        assert post(base, rig.mint(f"agent{i}"))[0] == 200
    assert {"tok-top", "tok-a", "tok-b"} <= set(up.hits)


def test_dk_r1_a_login_refreshed_while_running_is_served(rig, tmp_path):
    """`multiagents docker login claude` rewrites the top-level file."""
    vault = make_vault(tmp_path, top="tok-top-old", a="tok-a-expired")
    up = rig.upstream({"tok-top-old": 401, "tok-a-expired": 401})
    base = rig.start_proxy(vault)
    assert post(base, rig.mint("agent"))[0] == 401
    write_login(vault / ".credentials.json", "tok-top-new")
    status, body = post(base, rig.mint("agent"))
    assert (status, served_by(body)) == (200, "tok-top-new")


def test_dk_r1_an_unreadable_login_file_does_not_hide_the_others(rig, tmp_path):
    vault = make_vault(tmp_path, top="tok-top")
    bad = vault / "accounts" / "broken"
    bad.mkdir(parents=True)
    (bad / ".credentials.json").write_text("{not json")
    rig.upstream()
    base = rig.start_proxy(vault)
    status, body = post(base, rig.mint("agent"))
    assert (status, served_by(body)) == (200, "tok-top")


# --------------------------------------------------------------------------
# DK-R2: a 401 is logged, and fails over
# --------------------------------------------------------------------------

def test_dk_r2_a_401_fails_over_to_the_next_account_and_is_logged(rig, tmp_path):
    vault = make_vault(tmp_path, alpha="tok-alpha", beta="tok-beta")
    up = rig.upstream({"tok-alpha": 401})
    base = rig.start_proxy(vault)
    status, body = post(base, rig.mint("agent"))
    assert (status, served_by(body)) == (200, "tok-beta")
    events = rig.kinds("unauthenticated")
    assert len(events) == 1
    assert "alpha" in events[0].values()          # names the account label
    for _, fields in rig.events:                  # never the token
        assert "tok-" not in json.dumps(fields, default=str)


def test_dk_r2_a_401_is_marked_unusable_and_not_asked_again(rig, tmp_path):
    vault = make_vault(tmp_path, alpha="tok-alpha", beta="tok-beta")
    up = rig.upstream({"tok-alpha": 401})
    base = rig.start_proxy(vault)
    for i in range(5):
        status, body = post(base, rig.mint(f"agent{i}"))
        assert (status, served_by(body)) == (200, "tok-beta")
    assert up.count("tok-alpha") == 1


def test_dk_r2_401_reaches_the_agent_only_when_no_account_is_left(rig, tmp_path):
    vault = make_vault(tmp_path, top="tok-top", alpha="tok-alpha")
    up = rig.upstream({"tok-top": 401, "tok-alpha": 401})
    base = rig.start_proxy(vault)
    status, _ = post(base, rig.mint("agent"))
    assert status == 401
    assert len(rig.kinds("unauthenticated")) == 2


def test_dk_r2_a_changed_credential_makes_the_account_usable_again(rig, tmp_path):
    vault = make_vault(tmp_path, alpha="tok-alpha")
    up = rig.upstream({"tok-alpha": 401})
    base = rig.start_proxy(vault)
    assert post(base, rig.mint("agent"))[0] == 401
    write_login(vault / "accounts" / "alpha" / ".credentials.json", "tok-alpha-2")
    status, body = post(base, rig.mint("agent"))
    assert (status, served_by(body)) == (200, "tok-alpha-2")


def test_dk_r2_an_unchanged_credential_stays_unusable(rig, tmp_path):
    vault = make_vault(tmp_path, alpha="tok-alpha")
    up = rig.upstream({"tok-alpha": 401})
    base = rig.start_proxy(vault)
    assert post(base, rig.mint("agent"))[0] == 401
    assert up.count("tok-alpha") == 1
    assert post(base, rig.mint("agent"))[0] == 401
    assert up.count("tok-alpha") == 1


def test_dk_r2a_change_is_detected_by_content_not_mtime(rig, tmp_path):
    vault = make_vault(tmp_path, alpha="tok-alpha")
    up = rig.upstream({"tok-alpha": 401})
    base = rig.start_proxy(vault)
    assert post(base, rig.mint("agent"))[0] == 401
    path = vault / "accounts" / "alpha" / ".credentials.json"
    same = path.read_bytes()
    path.write_bytes(same)                        # new mtime, same bytes
    os.utime(path, (4000000000, 4000000000))
    assert post(base, rig.mint("agent"))[0] == 401
    assert up.count("tok-alpha") == 1


# --------------------------------------------------------------------------
# DK-R2a: bounded failover
# --------------------------------------------------------------------------

def test_dk_r2a_at_most_one_attempt_per_eligible_account(rig, tmp_path):
    vault = make_vault(tmp_path, top="tok-top", alpha="tok-alpha", beta="tok-beta")
    up = rig.upstream({"tok-top": 401, "tok-alpha": 401, "tok-beta": 401})
    base = rig.start_proxy(vault)
    assert post(base, rig.mint("agent"))[0] == 401
    for tok in ("tok-top", "tok-alpha", "tok-beta"):
        assert up.count(tok) == 1, up.hits


def test_dk_r2a_no_retry_once_a_response_byte_has_been_sent(rig, tmp_path):
    """Account alpha starts a 200 and then dies mid-body. The agent already
    holds a status line, so the proxy must not go and ask beta."""
    hits: list[str] = []

    class Truncating(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            n = int(self.headers.get("content-length") or 0)
            self.rfile.read(n)
            token = self.headers.get("authorization", "").replace("Bearer ", "")
            hits.append(token)
            self.send_response(200)
            self.send_header("content-length", "1000")
            self.end_headers()
            self.wfile.write(b"partial")
            self.wfile.flush()
            self.close_connection = True

        do_GET = do_POST

    vault = make_vault(tmp_path, alpha="tok-alpha", beta="tok-beta")
    rig.upstream(handler=Truncating)
    base = rig.start_proxy(vault)
    try:
        post(base, rig.mint("agent"))
    except Exception:
        pass                                      # a truncated body may raise
    assert hits == ["tok-alpha"]


def test_dk_r2a_a_rate_limit_still_fails_over_for_the_pool(rig, tmp_path):
    vault = make_vault(tmp_path, alpha="tok-alpha", beta="tok-beta")
    up = rig.upstream({"tok-alpha": 429})
    base = rig.start_proxy(vault)
    status, body = post(base, rig.mint("agent"))
    assert (status, served_by(body)) == (200, "tok-beta")
    assert post(base, rig.mint("other"))[0] == 200
    assert up.count("tok-alpha") == 1


def test_dk_r2a_a_529_also_fails_over_for_the_pool(rig, tmp_path):
    vault = make_vault(tmp_path, alpha="tok-alpha", beta="tok-beta")
    rig.upstream({"tok-alpha": 529})
    base = rig.start_proxy(vault)
    status, body = post(base, rig.mint("agent"))
    assert (status, served_by(body)) == (200, "tok-beta")


def test_dk_r2a_a_401_on_the_last_account_after_a_429_is_the_final_answer(rig, tmp_path):
    vault = make_vault(tmp_path, alpha="tok-alpha", beta="tok-beta")
    up = rig.upstream({"tok-alpha": 429, "tok-beta": 401})
    base = rig.start_proxy(vault)
    assert post(base, rig.mint("agent"))[0] == 401
    assert up.count("tok-alpha") == 1 and up.count("tok-beta") == 1


# --------------------------------------------------------------------------
# DK-R4 / R4a: pins (see module docstring for the assumed surface)
# --------------------------------------------------------------------------

PIN = {"claude-b": "bravo"}


def pinned_vault(tmp_path):
    return make_vault(tmp_path, top="tok-top", alpha="tok-alpha", bravo="tok-bravo")


def test_dk_r4_a_pinned_provider_is_served_only_with_its_account(rig, tmp_path):
    up = rig.upstream()
    base = rig.start_proxy(pinned_vault(tmp_path), pins=PIN)
    for i in range(6):
        status, body = post(base, rig.mint(f"pinned{i}", provider="claude-b"))
        assert (status, served_by(body)) == (200, "tok-bravo")
    assert set(up.hits) == {"tok-bravo"}


def test_dk_r4_a_pinned_provider_never_fails_over_on_a_401(rig, tmp_path):
    up = rig.upstream({"tok-bravo": 401})
    base = rig.start_proxy(pinned_vault(tmp_path), pins=PIN)
    status, body = post(base, rig.mint("agent", provider="claude-b"))
    assert status == 401
    assert set(up.hits) == {"tok-bravo"}
    events = rig.kinds("unauthenticated")
    assert len(events) == 1 and "bravo" in events[0].values()


def test_dk_r2a_a_pinned_provider_is_not_unpinned_by_a_429(rig, tmp_path):
    up = rig.upstream({"tok-bravo": 429})
    base = rig.start_proxy(pinned_vault(tmp_path), pins=PIN)
    status, body = post(base, rig.mint("agent", provider="claude-b"))
    assert status == 429
    assert "bravo" in body                         # the error says which account
    status, _ = post(base, rig.mint("agent2", provider="claude-b"))
    assert status == 429
    assert set(up.hits) <= {"tok-bravo"}


def test_dk_r2a_a_pinned_provider_is_not_unpinned_by_a_529(rig, tmp_path):
    up = rig.upstream({"tok-bravo": 529})
    base = rig.start_proxy(pinned_vault(tmp_path), pins=PIN)
    status, _ = post(base, rig.mint("agent", provider="claude-b"))
    assert status in (429, 529)
    assert set(up.hits) <= {"tok-bravo"}


def test_dk_r4_the_error_for_an_unusable_pinned_account_names_it(rig, tmp_path):
    rig.upstream({"tok-bravo": 401})
    base = rig.start_proxy(pinned_vault(tmp_path), pins=PIN)
    post(base, rig.mint("agent", provider="claude-b"))
    status, body = post(base, rig.mint("agent", provider="claude-b"))
    assert status == 401
    assert "bravo" in body


def test_dk_r4_a_pinned_agent_with_a_missing_account_is_refused_not_pooled(rig, tmp_path):
    up = rig.upstream()
    base = rig.start_proxy(pinned_vault(tmp_path), pins={"claude-b": "nosuch"})
    status, _ = post(base, rig.mint("agent", provider="claude-b"))
    assert status >= 400
    assert up.hits == []


def test_dk_r4_unpinned_agents_never_get_a_pinned_account(rig, tmp_path):
    up = rig.upstream()
    base = rig.start_proxy(pinned_vault(tmp_path), pins=PIN)
    for i in range(12):
        status, body = post(base, rig.mint(f"base{i}"))
        assert status == 200
        assert served_by(body) in ("tok-top", "tok-alpha")
    assert "tok-bravo" not in up.hits


def test_dk_r4_unpinned_agents_do_not_fall_back_to_a_pinned_account(rig, tmp_path):
    up = rig.upstream({"tok-top": 401, "tok-alpha": 401})
    base = rig.start_proxy(pinned_vault(tmp_path), pins=PIN)
    status, _ = post(base, rig.mint("base"))
    assert status == 401
    assert "tok-bravo" not in up.hits


def test_dk_r4_the_pin_is_derived_from_config_at_each_start(rig, tmp_path):
    vault = pinned_vault(tmp_path)
    up = rig.upstream()
    base = rig.start_proxy(vault, pins=PIN)
    assert served_by(post(base, rig.mint("a1", provider="claude-b"))[1]) == "tok-bravo"
    rig.servers[-1].shutdown()
    rig.servers[-1].server_close()
    base = rig.start_proxy(vault, pins=PIN)       # restart, same config
    for i in range(4):
        assert served_by(post(base, rig.mint(f"a{i}", provider="claude-b"))[1]) == "tok-bravo"
    assert "tok-alpha" not in up.hits and "tok-top" not in up.hits


def test_dk_r4_a_pinned_account_picks_up_a_refreshed_login(rig, tmp_path):
    vault = pinned_vault(tmp_path)
    rig.upstream({"tok-bravo": 401})
    base = rig.start_proxy(vault, pins=PIN)
    assert post(base, rig.mint("agent", provider="claude-b"))[0] == 401
    write_login(vault / "accounts" / "bravo" / ".credentials.json", "tok-bravo-2")
    status, body = post(base, rig.mint("agent", provider="claude-b"))
    assert (status, served_by(body)) == (200, "tok-bravo-2")


def test_dk_r4a_a_claim_signed_with_another_secret_is_not_trusted(rig, tmp_path):
    up = rig.upstream()
    base = rig.start_proxy(pinned_vault(tmp_path), pins=PIN)
    forged = rig.mint("agent", provider="claude-b", secret="not-the-secret")
    status, _ = post(base, forged)
    assert status == 401
    assert up.hits == []


def test_dk_r4a_the_provider_claim_cannot_be_rewritten_after_signing(rig, tmp_path):
    """A token minted for an unpinned provider, edited to name the pinned one
    (or the reverse), fails verification instead of changing routing."""
    up = rig.upstream()
    base = rig.start_proxy(pinned_vault(tmp_path), pins=PIN)
    plain = rig.mint("agent", provider="claude")
    swapped = plain.replace("claude", "claude-b", 1)
    assert swapped != plain
    status, _ = post(base, swapped)
    assert status == 401
    assert up.hits == []


# --------------------------------------------------------------------------
# DK-R4b: account namespace
# --------------------------------------------------------------------------

def test_dk_r4b_an_accounts_default_directory_is_a_load_time_error(rig, tmp_path):
    vault = make_vault(tmp_path, top="tok-top", default="tok-shadow")
    rig.upstream()
    with pytest.raises(Exception) as exc:
        rig.start_proxy(vault)
    assert "rename" in str(exc.value).lower()
    assert "default" in str(exc.value)


@pytest.mark.parametrize("label", ["Upper", "has space", "dot.ted", "ünï"])
def test_dk_r4b_an_invalid_label_is_rejected_or_never_served(rig, tmp_path, label):
    vault = make_vault(tmp_path, top="tok-top")
    write_login(vault / "accounts" / label / ".credentials.json", "tok-bad")
    up = rig.upstream()
    try:
        base = rig.start_proxy(vault)
    except Exception:
        return                                     # refused at load: acceptable
    for i in range(6):
        post(base, rig.mint(f"agent{i}"))
    assert "tok-bad" not in up.hits


@pytest.mark.parametrize("label", ["a", "b2", "my-acct", "my_acct", "0"])
def test_dk_r4b_valid_labels_are_served(rig, tmp_path, label):
    vault = make_vault(tmp_path, **{label: "tok-ok"})
    rig.upstream()
    base = rig.start_proxy(vault)
    status, body = post(base, rig.mint("agent"))
    assert (status, served_by(body)) == (200, "tok-ok")


# --------------------------------------------------------------------------
# DK-R4 generic (P0-R8)
# --------------------------------------------------------------------------

def test_dk_r4_no_provider_name_is_hardcoded_in_the_python_sources():
    offenders = []
    for path in SRC.rglob("*.py"):
        for n, line in enumerate(path.read_text().splitlines(), 1):
            if re.search(r"""["']claude-b["']""", line):
                offenders.append(f"{path.relative_to(SRC)}:{n}")
    assert offenders == []


# --------------------------------------------------------------------------
# DK-R6: no regression (these pass today and must keep passing)
# --------------------------------------------------------------------------

def test_dk_r6_a_single_account_vault_forwards_with_its_token(rig, tmp_path):
    rig.upstream()
    base = rig.start_proxy(make_vault(tmp_path, only="tok-only"))
    assert served_by(post(base, rig.mint("agent"))[1]) == "tok-only"


def test_dk_r6_an_unsigned_token_is_refused_and_forwards_nothing(rig, tmp_path):
    up = rig.upstream()
    base = rig.start_proxy(make_vault(tmp_path, only="tok-only"))
    assert post(base, "mxa_agent_" + "0" * 32)[0] == 401
    assert post(base, "plain-string")[0] == 401
    assert up.hits == []


def test_dk_r6_a_project_slug_with_underscores_is_still_served(rig, tmp_path):
    rig.upstream()
    base = rig.start_proxy(make_vault(tmp_path, only="tok-only"))
    assert post(base, rig.mint("my_project_slug"))[0] == 200


def test_dk_r6_every_account_rate_limited_answers_429(rig, tmp_path):
    rig.upstream({"tok-a": 429, "tok-b": 429})
    base = rig.start_proxy(make_vault(tmp_path, a="tok-a", b="tok-b"))
    assert post(base, rig.mint("agent"))[0] == 429


def test_dk_r6_an_ordinary_upstream_error_is_relayed(rig, tmp_path):
    rig.upstream({"tok-only": 400})
    base = rig.start_proxy(make_vault(tmp_path, only="tok-only"))
    assert post(base, rig.mint("agent"))[0] == 400
