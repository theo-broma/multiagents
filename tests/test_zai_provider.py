"""Black-box contract for the opencode-zai provider: context/specs/zai-provider.md.

ZA-R1  a shipped `opencode-zai` provider instance (providers.yaml, refresh-models)
ZA-R2  `check` / `login` of opencode.sh when MULTIAGENTS_OPENCODE_PLAN=zai-coding-plan
ZA-R3  `budget` from the z.ai quota endpoint
ZA-R4  `usage` shows both windows
ZA-R5  no regression without the plan variable, and no new docker egress

The script is driven as a subprocess with a temporary XDG_DATA_HOME holding a
fake opencode auth.json, a fake `opencode` binary, and (R3/R4) a local HTTP
server standing in for api.z.ai through MULTIAGENTS_ZAI_ORIGIN.

Assumptions the contract does not spell out (see the run report):
  * `usage` fetches the endpoint itself (it needs `currentValue`/`usage`, which
    the budget object's windows do not carry), so MULTIAGENTS_BUDGET is unset.
  * The `usage` reset time is shown in the existing fallback form: ISO-8601.
"""

from __future__ import annotations

import copy
import json
import os
import re
import shutil
import subprocess
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml

from multiagents.paths import shipped_defaults_dir

DEFAULTS = shipped_defaults_dir()
SCRIPT = DEFAULTS / "providers" / "opencode.sh"
PLAN = "zai-coding-plan"
KEY = "zk-test-1234567890abcdef.SECRETtail"
GO_KEY = "go-key-NEVER-SEND-TO-ZAI-98765"
QUOTA_PATH = "/api/monitor/usage/quota/limit"

FIVE_MS = 1790737981879      # 2026-09-30T03:13:01.879Z
WEEK_MS = 1791324417984      # 2026-10-06T22:06:57.984Z


def limit(unit, number, pct, reset, *, type="CREDIT_LIMIT", usage=None, current=None):
    d = {"type": type, "unit": unit, "number": number, "percentage": pct,
         "nextResetTime": reset}
    if usage is not None:
        d.update(usage=usage, currentValue=current, remaining=usage - (current or 0))
    return d


def envelope(limits, **over):
    body = {"code": 200, "success": True, "data": {"level": "lite", "limits": limits}}
    body.update(over)
    return body


LIVE = {"code": 200, "success": True, "data": {"level": "lite", "limits": [
    {"type": "CREDIT_LIMIT", "unit": 3, "number": 5, "usage": 2000, "currentValue": 1,
     "remaining": 1998, "percentage": 1, "nextResetTime": FIVE_MS},
    {"type": "CREDIT_LIMIT", "unit": 6, "number": 1, "usage": 10000, "currentValue": 1,
     "remaining": 9998, "percentage": 1, "nextResetTime": WEEK_MS}]}}

HIGH = envelope([limit(3, 5, 5, FIVE_MS, usage=2000, current=100),
                 limit(6, 1, 80, WEEK_MS, usage=10000, current=8000)])


# ---------------------------------------------------------------------------
# harness
# ---------------------------------------------------------------------------

class FakeZai:
    """A local stand-in for api.z.ai. `reply` is (status, body, headers) or a
    callable(handler) -> tuple; every request is recorded."""

    def __init__(self, reply=None, delay: float = 0.0):
        self.reply = reply if reply is not None else (200, LIVE, {})
        self.delay = delay
        self.requests: list[dict] = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):  # silence
                pass

            def do_GET(self):
                outer.requests.append({"method": "GET", "path": self.path,
                                       "auth": self.headers.get("Authorization"),
                                       "headers": dict(self.headers)})
                if outer.delay:
                    time.sleep(outer.delay)
                if self.path.startswith("/redirected"):
                    status, body, hdrs = 200, LIVE, {}
                else:
                    status, body, hdrs = outer.reply
                raw = body if isinstance(body, (bytes, str)) else json.dumps(body)
                raw = raw.encode() if isinstance(raw, str) else raw
                self.send_response(status)
                for k, v in hdrs.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                try:
                    self.wfile.write(raw)
                except OSError:
                    pass

            do_POST = do_PUT = do_HEAD = do_GET

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.httpd.daemon_threads = True
        self.origin = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    @property
    def quota_hits(self):
        return [r for r in self.requests if r["path"].split("?")[0] == QUOTA_PATH]


@pytest.fixture
def zai():
    servers: list[FakeZai] = []

    def make(reply=None, delay=0.0):
        s = FakeZai(reply, delay)
        servers.append(s)
        return s

    yield make
    for s in servers:
        s.close()


