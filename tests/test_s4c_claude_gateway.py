"""Slice 4C, D-S4C.2 / IC-C33: Claude in isolated mode goes through the 4B.2
auth gateway only.

Two layers:

* the gateway's HOST ADAPTER (assumed seam S7, `gateways.claude_adapter`), driven
  directly with a fake vault, a synthetic upstream and a fake OAuth refresh: it
  selects the pinned account, reads the vault token host-side, refreshes it
  host-side under a lock keyed on `(provider, account)`, and never lets the
  guest choose the account, the URL or the credential;
* the scheduler on the fake Docker CLI: no Anthropic token reaches the guest's
  env, HOME or any mount, and the legacy claude projection is never used.

The live check against the real Anthropic API needs the user's go-ahead and is
not written here. Fictional values only.
"""
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import ic_s4b as nb  # noqa: E402
import ic_s4b2_backend as be  # noqa: E402
import ic_s4c as c4  # noqa: E402

GUEST_BEARER = "ma-auth-v1." + "d" * 64
GATE = 4                  # a bounded wait for one thread to reach another
NEW_ACCESS, NEW_REFRESH = "FICTION-NEW-ACCESS-0b1e", "FICTION-NEW-REFRESH-6c4f"


# ------------------------------------------------------------------ the adapter world

class Upstream:
    """The synthetic transport behind the adapter's fixed upstream."""

    def __init__(self):
        self.calls = []
        self.lock = threading.Lock()

    def __call__(self, method, url, headers, body):
        with self.lock:
            self.calls.append({"method": method, "url": url, "headers": {k.lower(): v for k, v in dict(headers).items()},
                               "body": body})
        return be.Response()

    def seen(self) -> str:
        return repr(self.calls)


class Refresh:
    """The fake OAuth refresh call: records the refresh tokens it was given."""

    def __init__(self, result=None, hold: threading.Event | None = None, error: Exception | None = None):
        self.given = []
        self.running = 0
        self.max_running = 0
        self.entered = threading.Event()
        self.lock = threading.Lock()
        self.result, self.hold, self.error = result, hold, error

    def __call__(self, refresh_token):
        with self.lock:
            self.given.append(refresh_token)
            self.running += 1
            self.max_running = max(self.max_running, self.running)
        self.entered.set()
        try:
            if self.hold is not None:
                self.hold.wait(GATE)
            if self.error is not None:
                raise self.error
            return dict(self.result or {"accessToken": NEW_ACCESS, "refreshToken": NEW_REFRESH, "expiresAt": c4.FAR})
        finally:
            with self.lock:
                self.running -= 1


def bytes_of(path: Path) -> bytes:
    return path.read_bytes()


class World:
    def __init__(self, tmp_path, pins=None, expired=(), refresh=None):
        self.vault = c4.write_claude_vault(tmp_path / "vault", expired=expired)
        self.pins = pins if pins is not None else {c4.PROVIDER: "work"}
        self.upstream = Upstream()
        self.refresh = refresh or Refresh()
        self.adapters = []

    def adapter(self):
        build = c4.claude_adapter()
        ad = build(self.vault, pins=dict(self.pins), upstream=self.upstream, refresh=self.refresh)
        self.adapters.append(ad)
        return ad

    def open(self, ad=None, *, path="/v1/messages", headers=None, body=b'{"m":1}', provider=c4.PROVIDER,
             run_id="run_0001"):
        ad = ad or self.adapter()
        merged = {"authorization": "Bearer " + GUEST_BEARER, "content-type": "application/json"}
        merged.update(headers or {})
        response = ad.open("POST", path, merged, body, provider=provider, run_id=run_id)
        try:
            return response
        finally:
            close = getattr(response, "close", None)
            if close:
                close()

    def files(self, exclude=()):
        return {p: p.read_bytes() for p in self.vault.rglob("*") if p.is_file() and p not in exclude}


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def bearer_sent(call) -> str:
    return call["headers"].get("authorization", "")


# ------------------------------------------------------------ selecting the pinned account

