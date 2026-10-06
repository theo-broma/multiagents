"""NT-R2 round-2 regressions (review ag-f16f98, two blocking defects).

1. A token file whose content holds CR or LF past the trailing-newline strip
   is refused as a token-file problem, before any header is built — and the
   token appears in no result, no CLI output and no log (NT-R2, NT-R7).
   Before the fix, the token reached the `Authorization` header, http.client
   raised `ValueError("Invalid header value ...")` quoting it, and that text
   became the reason.
2. A server that trickles its response headers byte by byte cannot hold the
   tool past the 10 s overall bound: the call returns with a timeout reason
   (NT-R2). Before the fix, only per-operation socket timeouts applied, and
   a steady trickle kept `getresponse()` blocked far past 10 s.
"""
from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

from nc_harness import tool  # noqa: E402
from nt_harness import FakeNtfy, NotifyProject, leaks_of  # noqa: E402

from multiagents import cli, server  # noqa: E402
from multiagents.paths import state_root  # noqa: E402

FIRST_LINE = "tk_R2_first_LINE_0123456789abcdef"
SECOND_LINE = "tk_R2_SECOND_line_fedcba9876543210"
TRICKLE_BOUND = 11.0  # NT-R2's 10 s plus slack for a loaded machine


@pytest.fixture
def fake():
    f = FakeNtfy()
    yield f
    f.close()


def send(*args, **kwargs):
    return tool(server.notify, *args, **kwargs)


def cli_test(world, capsys):
    capsys.readouterr()
    try:
        rc = cli.main(["--path", str(world.root), "notify", "test"])
    except SystemExit as exc:
        rc = exc.code if isinstance(exc.code, int) else 1
    out, err = capsys.readouterr()
    return rc, out + err


def write_bad_token(tmp: Path, sep: str) -> Path:
    """A 0600 token file with an embedded line break (the trailing newline
    is stripped, the embedded one must refuse the send)."""
    path = tmp / "ntfy-token"
    path.write_text(f"{FIRST_LINE}{sep}{SECOND_LINE}\n")
    os.chmod(path, 0o600)
    return path


# ---------------------------------------------------------------- a broken token file


@pytest.mark.parametrize("sep", ["\n", "\r\n", "\r"])
def test_nt_r2_a_token_file_with_an_embedded_line_break_refuses_sending(
        tmp_path, monkeypatch, fake, caplog, sep):
    tf = write_bad_token(tmp_path, sep)
    NotifyProject(tmp_path, monkeypatch, {"ntfy_url": fake.url, "topic": "t1",
                                          "token_file": str(tf)})
    result = send("t", "m")
    assert isinstance(result, dict) and result.get("ok") is False
    assert isinstance(result.get("reason"), str) and result["reason"].strip() != ""
    assert fake.requests == []                        # refused before any header
    dumped = json.dumps(result)
    assert FIRST_LINE not in dumped and SECOND_LINE not in dumped
    assert FIRST_LINE not in caplog.text and SECOND_LINE not in caplog.text
    assert leaks_of(FIRST_LINE, tmp_path, state_root(), skip=(tf,)) == []
    assert leaks_of(SECOND_LINE, tmp_path, state_root(), skip=(tf,)) == []


@pytest.mark.parametrize("sep", ["\n", "\r\n", "\r"])
def test_nt_r2_a_token_file_with_an_embedded_line_break_leaks_nothing_through_the_cli(
        tmp_path, monkeypatch, fake, capsys, sep):
    tf = write_bad_token(tmp_path, sep)
    world = NotifyProject(tmp_path, monkeypatch, {"ntfy_url": fake.url, "topic": "t1",
                                                  "token_file": str(tf)})
    rc, output = cli_test(world, capsys)
    assert rc != 0 and output.strip() != ""
    assert "Traceback" not in output
    assert FIRST_LINE not in output and SECOND_LINE not in output
    assert fake.requests == []


# ---------------------------------------------------------------- a trickling server


class TricklingNtfy:
    """A fake ntfy server that answers 200 but sends its response headers
    one byte at a time. Bytes keep arriving, so a per-operation socket
    timeout never fires; only a true overall deadline ends the call."""

    INTERVAL = 0.25  # seconds between bytes: ~71 header bytes take ~18 s

    def __init__(self):
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.url = f"http://127.0.0.1:{self.sock.getsockname()[1]}"
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        try:
            client, _ = self.sock.accept()
        except OSError:
            return
        try:
            client.settimeout(30)
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = client.recv(4096)
                if not chunk:
                    return
                data += chunk
            response = (b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                        b"Content-Length: 27\r\n\r\n"
                        b'{"id": "x", "event": "message"}')
            for i in range(len(response)):
                time.sleep(self.INTERVAL)
                client.sendall(response[i:i + 1])
        except OSError:
            pass
        finally:
            try:
                client.close()
            except OSError:
                pass

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass
        self.thread.join(5)


def test_nt_r2_a_server_that_trickles_its_headers_cannot_hold_the_tool_past_ten_seconds(
        tmp_path, monkeypatch):
    trickle = TricklingNtfy()
    try:
        NotifyProject(tmp_path, monkeypatch, {"ntfy_url": trickle.url, "topic": "t1"})
        started = time.monotonic()
        result = send("t", "m")
        elapsed = time.monotonic() - started
        assert elapsed < TRICKLE_BOUND, elapsed
        assert isinstance(result, dict) and result.get("ok") is False
        assert "timeout" in str(result.get("reason", "")).lower()
    finally:
        trickle.close()