def _fake_bin(dir: Path, body: str = "echo '1 credentials'\n") -> Path:
    p = dir / "opencode"
    p.write_text("#!/bin/sh\n" + body)
    p.chmod(0o755)
    return p


class Env:
    def __init__(self, tmp_path: Path):
        self.root = tmp_path
        self.data = tmp_path / "xdg"
        self.home = tmp_path / "home"
        self.home.mkdir()
        self.data.mkdir()
        self.bin = _fake_bin(tmp_path)
        self.calls = tmp_path / "bin-calls.txt"

    def store(self, entries):
        d = self.data / "opencode"
        d.mkdir(parents=True, exist_ok=True)
        (d / "auth.json").write_text(json.dumps(entries) if not isinstance(entries, str)
                                     else entries)

    def zai_store(self, key=KEY, **extra):
        self.store({PLAN: {"type": "api", "key": key}, **extra})

    def run(self, action, *, plan=PLAN, origin=None, extra=None, timeout=60):
        env = {k: v for k, v in os.environ.items()
               if not k.lower().endswith("_proxy") and not k.startswith("MULTIAGENTS_")}
        env.update(HOME=str(self.home), XDG_DATA_HOME=str(self.data), TZ="UTC",
                   NO_PROXY="127.0.0.1,localhost", MULTIAGENTS_BIN=str(self.bin))
        if plan is not None:
            env["MULTIAGENTS_OPENCODE_PLAN"] = plan
        if origin is not None:
            env["MULTIAGENTS_ZAI_ORIGIN"] = origin
        env.update(extra or {})
        return subprocess.run(["sh", str(SCRIPT), action], env=env, text=True,
                              stdin=subprocess.DEVNULL, capture_output=True,
                              timeout=timeout)


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def budget(env: Env, server: FakeZai, **kw):
    cp = env.run("budget", origin=server.origin, **kw)
    assert cp.returncode == 0, (cp.returncode, cp.stdout, cp.stderr)
    return cp, json.loads(cp.stdout)


def assert_no_key(cp, *keys):
    for k in keys or (KEY,):
        assert k not in cp.stdout and k not in cp.stderr, (
            f"the key leaked:\nstdout={cp.stdout!r}\nstderr={cp.stderr!r}")


def iso(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc)


def parse_iso(s):
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    assert dt.tzinfo is not None, f"resets_at must be UTC-aware: {s!r}"
    return dt


def same_instant(s, ms):
    return abs((parse_iso(s) - iso(ms)).total_seconds()) < 1.0


# ===========================================================================
# ZA-R1: a shipped provider instance
# ===========================================================================

def _shipped_raw():
    return yaml.safe_load((DEFAULTS / "providers.yaml").read_text())["providers"]


def _load(raw):
    from multiagents.providers import load_providers
    return load_providers(copy.deepcopy(raw))


def test_za_r1_shipped_file_defines_opencode_zai_disabled_plan_own_family():
    p = _load(_shipped_raw()).get("opencode-zai")
    assert p is not None, "shipped providers.yaml has no `opencode-zai` instance"
    assert p.enabled is False
    assert p.billing == "plan"
    assert p.family == "opencode-zai"
    assert list(p.models_include) == ["zai-coding-plan/*"]
    assert p.env.get("MULTIAGENTS_OPENCODE_PLAN") == PLAN


def test_za_r1_inherits_opencode_spawn_stream_and_script():
    ps = _load(_shipped_raw())
    z, o = ps.get("opencode-zai"), ps["opencode"]
    assert z is not None, "no `opencode-zai` instance"
    assert z.spawn == o.spawn and z.spawn.get("args")
    assert z.stream == o.stream and z.stream.get("rules")
    assert z.bin == o.bin
    assert z.auth == o.auth, "the instance must use the same opencode.sh"


def test_za_r1_failover_between_go_and_zai_is_never_implicit():
    from multiagents.providers import families
    fam = families(_load(_shipped_raw()))
    assert "opencode-zai" in fam and fam["opencode-zai"] == ["opencode-zai"], fam
    assert "opencode-zai" not in fam.get("opencode", []), fam


def test_za_r1_opencode_itself_is_unchanged():
    o = _load(_shipped_raw())["opencode"]
    assert list(o.models_include) == ["opencode/*", "opencode-go/*"]
    assert o.family == "opencode"
    assert o.enabled is not False
    assert "MULTIAGENTS_OPENCODE_PLAN" not in (o.env or {})


