"""C19: a quota details page with a masked account identity.

Contract: context/specs/c19-quota-details-page.md, QD-R1..R7 with the revision
section (QD-R1a, R3a, R4a, R7) overriding the earlier wording. Test names carry
the requirement id.

Black box at three seams: the monitor HTTP server (a real ThreadingHTTPServer on
an ephemeral port, driven with http.client), the shipped provider scripts run as
subprocesses against a temporary HOME of fixture profile files, and the C18
panel model. Nothing reads real credentials and nothing starts docker.

ASSUMPTIONS the contract leaves open (each is a NEED_INFO in the run report; the
implementer may not change this file, so a wrong guess comes back to the tester):

* Routes. The details page is `POST /quota` (form body ``token=<token>``,
  urlencoded). Its data is `GET /api/quota` (header token) and returns
  ``{"providers": [row, ...]}`` where each row is the C18 `providers_view` row
  (``name``, ``lines``, ...) plus identity entries. The reveal is
  `POST /api/quota/identity` with a JSON body ``{"provider": ..., "account":
  ...}`` and answers JSON ``{"identity": <clear string or null/"unknown">}``.
* Identity entries. Anywhere inside a provider row, a dict that has the key
  ``identity_available`` is one identity entry. It also carries ``account`` (the
  label to reveal with; null for a provider with one account) and, when it
  carries ``identity`` at all, that is exactly ``*****``.
* "Unknown" in a reveal response is ``identity`` of null, "" or "unknown".
* The account an identity is for reaches the identity action through
  ``extra_env`` (any variable) or the provider's ``container_account``; the
  fake action below looks at both.
* The identity action is run through ``scripts.run_action(..., "identity", ...)``,
  looked up at call time (patched like the C18 tests patch it).
* Caches are in-process and cannot be reset from outside, so every test uses
  provider names and account labels of its own.
"""

from __future__ import annotations

import base64
import http.client
import itertools
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from multiagents.budget import Budget
from multiagents.config import Config
from multiagents.monitor import server, snapshot as snap
from multiagents.paths import ProjectPaths, shipped_defaults_dir
from multiagents.tree import Tree

pytestmark = pytest.mark.real_providers   # providers_view itself is under test

PROVIDERS_DIR = shipped_defaults_dir() / "providers"
MASK = "*****"
TOKEN_SECRET = "sk-ant-<redacted>"
REFRESH_SECRET = "rt-C19REFRESHSECRET-1a2b3c4d5e6f"
_ids = itertools.count()


def uniq(prefix: str) -> str:
    return f"{prefix}-c19-{next(_ids)}"


# --------------------------------------------------------------------------
# a fake identity action, and the panel fixture around it


class Actions:
    """Replaces scripts.run_action. `identities` maps (provider, account) to a
    clear string; `behave` maps a provider to a callable(account) ->
    (code, stdout, stderr) for the failure cases. `usage` maps a provider to its
    extras text."""

    def __init__(self, labels=()):
        self.labels = set(labels)
        self.identities: dict = {}
        self.behave: dict = {}
        self.usage: dict = {}
        self.delay = 0.0
        self.calls: list = []
        self.lock = threading.Lock()

    def account_of(self, provider, extra_env):
        seen = [*(extra_env or {}).values(),
                getattr(provider, "container_account", None)]
        for value in seen:
            if value in self.labels:
                return value
        return None

    def __call__(self, name, provider, executor, action, config_dir,
                 project_config=None, timeout=20, extra_env=None, cwd=None):
        if action == "usage":
            text = self.usage.get(name)
            return (0, text, "") if text else (64, "", "")
        if action != "identity":
            return 64, "", ""
        account = self.account_of(provider, extra_env)
        with self.lock:
            self.calls.append((name, account))
        if self.delay:
            time.sleep(self.delay)
        if name in self.behave:
            return self.behave[name](account)
        value = self.identities.get((name, account))
        if value is None:
            return 64, "", ""
        return 0, json.dumps({"identity": value, "kind": "email"}) + "\n", ""

    def runs(self, name):
        return [c for c in self.calls if c[0] == name]


def reading(name, windows=None, *, known=True, note=""):
    return Budget(provider=name, known=known, headroom=0.5 if known else None,
                  note=note, severity="normal" if known else "unknown",
                  windows=windows or {})


def win(percent, account=None, **kw):
    w = {"percent": percent,
         "resets_at": "2099-01-01T00:00:00+00:00", **kw}
    if account:
        w["account"] = account
    return w


class Fixture:
    def __init__(self, tmp_path, monkeypatch, readings, actions,
                 pins=None, host_has=True):
        import multiagents.executor as executor_mod
        import multiagents.scripts as scripts_mod

        self.paths = ProjectPaths(tmp_path)
        self.paths.ensure()
        self.root = tmp_path
        self.actions = actions
        self.tree = Tree(self.paths.tree_file, self.paths.events_file)
        self.config = Config(project={"executor": {"kind": "local"}}, providers={},
                             agents={}, models={}, instruction_dirs=[])
        pins = pins or {}
        providers = {n: SimpleNamespace(
            billing="plan", name=n, container_account=pins.get(n, ""),
            available=(lambda n=n: "/usr/bin/fake" if (
                host_has.get(n, True) if isinstance(host_has, dict) else host_has)
                else None))
            for n in readings}
        monkeypatch.setattr(snap, "load_providers", lambda _: providers)
        monkeypatch.setattr(snap, "read_all", lambda *a, **k: dict(readings))
        monkeypatch.setattr(executor_mod, "executor_for",
                            lambda *a: (lambda name: SimpleNamespace(kind="local")))
        monkeypatch.setattr(scripts_mod, "run_action", actions)
        monkeypatch.setattr(server, "load_config", lambda _paths: self.config)

        self.token = "tok-" + base64.b32encode(os.urandom(10)).decode().lower()
        monkeypatch.setattr(server.Handler, "paths", self.paths)
        monkeypatch.setattr(server.Handler, "token", self.token)
        monkeypatch.setattr(server.Handler, "bound_host", "127.0.0.1")
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.port = self.httpd.server_port
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    # -- http -------------------------------------------------------------

    def request(self, method, path, body=None, headers=None, auth=True,
                host=None, origin=None):
        h = dict(headers or {})
        if auth:
            h.setdefault("X-Monitor-Token", self.token)
        if host:
            h["Host"] = host
        if origin:
            h["Origin"] = origin
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        try:
            conn.request(method, path, body=body, headers=h)
            resp = conn.getresponse()
            data = resp.read()
            return SimpleNamespace(status=resp.status, body=data,
                                   text=data.decode("utf-8", "replace"),
                                   headers={k.lower(): v for k, v in resp.getheaders()})
        finally:
            conn.close()

    def get(self, path, **kw):
        return self.request("GET", path, **kw)

    def quota(self, **kw):
        resp = self.get("/api/quota", **kw)
        assert resp.status == 200, (resp.status, resp.text[:300])
        return json.loads(resp.text)

    def rows(self):
        return {r["name"]: r for r in self.quota()["providers"]}

    def reveal(self, provider, account, **kw):
        body = json.dumps({"provider": provider, "account": account})
        h = {"Content-Type": "application/json", **kw.pop("headers", {})}
        return self.request("POST", "/api/quota/identity", body=body,
                            headers=h, **kw)

    def revealed(self, provider, account):
        resp = self.reveal(provider, account)
        assert resp.status == 200, (resp.status, resp.text[:300])
        value = json.loads(resp.text).get("identity")
        return None if value in (None, "", "unknown") else value

    def open_details(self, body=None, **kw):
        if body is None:
            body = urllib.parse.urlencode({"token": self.token})
        return self.request("POST", "/quota", body=body, auth=False,
                            headers={"Content-Type":
                                     "application/x-www-form-urlencoded"}, **kw)

    def wait(self, done, seconds=8.0):
        end = time.time() + seconds
        while True:
            rows = self.rows()
            if done(rows) or time.time() > end:
                return rows
            time.sleep(0.1)


