"""Codex provider contract: edge inputs the main suites never use (CX-C8..C13).

Black box, as the other `test_codex_provider*` files: the adapter is executed
with an explicit environment, and the native CLI is the fake from
`support/codex_harness.py`. This file adds only two things to that fake,
defined below:

- `raw`: `codex exec` writes exact bytes, interleaving stdout and stderr, so a
  test can send non-UTF-8, a partial last line, or JSON that `json.dumps`
  cannot produce (`1e999`);
- `endless`: `codex app-server` writes a newline-free stream that never ends.

Every test names the contract id it targets. A red test here is a finding.
"""

from __future__ import annotations

import base64
import json
import os
import re
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import codex_harness as h                              # noqa: E402
from multiagents.providers import RAW, RESULT, TEXT, TOOL           # noqa: E402
from multiagents.supervisor import looks_like_quota_failure         # noqa: E402
from multiagents.tree import token_count                            # noqa: E402

SID = "0199a213-81c0-7800-8aa1-bbab2a035a53"

HAPPY = [
    {"type": "thread.started", "thread_id": "thread-1"},
    {"type": "turn.started"},
    {"type": "item.completed", "item": {"id": "a", "type": "agent_message", "text": "done"}},
    {"type": "turn.completed",
     "usage": {"input_tokens": 100, "cached_input_tokens": 70, "output_tokens": 10}},
]

# ------------------------------------------------------------- the fake --

_RAW_EXEC = r'''
if "exec" in argv and B.get("raw") is not None:
    import base64
    for channel, chunk in B["raw"]:
        stream = sys.stdout.buffer if channel == "out" else sys.stderr.buffer
        stream.write(base64.b64decode(chunk))
        stream.flush()
    sys.exit(B.get("exit", 0))
'''

_ENDLESS_APP_SERVER = r'''
if "app-server" in argv and B.get("endless") is not None:
    with open(HERE / "pids.txt", "a") as fh:
        fh.write(f"{os.getpid()}\n")
    chunk = b"x" * 65536
    written = 0
    try:
        while written < B["endless"]:
            sys.stdout.buffer.write(chunk)
            sys.stdout.buffer.flush()
            written += len(chunk)
    except BrokenPipeError:
        sys.exit(0)
    time.sleep(600)
'''

_APP_SERVER_START = '\nif "app-server" in argv:\n'
assert _APP_SERVER_START in h.FAKE_CODEX_APP_SERVER
assert h._MARK in h.FAKE_CODEX_APP_SERVER
FAKE_EDGES = (h.FAKE_CODEX_APP_SERVER
              .replace(_APP_SERVER_START, "\n" + _ENDLESS_APP_SERVER + _APP_SERVER_START, 1)
              .replace(h._MARK, "\n" + _RAW_EXEC + h._MARK, 1))


class EdgeFake(h.FakeCodexAppServer):
    """`FakeCodexAppServer`, plus the `raw` exec and the `endless` app-server."""

    def __init__(self, root: Path):
        super().__init__(root)
        self.path.write_text(FAKE_EDGES)
        # Without a configured result the app-server reading fails, so the
        # budget action falls back to the rollout files unless a test says so.
        self.app_server(mode="exit")

    def raw(self, *chunks: tuple[str, bytes | str]) -> None:
        self.set(raw=[[channel, base64.b64encode(
            data if isinstance(data, bytes) else data.encode()).decode()]
            for channel, data in chunks])


def line(obj) -> bytes:
    return (obj if isinstance(obj, str) else json.dumps(obj)).encode() + b"\n"


@pytest.fixture
def fake(tmp_path):
    return EdgeFake(tmp_path)


@pytest.fixture
def profile(tmp_path):
    return tmp_path / "profile"


# ------------------------------------------------------------- helpers --

def run_agent(tmp_path, fake, *, permission="sandbox", session_id=None, options=None,
              mcp_config=None, prompt="Do the task.", model="test-model",
              workdir=None, **env_extra):
    """Render argv with the real builder, then run the adapter in place of `bin`."""
    prov = h.provider()
    workdir = workdir or tmp_path / "work"
    workdir.mkdir(parents=True, exist_ok=True)
    argv = prov.build_command(prompt=prompt, model=model, workdir=str(workdir),
                              permission=permission, session_id=session_id,
                              options=options or {}, timeout=600)
    if mcp_config is not None:
        argv += prov.mcp_launch({"mcp_command": "python3", "mcp_args": [],
                                 "mcp_argv": ["python3"], "mcp_env": {},
                                 "mcp_config": str(mcp_config)})["args"]
    env = h.base_env(tmp_path, fake, **env_extra)
    result = h.invoke(argv[1:], env, cwd=workdir)
    return prov, workdir, result