def test_za_r1_shipped_yaml_documents_how_to_enable():
    text = (DEFAULTS / "providers.yaml").read_text()
    i = text.find("opencode-zai:")
    assert i >= 0, "no opencode-zai block in the shipped providers.yaml"
    block = text[max(0, i - 800):i + 800]
    assert "#" in block and "enabled" in block.lower(), "needs an enabling comment"


def test_za_r1_refresh_models_splits_the_namespaces(tmp_path, monkeypatch):
    from multiagents.models import refresh_models
    raw = _shipped_raw()
    assert "opencode-zai" in raw, "no `opencode-zai` instance in the shipped file"
    raw = {k: raw[k] for k in ("opencode", "opencode-zai")}
    raw["opencode-zai"]["enabled"] = True      # ships disabled; enable for the test
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _fake_bin(bindir, "[ \"$1\" = models ] && printf '%s\\n' "
                      "opencode/big-pickle opencode-go/glm-5.1 opencode-go/kimi-k2 "
                      "zai-coding-plan/glm-5.3-flash zai-coding-plan/glm-4.7 "
                      "deepinfra/other\n")
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}/usr/bin{os.pathsep}/bin")
    monkeypatch.setenv("HOME", str(tmp_path))
    target = tmp_path / "models.yaml"
    out = refresh_models(_load(raw), target, config_dir=tmp_path / "cfg")
    models = yaml.safe_load(target.read_text())["models"]
    ids = lambda n: sorted(m["id"] for m in models.get(n) or [])   # noqa: E731
    assert ids("opencode-zai") == ["zai-coding-plan/glm-4.7", "zai-coding-plan/glm-5.3-flash"], (
        models, out)
    assert ids("opencode") == ["opencode-go/glm-5.1", "opencode-go/kimi-k2",
                               "opencode/big-pickle"], models


def test_za_r1_disabled_instance_is_not_refreshed(tmp_path, monkeypatch):
    from multiagents.models import refresh_models
    raw = _shipped_raw()
    assert "opencode-zai" in raw
    raw = {k: raw[k] for k in ("opencode", "opencode-zai")}
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _fake_bin(bindir, "printf '%s\\n' zai-coding-plan/glm-4.7 opencode/x\n")
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}/usr/bin{os.pathsep}/bin")
    monkeypatch.setenv("HOME", str(tmp_path))
    target = tmp_path / "models.yaml"
    refresh_models(_load(raw), target, config_dir=tmp_path / "cfg")
    assert "opencode-zai" not in (yaml.safe_load(target.read_text())["models"] or {})


# ===========================================================================
# ZA-R2: check and login for the plan
# ===========================================================================

def test_za_r2_check_exits_0_when_the_entry_has_a_key(env):
    env.bin = _fake_bin(env.root, "echo '0 credentials'\n")   # `providers list` says none
    env.zai_store()
    cp = env.run("check")
    assert cp.returncode == 0, (cp.stdout, cp.stderr)
    assert_no_key(cp)


def test_za_r2_check_does_not_need_the_opencode_binary(env):
    # The store is the source of truth: `providers list` failing changes nothing.
    env.bin = _fake_bin(env.root, "exit 99\n")
    env.zai_store()
    assert env.run("check").returncode == 0


def test_za_r2_check_missing_entry_exits_10_naming_entry_and_fix(env):
    env.store({"opencode-go": {"type": "api", "key": GO_KEY}})
    cp = env.run("check")
    assert cp.returncode == 10, (cp.stdout, cp.stderr)
    msg = cp.stdout + cp.stderr
    assert PLAN in msg
    assert "multiagents auth login opencode-zai" in msg
    assert "Z.AI Coding Plan" in msg
    assert_no_key(cp, GO_KEY, KEY)


def test_za_r2_check_go_only_store_is_10_for_plan_and_0_without_it(env):
    env.store({"opencode-go": {"type": "api", "key": GO_KEY}})
    env.bin = _fake_bin(env.root, "echo '1 credentials'\n")
    assert env.run("check").returncode == 10
    assert env.run("check", plan=None).returncode == 0


def test_za_r2_check_missing_store_exits_10(env):
    cp = env.run("check")
    assert cp.returncode == 10, (cp.stdout, cp.stderr)
    assert (cp.stdout + cp.stderr).strip()