@pytest.fixture
def make(tmp_path, monkeypatch):
    made = []

    def build(readings, actions=None, **kw):
        fx = Fixture(tmp_path, monkeypatch, readings, actions or Actions(), **kw)
        made.append(fx)
        return fx

    yield build
    for fx in made:
        fx.close()


def routes_exist(fx, provider, account=None):
    """Guard for refusal tests: a 404 from a route that is not there yet would
    satisfy "refused" for the wrong reason."""
    assert fx.open_details().status == 200
    assert fx.get("/api/quota").status == 200
    assert fx.reveal(provider, account).status == 200


def entries(row):
    """Every identity entry inside one provider row."""
    found = []

    def walk(node):
        if isinstance(node, dict):
            if "identity_available" in node:
                found.append(node)
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
    walk(row)
    return found


def accounts(row):
    return {e.get("account") for e in entries(row)}


def one_provider(make, *, identity="solo@example.invalid", name=None):
    name = name or uniq("codex")
    acts = Actions()
    acts.identities[(name, None)] = identity
    fx = make({name: reading(name, {"weekly": win(30)})}, acts)
    return fx, name, acts


def pooled(make):
    """claude pooled over accounts a/b, claude-b pinned to b."""
    n = next(_ids)
    a, b = f"a{n}", f"b{n}"
    claude, pinned = f"claude-c19-{n}", f"claude-b-c19-{n}"
    acts = Actions(labels={a, b})
    acts.identities = {(claude, a): f"alice{n}@example.test",
                       (claude, b): f"bob{n}@example.test",
                       (pinned, b): f"bob{n}@example.test"}
    readings = {
        claude: reading(claude, {f"{a}/five_hour": win(40, a),
                                 f"{a}/seven_day": win(10, a),
                                 f"{b}/five_hour": win(70, b, counted=False),
                                 f"{b}/seven_day": win(20, b, counted=False)}),
        pinned: reading(pinned, {"five_hour": win(70, b), "seven_day": win(20, b)}),
    }
    fx = make(readings, acts, pins={pinned: b})
    return SimpleNamespace(fx=fx, a=a, b=b, claude=claude, pinned=pinned,
                           alice=f"alice{n}@example.test", bob=f"bob{n}@example.test",
                           acts=acts)


# --------------------------------------------------------------------------
# QD-R1 / QD-R1a: the button, the handoff


def test_qd_r1_the_monitor_page_has_a_button_that_posts_a_form_into_a_new_tab(make):
    fx, *_ = one_provider(make)
    page = fx.get("/", auth=False)
    assert page.status == 200
    forms = re.findall(r"<form\b[^>]*>", page.text, re.I)
    ours = [f for f in forms if re.search(r'target\s*=\s*["\']_blank["\']', f, re.I)]
    assert ours, "no <form target=_blank> on the monitor page"
    assert any(re.search(r'method\s*=\s*["\']?post', f, re.I) for f in ours), ours


def test_qd_r1a_the_form_action_carries_no_token_and_the_token_goes_in_the_body(make):
    fx, *_ = one_provider(make)
    page = fx.get("/", auth=False).text
    form = [f for f in re.findall(r"<form\b[^>]*>", page, re.I)
            if re.search(r'target\s*=\s*["\']_blank["\']', f, re.I)][0]
    action = re.search(r'action\s*=\s*["\']([^"\']*)["\']', form, re.I)
    assert action, form
    assert "token" not in action.group(1).lower() and "?" not in action.group(1)
    assert fx.token not in action.group(1)
    # the token is posted as a form field, not hard-wired into the markup's URL
    assert re.search(r'<input\b[^>]*name\s*=\s*["\']token["\']', page, re.I)


def test_qd_r1_the_details_route_answers_with_the_token_in_the_body(make):
    fx, *_ = one_provider(make)
    resp = fx.open_details()
    assert resp.status == 200, resp.text[:200]
    assert resp.headers["content-type"].startswith("text/html")


@pytest.mark.parametrize("body", ["", "token=", "token=wrong", "tok=" ],
                         ids=["no-body", "empty-token", "wrong-token", "wrong-field"])
def test_qd_r1_the_details_route_refuses_without_a_valid_token(make, body):
    fx, *_ = one_provider(make)
    assert fx.open_details().status == 200
    resp = fx.open_details(body=body)
    assert resp.status == 403, resp.status
    assert fx.token not in resp.text


def test_qd_r1_the_details_route_refuses_a_foreign_host(make):
    fx, *_ = one_provider(make)
    assert fx.open_details().status == 200
    assert fx.open_details(host="local.evil.example").status == 403


