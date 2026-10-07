"""Codex provider contract: the adapter review at 8bf298b (CX-C21..CX-C26).

Black box, as the rest of the codex suite: the adapter runs as an executable
with an explicit environment, the native CLI is the fake of
`support/codex_harness.py`, and only its stdout/stderr/exit status and what
the fake records (argv, CODEX_HOME) are asserted on.

Not covered here (see the tester's result): the `internal` note class of
CX-C24, which needs a programming error inside the live read and has no
black-box trigger; and the cleanup items of the review, which the spec marks
"not tested".

CX-C25 covers only its URL half. Its env allowlist was withdrawn (spec, last
section, 288f685): the multiagents entry's `env` reaches Codex through `-c`
by design, and test_codex_provider_edges.py::test_cx_c10_hostile_mcp_env_values_inject_no_config_keys
requires it verbatim there.
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import codex_harness as h                              # noqa: E402
from multiagents.providers import RESULT                            # noqa: E402


HAPPY = [
    {"type": "thread.started", "thread_id": "thread-1"},
    {"type": "turn.started"},
    {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}},
]

REFRESH_EXPIRED = ("Your access token could not be refreshed because your refresh "
                   "token has expired. Please log out and sign in again.")

NOTE_CLASSES = ("timeout", "exit", "jsonrpc-error", "not-logged-in", "unparseable",
                "internal")


@pytest.fixture
def fake(tmp_path):
    return h.FakeCodexAppServer(tmp_path)


@pytest.fixture
def profile(tmp_path):
    return tmp_path / "profile"


# ------------------------------------------------------------- helpers --

def run_agent(tmp_path, fake, *, mcp_config=None, **env_extra):
    """Render argv with the real builder, then run the adapter in place of `bin`."""
    prov = h.provider()
    workdir = tmp_path / "work"
    workdir.mkdir(parents=True, exist_ok=True)
    argv = prov.build_command(prompt="Do the task.", model="test-model",
                              workdir=str(workdir), permission="sandbox",
                              session_id=None, options={}, timeout=600)
    if mcp_config is not None:
        argv += prov.mcp_launch({"mcp_command": "python3", "mcp_args": [],
                                 "mcp_argv": ["python3"], "mcp_env": {},
                                 "mcp_config": str(mcp_config)})["args"]
    result = h.invoke(argv[1:], h.base_env(tmp_path, fake, **env_extra), cwd=workdir)
    assert "Traceback" not in result.stderr, result.stderr[-2000:]
    return prov, result


def emitted(result) -> list[dict]:
    """Every JSON object the adapter wrote to stdout (its normalized events)."""
    out = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        value = json.loads(line)
        assert isinstance(value, dict), f"adapter emitted a non-object: {line!r}"
        out.append(value)
    return out


def the_exec(fake) -> dict:
    calls = fake.exec_calls()
    assert len(calls) == 1, f"expected exactly one `codex exec`, got {[c['argv'] for c in calls]}"
    return calls[0]


def run_budget(tmp_path, fake, **extra):
    result = h.invoke(["budget"], h.base_env(tmp_path, fake, **extra), timeout=30)
    assert "Traceback" not in result.stderr, result.stderr[-2000:]
    assert result.returncode == 0, (result.returncode, result.stderr)
    data = json.loads(result.stdout)
    assert isinstance(data, dict)
    assert fake.exec_calls() == []
    return data, result


def live(fake, primary, secondary):
    fake.app_server(mode="ok", result=h.rate_limits_response(
        h.rl_snapshot(primary, secondary, limit_id="codex")))


def rollout_reading(profile, now, p5=11.0, pw=22.0, age=30, name="r"):
    h.write_rollout(profile, name, [h.token_count_line(
        now - age, h.window(p5, 300, int(now + 3600)), h.window(pw, 10080, int(now + 86400)))])


def classes_named(note: str) -> set[str]:
    return {c for c in NOTE_CLASSES if re.search(rf"(?<![\w-]){re.escape(c)}(?![\w-])", note)}


# ================================================================ CX-C21 ==

@pytest.mark.parametrize("minutes", [None, 60, 300], ids=["null", "equal-60", "equal-300"])
def test_cx_c21_live_windows_with_the_same_duration_do_not_collide(tmp_path, fake, minutes):
    now = time.time()
    r1, r2 = int(now + 3600), int(now + 7200)
    live(fake, h.rl_window(90, minutes, r1), h.rl_window(10, minutes, r2))
    data, _ = run_budget(tmp_path, fake)
    assert data["known"] is True, data
    windows = data["windows"]
    assert len(windows) == 2, f"two windows collapsed into one name: {windows!r}"
    primary = [n for n in windows if n.endswith("-primary")]
    secondary = [n for n in windows if n.endswith("-secondary")]
    assert len(primary) == 1 and len(secondary) == 1, sorted(windows)
    assert windows[primary[0]]["percent"] == pytest.approx(90)
    assert windows[secondary[0]]["percent"] == pytest.approx(10)
    # Headroom is the worst of all windows: 1 - 0.90, never 1 - 0.10.
    assert data["headroom"] == pytest.approx(0.10, abs=1e-6)
    assert h.parse_instant(data["resets_at"]) == pytest.approx(r1, abs=1)


@pytest.mark.parametrize("order", ["worst-primary", "worst-secondary"])
def test_cx_c21_headroom_is_the_worst_window_whichever_side_it_is(tmp_path, fake, order):
    now = time.time()
    hi, lo = h.rl_window(85, None, int(now + 3600)), h.rl_window(5, None, int(now + 3600))
    live(fake, *((hi, lo) if order == "worst-primary" else (lo, hi)))
    data, _ = run_budget(tmp_path, fake)
    assert data["known"] is True, data
    assert data["headroom"] == pytest.approx(0.15, abs=1e-6)


def test_cx_c21_rollout_windows_with_equal_durations_do_not_collide(tmp_path, fake, profile):
    fake.app_server(mode="exit")                    # the live read fails: rollout path
    now = time.time()
    h.write_rollout(profile, "r", [h.token_count_line(
        now - 30, h.window(90.0, 60, int(now + 3600)), h.window(10.0, 60, int(now + 7200)))])
    data, _ = run_budget(tmp_path, fake)
    assert data["known"] is True, data
    assert data["source"] == "rollout"
    windows = data["windows"]
    assert len(windows) == 2, f"two windows collapsed into one name: {windows!r}"
    assert sorted(w["percent"] for w in windows.values()) == pytest.approx([10.0, 90.0])
    assert data["headroom"] == pytest.approx(0.10, abs=1e-6)


# ================================================================ CX-C22 ==

def test_cx_c22_login_instructions_reach_a_piped_stdout_before_the_cli(tmp_path, fake):
    """`codex.py login | cat`: stdout is a pipe (as it is here). The adapter's
    own instruction line must come out, and before the native CLI's output."""
    result = h.invoke(["login"], h.base_env(tmp_path, fake))
    assert result.returncode == 0, result.stderr
    lines = [l for l in result.stdout.splitlines() if l.strip()]
    native = [i for i, l in enumerate(lines) if "auth.example/device" in l]
    assert native, f"the native login did not run: {result.stdout!r}"
    ours = lines[:native[0]]
    assert ours, f"no instruction line before the native CLI's output: {result.stdout!r}"