def test_za_r2_check_unparsable_store_exits_10_with_its_own_message(env):
    env.store({PLAN: {"key": KEY}})
    good_missing = None
    env.store("{ this is not json " + KEY)
    bad = env.run("check")
    assert bad.returncode == 10, (bad.stdout, bad.stderr)
    assert "Traceback" not in bad.stdout + bad.stderr
    assert_no_key(bad)
    # the three failures are told apart
    env.store({"opencode-go": {"key": GO_KEY}})
    entry_missing = env.run("check")
    shutil.rmtree(env.data / "opencode")
    store_missing = env.run("check")
    msgs = {(r.stdout + r.stderr).strip() for r in (bad, entry_missing, store_missing)}
    assert len(msgs) == 3, f"each cause needs its own message: {msgs}"
    del good_missing


@pytest.mark.parametrize("entry", [
    {"type": "api", "key": ""},
    {"type": "api"},
    {"type": "api", "key": None},
    "just-a-string",
    None,
])
def test_za_r2_check_entry_without_a_usable_key_exits_10(env, entry):
    env.store({PLAN: entry, "opencode-go": {"key": GO_KEY}})
    cp = env.run("check")
    assert cp.returncode == 10, (cp.stdout, cp.stderr)
    assert "Traceback" not in cp.stdout + cp.stderr


def test_za_r2_check_non_object_store_exits_10(env):
    env.store("[1, 2, 3]")
    cp = env.run("check")
    assert cp.returncode == 10, (cp.stdout, cp.stderr)
    assert "Traceback" not in cp.stdout + cp.stderr


def test_za_r2_check_is_repeatable(env):
    env.bin = _fake_bin(env.root, "echo '0 credentials'\n")
    env.zai_store()
    a, b = env.run("check"), env.run("check")
    assert (a.returncode, a.stdout) == (b.returncode, b.stdout) and a.returncode == 0


def test_za_r2_login_prints_one_line_then_runs_providers_login(env):
    env.bin = _fake_bin(env.root, f'echo "ARGS:$*" > "{env.calls}"; echo FAKE-LOGIN-RAN\n')
    cp = env.run("login")
    assert cp.returncode == 0, (cp.stdout, cp.stderr)
    assert env.calls.read_text().strip() == "ARGS:providers login"
    before = cp.stdout.split("FAKE-LOGIN-RAN")[0].strip().splitlines()
    assert len(before) == 1, f"exactly one instruction line first, got {before}"
    assert "z.ai" in before[0].lower()


def test_za_r2_login_passes_on_the_login_exit_status(env):
    env.bin = _fake_bin(env.root, "exit 7\n")
    cp = env.run("login")
    assert cp.returncode == 7 and "z.ai" in cp.stdout.lower()


# ===========================================================================
# ZA-R3: budget from the z.ai quota endpoint
# ===========================================================================

def test_za_r3_live_sample_is_known_with_headroom_099(env, zai):
    env.zai_store()
    s = zai()
    _, b = budget(env, s)
    assert b["known"] is True
    assert b["headroom"] == 0.99
    assert b["source"] == "api.z.ai/api/monitor/usage/quota/limit"
    assert set(b["windows"]) == {"five_hour", "weekly"}
    assert b["windows"]["five_hour"]["percent"] == 1
    assert b["windows"]["weekly"]["percent"] == 1
    assert same_instant(b["windows"]["five_hour"]["resets_at"], FIVE_MS)
    assert same_instant(b["windows"]["weekly"]["resets_at"], WEEK_MS)
    assert isinstance(b["note"], str) and b["note"]


def test_za_r3_the_fuller_window_is_the_constraint(env, zai):
    env.zai_store()
    _, b = budget(env, zai((200, HIGH, {})))
    assert b["headroom"] == 0.2
    assert same_instant(b["resets_at"], WEEK_MS)
    assert "weekly" in b["note"].lower() and "80%" in b["note"]
    assert b["windows"]["five_hour"]["percent"] == 5
    assert b["windows"]["weekly"]["percent"] == 80


def test_za_r3_five_hour_can_be_the_constraint(env, zai):
    env.zai_store()
    body = envelope([limit(3, 5, 90, FIVE_MS), limit(6, 1, 10, WEEK_MS)])
    _, b = budget(env, zai((200, body, {})))
    assert b["headroom"] == 0.1
    assert same_instant(b["resets_at"], FIVE_MS)
    assert "five" in b["note"].lower() and "90%" in b["note"]


def test_za_r3_sends_the_raw_key_with_no_bearer(env, zai):
    env.zai_store()
    s = zai()
    budget(env, s)
    assert len(s.quota_hits) == 1
    hit = s.quota_hits[0]
    assert hit["method"] == "GET"
    assert hit["auth"] == KEY
    assert "bearer" not in (hit["auth"] or "").lower()