def test_qd_r1a_the_details_page_destination_has_no_token_in_any_url(make):
    fx, *_ = one_provider(make)
    resp = fx.open_details()
    assert resp.status == 200
    assert fx.token not in resp.headers.get("location", "")
    assert resp.status not in (301, 302, 303, 307, 308), (
        "a redirect would put the destination in history; serve the page directly")
    # the page's own links, forms and fetch URLs never contain the token
    for url in re.findall(r'(?:href|src|action)\s*=\s*["\']([^"\']*)', resp.text, re.I):
        assert fx.token not in url and "token=" not in url, url


def test_qd_r1a_the_details_page_uses_the_header_and_not_the_query_for_later_requests(make):
    fx, *_ = one_provider(make)
    text = fx.open_details().text
    assert "X-Monitor-Token" in text
    assert not re.search(r"[?&]token=", text)


def test_qd_r1a_the_details_page_writes_nothing_to_browser_storage(make):
    fx, *_ = one_provider(make)
    resp = fx.open_details()
    assert resp.status == 200
    text = resp.text
    for api in ("localStorage", "sessionStorage", "indexedDB", "document.cookie",
                "caches."):
        assert api not in text, api


def test_qd_r1a_reloading_the_details_route_never_puts_a_token_in_a_url(make):
    fx, *_ = one_provider(make)
    resp = fx.get("/quota", auth=False)           # what a reload / address-bar visit does
    assert resp.status in (200, 401, 403, 405), resp.status
    if resp.status == 200:                        # a clear "reopen from the monitor"
        assert "monitor" in resp.text.lower() and fx.token not in resp.text
    assert fx.token not in resp.headers.get("location", "")
    leaked = fx.get(f"/quota?token={fx.token}", auth=False)
    assert fx.token not in leaked.text and "providers" not in leaked.text.lower() or \
        leaked.status != 200
    assert fx.token not in leaked.headers.get("location", "")


@pytest.mark.parametrize("how", ["details-page", "quota-data", "reveal", "refused"])
def test_qd_r1a_responses_carry_referrer_policy_no_referrer(make, how):
    fx, name, _ = one_provider(make)
    resp = {"details-page": lambda: fx.open_details(),
            "quota-data": lambda: fx.get("/api/quota"),
            "reveal": lambda: fx.reveal(name, None),
            "refused": lambda: fx.get("/api/quota", auth=False)}[how]()
    assert resp.headers.get("referrer-policy") == "no-referrer", resp.headers


# --------------------------------------------------------------------------
# QD-R2: every window and extra, on the C18 model


def test_qd_r2_every_window_and_extra_of_every_provider_is_in_the_page_data(make):
    n = next(_ids)
    claude, codex, agy, deep = (f"claude-c19-{n}", f"codex-c19-{n}",
                                f"agy-c19-{n}", f"opencode-deepinfra-c19-{n}")
    acts = Actions(labels={f"a{n}", f"b{n}"})
    acts.usage = {codex: f"credits 9963/10000 c19-{n}\nplan pro c19-{n}\n",
                  agy: f"project spend $1.25 c19-{n}\n"}
    readings = {
        claude: reading(claude, {f"a{n}/five_hour": win(40, f"a{n}"),
                                 f"b{n}/five_hour": win(70, f"b{n}", counted=False)},
                        note=f"vault account a{n}"),
        codex: reading(codex, {"primary": win(12), "secondary": win(88)},
                       note=f"codex note c19-{n}"),
        agy: reading(agy, {"gemini-pro": win(5), "gemini-flash": win(95)}),
        deep: reading(deep, None, known=False, note=f"metered per token c19-{n}"),
    }
    fx = make(readings, acts, host_has={agy: False})
    rows = fx.wait(lambda r: "c19-%d" % n in json.dumps(r.get(codex, {}))
                   and f"spend $1.25 c19-{n}" in json.dumps(r.get(agy, {})))
    assert set(readings) <= set(rows), sorted(rows)
    for name, r in readings.items():
        text = json.dumps(rows[name], default=str)
        for w in r.windows:
            assert w in text, (name, w)
    # the C18 model: the page's `lines` are exactly what the panel builds
    modelled = {row["name"]: row for row in snap.providers_view(
        fx.paths, fx.config, fx.tree, with_scripts=True)}
    for name in readings:
        assert rows[name]["lines"] == modelled[name]["lines"], name
    joined = json.dumps(rows, default=str)
    for extra in (f"credits 9963/10000 c19-{n}", f"plan pro c19-{n}",
                  f"project spend $1.25 c19-{n}"):
        assert extra in joined, extra
    assert f"metered per token c19-{n}" in json.dumps(rows[deep])
    assert "not installed" in json.dumps(rows[agy])
    assert "installed" in json.dumps(rows[codex])


def test_qd_r2_a_provider_with_no_reading_is_listed_and_says_so(make):
    name = uniq("opencode")
    fx = make({name: reading(name, None, known=False)}, Actions())
    row = fx.rows()[name]
    assert row["budget"]["known"] is False
    assert row["lines"], "a provider without a reading still says something"


def test_qd_r2_the_account_of_a_window_is_on_the_page(make):
    p = pooled(make)
    text = json.dumps(p.fx.rows()[p.claude])
    assert p.a in text and p.b in text


def test_qd_r2_the_data_route_needs_the_token_in_the_header(make):
    fx, *_ = one_provider(make)
    assert fx.get("/api/quota").status == 200
    assert fx.get("/api/quota", auth=False).status == 403
    assert fx.get("/api/quota", headers={"X-Monitor-Token": "wrong"}).status == 403
    assert fx.get("/api/quota").status == 200


# --------------------------------------------------------------------------
# QD-R3 / QD-R3a: masked by default, revealed per (provider, account)


def test_qd_r3_the_polled_data_carries_only_the_mask_and_an_availability_flag(make):
    fx, name, _ = one_provider(make, identity="solo.person@example.invalid")
    fx.revealed(name, None)                       # even after a reveal
    rows = fx.wait(lambda r: all(e["identity_available"] for e in entries(r[name])))
    es = entries(rows[name])
    assert es, rows[name]
    for e in es:
        assert e["identity_available"] is True
        assert e.get("identity", MASK) == MASK
    for path in ("/api/quota", "/api/state"):
        assert "solo.person@example.invalid" not in fx.get(path).text, path
    assert "solo.person" not in fx.get("/").text


