"""Codex provider contract: the live quota reading (CX-C11 revised, 2026-09-28).

`budget` starts `codex app-server` under the executor-implied profile (the
CX-C9 rule), sends `initialize`, then `account/rateLimits/read`, and shuts it
down. The whole exchange is bounded to 7 s; on timeout the process group is
killed. Only if the live read fails (non-zero exit, timeout, JSON-RPC error,
not logged in, unparseable response) does the rollout reading of CX-C11 apply,
with `source: "rollout"` and a one-line `note` saying why.

The native CLI is `FakeCodexAppServer` (support/codex_harness.py). The wire
assumptions (newline-delimited JSON-RPC, the handshake, the method name) live
in ONE block of that file, `APP_SERVER_FRAMING` and its neighbours, pending the
live check L5.

Test names carry `cx_c11r` (CX-C11 revised) so coverage can be grepped apart
from the rollout tests in test_codex_provider_budget.py.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import codex_harness as h                              # noqa: E402

# The contract bounds the exchange to 7 s, inside the engine's 10 s action
# timeout. The adapter's whole run (exchange + rollout fallback) must fit
# inside the engine's timeout, so that is the bound asserted.
EXCHANGE_BOUND = 7.0
ENGINE_TIMEOUT = 10.0


@pytest.fixture
def fake(tmp_path):
    return h.FakeCodexAppServer(tmp_path)


@pytest.fixture
def profile(tmp_path):
    return tmp_path / "profile"


def run_budget(tmp_path, fake, **extra):
    """Run `budget`; return (parsed JSON, completed process, elapsed seconds)."""
    started = time.monotonic()
    result = h.invoke(["budget"], h.base_env(tmp_path, fake, **extra), timeout=30)
    elapsed = time.monotonic() - started
    assert result.returncode == 0, result.stderr
    assert "Traceback" not in result.stderr
    data = json.loads(result.stdout)
    assert isinstance(data, dict)
    # Reading the quota never spends it: no model call.
    assert fake.exec_calls() == []
    return data, result, elapsed


def budget(tmp_path, fake, **extra) -> dict:
    return run_budget(tmp_path, fake, **extra)[0]


def live(fake, primary, secondary, *, reached=None, by_limit_id=None):
    fake.app_server(mode="ok", result=h.rate_limits_response(
        h.rl_snapshot(primary, secondary, limit_id="codex", reached=reached), by_limit_id))


def percent(data, name):
    return data["windows"][name]["percent"]


def resets(data, name):
    return h.parse_instant(data["windows"][name]["resets_at"])


def rollout_reading(profile, now, p5=11.0, pw=22.0, age=30):
    """A valid rollout reading, distinguishable from any live one."""
    h.write_rollout(profile, "r", [h.token_count_line(
        now - age, h.window(p5, 300, int(now + 3600)), h.window(pw, 10080, int(now + 86400)))])


def assert_one_line_note(data, tmp_path):
    note = data.get("note")
    assert isinstance(note, str) and note.strip(), f"no note saying why: {data!r}"
    assert "\n" not in note.strip() and "\r" not in note, f"note is not one line: {note!r}"
    # No credentials and no response bodies (the fake puts SECRET in error
    # data, garbage and stderr), no path inside the user's home.
    assert h.SECRET not in note
    assert str(tmp_path) not in note


def assert_all_dead(pids, within=2.0):
    deadline = time.monotonic() + within
    while time.monotonic() < deadline and not all(h.process_gone(p) for p in pids):
        time.sleep(0.05)
    alive = [p for p in pids if not h.process_gone(p)]
    assert not alive, f"app-server process group survived the budget action: {alive}"


# ------------------------------------------------------- single bucket --

def test_cx_c11r_single_bucket_windows_percent_reset_and_headroom(tmp_path, fake):
    now = time.time()
    r5, rw = int(now + 3600), int(now + 4 * 86400)
    live(fake, h.rl_window(25, 300, r5), h.rl_window(60, 10080, rw))
    data = budget(tmp_path, fake)
    assert data["known"] is True
    assert set(data["windows"]) == {"5h", "weekly"}
    assert percent(data, "5h") == pytest.approx(25)
    assert percent(data, "weekly") == pytest.approx(60)
    assert resets(data, "5h") == pytest.approx(r5, abs=1)
    assert resets(data, "weekly") == pytest.approx(rw, abs=1)
    assert data["headroom"] == pytest.approx(0.40, abs=1e-6)
    # resets_at of the worst window, ISO 8601 UTC.
    assert h.parse_instant(data["resets_at"]) == pytest.approx(rw, abs=1)


def test_cx_c11r_source_is_app_server_and_stale_seconds_zero(tmp_path, fake):
    now = time.time()
    live(fake, h.rl_window(10, 300, int(now + 3600)), h.rl_window(20, 10080, int(now + 86400)))
    data = budget(tmp_path, fake)
    assert data["source"] == "app-server"
    assert data["stale_seconds"] == 0


def test_cx_c11r_live_reading_is_primary_over_a_rollout_reading(tmp_path, fake, profile):
    now = time.time()
    rollout_reading(profile, now, p5=91.0, pw=92.0, age=1)
    live(fake, h.rl_window(15, 300, int(now + 3600)), h.rl_window(35, 10080, int(now + 86400)))
    data = budget(tmp_path, fake)
    assert data["source"] == "app-server"
    assert percent(data, "5h") == pytest.approx(15)
    assert percent(data, "weekly") == pytest.approx(35)
    assert data["headroom"] == pytest.approx(0.65, abs=1e-6)


def test_cx_c11r_windows_named_by_duration_not_position(tmp_path, fake):
    now = time.time()
    live(fake, h.rl_window(70, 10080, int(now + 86400)), h.rl_window(10, 300, int(now + 600)))
    data = budget(tmp_path, fake)
    assert percent(data, "weekly") == pytest.approx(70)
    assert percent(data, "5h") == pytest.approx(10)


def test_cx_c11r_other_durations_are_named_in_minutes(tmp_path, fake):
    now = time.time()
    r60, r1440 = int(now + 1200), int(now + 40000)
    live(fake, h.rl_window(45, 60, r60), h.rl_window(80, 1440, r1440))
    data = budget(tmp_path, fake)
    assert set(data["windows"]) == {"60m", "1440m"}
    assert percent(data, "60m") == pytest.approx(45)
    assert percent(data, "1440m") == pytest.approx(80)
    assert data["headroom"] == pytest.approx(0.20, abs=1e-6)
    assert h.parse_instant(data["resets_at"]) == pytest.approx(r1440, abs=1)


def test_cx_c11r_worst_window_decides_headroom_and_reset(tmp_path, fake):
    now = time.time()
    r5 = int(now + 1800)
    live(fake, h.rl_window(88, 300, r5), h.rl_window(20, 10080, int(now + 86400)))
    data = budget(tmp_path, fake)
    assert data["headroom"] == pytest.approx(0.12, abs=1e-6)
    assert h.parse_instant(data["resets_at"]) == pytest.approx(r5, abs=1)


@pytest.mark.parametrize("used, headroom", [(0, 1.0), (100, 0.0)])
def test_cx_c11r_headroom_at_the_percent_boundaries(tmp_path, fake, used, headroom):
    now = time.time()
    live(fake, h.rl_window(used, 300, int(now + 3600)), h.rl_window(used, 10080, int(now + 86400)))
    data = budget(tmp_path, fake)
    assert data["known"] is True
    assert data["headroom"] == pytest.approx(headroom, abs=1e-6)


def test_cx_c11r_a_null_window_is_simply_absent(tmp_path, fake):
    now = time.time()
    r5 = int(now + 3600)
    live(fake, h.rl_window(30, 300, r5), None)
    data = budget(tmp_path, fake)
    assert data["source"] == "app-server"
    assert set(data["windows"]) == {"5h"}
    assert data["headroom"] == pytest.approx(0.70, abs=1e-6)
    assert h.parse_instant(data["resets_at"]) == pytest.approx(r5, abs=1)


# -------------------------------------------------------- multi bucket --

def _two_buckets(now, reached_other=None):
    codex = h.rl_snapshot(h.rl_window(30, 300, int(now + 3600)),
                          h.rl_window(40, 10080, int(now + 5 * 86400)), limit_id="codex")
    other = h.rl_snapshot(h.rl_window(70, 300, int(now + 1500)),
                          h.rl_window(10, 10080, int(now + 6 * 86400)),
                          limit_id="codex_bengalfox", reached=reached_other)
    return codex, other


def test_cx_c11r_multi_bucket_windows_are_prefixed_by_limit_id(tmp_path, fake):
    now = time.time()
    codex, other = _two_buckets(now)
    # `rateLimits` mirrors the `codex` bucket, as the schema says it does.
    fake.app_server(mode="ok", result=h.rate_limits_response(
        codex, {"codex": codex, "codex_bengalfox": other}))
    data = budget(tmp_path, fake)
    assert data["source"] == "app-server"
    windows = data["windows"]
    for name in ("codex-5h", "codex-weekly", "codex_bengalfox-5h", "codex_bengalfox-weekly"):
        assert name in windows, f"{name} missing from {sorted(windows)}"
    assert percent(data, "codex-5h") == pytest.approx(30)
    assert percent(data, "codex-weekly") == pytest.approx(40)
    assert percent(data, "codex_bengalfox-5h") == pytest.approx(70)
    assert percent(data, "codex_bengalfox-weekly") == pytest.approx(10)
    assert resets(data, "codex_bengalfox-5h") == pytest.approx(int(now + 1500), abs=1)
    # Headroom is the worst across all buckets, not the `codex` one alone.
    assert data["headroom"] == pytest.approx(0.30, abs=1e-6)
    assert h.parse_instant(data["resets_at"]) == pytest.approx(int(now + 1500), abs=1)


# -------------------------------------------------------- limit reached --

def test_cx_c11r_limit_reached_means_zero_headroom(tmp_path, fake):
    now = time.time()
    live(fake, h.rl_window(40, 300, int(now + 3600)), h.rl_window(50, 10080, int(now + 86400)),
         reached="rate_limit_reached")
    data = budget(tmp_path, fake)
    assert data["known"] is True
    assert data["source"] == "app-server"
    assert data["headroom"] == 0


def test_cx_c11r_limit_reached_in_any_bucket_means_zero_headroom(tmp_path, fake):
    now = time.time()
    codex, other = _two_buckets(now, reached_other="workspace_member_usage_limit_reached")
    fake.app_server(mode="ok", result=h.rate_limits_response(
        codex, {"codex": codex, "codex_bengalfox": other}))
    data = budget(tmp_path, fake)
    assert data["source"] == "app-server"
    assert data["headroom"] == 0


# ------------------------------------------------------ nothing else out --

@pytest.mark.parametrize("buckets", ["single", "multi"])
def test_cx_c11r_emits_no_account_id_credits_balance_or_upsell(tmp_path, fake, buckets):
    now = time.time()
    codex, other = _two_buckets(now)
    by_id = {"codex": codex, "codex_bengalfox": other} if buckets == "multi" else None
    fake.app_server(mode="ok", result=h.rate_limits_response(codex, by_id))
    data, result, _ = run_budget(tmp_path, fake)
    assert data["source"] == "app-server"
    out = result.stdout + result.stderr
    for leak in (h.ACCOUNT_ID, h.CREDITS_BALANCE, h.UPSELL_TEXT, "UPSELL-LEAK"):
        assert leak not in out, f"{leak!r} emitted"
    for key in ("accountId", "balance", "rateLimitUpsell"):
        assert key not in result.stdout, f"{key!r} emitted"


# ------------------------------------------------------------- protocol --

def test_cx_c11r_handshake_initialize_initialized_then_read(tmp_path, fake):
    """The exchange as assumed in the harness's wire block (to confirm in L5)."""
    now = time.time()
    live(fake, h.rl_window(10, 300, int(now + 3600)), h.rl_window(20, 10080, int(now + 86400)))
    budget(tmp_path, fake)
    got = [m for m in fake.received() if isinstance(m, dict)]
    methods = [m.get("method") for m in got]
    request, notification = h.APP_SERVER_HANDSHAKE
    assert h.RATE_LIMITS_METHOD in methods, methods
    i_init, i_note, i_read = (methods.index(request), methods.index(notification),
                              methods.index(h.RATE_LIMITS_METHOD))
    assert i_init < i_note < i_read, methods
    init, note, read = got[i_init], got[i_note], got[i_read]
    info = (init.get("params") or {}).get("clientInfo")
    assert isinstance(info, dict) and isinstance(info.get("name"), str) \
        and isinstance(info.get("version"), str), init
    assert "id" in init and "id" in read and init["id"] != read["id"]
    assert "id" not in note, "`initialized` is a notification, not a request"