# ================================================================ CX-C23 ==

# Loaded into the adapter's interpreter through PYTHONPATH. The first stat of
# the file named by CODEX_TEST_VANISH returns its real result and then deletes
# it: the file is seen by the listing and gone by the time anything looks
# again. That is the race of review finding 3, made deterministic.
VANISH_SITECUSTOMIZE = r'''
import os
_name = os.environ.get("CODEX_TEST_VANISH")
_mark = os.environ.get("CODEX_TEST_VANISH_LOG")
if _name:
    _real_stat = os.stat
    _done = []
    def stat(path, *args, **kwargs):
        result = _real_stat(path, *args, **kwargs)
        try:
            text = os.fsdecode(path)
        except TypeError:
            return result
        if not _done and text.endswith(_name):
            _done.append(True)
            os.unlink(text)
            if _mark:
                with open(_mark, "a") as fh:
                    fh.write("vanished\n")
        return result
    os.stat = stat
'''


def test_cx_c23_a_rollout_file_vanishing_after_the_listing_still_exits_0(tmp_path, fake,
                                                                          profile):
    fake.app_server(mode="exit")                    # the live read fails: rollout path
    now = time.time()
    rollout_reading(profile, now, name="stays")
    doomed = h.write_rollout(profile, "vanish", [h.token_count_line(
        now - 10, h.window(50.0, 300, int(now + 3600)), None)])
    site = tmp_path / "site"
    site.mkdir()
    (site / "sitecustomize.py").write_text(VANISH_SITECUSTOMIZE)
    mark = tmp_path / "vanished.log"
    result = h.invoke(["budget"], h.base_env(
        tmp_path, fake, PYTHONPATH=str(site), CODEX_TEST_VANISH=doomed.name,
        CODEX_TEST_VANISH_LOG=str(mark)), timeout=30)
    assert mark.exists(), "the fixture never saw the rollout file: nothing was tested"
    assert not doomed.exists()
    assert "Traceback" not in result.stderr, result.stderr[-2000:]
    assert result.returncode == 0, (result.returncode, result.stderr)
    data = json.loads(result.stdout)
    assert isinstance(data, dict) and isinstance(data.get("known"), bool), data