def the_exec(fake) -> dict:
    calls = fake.exec_calls()
    assert len(calls) == 1, f"expected exactly one `codex exec`, got {[c['argv'] for c in calls]}"
    return calls[0]


def results(prov, result):
    return [e for e in h.events(prov, result.stdout) if e.kind == RESULT]


def no_traceback(result):
    assert "Traceback" not in result.stderr, result.stderr[-2000:]


def run_budget(tmp_path, fake, **extra):
    result = h.invoke(["budget"], h.base_env(tmp_path, fake, **extra), timeout=30)
    no_traceback(result)
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert isinstance(data, dict)
    assert fake.exec_calls() == []
    assert_whitelisted(data)
    return data, result


WINDOW_NAME = re.compile(r"^(?:[A-Za-z0-9_.-]+-)?(?:5h|weekly|\d+m|window)$")


def assert_whitelisted(data):
    """CX-C11: only `rate_limits`-derived fields, timestamps and window metadata."""
    allowed = {"known", "source", "windows", "headroom", "resets_at", "stale_seconds", "note"}
    assert set(data) <= allowed, f"unexpected fields: {set(data) - allowed}"
    assert isinstance(data["known"], bool)
    if data["known"]:
        assert 0.0 <= data["headroom"] <= 1.0
        h.parse_instant(data["resets_at"])
        for name, win in data["windows"].items():
            assert WINDOW_NAME.match(name), f"window name {name!r}"
            assert set(win) <= {"percent", "resets_at"}, win
            assert 0.0 <= win["percent"] <= 100.0, (name, win)
            if "resets_at" in win:
                h.parse_instant(win["resets_at"])


def reading(ts, p5, pw, now):
    return h.token_count_line(ts, h.window(p5, 300, int(now + 3600)),
                              h.window(pw, 10080, int(now + 5 * 86400)))


# =========================================================================
# CX-C10 — the event stream from the CLI
# =========================================================================

def _completed(usage) -> bytes:
    return line({"type": "turn.completed", "usage": usage})


@pytest.mark.parametrize("usage_json", [
    '{"input_tokens": null, "cached_input_tokens": 0, "output_tokens": 10}',
    '{"input_tokens": "lots", "cached_input_tokens": 0, "output_tokens": 10}',
    '{"input_tokens": [100], "cached_input_tokens": 0, "output_tokens": 10}',
    '[100, 70, 10]',
    '"100 tokens"',
    '{"input_tokens": 1e999, "cached_input_tokens": 0, "output_tokens": 10}',
], ids=["null", "string", "list", "usage-a-list", "usage-a-string", "overflow-1e999"])
def test_cx_c10_a_malformed_usage_still_completes_the_turn(tmp_path, fake, usage_json):
    """A `turn.completed` whose usage is malformed is still a completed turn.

    The run must end in a success `result` event (the engine reads the
    outcome from it), keep running, never crash, and never report negative
    or non-integer tokens.
    """
    fake.raw(("out", line(HAPPY[0])), ("out", line(HAPPY[1])),
             ("out", b'{"type": "turn.completed", "usage": ' + usage_json.encode() + b"}\n"))
    prov, _, result = run_agent(tmp_path, fake)
    no_traceback(result)
    assert result.returncode == 0, result.stderr
    res = results(prov, result)
    assert len(res) == 1 and res[0].status == "success", \
        f"no success result for a completed turn: {result.stdout!r}"
    for value in res[0].tokens.values():
        assert isinstance(value, int) and value >= 0, res[0].tokens


@pytest.mark.parametrize("usage,expected", [
    ({}, (0, 0, 0)),
    (None, (0, 0, 0)),
    ({"input_tokens": -50, "cached_input_tokens": -5, "output_tokens": -1}, (0, 0, 0)),
    ({"input_tokens": "100", "cached_input_tokens": "70", "output_tokens": "10"}, (30, 70, 10)),
    ({"input_tokens": 100, "cached_input_tokens": 500, "output_tokens": 10}, (0, 100, 10)),
], ids=["empty", "null", "negative", "numeric-strings", "cache-exceeds-input"])
def test_cx_c10_token_edges_never_negative_nor_double_counted(tmp_path, fake, usage, expected):
    fake.set(events=[HAPPY[0], HAPPY[1],
                     {"type": "turn.completed", **({} if usage is None else {"usage": usage})}])
    prov, _, result = run_agent(tmp_path, fake)
    no_traceback(result)
    assert result.returncode == 0, result.stderr
    usage_events = [e.tokens for e in h.events(prov, result.stdout) if e.tokens]
    fresh, cached, out = expected
    total = sum(token_count(u) for u in usage_events)
    assert total == fresh + cached + out
    assert sum(u.get("cache_read_input_tokens", 0) for u in usage_events) == cached
    assert sum(u.get("input_tokens", 0) for u in usage_events) == fresh
    for u in usage_events:
        assert all(isinstance(v, int) and v >= 0 for v in u.values()), u