def test_qd_r3_the_mask_does_not_depend_on_the_identity(make):
    n = next(_ids)
    short, long_ = f"a{n}@b.c", "x" * 40 + f"{n}@" + "y" * 40 + ".example.test"
    sn, ln = f"codex-c19-{n}", f"codex-b-c19-{n}"
    acts = Actions()
    acts.identities = {(sn, None): short, (ln, None): long_}
    fx = make({sn: reading(sn, {"w": win(1)}), ln: reading(ln, {"w": win(1)})}, acts)
    rows = fx.wait(lambda r: all(e["identity_available"]
                                 for n_ in (sn, ln) for e in entries(r[n_])))
    se = [e.get("identity", MASK) for e in entries(rows[sn])]
    le = [e.get("identity", MASK) for e in entries(rows[ln])]
    assert se == le == [MASK]


def test_qd_r3_identity_available_is_false_when_the_action_has_none(make):
    name = uniq("opencode")
    fx = make({name: reading(name, {"x": win(1)})}, Actions())   # exits 64
    time.sleep(0.5)
    es = entries(fx.wait(lambda r: False, seconds=1.0)[name])
    assert es and all(e["identity_available"] is False for e in es)
    assert all(e.get("identity", MASK) == MASK for e in es)


def test_qd_r3_the_reveal_returns_the_clear_value_with_the_token(make):
    fx, name, _ = one_provider(make, identity="carol@example.invalid")
    assert fx.revealed(name, None) == "carol@example.invalid"


def test_qd_r3_an_account_with_no_identity_reveals_unknown(make):
    name = uniq("opencode")
    fx = make({name: reading(name, {"x": win(1)})}, Actions())
    assert fx.revealed(name, None) is None


def test_qd_r3a_pooled_claude_has_one_identity_entry_per_account(make):
    p = pooled(make)
    row = p.fx.wait(lambda r: all(e["identity_available"] for e in entries(r[p.claude])))[p.claude]
    assert accounts(row) == {p.a, p.b}
    assert len(entries(row)) == 2


def test_qd_r3a_pinned_claude_b_exposes_only_its_account(make):
    p = pooled(make)
    row = p.fx.rows()[p.pinned]
    assert accounts(row) == {p.b}, entries(row)
    assert p.alice not in json.dumps(row)


def test_qd_r3a_each_account_reveals_its_own_identity(make):
    p = pooled(make)
    assert p.fx.revealed(p.claude, p.a) == p.alice
    assert p.fx.revealed(p.claude, p.b) == p.bob
    assert p.fx.revealed(p.pinned, p.b) == p.bob


def test_qd_r3a_a_pinned_provider_refuses_another_account(make):
    p = pooled(make)
    routes_exist(p.fx, p.pinned, p.b)
    resp = p.fx.reveal(p.pinned, p.a)
    assert 400 <= resp.status < 500, resp.status
    assert p.alice not in resp.text


@pytest.mark.parametrize("provider,account", [
    ("no-such-provider", None), ("no-such-provider", "b"), ("CLAUDE", "a"),
    ("", None), (None, None)])
def test_qd_r3a_the_pair_is_validated_against_the_configuration_provider(
        make, provider, account):
    p = pooled(make)
    routes_exist(p.fx, p.claude, p.a)
    resp = p.fx.reveal(provider, account)
    assert 400 <= resp.status < 500, resp.status
    assert p.alice not in resp.text and p.bob not in resp.text


@pytest.mark.parametrize("account", ["zz-no-such-account", "../b", "", "a/../b", 7,
                                     ["b"]])
def test_qd_r3a_an_account_that_is_not_configured_is_refused(make, account):
    p = pooled(make)
    routes_exist(p.fx, p.claude, p.a)
    resp = p.fx.reveal(p.claude, account)
    assert 400 <= resp.status < 500, resp.status
    assert p.alice not in resp.text and p.bob not in resp.text
    assert not [c for c in p.acts.calls if c[1] not in (p.a, p.b, None)]


@pytest.mark.parametrize("body", ["", "not json", "[]", "null", '{"provider": 1}',
                                  '{"account": "b"}'])
def test_qd_r3_a_malformed_reveal_request_is_a_400_not_a_500(make, body):
    p = pooled(make)
    routes_exist(p.fx, p.claude, p.a)
    resp = p.fx.request("POST", "/api/quota/identity", body=body,
                        headers={"Content-Type": "application/json"})
    assert resp.status in (400, 422), resp.status
    assert "Traceback" not in resp.text


def test_qd_r3_the_reveal_refuses_without_a_token(make):
    fx, name, _ = one_provider(make, identity="carol@example.invalid")
    routes_exist(fx, name)
    resp = fx.reveal(name, None, auth=False)
    assert resp.status == 403 and "carol" not in resp.text
    resp = fx.reveal(name, None, headers={"X-Monitor-Token": "wrong"}, auth=False)
    assert resp.status == 403 and "carol" not in resp.text


def test_qd_r3_the_reveal_state_is_not_kept_by_the_server(make):
    # reveal does not flip a server-side "shown" flag: polled data stays masked
    fx, name, _ = one_provider(make, identity="carol@example.invalid")
    fx.revealed(name, None)
    for _ in range(2):
        text = json.dumps(fx.quota())
        assert "carol@example.invalid" not in text


def test_qd_r3_the_details_page_has_an_eye_control_and_renders_the_mask(make):
    fx, *_ = one_provider(make)
    text = fx.open_details().text
    assert MASK in text
    assert re.search(r"eye|reveal|show", text, re.I)


# --------------------------------------------------------------------------
# QD-R4 / QD-R4a: where identities come from (the shipped scripts)


def b64(obj) -> str:
    raw = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def jwt(claims: dict) -> str:
    return f"{b64({'alg': 'RS256', 'typ': 'JWT'})}.{b64(claims)}.{b64(b'sig-' + TOKEN_SECRET.encode())}"


def run_script(script, action, env_extra, tmp_path, python=False):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("MULTIAGENTS_", "CLAUDE_", "CODEX_", "XDG_",
                                "OPENCODE", "GEMINI", "ANTHROPIC", "OPENAI"))}
    env.update(env_extra)
    argv = ([sys.executable] if python else ["sh"]) + [str(PROVIDERS_DIR / script), action]
    done = subprocess.run(argv, capture_output=True, text=True, env=env,
                          cwd=tmp_path, timeout=60, stdin=subprocess.DEVNULL)
    return done


