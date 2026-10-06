"""NT-R8 (the `multiagents notify test|status` CLI) and the CLI's share of
NT-R1 / NT-R7, ntfy notifications, first round. Run in-process through
`cli.main`, against the fake server on 127.0.0.1.

Out of this round: the NT-R4 pause that a successful `notify test` clears (it
needs the scheduler's sender).

A refusal is always paired with the same command succeeding once the cause is
fixed, so that "non-zero" cannot be satisfied by the subcommand merely not
existing (argparse's usage error is also non-zero).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

from nt_harness import FakeNtfy, NotifyProject, TOKEN, closed_port_url, leaks_of, token_file  # noqa: E402

from multiagents import cli  # noqa: E402
from multiagents.paths import state_root  # noqa: E402


@pytest.fixture
def fake():
    f = FakeNtfy()
    yield f
    f.close()


def run(world, *words, capsys):
    capsys.readouterr()
    try:
        rc = cli.main(["--path", str(world.root), "notify", *words])
    except SystemExit as exc:
        rc = exc.code if isinstance(exc.code, int) else 1
    out, err = capsys.readouterr()
    return rc, out + err


def configure(tmp_path, monkeypatch, fake, **extra):
    return NotifyProject(tmp_path, monkeypatch,
                         {"ntfy_url": fake.url, "topic": "multiagents-test", **extra})


# ---------------------------------------------------------------- notify test

def test_nt_r8_test_sends_one_message_and_exits_zero_saying_accepted(tmp_path, monkeypatch, fake, capsys):
    world = configure(tmp_path, monkeypatch, fake)
    rc, output = run(world, "test", capsys=capsys)
    assert rc == 0, output
    assert len(fake.requests) == 1
    req = fake.requests[0]
    assert req.method in ("POST", "PUT") and req.path == "/multiagents-test"
    assert req.text().strip() != ""
    assert "accepted" in output.lower()
    assert "delivered" not in (output + req.text()).lower()


def test_nt_r8_test_uses_the_projects_token(tmp_path, monkeypatch, fake, capsys):
    world = configure(tmp_path, monkeypatch, fake, token_file=str(token_file(tmp_path)))
    rc, output = run(world, "test", capsys=capsys)
    assert rc == 0, output
    assert fake.requests[0].headers["authorization"] == f"Bearer {TOKEN}"


@pytest.mark.parametrize("status", [400, 401, 403, 404, 429, 500, 503])
def test_nt_r8_test_exits_non_zero_with_a_reason_when_the_server_refuses(
        tmp_path, monkeypatch, fake, capsys, status):
    world = configure(tmp_path, monkeypatch, fake)
    fake.mode, fake.status = "status", status
    rc, output = run(world, "test", capsys=capsys)
    assert len(fake.requests) == 1                        # the command really ran
    assert rc != 0
    assert output.strip() != ""
    fake.mode = "accept"
    rc, output = run(world, "test", capsys=capsys)        # and works once the server does
    assert rc == 0, output


def test_nt_r8_test_exits_non_zero_when_nothing_listens(tmp_path, monkeypatch, capsys):
    world = NotifyProject(tmp_path, monkeypatch, {"ntfy_url": closed_port_url(), "topic": "t1"})
    rc, output = run(world, "test", capsys=capsys)
    assert rc != 0 and output.strip() != ""
    assert "Traceback" not in output
    fake = FakeNtfy()                                      # the same command works once it listens
    try:
        world.set_notify({"ntfy_url": fake.url, "topic": "t1"})
        rc, output = run(world, "test", capsys=capsys)
        assert rc == 0, output
    finally:
        fake.close()


def test_nt_r8_test_does_not_follow_a_redirect(tmp_path, monkeypatch, fake, capsys):
    other = FakeNtfy()
    try:
        fake.mode, fake.status, fake.location = "redirect", 307, other.url + "/stolen"
        world = configure(tmp_path, monkeypatch, fake, token_file=str(token_file(tmp_path)))
        rc, output = run(world, "test", capsys=capsys)
        assert len(fake.requests) == 1
        assert rc != 0
        assert other.requests == []
        assert TOKEN not in output
    finally:
        other.close()


def test_nt_r1_test_without_a_notify_section_sends_nothing_and_fails(tmp_path, monkeypatch, fake, capsys):
    world = NotifyProject(tmp_path, monkeypatch, "absent")
    rc, output = run(world, "test", capsys=capsys)
    assert rc != 0 and output.strip() != ""
    assert fake.requests == []
    world.set_notify({"ntfy_url": fake.url, "topic": "t1"})   # the same command works once configured
    rc, output = run(world, "test", capsys=capsys)
    assert rc == 0, output


@pytest.mark.parametrize("bad", [{"topic": "bad topic"}, {"topic": ""}, {"min_interval_seconds": 0},
                                 {"events": ["bogus"]}, {"ntfy_url": "ftp://example.invalid"},
                                 {"token": "tk_inline_SECRET"}])
def test_nt_r1_test_refuses_an_invalid_config_and_sends_nothing(tmp_path, monkeypatch, fake, capsys, bad):
    world = NotifyProject(tmp_path, monkeypatch, {"ntfy_url": fake.url, "topic": "t1", **bad})
    rc, output = run(world, "test", capsys=capsys)
    assert rc != 0 and output.strip() != ""
    assert "tk_inline_SECRET" not in output
    assert fake.requests == []
    world.set_notify({"ntfy_url": fake.url, "topic": "t1"})
    rc, output = run(world, "test", capsys=capsys)
    assert rc == 0, output


@pytest.mark.parametrize("case", ["missing", "empty", "group-readable", "world-readable"])
def test_nt_r1_test_refuses_a_bad_token_file_and_sends_nothing(tmp_path, monkeypatch, fake, capsys, case):
    tf = tmp_path / "tok"
    if case == "empty":
        token_file(tmp_path, content="\n", name="tok")
    elif case == "group-readable":
        token_file(tmp_path, mode=0o640, name="tok")
    elif case == "world-readable":
        token_file(tmp_path, mode=0o644, name="tok")
    world = configure(tmp_path, monkeypatch, fake, token_file=str(tf))
    rc, output = run(world, "test", capsys=capsys)
    assert rc != 0 and output.strip() != ""
    assert TOKEN not in output
    assert fake.requests == []
    token_file(tmp_path, name="tok")                       # fixed: 0600 with a token
    rc, output = run(world, "test", capsys=capsys)
    assert rc == 0, output
    assert fake.requests[0].headers["authorization"] == f"Bearer {TOKEN}"


# ---------------------------------------------------------------- notify status

def test_nt_r8_status_exits_zero_and_prints_the_pending_count(tmp_path, monkeypatch, fake, capsys):
    world = configure(tmp_path, monkeypatch, fake)
    rc, output = run(world, "status", capsys=capsys)
    assert rc == 0, output
    assert "pending" in output.lower()
    assert fake.requests == []                             # status never publishes


def test_nt_r8_status_with_a_token_prints_no_token(tmp_path, monkeypatch, fake, capsys):
    world = configure(tmp_path, monkeypatch, fake, token_file=str(token_file(tmp_path)))
    rc, output = run(world, "status", capsys=capsys)
    assert rc == 0, output
    assert "pending" in output.lower()
    assert TOKEN not in output


def test_nt_r8_an_unknown_notify_action_is_a_usage_error(tmp_path, monkeypatch, fake, capsys):
    world = configure(tmp_path, monkeypatch, fake)
    rc, _ = run(world, "test", capsys=capsys)              # the subcommand group exists...
    assert rc == 0
    rc, _ = run(world, "frobnicate", capsys=capsys)        # ...and an unknown action is refused
    assert rc != 0
    assert len(fake.requests) == 1


# ---------------------------------------------------------------- NT-R7 through the CLI

@pytest.mark.parametrize("mode,status", [("accept", 200), ("status", 401), ("status", 500),
                                         ("redirect", 302), ("drop", 0)])
def test_nt_r7_the_token_appears_in_no_cli_output_or_file(
        tmp_path, monkeypatch, fake, capsys, caplog, mode, status):
    tf = token_file(tmp_path)
    world = configure(tmp_path, monkeypatch, fake, token_file=str(tf))
    fake.mode, fake.status = mode, status
    fake.location = fake.url + "/elsewhere"
    rc, output = run(world, "test", capsys=capsys)
    assert (rc == 0) == (mode == "accept"), output
    assert len(fake.requests) >= 1                         # the command really ran
    _, status_output = run(world, "status", capsys=capsys)
    assert TOKEN not in output and TOKEN not in status_output and TOKEN not in caplog.text
    assert leaks_of(TOKEN, tmp_path, state_root(), skip=(tf,)) == []