@pytest.mark.parametrize("error", ["rate limit exceeded", ["rate limit exceeded"]],
                         ids=["error-a-string", "error-a-list"])
def test_cx_c10_a_turn_failed_with_a_malformed_error_is_still_reported(tmp_path, fake, error):
    """`turn.failed` whose `error` is not an object: still a failed result,
    and its text still reaches stderr for the engine's quota detector."""
    fake.set(events=[HAPPY[0], HAPPY[1], {"type": "turn.failed", "error": error}])
    prov, _, result = run_agent(tmp_path, fake)
    no_traceback(result)
    assert result.returncode != 0
    res = results(prov, result)
    assert res and res[-1].status not in ("", "success"), result.stdout
    assert looks_like_quota_failure("failed", result.stderr), result.stderr


def test_cx_c10_unknown_and_malformed_events_do_not_stop_the_run(tmp_path, fake):
    fake.raw(
        ("out", line(HAPPY[0])),
        ("out", line({"type": "turn.started"})),
        ("out", line({"type": "future.kind", "payload": {"x": [1, 2]}})),
        ("out", line({"no_type": True})),
        ("out", line({"type": None})),
        ("out", line([1, 2, 3])),
        ("out", line('"just a string"')),
        ("out", line({"type": "item.completed", "item": None})),
        ("out", line({"type": "item.completed", "item": "not-an-object"})),
        ("out", line({"type": "item.started", "item": {"id": ["x"], "type": "command_execution"}})),
        ("out", line({"type": "item.completed", "item": {"id": "t", "type": "agent_message",
                                                          "text": None}})),
        ("out", line({"type": "error", "message": {"nested": "retrying"}})),
        ("out", b"\xff\xfe not utf-8 at all \x80\n"),
        ("err", b"WARN interleaved stderr line {\"type\": \"turn.completed\"}\n"),
        ("out", line({"type": "item.completed", "item": {"id": "a", "type": "agent_message",
                                                          "text": "caf\xe9 done"}})),
        ("err", b"\xff another stderr line\n"),
        ("out", line(HAPPY[3])),
    )
    prov, _, result = run_agent(tmp_path, fake)
    no_traceback(result)
    assert result.returncode == 0, result.stderr
    evs = h.events(prov, result.stdout)
    assert "caf\xe9 done" in [e.text for e in evs if e.kind == TEXT]
    res = [e for e in evs if e.kind == RESULT]
    assert len(res) == 1 and res[0].status == "success"
    assert token_count(res[0].tokens) == 110
    # stderr never turns into events on stdout.
    assert "interleaved stderr" not in result.stdout


def test_cx_c10_a_very_long_line_and_a_final_line_without_newline(tmp_path, fake):
    big = "x" * (8 * 1024 * 1024)
    fake.raw(("out", line(HAPPY[0])), ("out", line(HAPPY[1])),
             ("out", line({"type": "item.completed",
                           "item": {"id": "a", "type": "agent_message", "text": big}})),
             ("out", json.dumps(HAPPY[3]).encode()))          # no trailing newline
    prov, _, result = run_agent(tmp_path, fake)
    no_traceback(result)
    assert result.returncode == 0, result.stderr[-2000:]
    evs = h.events(prov, result.stdout)
    assert [len(e.text) for e in evs if e.kind == TEXT] == [len(big)]
    res = [e for e in evs if e.kind == RESULT]
    assert len(res) == 1 and res[0].status == "success"


def test_cx_c10_a_truncated_final_turn_completed_is_not_success(tmp_path, fake):
    fake.raw(("out", line(HAPPY[0])), ("out", line(HAPPY[1])),
             ("out", json.dumps(HAPPY[3]).encode()[:25]))
    prov, _, result = run_agent(tmp_path, fake)
    no_traceback(result)
    assert result.returncode != 0
    res = results(prov, result)
    assert res and res[-1].status != "success"


def test_cx_c10_duplicate_call_ids_give_one_tool_event(tmp_path, fake):
    item = {"id": "dup", "type": "command_execution", "command": "ls"}
    fake.set(events=[HAPPY[0], HAPPY[1],
                     {"type": "item.started", "item": item},
                     {"type": "item.started", "item": item},
                     {"type": "item.completed", "item": {**item, "status": "completed"}},
                     {"type": "item.completed", "item": {**item, "status": "completed"}},
                     HAPPY[3]])
    prov, _, result = run_agent(tmp_path, fake)
    assert result.returncode == 0, result.stderr
    tools = [e for e in h.events(prov, result.stdout) if e.kind == TOOL]
    assert [(t.name, t.args) for t in tools] == [("command_execution", {"command": "ls"})]
    usage = [e.tokens for e in h.events(prov, result.stdout) if e.tokens]
    assert sum(token_count(u) for u in usage) == 110


