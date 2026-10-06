"""NT-R2 (the `notify` tool) and NT-R7 (what the tool and the config may leak
or truncate), ntfy notifications, first round.

The tool is `server.notify(title, message, priority="default", tags=[])`,
called the way the other tool tests call a tool (in-process, sync or async).
Everything is checked from the outside: the dict it returns and the HTTP
request a fake server on 127.0.0.1 receives.

Assumptions (also in the run report): `reason` is a non-empty string whose
vocabulary the spec does not fix, so only its presence, and that different
failure kinds give different reasons, are asserted; the HTTP method is POST or
PUT (both are ntfy-documented publishes); `Priority` is the word or its ntfy
number (min=1 .. urgent=5); the `Tags` header is comma separated.
"""
from __future__ import annotations

import email.header
import json
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

from nc_harness import tool  # noqa: E402
from nt_harness import (FakeNtfy, NotifyProject, TOKEN, TOOL_BOUND, closed_port_url,  # noqa: E402
                        leaks_of, token_file)

from multiagents import server  # noqa: E402
from multiagents.paths import state_root  # noqa: E402

PRIORITIES = ["min", "low", "default", "high", "urgent"]
NUMBER = {"min": "1", "low": "2", "default": "3", "high": "4", "urgent": "5"}


@pytest.fixture
def fake():
    f = FakeNtfy()
    yield f
    f.close()


@pytest.fixture
def world(tmp_path, monkeypatch, fake):
    """A project whose notify section points at the fake server."""
    return NotifyProject(tmp_path, monkeypatch,
                         {"ntfy_url": fake.url, "topic": "multiagents-test"})


def send(*args, **kwargs):
    return tool(server.notify, *args, **kwargs)


def decoded(value: str) -> str:
    return "".join(p.decode(c or "utf-8") if isinstance(p, bytes) else p
                   for p, c in email.header.decode_header(value))


def is_failure(result) -> bool:
    return (isinstance(result, dict) and result.get("ok") is False
            and isinstance(result.get("reason"), str) and result["reason"].strip() != "")


# ---------------------------------------------------------------- the publish

def test_nt_r2_a_message_is_published_to_the_base_url_and_topic(world, fake):
    result = send("Build done", "all green")
    assert result["ok"] is True
    (req,) = fake.requests
    assert req.method in ("POST", "PUT")
    assert req.path == "/multiagents-test"
    assert req.text() == "all green"
    assert decoded(req.headers["title"]) == "Build done"


def test_nt_r2_the_url_keeps_a_base_path_in_front_of_the_topic(tmp_path, monkeypatch, fake):
    NotifyProject(tmp_path, monkeypatch, {"ntfy_url": fake.url + "/ntfy", "topic": "t1"})
    assert send("t", "m")["ok"] is True
    assert fake.requests[0].path == "/ntfy/t1"


def test_nt_r2_a_success_says_accepted_and_never_delivered(world, fake):
    result = send("t", "m")
    assert result["ok"] is True
    assert "delivered" not in json.dumps(result).lower()


def test_nt_r2_a_multiline_message_arrives_intact_as_the_body(world, fake):
    message = "line one\nline two\r\nline three ✓"
    assert send("t", message)["ok"] is True
    assert fake.requests[0].text() == message


@pytest.mark.parametrize("priority", PRIORITIES)
def test_nt_r2_each_priority_reaches_the_priority_header(world, fake, priority):
    assert send("t", "m", priority=priority)["ok"] is True
    got = fake.requests[0].headers.get("priority")
    if priority == "default" and got is None:
        return                                 # ntfy's own default
    assert got is not None and got.lower() in (priority, NUMBER[priority])


def test_nt_r2_the_priority_defaults_to_default(world, fake):
    assert send("t", "m")["ok"] is True
    got = fake.requests[0].headers.get("priority")
    assert got is None or got.lower() in ("default", "3")


def test_nt_r2_tags_reach_the_tags_header(world, fake):
    assert send("t", "m", tags=["warning", "a_b-C9"])["ok"] is True
    assert fake.requests[0].headers["tags"].replace(" ", "").split(",") == ["warning", "a_b-C9"]


def test_nt_r2_no_tags_means_no_tag_in_the_header(world, fake):
    assert send("t", "m")["ok"] is True
    assert fake.requests[0].headers.get("tags", "").strip() == ""


def test_nt_r2_a_non_ascii_title_is_encoded_per_rfc_2047_and_arrives_intact(world, fake):
    title = "Héllo — 日本語 \U0001f680"
    assert send(title, "m")["ok"] is True
    raw = fake.requests[0].headers["title"]
    assert raw.isascii()
    assert raw.startswith("=?") and raw.endswith("?=")
    assert decoded(raw) == title


