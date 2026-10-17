"""The synthetic upstream for slice 4B.2: the test factory behind
MULTIAGENTS_TEST_GATEWAY_BACKEND (IC-C28, K4B2.5).

`factory(root, *, project)` returns an `Adapter` with the contract's synchronous
`open(method, path, headers, body, *, provider, run_id) -> response`. The
response exposes `.status`, `.headers`, `.read(size)` and `.close()`. Every
adapter is remembered by its root so a test reads what the gateway asked for,
and so a streaming response can be fed, held and cut by the test.

`broken` is a factory that fails: a factory failure refuses the launch.
"""
from __future__ import annotations

import queue
import threading
from pathlib import Path

ADAPTERS: dict[str, "Adapter"] = {}
SPEC = "ic_s4b2_backend:factory"
BROKEN_SPEC = "ic_s4b2_backend:broken"
MARKER = b"SYNTHETIC-UPSTREAM-OK"
GATE = 4                   # a bounded wait for a pending read to be unblocked


class Response:
    """A response whose body is fed by the test. `read` blocks until a chunk is
    fed or the response is closed; `close` unblocks every pending read."""

    def __init__(self, status=200, headers=None, body: bytes | None = None, stream=False):
        self.status = status
        self.headers = dict(headers or {})
        self.closed = threading.Event()
        self._chunks: queue.Queue = queue.Queue()
        self.stream = stream
        if not stream:
            self.headers.setdefault("content-type", "application/json")
            self._chunks.put(body if body is not None else MARKER)
            self._chunks.put(b"")

    def feed(self, data: bytes) -> None:
        self._chunks.put(data)

    def finish(self) -> None:
        self._chunks.put(b"")

    def read(self, size: int = -1) -> bytes:
        if self.closed.is_set():
            return b""
        item = self._chunks.get()
        if item is None:                # woken by close
            return b""
        if item == b"":
            self._chunks.put(b"")       # stay at end of body
        return item

    def close(self) -> None:
        self.closed.set()
        self._chunks.put(None)


class Adapter:
    def __init__(self, root, project):
        self.root, self.project = Path(root), project
        self.opened: list[dict] = []
        self.responses: list[Response] = []
        self.next: list[Response] = []        # queued responses, else a plain 200
        self.error: Exception | None = None
        self.lock = threading.Lock()

    def open(self, method, path, headers, body, *, provider, run_id):
        with self.lock:
            self.opened.append({"method": method, "path": path, "headers": dict(headers), "body": body,
                                "provider": provider, "run_id": run_id})
            if self.error:
                raise self.error
            response = self.next.pop(0) if self.next else Response()
            self.responses.append(response)
            return response

    def wait_opened(self, count=1, timeout=GATE) -> bool:
        import time
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if len(self.opened) >= count:
                return True
            time.sleep(0.01)
        return len(self.opened) >= count


def factory(root, *, project):
    adapter = ADAPTERS.get(str(root)) or Adapter(root, project)
    ADAPTERS[str(root)] = adapter
    return adapter


def broken(root, *, project):
    raise RuntimeError("the synthetic upstream cannot be built")