# =========================================================================
# CX-C10 — arguments and prompt handling
# =========================================================================

@pytest.mark.parametrize("prompt", [
    "--permission full",
    "-c sandbox_mode=\"danger-full-access\"",
    "--",
    "-",
    "\x1b[31mred\x1b[0m\r\n\ttab\x0bvt\x0cff\x7fdel ls ps",
    "x" * 100_000,
], ids=["flag-like", "config-like", "double-dash", "single-dash", "control-chars", "100k"])
def test_cx_c10_prompt_reaches_stdin_verbatim_and_never_argv(tmp_path, fake, prompt):
    fake.set(events=HAPPY)
    _, _, result = run_agent(tmp_path, fake, permission="readonly", prompt=prompt)
    assert result.returncode == 0, result.stderr
    call = the_exec(fake)
    assert call["stdin"] == prompt
    assert all(prompt not in token for token in call["argv"] if len(prompt) > 2)
    assert h.sandbox_mode(call["argv"]) == "read-only"


@pytest.mark.parametrize("model", ["gpt 5 turbo", "a=b", "m -c sandbox_mode=x"])
def test_cx_c10_model_with_spaces_or_equals_is_one_token(tmp_path, fake, model):
    fake.set(events=HAPPY)
    _, _, result = run_agent(tmp_path, fake, model=model)
    assert result.returncode == 0, result.stderr
    native = the_exec(fake)["argv"]
    assert model in native
    assert h.sandbox_mode(native) == "workspace-write"


@pytest.mark.parametrize("effort", [
    "very high", "a=b", 'x" sandbox_mode="danger-full-access',
    "high\nsandbox_mode = \"danger-full-access\"", "hi\x7fgh",
], ids=["space", "equals", "quote", "newline", "del-char"])
def test_cx_c10_effort_is_one_exact_toml_string(tmp_path, fake, effort):
    fake.set(events=HAPPY)
    _, _, result = run_agent(tmp_path, fake, options={"effort": effort})
    assert result.returncode == 0, result.stderr
    config = h.native_config(the_exec(fake)["argv"])
    assert h.lookup(config, "model_reasoning_effort") == effort
    assert h.sandbox_mode(the_exec(fake)["argv"]) == "workspace-write"


def test_cx_c10_a_workdir_with_spaces_and_quotes(tmp_path, fake):
    fake.set(events=HAPPY)
    workdir = tmp_path / "my work dir" / "it's \"here\""
    _, _, result = run_agent(tmp_path, fake, workdir=workdir)
    assert result.returncode == 0, result.stderr
    assert the_exec(fake)["cwd"] == str(workdir)


# =========================================================================
# CX-C10 — resume
# =========================================================================

def _option_safe(argv: list[str], value: str) -> bool:
    """True when `value` in the native argv cannot be read as an option:
    it does not start with '-', or an earlier `--` ends option parsing."""
    idx = [i for i, a in enumerate(argv) if a == value]
    if not idx:
        return True
    return not value.startswith("-") or "--" in argv[:idx[0]]


@pytest.mark.parametrize("sid", ["--dangerously-bypass-approvals-and-sandbox",
                                 "-c", "--full-auto"])
def test_cx_c10_a_session_id_cannot_become_a_native_option(tmp_path, fake, sid):
    """The resume id is a positional. One that looks like an option must be
    refused, or passed after `--`, never read as a flag (readonly asked)."""
    fake.set(events=HAPPY)
    _, _, result = run_agent(tmp_path, fake, permission="readonly", session_id=sid)
    no_traceback(result)
    for call in fake.exec_calls():
        assert _option_safe(call["argv"], sid), \
            f"session id {sid!r} lands in native argv as an option: {call['argv']}"


def test_cx_c10_a_resume_announcing_another_thread_fails_loudly(tmp_path, fake):
    # Distinct from the harness's "fresh" mode: the resume first re-announces
    # the requested id, then a second thread.started switches to another.
    # (Raw bytes: the harness's resume path drops any extra thread.started.)
    fake.raw(("out", line({"type": "thread.started", "thread_id": SID})),
             ("out", line(HAPPY[1])),
             ("out", line({"type": "thread.started", "thread_id": "someone-else"})),
             ("out", line(HAPPY[2])), ("out", line(HAPPY[3])))
    prov, _, result = run_agent(tmp_path, fake, session_id=SID)
    assert result.returncode != 0
    texts = result.stderr.splitlines() + [e.text for e in h.events(prov, result.stdout)]
    assert any(t.startswith(h.RESUME_FAILED) for t in texts), (result.stdout, result.stderr)