def test_cx_c11r_app_server_runs_with_the_update_check_off(tmp_path, fake):
    now = time.time()
    live(fake, h.rl_window(10, 300, int(now + 3600)), None)
    budget(tmp_path, fake)
    calls = fake.app_server_calls()
    assert len(calls) == 1, calls
    assert h.update_check_disabled(calls[0]["argv"]), calls[0]["argv"]


@pytest.mark.parametrize("linger", [False, True], ids=["exits-on-eof", "lingers-after-eof"])
def test_cx_c11r_app_server_is_shut_down_after_a_successful_read(tmp_path, fake, linger):
    """"...and shuts it down": the whole process group, even one that ignores EOF."""
    now = time.time()
    live(fake, h.rl_window(10, 300, int(now + 3600)), None)
    fake.app_server(grandchild=True, linger_after_eof=linger)
    data, _, elapsed = run_budget(tmp_path, fake)
    assert data["source"] == "app-server"
    assert elapsed < ENGINE_TIMEOUT - 0.5
    pids = fake.pids()
    assert len(pids) == 2, f"fake did not start with its grandchild: {pids}"
    assert_all_dead(pids)


# ------------------------------------------------------------ fallbacks --

FAILURES = {
    # JSON-RPC error on the read, and on initialize.
    "rpc-error": dict(mode="rpc_error"),
    "rpc-error-on-initialize": dict(mode="init_error"),
    # Not logged in: the read is refused, and `login status` agrees.
    "not-logged-in": dict(mode="not_logged_in"),
    # Garbage output in place of every response.
    "garbage-text": dict(mode="garbage", garbage="Welcome to codex! " + h.SECRET),
    "garbage-binary": dict(mode="garbage", garbage="\x00\x01{{{" + h.SECRET),
    # Well-framed responses whose result is unparseable as rate limits.
    "result-null": dict(mode="ok", result=None),
    "result-without-rateLimits": dict(mode="ok", result={"accountId": h.ACCOUNT_ID}),
    "rateLimits-not-an-object": dict(mode="ok", result={"rateLimits": "lots " + h.SECRET}),
    # A non-zero exit before any response.
    "non-zero-exit": dict(mode="exit", exit=3),
}


