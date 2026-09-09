"""The web front end: one page, a JSON API, and nothing listening off-machine.

`http.server` rather than a framework, for the same reason this project has two
dependencies: a monitor that made the tool harder to install would be a bad
trade for a page that polls every two seconds.

Three locks, and it takes all three, because the API can stop agents and
rewrite config.

* **The bind.** 127.0.0.1 and nothing else, so the network cannot reach it.
* **The token.** Minted per run, embedded in the page it serves, required on
  every call — localhost is reachable by other programs on this machine, a
  hostile browser tab included.
* **The Host header.** Which is the one that is easy to miss, and an advisor
  did not miss it: a site can point `local.evil.com` at 127.0.0.1, so the
  *browser* believes it is same-origin and sends the request without a
  preflight. The bind sees a loopback connection and the request looks
  ordinary — and `GET /` would hand back the page with the token in it. So a
  request whose Host is not a loopback name we recognise is refused before
  anything is served.
"""

from __future__ import annotations

import json
import secrets
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from ..config import load as load_config
from . import actions, settings, snapshot

PAGE = Path(__file__).parent / "page.html"


class Handler(BaseHTTPRequestHandler):
    paths = None                              # set by serve()
    token = ""
    bound_host = "127.0.0.1"

    # -- plumbing ---------------------------------------------------------

    def log_message(self, *_args):            # noqa: D401 - quiet by default
        """A polling page would fill a terminal with 200s nobody reads."""

    def _send(self, code: int, body: bytes, kind: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        # The page talks only to itself; nothing here should ever be framed,
        # sniffed into another type, or fetched cross-origin.
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data, code: int = 200) -> None:
        self._send(code, json.dumps(data, default=str).encode(), "application/json")

    def _authorised(self, query: dict) -> bool:
        header = self.headers.get("X-Monitor-Token", "")
        given = header or (query.get("token") or [""])[0]
        return secrets.compare_digest(given, self.token)

    def _host_is_ours(self) -> bool:
        """Defeat DNS rebinding: only loopback names may ask, by any name.

        Checked on EVERY route including `/`, because `/` is the one that hands
        out the token — a rebinding attack that only gets the page has already
        got everything.
        """
        host = (self.headers.get("Host") or "").strip()
        name = host.rsplit(":", 1)[0].strip("[]") if ":" in host else host
        return name in ("127.0.0.1", "localhost", "::1", "") or name == self.bound_host

    # -- routes -----------------------------------------------------------

    def do_GET(self) -> None:                 # noqa: N802 - http.server's API
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        route = parsed.path

        if not self._host_is_ours():
            return self._send(403, b"bad host", "text/plain")
        if route in ("/", "/index.html"):
            page = PAGE.read_text().replace("__TOKEN__", self.token)
            return self._send(200, page.encode(), "text/html; charset=utf-8")
        if not route.startswith("/api/"):
            return self._send(404, b"not found", "text/plain")
        if not self._authorised(query):
            return self._json({"error": "bad or missing token"}, 403)

        config = load_config(self.paths)
        try:
            if route == "/api/state":
                scripts = (query.get("scripts") or ["1"])[0] != "0"
                return self._json(snapshot.snapshot(self.paths, config,
                                                    with_scripts=scripts))
            if route == "/api/settings":
                return self._json({"settings": settings.describe(self.paths, config),
                                   "editable": list(settings.EDITABLE)})
            if route == "/api/transcript":
                agent_id = (query.get("id") or [""])[0]
                return self._json(snapshot.transcript(self.paths, agent_id))
            if route == "/api/branches":
                return self._json({"branches": snapshot.branches(self.paths, config)})
            if route == "/api/checks":
                return self._json({"checks": snapshot.deep_checks(self.paths, config)})
            if route == "/api/events":
                return self._json({"events": snapshot.events(self.paths)})
        except Exception as exc:              # a broken view is a message, not a 500
            return self._json({"error": f"{type(exc).__name__}: {exc}"}, 200)
        return self._json({"error": "unknown endpoint"}, 404)

    def do_POST(self) -> None:                # noqa: N802
        parsed = urlparse(self.path)
        if not self._host_is_ours():
            return self._send(403, b"bad host", "text/plain")
        if parsed.path != "/api/action":
            return self._json({"error": "unknown endpoint"}, 404)
        if not self._authorised(parse_qs(parsed.query)):
            return self._json({"ok": False, "message": "bad or missing token"}, 403)
        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, OSError) as exc:
            return self._json({"ok": False, "message": f"bad request: {exc}"}, 400)
        name = str(payload.get("action") or "")
        return self._json(actions.perform(self.paths, name, payload))


def serve(paths, port: int = 8787, open_browser: bool = True,
          host: str = "127.0.0.1") -> int:
    """Run the monitor until interrupted. Returns an exit code."""
    Handler.paths = paths
    Handler.token = secrets.token_urlsafe(24)
    Handler.bound_host = host

    try:
        httpd = ThreadingHTTPServer((host, port), Handler)
    except OSError as exc:
        print(f"could not listen on {host}:{port} — {exc}")
        print("another monitor may already be running; --port picks a different one")
        return 1

    url = f"http://{host}:{httpd.server_port}/?token={Handler.token}"
    print(f"monitor      {paths.root.name}")
    print(f"             {url}")
    print("             ctrl-c to stop\n")
    if open_browser:
        threading.Timer(0.3, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")
    finally:
        httpd.server_close()
    return 0