def test_cx_c10_a_resumed_turn_that_fails_is_not_a_resume_failure(tmp_path, fake):
    fake.set(events=[HAPPY[1], {"type": "turn.failed", "error": {"message": "rate limit"}}])
    prov, _, result = run_agent(tmp_path, fake, session_id=SID)
    assert result.returncode != 0
    texts = result.stderr.splitlines() + [e.text for e in h.events(prov, result.stdout)]
    assert not any(t.startswith(h.RESUME_FAILED) for t in texts), texts
    assert looks_like_quota_failure("failed", result.stderr)


def test_cx_c10_resume_error_text_does_not_leak_into_a_success(tmp_path, fake):
    fake.set(events=HAPPY, resume_gone="error")
    prov, _, result = run_agent(tmp_path, fake, session_id=SID)
    no_traceback(result)
    assert result.returncode != 0
    assert not any(r.status == "success" for r in results(prov, result))
    assert any(line.startswith(h.RESUME_FAILED) for line in result.stderr.splitlines())


# =========================================================================
# CX-C10 — MCP config injection
# =========================================================================

HOSTILE = [
    'a"b', "a\nb", "a]b", "a}b", "a=b", "a#b", "a'b", 'x" }, sandbox_mode = "danger',
    "a\\b", "tab\there", " ", "del\x7fchar",
]


@pytest.mark.parametrize("value", HOSTILE, ids=[f"v{i}" for i in range(len(HOSTILE))])
def test_cx_c10_hostile_mcp_env_values_inject_no_config_keys(tmp_path, fake, value):
    fake.set(events=HAPPY)
    config = tmp_path / "mcp.json"
    config.write_text(json.dumps({"mcpServers": {"multiagents": {
        "command": "python3", "args": ["-m", value], "env": {"K": value, value: "v"}}}}))
    _, _, result = run_agent(tmp_path, fake, mcp_config=config, MULTIAGENTS_CAN_SPAWN="1")
    assert result.returncode == 0, result.stderr
    native = the_exec(fake)["argv"]
    cfg = h.native_config(native)
    servers = h.lookup(cfg, "mcp_servers")
    assert isinstance(servers, dict), f"mcp_servers is not a TOML table: {servers!r}"
    assert set(servers) == {"multiagents"}
    assert servers["multiagents"]["env"] == {"K": value, value: "v"}
    assert servers["multiagents"]["args"] == ["-m", value]
    assert h.sandbox_mode(native) == "workspace-write"
    assert set(cfg) <= {"check_for_update_on_startup", "approval_policy", "sandbox_mode",
                        "mcp_servers", "features", "model_reasoning_effort"}, set(cfg)


@pytest.mark.parametrize("name", HOSTILE, ids=[f"n{i}" for i in range(len(HOSTILE))])
def test_cx_c10_hostile_inherited_server_names_are_still_disabled(tmp_path, fake, name):
    fake.set(events=HAPPY, mcp_list=json.dumps([
        {"name": name, "transport": {"command": "evil"}}]))
    _, _, result = run_agent(tmp_path, fake)
    assert result.returncode == 0, result.stderr
    native = the_exec(fake)["argv"]
    servers = h.lookup(h.native_config(native), "mcp_servers")
    assert isinstance(servers, dict), f"mcp_servers is not a TOML table: {servers!r}"
    assert servers == {name: {"command": "evil", "enabled": False}}
    assert h.sandbox_mode(native) == "workspace-write"


@pytest.mark.parametrize("listing", [
    '["not-an-object"]',
    '[{"name": "x", "transport": "stdio"}]',
    '{"servers": []}',
    'not json ' + h.SECRET,
], ids=["entry-a-string", "transport-a-string", "not-a-list", "not-json"])
def test_cx_c10_a_malformed_mcp_listing_refuses_with_a_codex_line(tmp_path, fake, listing):
    """Decision 8: a failed `mcp list` refuses the run, its stderr line starts
    `codex:`; never a traceback, never the listing's content."""
    fake.set(events=HAPPY, mcp_list=listing)
    _, _, result = run_agent(tmp_path, fake)
    assert result.returncode != 0
    assert fake.exec_calls() == []
    no_traceback(result)
    assert h.SECRET not in result.stdout + result.stderr
    assert any(l.startswith("codex:") for l in result.stderr.splitlines()), result.stderr


def test_cx_c10_a_failing_mcp_list_says_codex_on_stderr(tmp_path, fake):
    fake.set(events=HAPPY, mcp_list_exit=1, mcp_list="boom " + h.SECRET)
    _, _, result = run_agent(tmp_path, fake)
    assert result.returncode != 0
    assert h.SECRET not in result.stdout + result.stderr
    assert any(l.startswith("codex:") for l in result.stderr.splitlines()), result.stderr


# =========================================================================
# CX-C8 — profile selection
# =========================================================================