@pytest.mark.parametrize("label", ["default", "work", "spare"])
def test_c33_the_adapter_sends_the_pinned_accounts_access_token(tmp_path, label):
    w = World(tmp_path, pins={c4.PROVIDER: label})
    w.open()
    (call,) = w.upstream.calls
    assert bearer_sent(call) == "Bearer " + c4.ACCESS[label]
    for other, token in c4.ACCESS.items():
        if other != label:
            assert token not in w.upstream.seen(), f"account {other}'s token reached the upstream"


def test_c33_two_providers_pinned_to_two_accounts_never_cross(tmp_path):
    w = World(tmp_path, pins={"acme": "work", "other": "spare"})
    ad = w.adapter()
    w.open(ad, provider="acme")
    w.open(ad, provider="other")
    first, second = w.upstream.calls
    assert bearer_sent(first) == "Bearer " + c4.ACCESS["work"]
    assert bearer_sent(second) == "Bearer " + c4.ACCESS["spare"]


@pytest.mark.parametrize("headers,path", [
    ({"x-account": "spare"}, "/v1/messages"),
    ({"x-multiagents-account": "spare", "anthropic-account": "spare"}, "/v1/messages"),
    ({}, "/v1/messages?account=spare&label=spare"),
    ({"authorization": "Bearer " + c4.ACCESS["spare"]}, "/v1/messages"),
    ({"x-api-key": c4.ACCESS["spare"]}, "/v1/messages"),
])
def test_c33_nothing_in_the_request_selects_the_account(world, headers, path):
    world.open(path=path, headers=headers)
    (call,) = world.upstream.calls
    assert bearer_sent(call) == "Bearer " + c4.ACCESS["work"]
    assert c4.ACCESS["spare"] not in world.upstream.seen()


def test_c33_the_guests_own_credentials_are_never_forwarded(world):
    world.open(headers={"x-api-key": "FICTION-GUEST-KEY", "proxy-authorization": "Basic ZmljdGlvbjpmaWN0aW9u",
                        "cookie": "session=FICTION-COOKIE"})
    (call,) = world.upstream.calls
    assert GUEST_BEARER not in world.upstream.seen()
    assert "x-api-key" not in call["headers"] and "proxy-authorization" not in call["headers"]
    assert bearer_sent(call) == "Bearer " + c4.ACCESS["work"]


@pytest.mark.parametrize("path", ["/v1/messages", "//evil.test/v1/messages", "/\\evil.test/x", "/@evil.test/x",
                                  "http://evil.test/v1/messages", "/../../v1/messages"])
def test_c33_the_upstream_is_always_the_fixed_anthropic_origin(world, path):
    ad = world.adapter()
    try:
        world.open(ad, path=path)
    except Exception:  # noqa: BLE001 - refusing a hostile target is allowed; reaching another host is not
        pass
    for call in world.upstream.calls:
        parts = urlsplit(call["url"])
        assert (parts.scheme, parts.hostname) == ("https", "api.anthropic.com"), call["url"]


def test_c33_a_missing_pinned_account_fails_closed_without_using_another_account(tmp_path):
    w = World(tmp_path, pins={c4.PROVIDER: "ghost"})
    ad = w.adapter()
    try:
        response = w.open(ad)
    except Exception:  # noqa: BLE001
        response = None
    if response is not None:
        assert response.status >= 400
    assert w.upstream.calls == [], "no upstream request may be made without the pinned account"
    assert w.refresh.given == []


# ------------------------------------------------------------ host-side refresh

def test_c33_a_fresh_token_is_used_without_any_refresh(world):
    world.open()
    assert world.refresh.given == []


def test_c33_an_expired_token_is_refreshed_host_side_with_the_pinned_accounts_refresh_token(tmp_path):
    w = World(tmp_path, expired=("work",))
    before = {p: b for p, b in w.files().items() if "work" not in str(p)}
    w.open()
    assert w.refresh.given == [c4.REFRESH["work"]]
    (call,) = w.upstream.calls
    assert bearer_sent(call) == "Bearer " + NEW_ACCESS, "the request must carry the refreshed token"
    after = {p: b for p, b in w.files().items() if "work" not in str(p)}
    assert after == before, "another account's vault entry changed"