@pytest.mark.parametrize("kind", list(FAILURES))
def test_cx_c11r_live_failure_falls_back_to_rollout_with_a_note(tmp_path, fake, profile, kind):
    now = time.time()
    rollout_reading(profile, now, p5=11.0, pw=22.0, age=30)
    fake.app_server(**FAILURES[kind])
    if kind == "not-logged-in":
        fake.set(status="not_logged_in")
    data, result, _ = run_budget(tmp_path, fake)
    assert fake.app_server_calls(), "the live read was never attempted"
    assert data["known"] is True
    assert data["source"] == "rollout"
    assert percent(data, "5h") == pytest.approx(11.0)
    assert percent(data, "weekly") == pytest.approx(22.0)
    assert data["headroom"] == pytest.approx(0.78, abs=1e-6)
    assert data["stale_seconds"] == pytest.approx(30, abs=15)
    assert_one_line_note(data, tmp_path)
    assert h.SECRET not in result.stdout + result.stderr
    assert h.ACCOUNT_ID not in result.stdout


def test_cx_c11r_live_failure_without_a_rollout_reading_is_unknown(tmp_path, fake):
    fake.app_server(mode="rpc_error")
    data = budget(tmp_path, fake)
    assert data["known"] is False
    assert data.get("source") != "app-server"
    assert_one_line_note(data, tmp_path)