def test_cx_c8_a_profile_that_is_a_symlink_to_a_directory(tmp_path, fake):
    fake.set(events=HAPPY, status="logged_in")
    target = tmp_path / "real-profile"
    target.mkdir()
    link = tmp_path / "linked-profile"
    link.symlink_to(target)
    env_extra = {"MULTIAGENTS_CODEX_PROFILE": str(link)}
    _, _, result = run_agent(tmp_path, fake, **env_extra)
    assert result.returncode == 0, result.stderr
    assert h.invoke(["check"], h.base_env(tmp_path, fake, **env_extra)).returncode == 0
    homes = {c["codex_home"] for c in fake.calls()}
    assert {os.path.realpath(p) for p in homes} == {str(target)}


def test_cx_c8_a_profile_path_with_dotdot(tmp_path, fake):
    fake.set(events=HAPPY)
    raw = str(tmp_path / "x" / ".." / "p2")
    _, _, result = run_agent(tmp_path, fake, MULTIAGENTS_CODEX_PROFILE=raw)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "p2").is_dir()
    assert {os.path.realpath(c["codex_home"]) for c in fake.calls()} == {str(tmp_path / "p2")}


@pytest.mark.parametrize("action", ["run", "check", "budget", "models"])
def test_cx_c8_a_profile_that_is_a_file_is_a_clean_refusal(tmp_path, fake, action):
    fake.set(events=HAPPY, status="logged_in")
    bogus = tmp_path / "profile-file"
    bogus.write_text("I am a file " + h.SECRET)
    env = h.base_env(tmp_path, fake, MULTIAGENTS_CODEX_PROFILE=str(bogus))
    if action == "run":
        _, _, result = run_agent(tmp_path, fake, MULTIAGENTS_CODEX_PROFILE=str(bogus))
    else:
        result = h.invoke([action], env)
    no_traceback(result)
    assert h.SECRET not in result.stdout + result.stderr
    assert fake.exec_calls() == []
    assert bogus.read_text().startswith("I am a file")
    if action == "budget":
        assert result.returncode == 0
        assert json.loads(result.stdout)["known"] is False
    elif action == "check":
        assert result.returncode == 20
    else:
        assert result.returncode != 0


@pytest.mark.parametrize("action", ["run", "check", "budget", "models"])
def test_cx_c8_an_ambient_codex_home_at_the_users_own_dir_is_never_used(tmp_path, fake,
                                                                         action):
    fake.set(events=HAPPY, status="logged_in")
    own = tmp_path / "home" / ".codex"
    own.mkdir(parents=True)
    (own / "auth.json").write_text(json.dumps({"tokens": h.SECRET}))
    (own / "models_cache.json").write_text(json.dumps({"models": [
        {"slug": "users-own", "display_name": h.SECRET, "visibility": "list"}]}))
    h.write_rollout(own, "own", [reading(time.time() - 5, 99.0, 99.0, time.time())])
    before = sorted((p, p.stat().st_mtime_ns) for p in own.rglob("*"))
    env = h.base_env(tmp_path, fake, CODEX_HOME=str(own))
    if action == "run":
        _, _, result = run_agent(tmp_path, fake, CODEX_HOME=str(own))
    else:
        result = h.invoke([action], env)
    no_traceback(result)
    assert h.SECRET not in result.stdout + result.stderr
    assert "users-own" not in result.stdout
    if action == "budget":
        assert json.loads(result.stdout)["known"] is False
    assert all(c["codex_home"] != str(own) for c in fake.calls())
    assert sorted((p, p.stat().st_mtime_ns) for p in own.rglob("*")) == before


# =========================================================================
# CX-C11 — budget
# =========================================================================

def test_cx_c11_a_live_percent_out_of_range_skips_that_window(tmp_path, fake):
    """Decision (ag-fc872d): a live usedPercent out of 0..100 skips the window."""
    now = time.time()
    fake.app_server(mode="ok", result=h.rate_limits_response(h.rl_snapshot(
        h.rl_window(150, 300, int(now + 3600)), h.rl_window(20, 10080, int(now + 86400)))))
    data, _ = run_budget(tmp_path, fake)
    assert data["known"] is True
    assert "5h" not in data["windows"], data
    assert data["windows"]["weekly"]["percent"] == pytest.approx(20)
    assert data["headroom"] == pytest.approx(0.80)


def test_cx_c11_a_negative_live_percent_skips_that_window(tmp_path, fake):
    now = time.time()
    fake.app_server(mode="ok", result=h.rate_limits_response(h.rl_snapshot(
        h.rl_window(-5, 300, int(now + 3600)), h.rl_window(40, 10080, int(now + 86400)))))
    data, _ = run_budget(tmp_path, fake)
    assert "5h" not in data["windows"], data
    assert data["headroom"] == pytest.approx(0.60)