def test_za_r3_key_is_sent_verbatim_even_with_quotes_and_backslashes(env, zai):
    odd = 'ab"cd\\ef gh#i'
    env.zai_store(key=odd)
    s = zai()
    _, b = budget(env, s)
    assert s.quota_hits and s.quota_hits[0]["auth"] == odd
    assert b["known"] is True


def test_za_r3_uses_the_plan_key_not_the_go_key(env, zai):
    env.store({"opencode-go": {"type": "api", "key": GO_KEY},
               PLAN: {"type": "api", "key": KEY}})
    s = zai()
    cp, _ = budget(env, s)
    assert s.quota_hits[0]["auth"] == KEY
    assert_no_key(cp, KEY, GO_KEY)


def test_za_r3_go_key_is_never_sent_to_zai(env, zai):
    env.store({"opencode-go": {"type": "api", "key": GO_KEY}})
    s = zai()
    cp, b = budget(env, s)
    assert b["known"] is False and "no key" in b["note"].lower()
    assert s.requests == [], "no plan key: nothing may be sent, least of all the Go key"
    assert_no_key(cp, GO_KEY)


def test_za_r3_key_never_appears_in_any_output(env, zai):
    env.zai_store()
    cp, b = budget(env, zai())
    assert b["known"] is True
    assert_no_key(cp)


def test_za_r3_key_is_not_on_any_process_command_line(env, zai):
    env.zai_store()
    s = zai(delay=1.5)
    proc = subprocess.Popen(
        ["sh", "-c", 'exec sh "$0" budget', str(SCRIPT)], text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env={**os.environ, "HOME": str(env.home), "XDG_DATA_HOME": str(env.data),
             "MULTIAGENTS_BIN": str(env.bin), "MULTIAGENTS_OPENCODE_PLAN": PLAN,
             "MULTIAGENTS_ZAI_ORIGIN": s.origin, "NO_PROXY": "127.0.0.1"})
    seen, deadline = [], time.time() + 20
    while proc.poll() is None and time.time() < deadline:
        for cmdline in Path("/proc").glob("[0-9]*/cmdline"):
            try:
                if KEY.encode() in cmdline.read_bytes():
                    seen.append(str(cmdline))
            except OSError:
                pass
        time.sleep(0.02)
    proc.communicate(timeout=20)
    assert s.quota_hits, "the request never happened, so the check proved nothing"
    assert seen == [], f"the key was visible on a command line: {seen}"


def test_za_r3_refuses_redirects(env, zai):
    env.zai_store()
    s = zai((302, "", {"Location": "/redirected"}))
    cp, b = budget(env, s)
    assert b["known"] is False
    assert s.quota_hits, "the request was never made, so nothing was refused"
    assert not any(r["path"].startswith("/redirected") for r in s.requests), (
        "a redirect was followed (and would carry the key with it)")
    assert_no_key(cp)


def test_za_r3_does_not_hang_on_a_slow_endpoint(env, zai):
    env.zai_store()
    s = zai(delay=40)
    t0 = time.time()
    cp = env.run("budget", origin=s.origin, timeout=60)
    assert s.quota_hits, "the request was never made"
    assert time.time() - t0 < 25, "timeout must be 15 s or less"
    assert cp.returncode == 0
    assert json.loads(cp.stdout)["known"] is False
    assert_no_key(cp)


def test_za_r3_headroom_never_negative(env, zai):
    env.zai_store()
    body = envelope([limit(3, 5, 120, FIVE_MS), limit(6, 1, 30, WEEK_MS)])
    _, b = budget(env, zai((200, body, {})))
    assert b["known"] is True and b["headroom"] == 0.0


def test_za_r3_exactly_full_and_exactly_empty(env, zai):
    env.zai_store()
    _, full = budget(env, zai((200, envelope([limit(3, 5, 100, FIVE_MS)]), {})))
    assert full["headroom"] == 0.0
    _, empty = budget(env, zai((200, envelope([limit(3, 5, 0, FIVE_MS),
                                               limit(6, 1, 0, WEEK_MS)]), {})))
    assert empty["known"] is True and empty["headroom"] == 1.0


def test_za_r3_headroom_is_rounded_to_4_decimals(env, zai):
    env.zai_store()
    body = envelope([limit(3, 5, 33.33333, FIVE_MS)])
    _, b = budget(env, zai((200, body, {})))
    assert b["headroom"] == 0.6667