def identity_of(done):
    assert done.returncode == 0, (done.returncode, done.stdout, done.stderr)
    out = json.loads(done.stdout.strip())
    assert set(out) <= {"identity", "kind"}, out
    assert out["kind"] in ("email", "account", "org")
    return out["identity"]


def never_leaks(done, *secrets):
    for s in (TOKEN_SECRET, REFRESH_SECRET, *secrets):
        assert s not in done.stdout and s not in done.stderr, s


def claude_profile(path: Path, email=None, *, extra=None):
    path.mkdir(parents=True, exist_ok=True)
    doc = {"numStartups": 3, "primaryApiKey": TOKEN_SECRET, **(extra or {})}
    if email is not None:
        doc["oauthAccount"] = {"emailAddress": email, "organizationName": "Org",
                               "accountUuid": "uuid-1"}
    (path / ".claude.json").write_text(json.dumps(doc))
    (path / ".credentials.json").write_text(json.dumps(
        {"claudeAiOauth": {"accessToken": TOKEN_SECRET, "refreshToken": REFRESH_SECRET}}))


def test_qd_r4a_claude_identity_is_the_profile_email_and_nothing_else(tmp_path):
    home = tmp_path / "home"
    claude_profile(home, "dana@example.invalid")
    done = run_script("claude.sh", "identity", {"HOME": str(home),
                      "MULTIAGENTS_EXECUTOR": "local"}, tmp_path)
    assert identity_of(done) == "dana@example.invalid"
    assert json.loads(done.stdout)["kind"] == "email"
    never_leaks(done)


@pytest.mark.parametrize("meta", [None, {"oauthAccount": {}},
                                  {"oauthAccount": {"emailAddress": ""}},
                                  {"oauthAccount": {"emailAddress": 42}},
                                  {"oauthAccount": "x"}],
                         ids=["no-oauthAccount", "empty-account", "empty-email",
                              "non-string", "wrong-type"])
def test_qd_r4a_claude_without_a_usable_email_exits_64_and_prints_nothing_secret(
        tmp_path, meta):
    home = tmp_path / "home"
    claude_profile(home, None)
    if meta:
        doc = json.loads((home / ".claude.json").read_text())
        doc.update(meta)
        (home / ".claude.json").write_text(json.dumps(doc))
    done = run_script("claude.sh", "identity", {"HOME": str(home),
                      "MULTIAGENTS_EXECUTOR": "local"}, tmp_path)
    assert done.returncode == 64, (done.returncode, done.stdout, done.stderr)
    assert done.stdout.strip() == ""
    never_leaks(done)


def test_qd_r4a_claude_with_no_profile_at_all_is_unknown(tmp_path):
    done = run_script("claude.sh", "identity", {"HOME": str(tmp_path / "empty"),
                      "MULTIAGENTS_EXECUTOR": "local"}, tmp_path)
    assert done.returncode == 64 and done.stdout.strip() == ""


def docker_claude(tmp_path, pin=None, *, vault_b=True):
    host = tmp_path / "host-home"
    claude_profile(host, "host-default@example.invalid")
    backing = tmp_path / "backing"
    claude_profile(backing, "placeholder@example.invalid")
    vault = tmp_path / "vault"
    claude_profile(vault, "default@example.invalid")
    if vault_b:
        claude_profile(vault / "accounts" / "b", "bob@example.invalid")
    env = {"HOME": str(host), "MULTIAGENTS_EXECUTOR": "docker",
           "MULTIAGENTS_PRIVATE_BACKING": str(backing),
           "MULTIAGENTS_PRIVATE_HOME": "/home/agent",
           "MULTIAGENTS_PRIVATE_VAULT": str(vault),
           "MULTIAGENTS_RESERVED_ACCOUNTS": '["b"]'}
    if pin:
        env["MULTIAGENTS_CONTAINER_ACCOUNT"] = pin
    return env


def test_qd_r4a_claude_b_under_docker_reads_the_vault_account_b_profile(tmp_path):
    done = run_script("claude.sh", "identity", docker_claude(tmp_path, "b"), tmp_path)
    assert identity_of(done) == "bob@example.invalid"
    never_leaks(done, "host-default@example.invalid", "placeholder@example.invalid")


def test_qd_r4a_claude_under_docker_shows_the_default_account_not_the_placeholder(tmp_path):
    done = run_script("claude.sh", "identity", docker_claude(tmp_path), tmp_path)
    assert identity_of(done) == "default@example.invalid"


def test_qd_r4a_a_pinned_account_without_metadata_is_unknown_never_the_default(tmp_path):
    done = run_script("claude.sh", "identity",
                      docker_claude(tmp_path, "b", vault_b=False), tmp_path)
    assert done.returncode == 64, (done.returncode, done.stdout)
    assert done.stdout.strip() == ""
    never_leaks(done, "default@example.invalid", "host-default@example.invalid",
                "placeholder@example.invalid")


def codex_profile(path: Path, claims, *, id_token=True):
    path.mkdir(parents=True, exist_ok=True)
    tokens = {"access_token": TOKEN_SECRET, "refresh_token": REFRESH_SECRET,
              "account_id": "acct-123"}
    if id_token is True:
        tokens["id_token"] = jwt(claims)
    elif id_token:
        tokens["id_token"] = id_token
    (path / "auth.json").write_text(json.dumps({"OPENAI_API_KEY": None,
                                                "tokens": tokens}))


def codex(tmp_path, profile, **env):
    return run_script("codex.py", "identity",
                      {"HOME": str(tmp_path / "home"),
                       "MULTIAGENTS_CODEX_PROFILE": str(profile),
                       "MULTIAGENTS_EXECUTOR": "local", **env},
                      tmp_path, python=True)


def test_qd_r4a_codex_identity_is_the_id_token_email_claim(tmp_path):
    prof = tmp_path / "prof"
    codex_profile(prof, {"email": "erin@example.invalid", "sub": "auth0|SUBJECT",
                         "name": "Erin Example", "https://api.openai.com/auth":
                         {"chatgpt_account_id": "acct-123"}})
    done = codex(tmp_path, prof)
    assert identity_of(done) == "erin@example.invalid"
    assert json.loads(done.stdout)["kind"] == "email"
    never_leaks(done, "auth0|SUBJECT", "Erin Example")


