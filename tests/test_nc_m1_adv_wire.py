"""Adversary (M1, NC-R8/NC-R15): malformed wire input must get an error reply
on the same connection, and the scheduler must keep serving afterwards.

Each test sends one hostile line, then asserts (1) a reply line arrived with
`ok: false`, and (2) a normal `scheduler_status` still answers.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

from nc_harness import live, nc  # noqa: E402,F401


def _send_raw(live, line: bytes, timeout: float = 15) -> bytes:
    """Send one raw line; return the first reply line, or b"" if the scheduler
    closed the connection without answering."""
    with live.connect(timeout) as conn:
        conn.sendall(line)
        buf = b""
        while b"\n" not in buf:
            chunk = conn.recv(65536)
            if not chunk:
                break
            buf += chunk
    return buf.split(b"\n", 1)[0]


def _still_serving(live):
    reply = live.rpc("scheduler_status")
    assert reply.get("ok") is True, reply


def _assert_error_reply(raw: bytes):
    assert raw, "the scheduler closed the connection without any reply"
    reply = json.loads(raw)
    assert reply.get("ok") is False, reply


def test_adv_deeply_nested_json_gets_an_error_reply(live):
    # 200k nested arrays: a valid-looking single line well under the size cap.
    line = b'{"op":"scheduler_status","args":' + b"[" * 200_000 + b"]" * 200_000 + b"}\n"
    _assert_error_reply(_send_raw(live, line))
    _still_serving(live)


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity", "1e400"])
def test_adv_non_finite_request_id_gets_an_error_reply(live, literal):
    # Python's json accepts these; the scheduler echoes request_id into the reply.
    token = live.root_token()
    line = ('{"op":"scheduler_status","args":{},"token":%s,"request_id":%s}\n'
            % (json.dumps(token), literal)).encode()
    _assert_error_reply(_send_raw(live, line))
    _still_serving(live)


def test_adv_wait_with_a_cursor_past_sqlite_integer_range_gets_a_reply(live):
    token = live.root_token()
    line = json.dumps({"op": "wait_for_nodes", "token": token, "request_id": "big-cursor",
                       "args": {"cursor": 2 ** 63, "timeout": 0}}).encode() + b"\n"
    raw = _send_raw(live, line)
    assert raw, "the scheduler closed the connection without any reply"
    _still_serving(live)


def test_adv_wait_with_an_integer_timeout_too_large_for_a_float_gets_a_reply(live):
    token = live.root_token()
    line = ('{"op":"wait_for_nodes","token":%s,"request_id":"big-timeout",'
            '"args":{"timeout":1%s}}\n' % (json.dumps(token), "0" * 400)).encode()
    _assert_error_reply(_send_raw(live, line))
    _still_serving(live)


def test_adv_oversized_line_gets_an_error_reply(live):
    line = b'{"op":"scheduler_status","args":{"pad":"' + b"x" * (2 * 1024 * 1024) + b'"}}\n'
    _assert_error_reply(_send_raw(live, line))
    _still_serving(live)


def test_adv_run_token_wait_with_huge_cursor_gets_a_reply(live):
    node = live.create(task="n")
    token = live.issue("run-big", node["id"], {"read"})
    line = json.dumps({"op": "wait_for_nodes", "token": token, "request_id": "rb",
                       "args": {"cursor": 10 ** 20, "timeout": 0}}).encode() + b"\n"
    assert _send_raw(live, line), "no reply for a run's wait with a huge cursor"
    _still_serving(live)