def test_za_r3_only_windows_present_are_listed(env, zai):
    env.zai_store()
    _, only5 = budget(env, zai((200, envelope([limit(3, 5, 40, FIVE_MS)]), {})))
    assert set(only5["windows"]) == {"five_hour"} and only5["headroom"] == 0.6
    _, onlyw = budget(env, zai((200, envelope([limit(6, 1, 25, WEEK_MS)]), {})))
    assert set(onlyw["windows"]) == {"weekly"} and onlyw["headroom"] == 0.75
    assert same_instant(onlyw["resets_at"], WEEK_MS)


def test_za_r3_a_missing_window_is_unknown_not_zero(env, zai):
    # Only the 5 h window at 90%: the absent weekly must not be invented as 0%
    # (which would change nothing) or as 100% (which would zero the headroom).
    env.zai_store()
    _, b = budget(env, zai((200, envelope([limit(3, 5, 90, FIVE_MS)]), {})))
    assert b["headroom"] == 0.1 and "weekly" not in b["windows"]


@pytest.mark.parametrize("types", ["TOKENS_LIMIT", "CREDIT_LIMIT"])
def test_za_r3_both_limit_types_are_recognised(env, zai, types):
    env.zai_store()
    body = envelope([limit(3, 5, 10, FIVE_MS, type=types),
                     limit(6, 1, 60, WEEK_MS, type=types)])
    _, b = budget(env, zai((200, body, {})))
    assert b["headroom"] == 0.4 and set(b["windows"]) == {"five_hour", "weekly"}


def test_za_r3_other_limit_types_are_ignored(env, zai):
    env.zai_store()
    body = envelope([limit(3, 5, 99, FIVE_MS, type="TIME_LIMIT"),
                     limit(6, 1, 99, WEEK_MS, type="MCP_LIMIT"),
                     limit(3, 5, 10, FIVE_MS, type="CREDIT_LIMIT")])
    _, b = budget(env, zai((200, body, {})))
    assert set(b["windows"]) == {"five_hour"} and b["headroom"] == 0.9


def test_za_r3_five_hour_needs_unit_3_and_number_5(env, zai):
    env.zai_store()
    body = envelope([limit(3, 1, 99, FIVE_MS),       # unit 3 but not 5 hours
                     limit(3, 5, 20, FIVE_MS)])
    _, b = budget(env, zai((200, body, {})))
    assert b["windows"]["five_hour"]["percent"] == 20 and b["headroom"] == 0.8


def test_za_r3_first_matching_limit_wins(env, zai):
    env.zai_store()
    body = envelope([limit(3, 5, 10, FIVE_MS), limit(3, 5, 90, FIVE_MS),
                     limit(6, 1, 20, WEEK_MS), limit(6, 4, 95, WEEK_MS)])
    _, b = budget(env, zai((200, body, {})))
    assert b["windows"]["five_hour"]["percent"] == 10
    assert b["windows"]["weekly"]["percent"] == 20
    assert b["headroom"] == 0.8


@pytest.mark.parametrize("extra", [
    {"code": 0}, {"code": None}, {"code": 200, "success": True}])
def test_za_r3_success_codes_are_accepted(env, zai, extra):
    env.zai_store()
    body = envelope([limit(3, 5, 50, FIVE_MS)], **extra)
    _, b = budget(env, zai((200, body, {})))
    assert b["known"] is True and b["headroom"] == 0.5


def test_za_r3_unwrapped_data_without_code_or_success_is_accepted(env, zai):
    env.zai_store()
    body = {"data": {"level": "lite", "limits": [limit(3, 5, 50, FIVE_MS)]}}
    _, b = budget(env, zai((200, body, {})))
    assert b["known"] is True and b["headroom"] == 0.5


@pytest.mark.parametrize("bad", [
    {"code": 500, "success": False},
    {"code": 200, "success": False},
    {"code": 401, "success": True},
    {"code": 500},
])
def test_za_r3_failure_envelopes_report_unknown(env, zai, bad):
    env.zai_store()
    body = envelope([limit(3, 5, 50, FIVE_MS)], **bad)   # limits present, still a failure
    cp, b = budget(env, zai((200, body, {})))
    assert b["known"] is False and "headroom" not in b
    assert "fail" in b["note"].lower()
    assert_no_key(cp)


def test_za_r3_failure_envelope_without_data_and_http_error(env, zai):
    env.zai_store()
    cp, b = budget(env, zai((500, {"code": 500, "success": False}, {})))
    assert b["known"] is False and "fail" in b["note"].lower()
    assert_no_key(cp)


def test_za_r3_non_json_body_reports_unknown(env, zai):
    env.zai_store()
    cp, b = budget(env, zai((200, "<html>gateway</html>", {})))
    assert b["known"] is False and "json" in b["note"].lower()
    assert_no_key(cp)