def test_qd_r4a_codex_b_reads_its_own_selected_backing(tmp_path):
    one, two = tmp_path / "one", tmp_path / "two"
    codex_profile(one, {"email": "first@example.invalid"})
    codex_profile(two, {"email": "second@example.invalid"})
    assert identity_of(codex(tmp_path, one)) == "first@example.invalid"
    assert identity_of(codex(tmp_path, two)) == "second@example.invalid"


def test_qd_r4a_codex_under_docker_reads_the_private_backing(tmp_path):
    backing, host = tmp_path / "backing", tmp_path / "hostprof"
    codex_profile(backing, {"email": "backing@example.invalid"})
    codex_profile(host, {"email": "host@example.invalid"})
    done = codex(tmp_path, host, MULTIAGENTS_EXECUTOR="docker",
                 MULTIAGENTS_PRIVATE_BACKING=str(backing),
                 MULTIAGENTS_PRIVATE_HOME="/home/agent")
    assert identity_of(done) == "backing@example.invalid"
    never_leaks(done, "host@example.invalid")


@pytest.mark.parametrize("claims", [{"sub": "x"}, {"email": ""}, {"email": 5},
                                    {"email": ["a@example.invalid"]}, {"email": None}],
                         ids=["no-email", "empty", "number", "list", "null"])
def test_qd_r4a_codex_without_a_usable_email_claim_is_unknown(tmp_path, claims):
    prof = tmp_path / "prof"
    codex_profile(prof, claims)
    done = codex(tmp_path, prof)
    assert done.returncode == 64, (done.returncode, done.stdout, done.stderr)
    assert done.stdout.strip() == ""
    never_leaks(done)


@pytest.mark.parametrize("id_token", [False, "not-a-jwt", "a.b.c", "a..c",
                                      "e30.%%%%.sig"],
                         ids=["absent", "no-dots", "garbage-parts", "empty-payload",
                              "bad-base64"])
def test_qd_r4a_codex_with_a_missing_or_malformed_id_token_is_unknown(tmp_path, id_token):
    prof = tmp_path / "prof"
    codex_profile(prof, {}, id_token=id_token)
    done = codex(tmp_path, prof)
    assert done.returncode == 64, (done.returncode, done.stdout, done.stderr)
    assert done.stdout.strip() == ""
    never_leaks(done, "not-a-jwt")


def test_qd_r4a_codex_with_no_auth_file_is_unknown(tmp_path):
    done = codex(tmp_path, tmp_path / "nothing-here")
    assert done.returncode == 64 and done.stdout.strip() == ""


def test_qd_r4a_codex_never_prints_a_claim_it_was_not_asked_for(tmp_path):
    prof = tmp_path / "prof"
    codex_profile(prof, {"email": "erin@example.invalid", "jti": "JTI-CLAIM-VALUE",
                         "exp": 1893456000, "at_hash": "ATHASH-VALUE"})
    done = codex(tmp_path, prof)
    never_leaks(done, "JTI-CLAIM-VALUE", "ATHASH-VALUE", "RS256")


def private_gemini(home: Path):
    g = home / ".gemini"
    g.mkdir(parents=True)
    (g / "oauth_creds.json").write_text(json.dumps(
        {"access_token": TOKEN_SECRET, "refresh_token": REFRESH_SECRET,
         "id_token": jwt({"sub": "SUBJECT-G"})}))


@pytest.mark.parametrize("script,env", [
    ("agy.sh", {}),
    ("opencode.sh", {"OPENCODE_API_KEY": TOKEN_SECRET}),
    ("opencode.sh", {"MULTIAGENTS_OPENCODE_PLAN": "zai-coding-plan",
                     "MULTIAGENTS_PROVIDER": "opencode-zai"}),
], ids=["agy", "opencode", "opencode-zai"])
def test_qd_r4a_agy_and_opencode_family_are_unknown_without_a_proven_source(
        tmp_path, script, env):
    home = tmp_path / "home"
    private_gemini(home)
    data = home / ".local" / "share" / "opencode"
    data.mkdir(parents=True)
    (data / "auth.json").write_text(json.dumps(
        {"opencode": {"type": "api", "key": TOKEN_SECRET},
         "zai-coding-plan": {"type": "api", "key": TOKEN_SECRET}}))
    done = run_script(script, "identity",
                      {"HOME": str(home), "XDG_DATA_HOME": str(home / ".local" / "share"),
                       "MULTIAGENTS_EXECUTOR": "local", **env}, tmp_path)
    assert done.returncode == 64, (done.returncode, done.stdout, done.stderr)
    assert done.stdout.strip() == ""            # neither the provider name nor a key tail
    never_leaks(done, TOKEN_SECRET[-4:], "SUBJECT-G")


# --------------------------------------------------------------------------
# QD-R4 / QD-R7: no leak through the server, whatever the action does


SECRET_STDERR = f"auth failed for {TOKEN_SECRET}"
FAILURES = {
    "stderr-failure": lambda a: (1, "", SECRET_STDERR),
    "stderr-and-stdout": lambda a: (2, f"partial {TOKEN_SECRET}", SECRET_STDERR),
    "timeout": lambda a: (124, "", f"TimeoutExpired: {TOKEN_SECRET}"),
    "not-found": lambda a: (127, "", f"no script {TOKEN_SECRET}"),
    "malformed-json": lambda a: (0, f'{{"identity": "x@example.invalid", {TOKEN_SECRET}', ""),
    "json-list": lambda a: (0, f'["{TOKEN_SECRET}"]', ""),
    "token-like-plain": lambda a: (0, TOKEN_SECRET + "\n", ""),
    "non-string-identity": lambda a: (0, '{"identity": 12345, "kind": "email"}', ""),
    "empty-identity": lambda a: (0, '{"identity": "  ", "kind": "email"}', ""),
    "empty-stdout": lambda a: (0, "", SECRET_STDERR),
    "raises": lambda a: (_ for _ in ()).throw(RuntimeError(f"boom {TOKEN_SECRET}")),
}