def test_c33_the_refreshed_credential_is_written_back_to_the_vault(tmp_path):
    w = World(tmp_path, expired=("work",))
    w.open()
    entry = c4.claude_entry(w.vault, "work")
    assert (entry["accessToken"], entry["refreshToken"]) == (NEW_ACCESS, NEW_REFRESH)
    assert entry["expiresAt"] == c4.FAR


def test_c33_a_rotated_refresh_token_is_the_one_used_next_time(tmp_path):
    w = World(tmp_path, expired=("work",))
    w.open()
    entry = c4.claude_entry(w.vault, "work")
    entry["expiresAt"] = c4.PAST                              # the access token expires again
    (w.vault / "accounts" / "work" / ".credentials.json").write_text(__import__("json").dumps({"claudeAiOauth": entry}))
    w.open()
    assert w.refresh.given == [c4.REFRESH["work"], NEW_REFRESH]


def test_c33_no_refresh_token_ever_reaches_the_upstream(tmp_path):
    w = World(tmp_path, expired=("work",))
    w.open()
    w.open(run_id="run_0002")
    seen = w.upstream.seen()
    for token in list(c4.REFRESH.values()) + [NEW_REFRESH]:
        assert token not in seen


def test_c33_a_token_replaced_in_the_vault_is_picked_up_by_the_next_request(tmp_path):
    w = World(tmp_path)
    ad = w.adapter()
    w.open(ad)
    path = w.vault / "accounts" / "work" / ".credentials.json"
    path.write_text(__import__("json").dumps({"claudeAiOauth": {
        "accessToken": "FICTION-RELOGIN-ACCESS", "refreshToken": "FICTION-RELOGIN-REFRESH", "expiresAt": c4.FAR}}))
    w.open(ad)
    assert [bearer_sent(c) for c in w.upstream.calls] == ["Bearer " + c4.ACCESS["work"], "Bearer FICTION-RELOGIN-ACCESS"]


def test_c33_a_failed_refresh_sends_nothing_upstream_and_leaves_the_vault_alone(tmp_path):
    boom = Refresh(error=RuntimeError("refresh endpoint down"))
    w = World(tmp_path, expired=("work",), refresh=boom)
    before = w.files()
    ad = w.adapter()
    try:
        response = w.open(ad)
    except Exception as exc:  # noqa: BLE001
        text = repr(exc)
    else:
        assert response.status >= 400
        text = ""
    assert w.upstream.calls == [], "an expired token must never be sent upstream"
    assert w.files() == before
    for token in c4.secrets_of_claude():
        assert token not in text
    # not wedged: once the endpoint recovers the next request refreshes and succeeds
    boom.error = None
    w.open(ad)
    assert bearer_sent(w.upstream.calls[-1]) == "Bearer " + NEW_ACCESS


# ------------------------------------------------------------ the (provider, account) lock

def run_in_threads(fns):
    errors = []

    def wrap(fn):
        def go():
            try:
                fn()
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)
        return go
    threads = [threading.Thread(target=wrap(fn)) for fn in fns]
    for t in threads:
        t.start()
    for t in threads:
        t.join(GATE * 3)
    assert not any(t.is_alive() for t in threads), "a request is stuck"
    return errors


def test_c33_concurrent_requests_on_one_expired_account_refresh_it_once(tmp_path):
    hold = threading.Event()
    w = World(tmp_path, expired=("work",), refresh=Refresh(hold=hold))
    ad = w.adapter()
    threading.Timer(0.3, hold.set).start()
    errors = run_in_threads([lambda i=i: w.open(ad, run_id=f"run_{i}") for i in range(6)])
    assert errors == []
    assert w.refresh.given == [c4.REFRESH["work"]]
    assert len(w.upstream.calls) == 6
    assert {bearer_sent(c) for c in w.upstream.calls} == {"Bearer " + NEW_ACCESS}