def test_nt_r2_an_ascii_title_is_not_encoded(world, fake):
    assert send("Plain title", "m")["ok"] is True
    assert fake.requests[0].headers["title"] == "Plain title"


# ---------------------------------------------------------------- the token

def test_nt_r2_a_configured_token_is_sent_as_a_bearer_header(tmp_path, monkeypatch, fake):
    tf = token_file(tmp_path)
    NotifyProject(tmp_path, monkeypatch, {"ntfy_url": fake.url, "topic": "t1",
                                          "token_file": str(tf)})
    assert send("t", "m")["ok"] is True
    assert fake.requests[0].headers["authorization"] == f"Bearer {TOKEN}"


def test_nt_r2_the_token_files_trailing_newline_is_not_part_of_the_token(tmp_path, monkeypatch, fake):
    tf = token_file(tmp_path, content=TOKEN + "\r\n\n")
    NotifyProject(tmp_path, monkeypatch, {"ntfy_url": fake.url, "topic": "t1",
                                          "token_file": str(tf)})
    assert send("t", "m")["ok"] is True
    assert fake.requests[0].headers["authorization"] == f"Bearer {TOKEN}"


def test_nt_r2_a_token_file_under_the_home_tilde_is_found(tmp_path, monkeypatch, fake):
    home = tmp_path / "home"
    (home / ".config").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    token_file(home / ".config", name="tok")
    NotifyProject(tmp_path, monkeypatch, {"ntfy_url": fake.url, "topic": "t1",
                                          "token_file": "~/.config/tok"})
    assert send("t", "m")["ok"] is True
    assert fake.requests[0].headers["authorization"] == f"Bearer {TOKEN}"


def test_nt_r2_without_a_token_file_there_is_no_authorization_header(world, fake):
    assert send("t", "m")["ok"] is True
    assert "authorization" not in fake.requests[0].headers


def _with_token_file(tmp_path, monkeypatch, fake, path):
    return NotifyProject(tmp_path, monkeypatch, {"ntfy_url": fake.url, "topic": "t1",
                                                 "token_file": str(path)})


def test_nt_r2_a_missing_token_file_refuses_sending_and_sends_nothing(tmp_path, monkeypatch, fake):
    _with_token_file(tmp_path, monkeypatch, fake, tmp_path / "absent")
    assert is_failure(send("t", "m"))
    assert fake.requests == []


@pytest.mark.parametrize("content", ["", "\n", "   \n\t\n"])
def test_nt_r1_an_empty_token_file_refuses_sending(tmp_path, monkeypatch, fake, content):
    _with_token_file(tmp_path, monkeypatch, fake, token_file(tmp_path, content=content))
    assert is_failure(send("t", "m"))
    assert fake.requests == []


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o604, 0o660, 0o606, 0o666, 0o777, 0o610])
def test_nt_r1_a_token_file_readable_by_group_or_others_refuses_sending(
        tmp_path, monkeypatch, fake, mode):
    _with_token_file(tmp_path, monkeypatch, fake, token_file(tmp_path, mode=mode))
    assert is_failure(send("t", "m"))
    assert fake.requests == []


@pytest.mark.parametrize("mode", [0o600, 0o400])
def test_nt_r1_a_token_file_at_0600_or_stricter_is_accepted(tmp_path, monkeypatch, fake, mode):
    _with_token_file(tmp_path, monkeypatch, fake, token_file(tmp_path, mode=mode))
    assert send("t", "m")["ok"] is True


def test_nt_r1_an_unreadable_token_file_refuses_sending(tmp_path, monkeypatch, fake):
    if os.geteuid() == 0:
        pytest.skip("root reads any file")
    tf = token_file(tmp_path, mode=0o000)
    try:
        _with_token_file(tmp_path, monkeypatch, fake, tf)
        assert is_failure(send("t", "m"))
        assert fake.requests == []
    finally:
        os.chmod(tf, 0o600)


def test_nt_r1_a_directory_as_token_file_refuses_sending(tmp_path, monkeypatch, fake):
    d = tmp_path / "adir"
    d.mkdir(mode=0o700)
    _with_token_file(tmp_path, monkeypatch, fake, d)
    assert is_failure(send("t", "m"))
    assert fake.requests == []


def test_nt_r1_fixing_the_token_file_lets_the_next_send_through(tmp_path, monkeypatch, fake):
    tf = token_file(tmp_path, mode=0o644)
    _with_token_file(tmp_path, monkeypatch, fake, tf)
    assert is_failure(send("t", "m"))
    os.chmod(tf, 0o600)
    assert send("t", "m")["ok"] is True
    assert len(fake.requests) == 1


# ---------------------------------------------------------------- not configured

def test_nt_r1_without_a_notify_section_nothing_is_sent(tmp_path, monkeypatch, fake):
    NotifyProject(tmp_path, monkeypatch, "absent")
    assert is_failure(send("t", "m"))
    assert fake.requests == []