@pytest.mark.parametrize("failure", sorted(FAILURES))
def test_qd_r7_a_failing_or_malformed_identity_action_gives_unknown_and_leaks_nothing(
        make, failure, capfd, caplog):
    name = uniq("codex")
    acts = Actions()
    acts.behave[name] = FAILURES[failure]
    fx = make({name: reading(name, {"weekly": win(30)})}, acts)
    resp = fx.reveal(name, None)
    assert resp.status == 200, (resp.status, resp.text[:200])
    assert json.loads(resp.text).get("identity") in (None, "", "unknown")
    time.sleep(0.5)
    bodies = [resp.text, fx.get("/api/quota").text, fx.get("/api/state").text,
              fx.open_details().text]
    for body in bodies:
        assert TOKEN_SECRET not in body and "boom" not in body
        assert "Traceback" not in body and "TimeoutExpired" not in body
    rows = fx.rows()
    assert all(e["identity_available"] is False for e in entries(rows[name]))
    out = capfd.readouterr()
    assert TOKEN_SECRET not in out.out + out.err + caplog.text


def test_qd_r7_a_json_identity_response_with_extra_credential_keys_leaks_only_the_identity(
        make, capfd):
    name = uniq("codex")
    acts = Actions()
    acts.behave[name] = lambda a: (0, json.dumps(
        {"identity": "frank@example.invalid", "kind": "email",
         "access_token": TOKEN_SECRET, "refresh_token": REFRESH_SECRET}), "")
    fx = make({name: reading(name, {"weekly": win(30)})}, acts)
    resp = fx.reveal(name, None)
    assert TOKEN_SECRET not in resp.text and REFRESH_SECRET not in resp.text
    assert set(json.loads(resp.text)) - {"identity", "kind", "provider", "account",
                                         "available", "ok"} == set()
    for body in (fx.get("/api/quota").text, fx.get("/api/state").text):
        assert TOKEN_SECRET not in body and "frank@example.invalid" not in body
    out = capfd.readouterr()
    assert TOKEN_SECRET not in out.out + out.err


def test_qd_r4_the_fixture_token_is_in_no_response_of_a_normal_run(make, capfd):
    p = pooled(make)
    for path in ("/", "/api/quota", "/api/state", "/api/settings", "/api/events"):
        assert TOKEN_SECRET not in p.fx.get(path).text
    p.fx.revealed(p.claude, p.a)
    assert TOKEN_SECRET not in p.fx.open_details().text
    out = capfd.readouterr()
    assert TOKEN_SECRET not in out.out + out.err


# --------------------------------------------------------------------------
# QD-R5: identities are never recorded


def test_qd_r5_after_a_reveal_no_file_in_the_project_contains_the_identity(make, tmp_path):
    p = pooled(make)
    fx = p.fx
    for who, acct in ((p.claude, p.a), (p.claude, p.b), (p.pinned, p.b)):
        assert fx.revealed(who, acct)
    fx.wait(lambda r: all(e["identity_available"] for e in entries(r[p.claude])))
    fx.quota()
    needles = (p.alice.encode(), p.bob.encode(),
               p.alice.split("@")[0].encode())
    seen = 0
    for base in (tmp_path, fx.paths.root):
        for path in base.rglob("*"):
            if path.is_file():
                seen += 1
                blob = path.read_bytes()
                for n in needles:
                    assert n not in blob, f"{path} contains an identity"
    assert seen >= 0
    assert (fx.paths.root / ".multiagents").exists() or fx.paths.config.exists()


# --------------------------------------------------------------------------
# QD-R7: the reveal endpoint's hardening, and the identity lifecycle


def test_qd_r7_the_reveal_rejects_a_token_in_the_query_string(make):
    fx, name, _ = one_provider(make, identity="carol@example.invalid")
    routes_exist(fx, name)
    body = json.dumps({"provider": name, "account": None})
    resp = fx.request("POST", f"/api/quota/identity?token={fx.token}", body=body,
                      headers={"Content-Type": "application/json"}, auth=False)
    assert resp.status == 403 and "carol" not in resp.text


def test_qd_r7_the_data_route_rejects_a_token_in_the_query_string(make):
    fx, name, _ = one_provider(make)
    routes_exist(fx, name)
    resp = fx.get(f"/api/quota?token={fx.token}", auth=False)
    assert resp.status == 403


@pytest.mark.parametrize("origin", ["http://evil.example", "https://127.0.0.1",
                                    "http://127.0.0.1.evil.example", "null",
                                    "http://localhost.evil.example:80"])
def test_qd_r7_a_foreign_origin_is_rejected_on_every_quota_route(make, origin):
    fx, name, _ = one_provider(make, identity="carol@example.invalid")
    routes_exist(fx, name)
    for resp in (fx.reveal(name, None, origin=origin),
                 fx.get("/api/quota", origin=origin),
                 fx.open_details(origin=origin)):
        assert resp.status == 403, (origin, resp.status)
        assert "carol" not in resp.text


def test_qd_r7_the_monitors_own_origin_and_a_missing_origin_are_accepted(make):
    fx, name, _ = one_provider(make, identity="carol@example.invalid")
    own = f"http://127.0.0.1:{fx.port}"
    for origin in (None, own, f"http://localhost:{fx.port}"):
        resp = fx.reveal(name, None, origin=origin)
        assert resp.status == 200, (origin, resp.status)
        assert fx.open_details(origin=origin).status == 200


def test_qd_r7_the_host_check_still_applies_to_the_reveal(make):
    fx, name, _ = one_provider(make, identity="carol@example.invalid")
    routes_exist(fx, name)
    resp = fx.reveal(name, None, host="local.evil.example")
    assert resp.status == 403 and "carol" not in resp.text


def test_qd_r7_no_cors_is_enabled_on_any_response(make):
    fx, name, _ = one_provider(make)
    routes_exist(fx, name)
    for resp in (fx.reveal(name, None), fx.get("/api/quota"), fx.open_details(),
                 fx.request("OPTIONS", "/api/quota/identity",
                            origin="http://evil.example"),
                 fx.get("/api/quota", auth=False)):
        assert not [h for h in resp.headers if h.startswith("access-control-")], resp.headers


@pytest.mark.parametrize("how", ["details-page", "quota-data", "reveal",
                                 "reveal-refused", "reveal-bad-pair", "state"])
def test_qd_r7_all_responses_are_no_store(make, how):
    fx, name, _ = one_provider(make)
    routes_exist(fx, name)
    resp = {"details-page": lambda: fx.open_details(),
            "quota-data": lambda: fx.get("/api/quota"),
            "reveal": lambda: fx.reveal(name, None),
            "reveal-refused": lambda: fx.reveal(name, None, auth=False),
            "reveal-bad-pair": lambda: fx.reveal("nope", None),
            "state": lambda: fx.get("/api/state")}[how]()
    assert "no-store" in resp.headers.get("cache-control", ""), resp.headers