def test_za_r3_non_json_body_that_echoes_the_key_does_not_leak_it(env, zai):
    env.zai_store()
    s = zai((401, f"invalid key {KEY}", {}))
    cp, b = budget(env, s)
    assert b["known"] is False and s.quota_hits
    assert s.quota_hits, "the request was never made"
    assert_no_key(cp)


def test_za_r3_no_matching_window_reports_unknown(env, zai):
    env.zai_store()
    body = envelope([limit(1, 1, 50, FIVE_MS, type="TIME_LIMIT"), limit(3, 2, 50, FIVE_MS)])
    cp, b = budget(env, zai((200, body, {})))
    assert b["known"] is False and "no window" in b["note"].lower()
    assert_no_key(cp)


def test_za_r3_empty_limits_and_missing_data_report_unknown(env, zai):
    env.zai_store()
    _, a = budget(env, zai((200, envelope([]), {})))
    _, b = budget(env, zai((200, {"code": 200, "success": True}, {})))
    assert a["known"] is False and b["known"] is False
    assert "no window" in a["note"].lower()


def test_za_r3_unreachable_endpoint_reports_unknown(env):
    import socket
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    env.zai_store()
    cp = env.run("budget", origin=f"http://127.0.0.1:{port}")
    assert cp.returncode == 0
    b = json.loads(cp.stdout)
    assert b["known"] is False and "unreachable" in b["note"].lower()
    assert_no_key(cp)


@pytest.mark.parametrize("store", [
    None,
    {"opencode-go": {"key": GO_KEY}},
    {PLAN: {"type": "api", "key": ""}},
    "not json at all",
])
def test_za_r3_no_key_reports_unknown_without_a_request(env, zai, store):
    if store is not None:
        env.store(store)
    s = zai()
    cp, b = budget(env, s)
    assert b["known"] is False and "no key" in b["note"].lower()
    assert s.requests == []
    assert_no_key(cp, GO_KEY)


def test_za_r3_prints_a_single_json_object_and_is_repeatable(env, zai):
    env.zai_store()
    s = zai()
    a, ja = budget(env, s)
    b, jb = budget(env, s)
    assert isinstance(ja, dict) and ja == jb
    assert len(s.quota_hits) == 2


# ===========================================================================
# ZA-R4: usage prints the credits, and only the credits
#
# C18 MQ-R2a: a usage action prints extras, never window bars. The monitor core
# draws both windows (percent, bar, reset) from the budget reading, which
# ZA-R3 holds; what survives here is the `used/limit credits` figure, which
# exists only in this script's live fetch.
# ===========================================================================

def usage(env, server, **kw):
    cp = env.run("usage", origin=server.origin, **kw)
    assert cp.returncode in (0, 64), (cp.returncode, cp.stdout, cp.stderr)
    return cp


def _line(cp, needle):
    hits = [ln for ln in cp.stdout.splitlines() if needle in ln]
    assert len(hits) == 1, f"expected one line with {needle!r}:\n{cp.stdout}"
    return hits[0]


def test_za_r4_usage_shows_credits_when_present(env, zai):
    env.zai_store()
    cp = usage(env, zai((200, HIGH, {})))
    assert cp.returncode == 0, cp.stderr
    assert "100/2000 credits" in _line(cp, "2000"), cp.stdout
    assert "8000/10000 credits" in _line(cp, "10000"), cp.stdout
    assert_no_key(cp)


def test_za_r4_usage_prints_no_window_lines(env, zai):
    env.zai_store()
    cp = usage(env, zai((200, HIGH, {})))
    assert not re.search(r"[#.]{5,}|[\u2588\u2591]|\d\s*%", cp.stdout), cp.stdout
    assert "2026-09-30" not in cp.stdout and "2026-10-06" not in cp.stdout, (
        "reset times are drawn by the monitor core", cp.stdout)
    assert "five_hour" not in cp.stdout and "weekly" not in cp.stdout, cp.stdout


def test_za_r4_usage_with_one_window_shows_only_that_ones_credits(env, zai):
    env.zai_store()
    body = envelope([limit(6, 1, 33, WEEK_MS, usage=10000, current=8000)])
    cp = usage(env, zai((200, body, {})))
    assert cp.stdout.strip().splitlines() == [cp.stdout.strip()], cp.stdout
    assert "8000/10000 credits" in cp.stdout and "33" not in cp.stdout, cp.stdout