# ================================================================ CX-C24 ==

FAILURES = {
    "exit": dict(mode="exit"),
    "jsonrpc-error": dict(mode="rpc_error"),
    "not-logged-in": dict(mode="not_logged_in"),
    "unparseable": dict(mode="garbage", garbage="not json at all " + h.SECRET),
    "timeout": dict(mode="hang_silent"),
}


@pytest.mark.parametrize("expected", list(FAILURES))
def test_cx_c24_the_fallback_note_names_the_failure_class(tmp_path, fake, profile, expected):
    fake.app_server(**FAILURES[expected])
    rollout_reading(profile, time.time())
    data, _ = run_budget(tmp_path, fake)
    assert data["known"] is True, data
    assert data["source"] == "rollout"
    note = data.get("note")
    assert isinstance(note, str) and note.strip(), f"no note: {data!r}"
    assert "\n" not in note.strip() and "\r" not in note, f"note is not one line: {note!r}"
    assert h.SECRET not in note, "a response body reached the note"
    assert str(tmp_path) not in note, "a path reached the note"
    assert classes_named(note) == {expected}, f"note {note!r} should name {expected!r}"


def test_cx_c24_a_jsonrpc_error_on_initialize_is_a_jsonrpc_error(tmp_path, fake, profile):
    fake.app_server(mode="init_error")
    rollout_reading(profile, time.time())
    data, _ = run_budget(tmp_path, fake)
    assert data["source"] == "rollout"
    assert classes_named(data.get("note") or "") == {"jsonrpc-error"}, data.get("note")


# ================================================================ CX-C25 ==

URL_TOKEN = "URLTOKEN-SECRET-8c1f"
URL_PASSWORD = "URLPASS-SECRET-3b2e"


@pytest.mark.parametrize("url", [
    f"https://mcp.example/sse?token={URL_TOKEN}",
    f"https://someone:{URL_PASSWORD}@mcp.example/sse",
    f"https://someone:{URL_PASSWORD}@mcp.example/sse?token={URL_TOKEN}",
], ids=["query-token", "userinfo", "both"])
def test_cx_c25_an_inherited_server_url_never_reaches_the_native_argv(tmp_path, fake, url):
    fake.set(events=HAPPY, mcp_list=json.dumps([
        {"name": "remote", "transport": {"type": "streamable_http", "url": url}}]))
    _, result = run_agent(tmp_path, fake)
    assert result.returncode == 0, result.stderr
    native = the_exec(fake)["argv"]
    flat = "\0".join(native)
    assert URL_TOKEN not in flat and URL_PASSWORD not in flat, native
    for call in fake.calls():
        joined = "\0".join(call["argv"])
        assert URL_TOKEN not in joined and URL_PASSWORD not in joined, call["argv"]
    # ... and the inherited server is still disabled.
    servers = h.lookup(h.native_config(native), "mcp_servers")
    assert isinstance(servers, dict) and "remote" in servers, servers
    assert servers["remote"].get("enabled") is False, servers["remote"]