def test_nt_r2_a_config_that_fails_to_load_still_returns_a_reason_and_sends_nothing(
        tmp_path, monkeypatch, fake):
    NotifyProject(tmp_path, monkeypatch, {"ntfy_url": fake.url, "topic": "bad topic"})
    assert is_failure(send("t", "m"))
    assert fake.requests == []


# ---------------------------------------------------------------- invalid arguments

@pytest.mark.parametrize("priority", ["critical", "", "5", "HIGH ", "max", "urgent\r\nX-A: 1"])
def test_nt_r2_an_unknown_priority_is_refused_and_nothing_is_sent(world, fake, priority):
    assert is_failure(send("t", "m", priority=priority))
    assert fake.requests == []


@pytest.mark.parametrize("tags", [["a b"], ["a,b"], ["a\nb"], ["a\r\nX-Injected: 1"],
                                  ["café"], ["ok", "bad tag"], ["a/b"]])
def test_nt_r2_a_tag_outside_the_topic_rule_is_refused_and_nothing_is_sent(world, fake, tags):
    assert is_failure(send("t", "m", tags=tags))
    assert fake.requests == []


@pytest.mark.parametrize("kwargs", [{"title": 123}, {"title": ["x"]}, {"message": ["x"]},
                                    {"message": 5}, {"tags": [1]}, {"tags": [None]},
                                    {"priority": 3}, {"priority": None}])
def test_nt_r2_arguments_of_the_wrong_type_return_a_reason_and_never_raise(world, fake, kwargs):
    args = {"title": "t", "message": "m", **kwargs}
    assert is_failure(send(**args))
    assert fake.requests == []


@pytest.mark.parametrize("title", ["x\r\nX-Injected: 1", "x\nX-Injected: 1", "x\rX-Injected: 1"])
def test_nt_r2_cr_and_lf_in_a_title_never_reach_a_header(world, fake, title):
    result = send(title, "m")
    assert isinstance(result, dict) and result.get("ok") in (True, False)
    for req in fake.requests:                    # refused (none) or sanitised (one)
        assert "x-injected" not in req.headers
        assert "\r" not in req.headers["title"] and "\n" not in req.headers["title"]
        assert "\r" not in decoded(req.headers["title"]) and "\n" not in decoded(req.headers["title"])


def test_nt_r2_cr_and_lf_in_a_non_ascii_title_never_reach_a_header(world, fake):
    send("hé\r\nX-Injected: 1", "m")
    for req in fake.requests:
        assert "x-injected" not in req.headers
        assert "\n" not in decoded(req.headers["title"])


def test_nt_r2_a_message_with_crlf_does_not_inject_headers(world, fake):
    assert send("t", "a\r\nX-Injected: 1\r\n\r\nbody")["ok"] is True
    assert "x-injected" not in fake.requests[0].headers


# ---------------------------------------------------------------- failure reasons

@pytest.mark.parametrize("status", [400, 401, 403, 404, 429, 500, 502, 503])
def test_nt_r2_an_http_error_status_is_a_failure_with_a_reason(world, fake, status):
    fake.mode, fake.status = "status", status
    assert is_failure(send("t", "m"))
    assert len(fake.requests) == 1               # one attempt: retrying is the scheduler's


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_nt_r2_a_redirect_is_not_followed_and_the_token_goes_nowhere_else(
        tmp_path, monkeypatch, fake, status):
    other = FakeNtfy()
    try:
        fake.mode, fake.status = "redirect", status
        fake.location = other.url + "/stolen"
        NotifyProject(tmp_path, monkeypatch, {"ntfy_url": fake.url, "topic": "t1",
                                              "token_file": str(token_file(tmp_path))})
        assert is_failure(send("t", "m"))
        assert len(fake.requests) == 1
        assert other.requests == []
    finally:
        other.close()


def test_nt_r2_a_refused_connection_is_a_failure_with_a_reason(tmp_path, monkeypatch):
    NotifyProject(tmp_path, monkeypatch, {"ntfy_url": closed_port_url(), "topic": "t1"})
    started = time.monotonic()
    assert is_failure(send("t", "m"))
    assert time.monotonic() - started < TOOL_BOUND


def test_nt_r2_a_connection_dropped_without_an_answer_is_a_failure(world, fake):
    fake.mode = "drop"
    assert is_failure(send("t", "m"))