def test_qd_r7_the_details_page_renders_identities_as_text_not_html(make):
    fx, *_ = one_provider(make)
    text = fx.open_details().text
    assert "textContent" in text
    assert "innerHTML" not in text


def test_qd_r7_concurrent_reveals_of_one_account_run_the_action_once(make):
    fx, name, acts = one_provider(make, identity="gail@example.invalid")
    acts.delay = 0.6
    results = []

    def go():
        results.append(fx.revealed(name, None))
    threads = [threading.Thread(target=go) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert results == ["gail@example.invalid"] * 8
    assert len(acts.runs(name)) == 1, acts.runs(name)


def test_qd_r7_reveals_and_polls_together_still_fetch_each_account_once(make):
    p = pooled(make)
    p.acts.delay = 0.4
    threads = [threading.Thread(target=p.fx.revealed, args=(p.claude, p.a)) for _ in range(4)]
    threads += [threading.Thread(target=p.fx.quota) for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len([c for c in p.acts.runs(p.claude) if c[1] == p.a]) == 1


def test_qd_r7_a_repeated_reveal_is_served_from_memory(make):
    fx, name, acts = one_provider(make, identity="gail@example.invalid")
    assert fx.revealed(name, None) == fx.revealed(name, None) == "gail@example.invalid"
    assert len(acts.runs(name)) == 1


def test_qd_r7_the_cache_is_keyed_by_account_not_by_provider_name(make):
    p = pooled(make)
    assert p.fx.revealed(p.claude, p.a) == p.alice      # same provider, two accounts
    assert p.fx.revealed(p.claude, p.b) == p.bob
    assert p.fx.revealed(p.pinned, p.b) == p.bob        # other provider, same account
    assert p.fx.revealed(p.claude, p.a) == p.alice


def test_qd_r7_two_providers_with_the_same_kind_of_account_do_not_share_an_identity(make):
    n = next(_ids)
    one, two = f"codex-c19-{n}", f"codex-b-c19-{n}"
    acts = Actions()
    acts.identities = {(one, None): f"one{n}@example.test",
                       (two, None): f"two{n}@example.test"}
    fx = make({one: reading(one, {"w": win(1)}), two: reading(two, {"w": win(1)})},
              acts)
    assert fx.revealed(one, None) == f"one{n}@example.test"
    assert fx.revealed(two, None) == f"two{n}@example.test"


def test_qd_r7_the_poll_never_waits_for_the_identity_fetch(make):
    fx, name, acts = one_provider(make, identity="gail@example.invalid")
    acts.delay = 3.0
    started = time.time()
    first = fx.rows()
    elapsed = time.time() - started
    assert elapsed < 1.5, elapsed
    assert all(e["identity_available"] is False for e in entries(first[name]))
    # ...and the background fetch fills it in afterwards, without any reveal
    rows = fx.wait(lambda r: all(e["identity_available"] for e in entries(r[name])),
                   seconds=10)
    assert all(e["identity_available"] is True for e in entries(rows[name]))
    assert len(acts.runs(name)) == 1


def test_qd_r7_the_state_poll_is_not_slowed_by_a_slow_identity_action(make):
    fx, name, acts = one_provider(make)
    acts.delay = 3.0
    fx.quota()
    started = time.time()
    assert fx.get("/api/state").status == 200
    assert time.time() - started < 2.0


# --------------------------------------------------------------------------
# QD-R6: nothing existing changed


def test_qd_r6_api_state_keeps_its_token_checks_and_shape(make):
    fx, name, _ = one_provider(make)
    assert fx.get("/api/state", auth=False).status == 403
    assert fx.get("/api/state", headers={"X-Monitor-Token": "no"}, auth=False).status == 403
    assert fx.get(f"/api/state?token={fx.token}", auth=False).status == 200   # as before
    state = json.loads(fx.get("/api/state?scripts=0").text)
    baseline = snap.snapshot(fx.paths, fx.config, with_scripts=False)
    assert set(state) == set(baseline)
    assert [r["name"] for r in state["providers"]] == [r["name"] for r in baseline["providers"]]
    assert [r["lines"] for r in state["providers"]] == [r["lines"] for r in baseline["providers"]]


def test_qd_r6_the_main_page_and_its_routes_are_unchanged(make):
    fx, *_ = one_provider(make)
    page = fx.get("/", auth=False)
    assert page.status == 200 and fx.token in page.text
    assert fx.get("/", auth=False, host="local.evil.example").status == 403
    assert fx.get("/api/nope").status == 404
    assert fx.get("/nope", auth=False).status == 404
    assert fx.get("/api/settings").status == 200
    assert fx.request("POST", "/api/action", body="{}", auth=False).status == 403


def test_qd_r6_the_c18_panel_lines_are_unchanged_by_identities(make):
    p = pooled(make)
    p.fx.revealed(p.claude, p.a)
    rows = {r["name"]: r for r in snap.providers_view(
        p.fx.paths, p.fx.config, p.fx.tree, with_scripts=False)}
    text = "\n".join(rows[p.claude]["lines"])
    assert p.alice not in text and p.bob not in text and MASK not in text
    assert "constraining" in text and "not counted" in text


# --------------------------------------------------------------------------
# C18 leftover (MQ-R2): a provider without windows shows its note once


def test_c19_leftover_a_provider_without_windows_shows_its_note_once(
        tmp_path, monkeypatch):
    import multiagents.scripts as scripts_mod
    real = scripts_mod.run_action
    note = "metered per token; no quota surface"
    acts = Actions()
    fx = Fixture(tmp_path, monkeypatch, {"opencode": reading(
        "opencode", None, known=False, note=note)}, acts)
    monkeypatch.setattr(scripts_mod, "run_action", real)   # the real usage script
    try:
        end = time.time() + 15
        while True:
            rows = {r["name"]: r for r in snap.providers_view(
                fx.paths, fx.config, fx.tree, with_scripts=True)}
            lines = rows["opencode"]["lines"]
            if rows["opencode"]["lines_from"] == "script" or time.time() > end:
                break
            time.sleep(0.2)
        again = {r["name"]: r for r in snap.providers_view(
            fx.paths, fx.config, fx.tree, with_scripts=True)}["opencode"]["lines"]
    finally:
        fx.close()
    for shown in (lines, again):
        assert sum(note in line for line in shown) == 1, shown