# ================================================================ CX-C26 ==

@pytest.mark.parametrize("thread_id", [
    {"id": "OBJ-SENTINEL"}, ["OBJ-SENTINEL"], 4242, True, None,
], ids=["object", "list", "int", "bool", "null"])
def test_cx_c26_a_non_string_thread_id_is_ignored(tmp_path, fake, thread_id):
    fake.set(events=[{"type": "thread.started", "thread_id": thread_id}] + HAPPY[1:])
    _, result = run_agent(tmp_path, fake)
    events = emitted(result)
    assert events, result.stderr
    for event in events:
        if "session_id" in event:
            sid = event["session_id"]
            assert isinstance(sid, str), f"session_id is not a string: {event!r}"
            assert sid not in ("4242", "True", "None"), event
    assert "OBJ-SENTINEL" not in result.stdout


def test_cx_c26_a_later_string_thread_id_is_still_taken(tmp_path, fake):
    fake.set(events=[{"type": "thread.started", "thread_id": {"id": "x"}},
                     {"type": "thread.started", "thread_id": "thread-good"}] + HAPPY[1:])
    _, result = run_agent(tmp_path, fake)
    assert result.returncode == 0, result.stderr
    finals = [e for e in emitted(result) if e.get("kind") == "result"]
    assert finals and finals[-1].get("session_id") == "thread-good", finals


RAW_SENTINEL = "RAW-SENTINEL-unmapped-e41c"


def test_cx_c26_mapped_events_do_not_carry_the_raw_codex_object(tmp_path, fake):
    extra = {"x_unmapped": RAW_SENTINEL}
    stream = [
        {"type": "thread.started", "thread_id": "thread-1", **extra},
        {"type": "turn.started", **extra},
        {"type": "item.started", "item": {"id": "c1", "type": "command_execution",
                                          "command": "ls", "status": "in_progress", **extra},
         **extra},
        {"type": "item.completed", "item": {"id": "m1", "type": "agent_message",
                                            "text": "hello", **extra}, **extra},
        {"type": "error", "message": "stream hiccup, retrying", **extra},
        {"type": "turn.completed", "usage": {"input_tokens": 3, "output_tokens": 2}, **extra},
    ]
    fake.set(events=stream)
    prov, result = run_agent(tmp_path, fake)
    assert result.returncode == 0, result.stderr
    events = emitted(result)
    assert any(e.get("kind") == "text" and e.get("text") == "hello" for e in events), events
    for event in events:
        for key, value in event.items():
            assert value not in stream and value not in [s.get("item") for s in stream], \
                f"event key {key!r} carries a raw Codex object: {event!r}"
    assert RAW_SENTINEL not in result.stdout, "an unmapped Codex field reached the events"
    results = [e for e in h.events(prov, result.stdout) if e.kind == RESULT]
    assert results and results[-1].status == "success"


def test_cx_c26_an_auth_marker_in_a_retried_error_does_not_fail_the_run(tmp_path, fake):
    fake.set(events=[
        {"type": "thread.started", "thread_id": "thread-1"},
        {"type": "turn.started"},
        {"type": "error", "message": REFRESH_EXPIRED + " Retrying (1/5)"},
        {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}},
    ], exit=0)
    prov, result = run_agent(tmp_path, fake)
    assert result.returncode == 0, (result.returncode, result.stderr)
    assert h.AUTH_LINE not in result.stderr.splitlines(), result.stderr
    results = [e for e in h.events(prov, result.stdout) if e.kind == RESULT]
    assert results and results[-1].status == "success", results


def test_cx_c26_an_auth_failure_in_the_final_result_is_still_one(tmp_path, fake):
    """The other side of the same rule: the final result decides."""
    fake.set(events=[
        {"type": "thread.started", "thread_id": "thread-1"},
        {"type": "turn.started"},
        {"type": "turn.failed", "error": {"message": REFRESH_EXPIRED}},
    ], exit=1)
    _, result = run_agent(tmp_path, fake)
    assert result.returncode != 0
    assert h.AUTH_LINE in result.stderr.splitlines(), result.stderr