def test_nt_r2_failure_kinds_have_distinct_reasons(tmp_path, monkeypatch, fake):
    reasons = {}      # one project per case: the harness refuses to build twice in a directory
    NotifyProject(tmp_path / "case1", monkeypatch, "absent")
    reasons["not configured"] = send("t", "m")["reason"]
    NotifyProject(tmp_path / "case2", monkeypatch, {"ntfy_url": fake.url, "topic": "t1"})
    reasons["invalid argument"] = send("t", "m", priority="critical")["reason"]
    fake.mode, fake.status = "status", 500
    reasons["http status"] = send("t", "m")["reason"]
    fake.mode, fake.status, fake.location = "redirect", 302, fake.url + "/elsewhere"
    reasons["redirect"] = send("t", "m")["reason"]
    NotifyProject(tmp_path / "case3", monkeypatch, {"ntfy_url": closed_port_url(), "topic": "t1"})
    reasons["network"] = send("t", "m")["reason"]
    NotifyProject(tmp_path / "case4", monkeypatch, {"ntfy_url": fake.url, "topic": "t1",
                                          "token_file": str(tmp_path / "absent")})
    reasons["token file"] = send("t", "m")["reason"]
    assert len(set(reasons.values())) == len(reasons), reasons


# ---------------------------------------------------------------- bounded in time (the two slow tests: ~10 s of real time each)

@pytest.mark.parametrize("mode", ["hang", "drip"])
def test_nt_r2_a_server_that_never_finishes_cannot_hold_the_tool_past_ten_seconds(world, fake, mode):
    fake.mode = mode
    started = time.monotonic()
    result = send("t", "m")
    assert time.monotonic() - started < TOOL_BOUND
    assert isinstance(result, dict) and "ok" in result
    if mode == "hang":
        assert is_failure(result)


# ---------------------------------------------------------------- no rate limit, no dedupe

def test_nt_r2_the_tool_is_not_rate_limited_and_repeats_are_sent_again(tmp_path, monkeypatch, fake):
    NotifyProject(tmp_path, monkeypatch, {"ntfy_url": fake.url, "topic": "t1",
                                          "min_interval_seconds": 3600})
    results = [send("same", "same") for _ in range(3)]
    assert all(r["ok"] is True for r in results)
    assert len(fake.requests) == 3


# ---------------------------------------------------------------- root only

def test_nt_r2_a_subagent_calling_the_tool_directly_is_refused_and_nothing_is_sent(
        world, fake, monkeypatch):
    assert send("t", "as root")["ok"] is True            # the tool works for the root...
    assert len(fake.requests) == 1
    monkeypatch.setenv("MULTIAGENTS_AGENT_ID", "ag-sub")
    server._reset()
    result = send("t", "as subagent")
    assert isinstance(result, dict) and result.get("ok") is not True
    assert len(fake.requests) == 1                       # ...and the subagent's never left


# ---------------------------------------------------------------- NT-R7: truncation, no leaks

def test_nt_r7_a_body_of_exactly_1000_characters_is_sent_whole(world, fake):
    message = "a" * 1000
    assert send("t", message)["ok"] is True
    assert fake.requests[0].text() == message


@pytest.mark.parametrize("length", [1001, 1500, 100000])
def test_nt_r7_a_longer_body_is_truncated_to_1000_characters(world, fake, length):
    message = "".join(chr(ord("a") + i % 26) for i in range(length))
    assert send("t", message)["ok"] is True
    body = fake.requests[0].text()
    assert 900 <= len(body) <= 1000
    assert message.startswith(body[:990])


def test_nt_r7_the_limit_counts_characters_not_bytes(world, fake):
    message = "é" * 1000                            # 2000 bytes, 1000 characters
    assert send("t", message)["ok"] is True
    assert fake.requests[0].text() == message


@pytest.mark.parametrize("scenario", ["ok", "401", "500", "redirect", "token-refused", "drop"])
def test_nt_r7_the_token_appears_in_no_result_log_output_or_file(
        tmp_path, monkeypatch, fake, caplog, capfd, scenario):
    tf = token_file(tmp_path, mode=0o644 if scenario == "token-refused" else 0o600)
    NotifyProject(tmp_path, monkeypatch, {"ntfy_url": fake.url, "topic": "t1",
                                          "token_file": str(tf)})
    if scenario in ("401", "500"):
        fake.mode, fake.status = "status", int(scenario)
    elif scenario == "redirect":
        fake.mode, fake.status, fake.location = "redirect", 307, fake.url + "/elsewhere"
    elif scenario == "drop":
        fake.mode = "drop"
    result = send("t", "m")
    assert isinstance(result, dict) and "ok" in result       # the tool ran
    assert (result["ok"] is True) == (scenario == "ok")
    out, err = capfd.readouterr()
    assert TOKEN not in json.dumps(result)
    assert TOKEN not in caplog.text and TOKEN not in out and TOKEN not in err
    assert leaks_of(TOKEN, tmp_path, state_root(), skip=(tf,)) == []
    if scenario != "token-refused":
        assert fake.requests[0].headers["authorization"] == f"Bearer {TOKEN}"
    assert TOKEN not in fake.requests[0].text() if fake.requests else True