@pytest.mark.parametrize("resets", [1.79e12, 1e20, -1e20],
                         ids=["milliseconds", "far-future", "far-past"])
def test_cx_c11_a_live_reset_time_out_of_range_is_not_a_crash(tmp_path, fake, profile, resets):
    now = time.time()
    h.write_rollout(profile, "r", [reading(now - 30, 11.0, 22.0, now)])
    fake.app_server(mode="ok", result=h.rate_limits_response(h.rl_snapshot(
        h.rl_window(30, 300, resets), h.rl_window(40, 10080, int(now + 86400)))))
    data, _ = run_budget(tmp_path, fake)
    assert data["known"] is True


@pytest.mark.parametrize("resets", [1.79e12, 1e20],
                         ids=["milliseconds", "far-future"])
def test_cx_c11_a_rollout_reset_time_out_of_range_is_skipped(tmp_path, fake, profile, resets):
    now = time.time()
    h.write_rollout(profile, "r", [
        reading(now - 600, 10.0, 12.0, now),
        h.token_count_line(now - 5, h.window(90.0, 300, resets),
                           h.window(90.0, 10080, int(now + 86400)))])
    data, _ = run_budget(tmp_path, fake)
    assert data["known"] is True
    assert data["windows"]["5h"]["percent"] == pytest.approx(10.0)


def test_cx_c11_a_rollout_window_minutes_overflow_is_skipped(tmp_path, fake, profile):
    now = time.time()
    good = reading(now - 600, 10.0, 12.0, now)
    bad = good.replace('"window_minutes": 300', '"window_minutes": 1e999')
    bad = bad.replace(h.iso(now - 600), h.iso(now - 5))
    assert "1e999" in bad
    h.write_rollout(profile, "r", [good, bad])
    data, _ = run_budget(tmp_path, fake)
    assert data["known"] is True
    assert data["windows"]["5h"]["percent"] == pytest.approx(10.0)


def test_cx_c11_non_rollout_jsonl_files_are_not_rollouts(tmp_path, fake, profile):
    """Only `sessions/**/rollout-*.jsonl` is a rollout file."""
    now = time.time()
    h.write_rollout(profile, "real", [reading(now - 600, 10.0, 12.0, now)], mtime=now - 600)
    other = profile / "sessions" / "2026" / "09" / "28" / "history.jsonl"
    other.write_text(reading(now - 5, 95.0, 95.0, now) + "\n")
    data, _ = run_budget(tmp_path, fake)
    assert data["known"] is True
    assert data["windows"]["5h"]["percent"] == pytest.approx(10.0), data


def _day(profile) -> Path:
    d = profile / "sessions" / "2026" / "09" / "28"
    d.mkdir(parents=True, exist_ok=True)
    return d


@pytest.mark.parametrize("kind", ["fifo", "symlink-to-fifo", "directory", "dangling-symlink",
                                  "unreadable", "symlink-outside", "unreadable-dir"])
def test_cx_c11_odd_rollout_entries_are_skipped_without_hanging(tmp_path, fake, profile, kind):
    now = time.time()
    h.write_rollout(profile, "good", [reading(now - 600, 10.0, 12.0, now)], mtime=now - 3600)
    day = _day(profile)
    odd = day / "rollout-odd.jsonl"
    outside = tmp_path / "outside.jsonl"
    outside.write_text(reading(now - 5, 10.0, 12.0, now) + "\n")
    if kind == "fifo":
        os.mkfifo(odd)
    elif kind == "symlink-to-fifo":
        os.mkfifo(tmp_path / "pipe")
        odd.symlink_to(tmp_path / "pipe")
    elif kind == "directory":
        odd.mkdir()
    elif kind == "dangling-symlink":
        odd.symlink_to(tmp_path / "nowhere")
    elif kind == "unreadable":
        odd.write_text(reading(now - 5, 90.0, 90.0, now) + "\n")
        odd.chmod(0)
    elif kind == "symlink-outside":
        odd.symlink_to(outside)
    elif kind == "unreadable-dir":
        locked = profile / "sessions" / "2026" / "09" / "29"
        locked.mkdir(parents=True)
        (locked / "rollout-x.jsonl").write_text(reading(now - 5, 90.0, 90.0, now) + "\n")
        locked.chmod(0)
    try:
        started = time.monotonic()
        data, result = run_budget(tmp_path, fake)
        assert time.monotonic() - started < 9.5
    finally:
        for p in (odd, profile / "sessions" / "2026" / "09" / "29"):
            if p.exists() and not p.is_symlink():
                p.chmod(0o700)
    assert data["known"] is True
    assert data["windows"]["5h"]["percent"] == pytest.approx(10.0)
    assert str(tmp_path) not in result.stdout


