"""Harness for the ntfy notification contract tests (NT-R*),
`context/specs/ntfy-notifications.md`, first round: config, the `notify` tool,
the CLI.

Everything is a client's view: a project whose `project.yaml` carries a
`notify:` section, a fake ntfy server on 127.0.0.1 (port 0), and the leak scan
over files. Nothing here imports a private name of the implementation, and no
real host name appears anywhere: URLs are 127.0.0.1 or `example.invalid`.
"""
from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import c3_harness as h  # noqa: E402
import sc_harness as sc  # noqa: E402

TOKEN = "tk_SECRETsecret0123456789abcdef"
HANG_CAP = 20            # a hanging handler gives up by itself after this long
TOOL_BOUND = 12          # NT-R2's 10 s plus slack for a loaded machine


class Request:
    def __init__(self, method, path, headers, body):
        self.method, self.path, self.body = method, path, body
        self.headers = {k.lower(): v for k, v in headers}
        self.raw_names = [k for k, _ in headers]

    def text(self) -> str:
        return self.body.decode("utf-8")


class FakeNtfy:
    """A local HTTP server that records what it receives.

    `mode`: "accept" (200 JSON), "status" (answer `status`), "redirect" (answer
    `status` with `Location: location`), "hang" (read the request, never
    answer), "drip" (answer 200 and trickle the body forever), "drop" (close
    the connection without an answer).
    """

    def __init__(self):
        self.requests: list[Request] = []
        self.mode = "accept"
        self.status = 200
        self.location = ""
        self.extra_headers: dict[str, str] = {}      # sent with every "status" answer (e.g. Retry-After)
        self.release = threading.Event()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def handle_any(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else b""
                fake.requests.append(Request(self.command, self.path,
                                             list(self.headers.items()), body))
                mode = fake.mode
                if mode == "hang":
                    fake.release.wait(HANG_CAP)
                    self.close_connection = True
                    return
                if mode == "drop":
                    self.close_connection = True
                    try:
                        self.request.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    return
                if mode == "drip":
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", "100000")
                    self.end_headers()
                    try:
                        for _ in range(int(HANG_CAP / 0.2)):
                            if fake.release.wait(0.2):
                                break
                            self.wfile.write(b" ")
                            self.wfile.flush()
                    except OSError:
                        pass
                    self.close_connection = True
                    return
                out = json.dumps({"id": "x", "event": "message"}).encode()
                self.send_response(fake.status if mode in ("status", "redirect") else 200)
                if mode == "redirect":
                    self.send_header("Location", fake.location)
                if mode == "status":
                    for name, value in fake.extra_headers.items():
                        self.send_header(name, value)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            do_POST = do_PUT = do_GET = handle_any

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.release.set()
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(5)


def closed_port_url() -> str:
    """A 127.0.0.1 URL on which nothing listens (connection refused)."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return f"http://127.0.0.1:{port}"


def token_file(tmp: Path, content: str = TOKEN + "\n", mode: int = 0o600,
               name: str = "ntfy-token") -> Path:
    path = tmp / name
    path.write_text(content)
    os.chmod(path, mode)
    return path


class NotifyProject:
    """A real project directory whose `notify:` section the test controls."""

    def __init__(self, tmp: Path, monkeypatch, notify="absent"):
        h.as_root(monkeypatch)
        self.tmp = tmp
        self.p = sc.Project(tmp)
        self.p.add_provider("acme")
        self.p.add_agent("worker", "acme", "acme/m1")
        self.root = self.p.root
        self.monkeypatch = monkeypatch
        monkeypatch.setenv("MULTIAGENTS_PROJECT", str(self.root))
        self.set_notify(notify)

    def set_notify(self, notify) -> None:
        if notify == "absent":
            self.p.project.pop("notify", None)
        else:
            self.p.project["notify"] = notify
        self.p.write()
        from multiagents import server
        server._reset()


def leaks_of(secret: str, *roots: Path, skip: tuple[Path, ...] = ()) -> list[str]:
    """Files under `roots` whose bytes contain `secret`."""
    found = []
    needle = secret.encode()
    for root in roots:
        for dirpath, _dirs, files in os.walk(root):
            for name in files:
                path = Path(dirpath) / name
                if path in skip or path.is_symlink():
                    continue
                try:
                    if needle in path.read_bytes():
                        found.append(str(path))
                except OSError:
                    pass
    return found


def wait_until(predicate, timeout: float = 3.0, interval: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def plan_db(world) -> Path:
    """The scheduler's own store: the only place its transitions are visible to
    a test. Read it through `sqlite3` in read-only mode."""
    files = [p for p in world.scheduler_files() if p.name == "plan.sqlite3"]
    assert files, "the scheduler has no database"
    return files[0]
