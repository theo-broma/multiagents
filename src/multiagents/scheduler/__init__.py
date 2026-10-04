"""Host plan scheduler. M1 serves durable plans and never launches runs.

The capability seams are deliberately host-only, absent from RPC dispatch.
"""
from __future__ import annotations

import fcntl
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from ..scheduler_config import settings
from ..paths import ProjectPaths
from ..tree import now
from .store import (Store, directory, encode, issue_run_capability, private_directory,
                    revoke_run_capability, root_capability, transport_directory)


def enabled(project_root: Path) -> bool:
    return settings(project_root).get("enabled", False)


def pending_nodes(project_root: Path) -> list[str]:
    return Store(project_root).pending()


def call(project_root: Path, op: str, args: dict, token: str | None,
         request_id: str | None = None) -> dict:
    """A fresh connection per call also reconnects after scheduler restarts."""
    request = {"op": op, "args": args, "token": token,
               "request_id": request_id or uuid.uuid4().hex}
    timeout = args.get("timeout", 0) if op == "wait_for_nodes" else 0
    if type(timeout) not in (int, float) or timeout < 0:
        timeout = 0
    for attempt in range(2):
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
                conn.settimeout(max(10, timeout + 10))
                conn.connect(str(transport_directory(project_root) / "rpc.sock"))
                conn.sendall(encode(request).encode() + b"\n")
                with conn.makefile("rb") as reader:
                    line = reader.readline()
                    if not line:
                        raise ConnectionError("scheduler closed the connection")
                    return json.loads(line)
        except OSError:
            if attempt:
                raise
            # The first request may already have committed. Reconnect with
            # exactly the same request id and payload to reconcile its reply.
            time.sleep(0.05)


def status(project_root: Path) -> dict:
    try:
        reply = call(project_root, "scheduler_status", {}, root_capability(project_root))
        return reply.get("result") or reply.get("error")
    except (OSError, ValueError):
        return {"error": "scheduler_unavailable"}


def start(project_root: Path, foreground=False) -> dict:
    if not enabled(project_root):
        pending = pending_nodes(project_root)
        return {"error": "scheduler_disabled", **({"pending_nodes": len(pending)} if pending else {})}
    current = status(project_root)
    if "error" not in current:
        return current
    if foreground:
        serve(project_root)
        return {}
    proc = subprocess.Popen(
        [sys.executable, "-m", "multiagents.cli", "--path", str(project_root),
         "scheduler", "start", "--foreground"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True, cwd=project_root)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        current = status(project_root)
        if "error" not in current:
            return current
        if proc.poll() not in (None, 0):
            break
        # A child that exited zero may have lost the singleton race. Keep
        # waiting for the winning process to make its socket ready.
        time.sleep(0.05)
    if proc.poll() is None:
        proc.terminate()
        proc.wait(timeout=10)
    return {"error": "scheduler_unavailable"}


def stop(project_root: Path) -> dict:
    from .. import procs
    record = directory(project_root) / "process.json"
    try:
        info = json.loads(record.read_text())
        pid, start_time = info["pid"], info["pid_start"]
    except (OSError, ValueError, KeyError):
        return {"stopped": True}
    if not procs.alive(pid, start_time):
        return {"stopped": True}
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + 10
    while record.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    return {"stopped": not record.exists()}


def serve(project_root: Path) -> None:
    from .. import procs
    from .rpc import Handler, Server, Service
    os.umask(0o077)
    store = Store(project_root)
    private_directory(store.directory)
    with (store.directory / "singleton.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        store.initialize()
        private_directory(transport_directory(project_root))
        sock = transport_directory(project_root) / "rpc.sock"
        sock.unlink(missing_ok=True)
        since = now()
        service = Service(project_root, since)
        stopped = threading.Event()
        signal.signal(signal.SIGTERM, lambda *_: stopped.set())
        signal.signal(signal.SIGINT, lambda *_: stopped.set())
        record = store.directory / "process.json"
        record.write_text(encode({"pid": os.getpid(), "pid_start": procs.start_time(os.getpid()), "since": since}))
        try:
            with Server(str(sock), Handler) as server:
                server.service = service
                server.timeout = 0.2
                sock.chmod(0o600)
                with store.transaction() as db:
                    store.transition(db, "scheduler_started")
                store.mirror()
                retry_at = time.monotonic() + 1
                while not stopped.is_set():
                    server.handle_request()
                    if time.monotonic() >= retry_at:
                        store.mirror()
                        retry_at = time.monotonic() + 1
                with service.changed:
                    service.stopping = True
                    with store.transaction() as db:
                        store.transition(db, "scheduler_stopped")
                    store.mirror()
                    service.changed.notify_all()
        finally:
            sock.unlink(missing_ok=True)
            record.unlink(missing_ok=True)
