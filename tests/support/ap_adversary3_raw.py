"""A loopback upstream that writes a fixed byte string as its response.

`ap_support.Upstream` goes through `BaseHTTPRequestHandler.send_header`,
which cannot put an obs-fold or a bare 1xx on the wire. This one answers
every request with exactly the bytes it was given, then closes. Nothing in
it leaves 127.0.0.1.
"""

from __future__ import annotations

import contextlib
import socket
import threading


@contextlib.contextmanager
def raw_upstream(response: bytes):
    """Yields the upstream URL; `requests` on the returned list grows by
    one raw request head per connection."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(16)
    server.settimeout(0.05)
    stop = threading.Event()

    def answer(conn: socket.socket) -> None:
        with conn:
            conn.settimeout(5)
            data = b""
            try:
                while b"\r\n\r\n" not in data:
                    chunk = conn.recv(65536)
                    if not chunk:
                        return
                    data += chunk
                conn.sendall(response)
                conn.shutdown(socket.SHUT_WR)
                while conn.recv(65536):
                    pass
            except OSError:
                pass

    def serve() -> None:
        while not stop.is_set():
            try:
                conn, _ = server.accept()
            except (socket.timeout, OSError):
                continue
            threading.Thread(target=answer, args=(conn,), daemon=True).start()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.getsockname()[1]}"
    finally:
        stop.set()
        thread.join(1)
        server.close()