@pytest.mark.parametrize("ignore_sigterm", [False, True], ids=["plain", "ignores-sigterm"])
@pytest.mark.parametrize("hang", ["hang_silent", "hang_after_initialize"])
def test_cx_c11r_a_hang_is_killed_within_the_bound_and_falls_back(tmp_path, fake, profile,
                                                                  hang, ignore_sigterm):
    now = time.time()
    rollout_reading(profile, now, p5=11.0, pw=22.0, age=30)
    fake.app_server(mode=hang, ignore_sigterm=ignore_sigterm)
    data, result, elapsed = run_budget(tmp_path, fake)
    assert elapsed < ENGINE_TIMEOUT - 0.5, (
        f"budget took {elapsed:.1f}s; the exchange is bounded to {EXCHANGE_BOUND}s "
        f"inside the engine's {ENGINE_TIMEOUT}s action timeout")
    pids = fake.pids()
    assert len(pids) == 2, f"fake did not start with its grandchild: {pids}"
    assert_all_dead(pids)
    assert data["known"] is True
    assert data["source"] == "rollout"
    assert percent(data, "5h") == pytest.approx(11.0)
    assert_one_line_note(data, tmp_path)


# -------------------------------------------------------------- profile --

def _home_of_the_live_read(fake):
    calls = fake.app_server_calls()
    assert len(calls) == 1, calls
    return calls[0]["codex_home"]