def test_cx_c11_an_app_server_with_a_huge_garbage_line_falls_back(tmp_path, fake, profile):
    now = time.time()
    h.write_rollout(profile, "r", [reading(now - 30, 11.0, 22.0, now)])
    fake.app_server(mode="garbage", garbage=h.SECRET + "y" * (16 * 1024 * 1024))
    data, result = run_budget(tmp_path, fake)
    assert data["source"] == "rollout"
    assert h.SECRET not in result.stdout + result.stderr


def test_cx_c11_a_huge_valid_response_emits_only_whitelisted_fields(tmp_path, fake):
    now = time.time()
    response = h.rate_limits_response(h.rl_snapshot(
        h.rl_window(30, 300, int(now + 3600)), h.rl_window(40, 10080, int(now + 86400))))
    response["rateLimitUpsell"]["body_text"] = h.UPSELL_TEXT * 200_000
    fake.app_server(mode="ok", result=response)
    data, result = run_budget(tmp_path, fake)
    assert data["source"] == "app-server"
    for leak in (h.UPSELL_TEXT, h.ACCOUNT_ID, h.CREDITS_BALANCE):
        assert leak not in result.stdout + result.stderr


def test_cx_c11_an_app_server_that_never_ends_its_line_is_bounded(tmp_path, fake, profile):
    now = time.time()
    h.write_rollout(profile, "r", [reading(now - 30, 11.0, 22.0, now)])
    fake.set(endless=64 * 1024 * 1024)
    started = time.monotonic()
    data, _ = run_budget(tmp_path, fake)
    assert time.monotonic() - started < 9.5
    assert data["source"] == "rollout"
    assert fake.pids(), "the endless app-server never started"
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and not all(h.process_gone(p) for p in fake.pids()):
        time.sleep(0.05)
    assert all(h.process_gone(p) for p in fake.pids())


# =========================================================================
# CX-C12 — models
# =========================================================================

def test_cx_c12_an_entry_without_visibility_is_not_listed(tmp_path, fake, profile):
    """Decision 3 (ag-155ed5): only an explicit `visibility: list` counts."""
    profile.mkdir()
    (profile / "models_cache.json").write_text(json.dumps({"models": [
        {"slug": "shown", "display_name": "Shown", "visibility": "list"},
        {"slug": "unmarked", "display_name": "Unmarked"}]}))
    result = h.invoke(["models"], h.base_env(tmp_path, fake))
    assert result.returncode == 0, result.stderr
    assert result.stdout == "shown\tShown\n"


def test_cx_c12_a_slug_with_tab_or_newline_cannot_forge_a_row(tmp_path, fake, profile):
    profile.mkdir()
    (profile / "models_cache.json").write_text(json.dumps({"models": [
        {"slug": "ok\tforged\nevil-model", "display_name": "X", "visibility": "list"},
        {"slug": "plain", "display_name": "Plain", "visibility": "list"}]}))
    result = h.invoke(["models"], h.base_env(tmp_path, fake))
    no_traceback(result)
    ids = [m["id"] for m in h.provider().parse_models(result.stdout)]
    assert "evil-model" not in ids, result.stdout
    assert all(line.count("\t") == 1 for line in result.stdout.splitlines()), result.stdout


# =========================================================================
# Output hygiene (CX-C9, CX-C11): no credential, auth.json or message body
# =========================================================================

@pytest.mark.parametrize("status", ["logged_in", "not_logged_in", "refresh_failed", "weird"])
def test_cx_c9_check_never_prints_auth_json_or_cli_output(tmp_path, fake, profile, status):
    profile.mkdir(mode=0o700)
    (profile / "auth.json").write_text(json.dumps({"tokens": {"access": h.SECRET}}))
    fake.set(status=status, refresh_message="token could not be refreshed")
    result = h.invoke(["check"], h.base_env(tmp_path, fake))
    no_traceback(result)
    assert h.SECRET not in result.stdout + result.stderr


@pytest.mark.parametrize("mode", ["exit", "garbage", "rpc_error", "not_logged_in",
                                  "init_error"])
def test_cx_c11_budget_error_paths_print_no_secret_or_body(tmp_path, fake, profile, mode):
    now = time.time()
    profile.mkdir(mode=0o700)
    (profile / "auth.json").write_text(json.dumps({"tokens": {"access": h.SECRET}}))
    h.write_rollout(profile, "r", [h.message_line(now - 40, "BODY " + h.SECRET),
                                   reading(now - 30, 11.0, 22.0, now),
                                   h.message_line(now - 20, "BODY " + h.SECRET)])
    fake.app_server(mode=mode)
    data, result = run_budget(tmp_path, fake)
    assert h.SECRET not in result.stdout + result.stderr
    assert data["source"] == "rollout"