def test_c33_two_attempts_on_one_account_never_refresh_concurrently(tmp_path):
    """Two adapters (two attempts) over one vault: one refresh, never overlapping."""
    hold = threading.Event()
    w = World(tmp_path, expired=("work",), refresh=Refresh(hold=hold))
    a, b = w.adapter(), w.adapter()
    threading.Timer(0.3, hold.set).start()
    errors = run_in_threads([lambda: w.open(a, run_id="run_a"), lambda: w.open(b, run_id="run_b")])
    assert errors == []
    assert w.refresh.max_running == 1
    assert w.refresh.given == [c4.REFRESH["work"]], "the second attempt must see the first one's refreshed token"
    assert {bearer_sent(c) for c in w.upstream.calls} == {"Bearer " + NEW_ACCESS}


def test_c33_different_accounts_refresh_independently(tmp_path):
    """The lock is keyed on exactly (provider, account): a slow refresh of one account never blocks another's."""
    spare_in = threading.Event()
    release = threading.Event()

    def refresh(refresh_token):
        if refresh_token == c4.REFRESH["work"]:
            assert spare_in.wait(GATE), "the other account's refresh was blocked behind this one"
        else:
            spare_in.set()
        return {"accessToken": "FICTION-NEW-" + refresh_token[-4:], "refreshToken": "FICTION-ROT-" + refresh_token[-4:],
                "expiresAt": c4.FAR}
    w = World(tmp_path, pins={"acme": "work", "other": "spare"}, expired=("work", "spare"))
    w.refresh = refresh
    ad = w.adapter()
    errors = run_in_threads([lambda: w.open(ad, provider="acme", run_id="r1"),
                             lambda: w.open(ad, provider="other", run_id="r2")])
    assert errors == []
    assert len(w.upstream.calls) == 2
    release.set()


# ------------------------------------------------------------ the scheduler: nothing reaches the guest

@pytest.fixture
def gw(tmp_path, monkeypatch):
    made = []

    def build(**kw):
        s = c4.CredSched(tmp_path, monkeypatch, mode="gateway", **kw)
        made.append(s)
        return s
    yield build
    for s in made:
        s.close()


@pytest.mark.parametrize("expired", [False, True])
def test_c33_no_anthropic_token_reaches_the_guest_env_home_or_any_mount(gw, expired):
    s = gw(expired=expired)
    s.start()
    node = s.launch()
    container = s.running(node)
    env = s.invocation(container)["env"]
    assert env.get("ANTHROPIC_BASE_URL") == "http://ma-auth:8930"
    assert env["ANTHROPIC_AUTH_TOKEN"].startswith("ma-auth-v1."), "the guest holds only its ma-auth-v1 bearer"
    assert "ANTHROPIC_API_KEY" not in env
    secrets = c4.secrets_of_claude()
    assert c4.leaks_of({"env": repr(env)}, secrets) == {}
    assert c4.leaks_of(s.guest_files(container, with_input=True), secrets) == {}, "a vault value is in a mount"
    assert c4.leaks_of(s.public_texts(node), secrets) == {}


def test_c33_the_legacy_claude_projection_is_never_used_in_isolated_mode(gw):
    s = gw()
    s.start()
    container = s.running(s.launch())
    files = s.guest_files(container, with_input=True)
    assert [p for p in files if Path(p).name == ".credentials.json"] == []
    assert not any("mxa" in text and "_" in text and "ma-auth" not in text for text in files.values()), \
        "a legacy proxy claim is in a mount"
    vault_root = str(s.ma_state / "container-state")
    assert [m for m in s.mounts(container) if vault_root in m["source"]] == [], \
        "no mount may be backed by the host's credential state"


def test_c33_the_host_never_launches_the_cli_with_the_hosts_claude_credentials(gw):
    s = gw()
    s.start()
    s.running(s.launch())
    assert s.fake.spawns() == 0, "the CLI must run in the container only"