def _ok(fake):
    now = time.time()
    live(fake, h.rl_window(10, 300, int(now + 3600)), None)


def test_cx_c11r_local_uses_the_codex_profile_variable(tmp_path, fake):
    _ok(fake)
    second = tmp_path / "second-account"
    budget(tmp_path, fake, MULTIAGENTS_CODEX_PROFILE=str(second))
    assert _home_of_the_live_read(fake) == str(second)


def test_cx_c11r_local_default_profile(tmp_path, fake):
    _ok(fake)
    env = h.base_env(tmp_path, fake)
    env.pop("MULTIAGENTS_CODEX_PROFILE")
    result = h.invoke(["budget"], env, timeout=30)
    assert result.returncode == 0, result.stderr
    assert _home_of_the_live_read(fake) == str(
        tmp_path / "home" / ".multiagents" / "profiles" / "codex")


def test_cx_c11r_docker_uses_the_private_backing(tmp_path, fake):
    _ok(fake)
    backing = tmp_path / "backing"
    backing.mkdir()
    budget(tmp_path, fake, MULTIAGENTS_EXECUTOR="docker",
           MULTIAGENTS_PRIVATE_BACKING=str(backing))
    assert _home_of_the_live_read(fake) == str(backing)


def test_cx_c11r_docker_with_profile_host_uses_the_host_profile(tmp_path, fake):
    _ok(fake)
    budget(tmp_path, fake, MULTIAGENTS_EXECUTOR="docker",
           MULTIAGENTS_PRIVATE_BACKING=str(tmp_path / "backing"), MULTIAGENTS_PROFILE="host")
    assert _home_of_the_live_read(fake) == str(tmp_path / "profile")


def test_cx_c11r_an_ambient_codex_home_is_ignored(tmp_path, fake):
    _ok(fake)
    own = tmp_path / "home" / ".codex"
    budget(tmp_path, fake, CODEX_HOME=str(own))
    assert _home_of_the_live_read(fake) == str(tmp_path / "profile")
