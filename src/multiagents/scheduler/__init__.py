"""Host plan scheduler and the client-side launch admission bridge.

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


def start(project_root: Path, foreground=False, clock_file=None) -> dict:
    if not enabled(project_root):
        pending = pending_nodes(project_root)
        return {"error": "scheduler_disabled", **({"pending_nodes": len(pending)} if pending else {})}
    current = status(project_root)
    if "error" not in current:
        return current
    if foreground:
        serve(project_root, clock_file=clock_file)
        return {}
    private_directory(directory(project_root))
    log_path = directory(project_root) / "scheduler.log"
    log_fd = os.open(log_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    os.fchmod(log_fd, 0o600)
    log = os.fdopen(log_fd, "ab")
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "multiagents.cli", "--path", str(project_root),
             "scheduler", "start", "--foreground",
             *(["--clock-file", str(clock_file)] if clock_file else [])],
            stdin=subprocess.DEVNULL, stdout=log, stderr=log,
            start_new_session=True, cwd=project_root)
    finally:
        log.close()
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
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
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


def serve(project_root: Path, clock_file=None) -> None:
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
        from .engine import Engine
        service.engine = Engine(service, clock_file=clock_file)
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
                service.engine.start()
                retry_at = time.monotonic() + 1
                while not stopped.is_set():
                    server.handle_request()
                    if time.monotonic() >= retry_at:
                        store.mirror()
                        retry_at = time.monotonic() + 1
                service.engine.stop()
                with service.changed:
                    service.stopping = True
                    with store.transaction() as db:
                        store.transition(db, "scheduler_stopped")
                    store.mirror()
                    service.changed.notify_all()
        finally:
            sock.unlink(missing_ok=True)
            record.unlink(missing_ok=True)


def submit(project_root, agent, task, *, caller=None, request_id=None, **kwargs):
    """Deposit work and wait for the first bounded admission result (NC-R51)."""
    run_server = "MULTIAGENTS_AGENT_ID" in os.environ or caller is not None
    token = os.environ.get("MULTIAGENTS_RPC_TOKEN") if run_server else None
    if run_server and not token:
        return {"error": "unauthenticated"}
    try:
        token = token if run_server else root_capability(project_root)
        fields = {k: v for k, v in kwargs.items() if v is not None and not (k == "timeout" and v == 0)}
        reply = call(project_root, "start_agent", {"agent": agent, "task": task, **fields},
                     token, request_id=request_id)
        if not reply.get("ok"):
            return reply["error"]
        id = reply["result"]["id"]
        deadline = time.monotonic() + settings(project_root)["admission_timeout_seconds"]
        while True:
            reply = call(project_root, "get_node", {"id": id}, token)
            if not reply.get("ok"):
                return reply["error"]
            node = reply["result"]
            result = {"node_id": id, "status": node["state"]}
            if node["runs"]:
                result["agent_id"] = node["runs"][-1]["run_id"]
                return result
            if node.get("blocked") or time.monotonic() >= deadline:
                return {**result, "blocked": node.get("blocked", [])}
            time.sleep(0.05)
    except (OSError, ValueError):
        return {"error": "scheduler_unavailable"}


def resume_admission(project_root, run_id, caller=None):
    try:
        token = os.environ.get("MULTIAGENTS_RPC_TOKEN") if caller else root_capability(project_root)
        reply = call(project_root, "admit_run", {"run_id": run_id}, token)
        return reply["result"] if reply.get("ok") else reply["error"]
    except (OSError, ValueError):
        return {"error": "scheduler_unavailable"}


def agent_admission(project_root, agent, caller=None, model=None):
    try:
        token = os.environ.get("MULTIAGENTS_RPC_TOKEN") if caller else root_capability(project_root)
        fields = {"agent": agent, **({"model": model} if model else {})}
        reply = call(project_root, "admit_agent", fields, token)
        return reply["result"] if reply.get("ok") else reply["error"]
    except (OSError, ValueError):
        return {"error": "scheduler_unavailable"}


def steer_managed(project_root, run_id, message, caller=None):
    """Ask the owning supervisor to perform Runner's ordinary session handoff."""
    try:
        token = os.environ.get("MULTIAGENTS_RPC_TOKEN") if caller else root_capability(project_root)
        reply = call(project_root, "steer_run", {"run_id": run_id, "message": message}, token)
        result = reply["result"] if reply.get("ok") else reply["error"]
        if "command_id" not in result:
            return result
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            reply = call(project_root, "steer_result", {"run_id": run_id, "command_id": result["command_id"]}, token)
            if not reply.get("ok"):
                return reply["error"]
            if reply["result"]["result"] is not None:
                return reply["result"]["result"]
            time.sleep(0.05)
        return {"error": "scheduler_unavailable"}
    except (OSError, ValueError):
        return {"error": "scheduler_unavailable"}