@pytest.mark.parametrize("reply", [
    (200, "<html/>", {}),
    (500, {"code": 500, "success": False}, {}),
    (200, envelope([]), {}),
    (302, "", {"Location": "/redirected"}),
])
def test_za_r4_usage_when_unknown_prints_no_extras_and_leaks_nothing(env, zai, reply):
    env.zai_store()
    s = zai(reply)
    cp = usage(env, s)
    assert s.quota_hits, "the request was never made"
    assert cp.returncode in (0, 64)      # no extras; the core shows the reading's note
    assert "Traceback" not in cp.stdout + cp.stderr
    assert not any(r["path"].startswith("/redirected") for r in s.requests)
    assert_no_key(cp)


def test_za_r4_usage_without_a_key_sends_nothing(env, zai):
    env.store({"opencode-go": {"key": GO_KEY}})
    s = zai()
    cp = usage(env, s)
    assert s.requests == []
    assert_no_key(cp, GO_KEY)


def test_za_r4_usage_sends_the_raw_key(env, zai):
    env.zai_store()
    s = zai()
    usage(env, s)
    assert s.quota_hits and s.quota_hits[0]["auth"] == KEY


# ===========================================================================
# ZA-R5: no regression, no new egress
# ===========================================================================

def test_za_r5_check_without_plan_ignores_the_store_and_uses_providers_list(env):
    env.store({"opencode-go": {"type": "api", "key": GO_KEY}})
    env.bin = _fake_bin(env.root, "echo '1 credentials'\n")
    cp = env.run("check", plan=None)
    assert (cp.returncode, cp.stdout) == (0, "1 stored credential(s)\n")
    assert_no_key(cp, GO_KEY)


def test_za_r5_check_without_plan_zero_credentials_is_unchanged_despite_a_zai_key(env):
    env.zai_store()
    env.bin = _fake_bin(env.root, "echo '0 credentials'\n")
    cp = env.run("check", plan=None)
    assert (cp.returncode, cp.stdout) == (
        10, "no stored credentials (free tier / env keys only)\n")


def test_za_r5_check_without_plan_when_binary_fails_is_unchanged(env):
    env.bin = _fake_bin(env.root, "exit 3\n")
    cp = env.run("check", plan=None)
    assert cp.returncode == 20 and "providers list" in cp.stdout


def test_za_r5_budget_without_plan_never_contacts_zai_and_keeps_its_message(env, zai):
    env.zai_store()                       # a z.ai key and no Go key
    s = zai()
    cp = env.run("budget", plan=None, origin=s.origin)
    assert cp.returncode == 0
    assert json.loads(cp.stdout) == {
        "known": False, "note": "no opencode-go key; free tier has no quota surface"}
    assert s.requests == []


def test_za_r5_budget_without_plan_and_no_store_is_unchanged(env):
    cp = env.run("budget", plan=None)
    assert cp.returncode == 0
    assert json.loads(cp.stdout) == {
        "known": False, "note": "no opencode auth store; run `opencode auth login`"}


def test_za_r5_usage_without_plan_prints_no_window_lines_and_never_contacts_zai(env, zai):
    """C18 MQ-R2a: the opencode rendering is extras-only too. A reading with a
    window and nothing else to say is "no extras": exit 64, quietly."""
    s = zai()
    b = {"known": True, "headroom": 0.5, "windows": {
        "rolling": {"percent": 50.0, "resets_at": "2026-10-01T00:00:00+00:00"}}}
    cp = env.run("usage", plan=None, origin=s.origin,
                 extra={"MULTIAGENTS_BUDGET": json.dumps(b)})
    assert (cp.returncode, cp.stdout) == (64, "")
    assert s.requests == []


def test_za_r5_login_without_plan_is_unchanged(env):
    env.bin = _fake_bin(env.root, f'echo "ARGS:$*" > "{env.calls}"\n')
    cp = env.run("login", plan=None)
    assert cp.returncode == 0
    assert "OpenCode Go" in cp.stdout and "z.ai" not in cp.stdout.lower()
    assert env.calls.read_text().strip() == "ARGS:providers login"


def test_za_r5_docker_egress_allowlist_does_not_gain_zai():
    cfg = yaml.safe_load((DEFAULTS / "project.yaml").read_text())

    def find(node):
        if isinstance(node, dict):
            for k, v in node.items():
                if k == "egress_allowlist":
                    yield v
                yield from find(v)
        elif isinstance(node, list):
            for v in node:
                yield from find(v)

    lists = list(find(cfg))
    assert lists, "egress_allowlist not found in the shipped project.yaml"
    for allow in lists:
        assert not any("z.ai" in str(h) or "bigmodel" in str(h) for h in allow), allow
