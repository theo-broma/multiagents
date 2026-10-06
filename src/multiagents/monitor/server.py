"""The web front end: one page, a JSON API, and nothing listening off-machine.

`http.server` rather than a framework, for the same reason this project has two
dependencies: a monitor that made the tool harder to install would be a bad
trade for a page that polls every two seconds.

Three locks, and it takes all three, because the API can stop agents and
rewrite config.

* **The bind.** 127.0.0.1 and nothing else, so the network cannot reach it.
* **The token.** Minted per run and required on every route including `/`,
  it keeps out local processes that do not have the printed URL and cross-site
  browser requests. Localhost alone does not keep those callers out.
* **The Host header.** Defeats DNS rebinding: a site can point
  `local.evil.com` at 127.0.0.1, so the browser believes it is same-origin.
  Requests with a missing, empty or unrecognised Host are refused before
  anything is served.

None of the three moves to reach the monitor from a phone through
`tailscale serve`: `--allow-host` adds a name to the third, `--persistent-token`
to the second, and the bind stays 127.0.0.1 whatever is asked for (MT-R1..R7).
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import secrets
import stat
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from ..config import load as load_config
from ..paths import xdg_state_dir
from . import actions, quota, settings, snapshot

PAGE = Path(__file__).parent / "page.html"
QUOTA_PAGE = Path(__file__).parent / "quota.html"
QUOTA_ROUTES = ("/quota", "/api/quota", "/api/quota/identity")

LOOPBACK = "127.0.0.1"
LOOPBACK_NAMES = ("127.0.0.1", "localhost", "::1")
TOKEN_BYTES = 24                            # secrets.token_urlsafe(24): 32 characters
LABEL = re.compile(r"[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?")


class MonitorHTTPServer(ThreadingHTTPServer):
    request_queue_size = 64


class Handler(BaseHTTPRequestHandler):
    paths = None                              # set by serve()
    token = ""
    bound_host = LOOPBACK
    allowed_hosts = frozenset()               # lowercased; set by serve()

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
        self.end_headers()
        self.wfile.write(body)

    def end_headers(self) -> None:
        # Include refusals and http.server's method errors, too.
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "same-origin")
        super().end_headers()

    def _json(self, data, code: int = 200) -> None:
        self._send(code, json.dumps(data, default=str).encode(), "application/json")

    def _authorised(self, query: dict) -> bool:
        header = self.headers.get("X-Monitor-Token", "")
        given = header or (query.get("token") or [""])[0]
        return bool(given) and secrets.compare_digest(given.encode(), self.token.encode())

    @staticmethod
    def _host_name(raw: str) -> str:
        """The name in a Host header, without its port and without its brackets."""
        host = (raw or "").strip()
        if host.startswith("[") and host.endswith("]"):
            return host[1:-1]
        return host.rsplit(":", 1)[0].strip("[]") if ":" in host else host

    def _host_is_ours(self) -> bool:
        """Defeat DNS rebinding: only our own names may ask, by any name.

        Checked on EVERY route including `/`, before checking the token or
        serving the page. An `--allow-host` name is an exact, case-insensitive
        match: `x.example.ts.net` does not admit `evil.x.example.ts.net`, so
        adding a proxy's name cannot widen this to a domain (MT-R1).
        """
        name = self._host_name(self.headers.get("Host") or "")
        if not name:
            return False
        return (name in LOOPBACK_NAMES or name == self.bound_host
                or name.lower() in self.allowed_hosts)

    def _origin_is_ours(self) -> bool:
        """A browser POSTs cross-origin with an Origin; refuse the ones that are
        not this same site.

        The Origin must name the same host as the Host the request arrived with,
        compared case-insensitively. On loopback that has always meant the exact
        scheme and port, because that pair is what the browser saw. Behind a
        reverse proxy the browser is talking https to the proxy on a port the
        proxy picked, and the proxy is talking http to us on ours, so for an
        allowed name either scheme is right and the ports are not compared
        (MT-R2). No Origin at all keeps today's behaviour: not a browser POST.
        """
        origin = self.headers.get("Origin")
        if origin is None:
            return True
        if not self._host_is_ours():
            return False
        try:
            parsed = urlparse(origin)
            host = urlparse("//" + (self.headers.get("Host") or "").strip())
            for part in (parsed, host):
                if part.username is not None or part.password is not None:
                    return False
                if part.path or part.query or part.fragment:
                    return False
            if parsed.hostname is None or host.hostname is None:
                return False
            # `hostname` is already lowercased by urlsplit, which is the
            # case-insensitivity MT-R2 asks for.
            if parsed.hostname != host.hostname:
                return False
            if host.hostname in LOOPBACK_NAMES or host.hostname == self.bound_host:
                return (parsed.scheme == "http" and
                        (parsed.port if parsed.port is not None else 80) ==
                        (host.port if host.port is not None else 80))
            return (host.hostname in self.allowed_hosts and
                    parsed.scheme in ("http", "https"))
        except ValueError:
            return False

    def _quota_authorised(self) -> bool:
        given = self.headers.get("X-Monitor-Token", "")
        return bool(given) and secrets.compare_digest(given.encode(), self.token.encode())

    def _quota_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        if length < 0 or length > 16384:
            raise ValueError("invalid body size")
        return self.rfile.read(length)

    # -- routes -----------------------------------------------------------

    def do_GET(self) -> None:                 # noqa: N802 - http.server's API
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        route = parsed.path

        if not self._host_is_ours():
            return self._send(403, b"bad host", "text/plain")
        if route in QUOTA_ROUTES:
            if not self._origin_is_ours():
                return self._json({"error": "bad origin"}, 403)
            if route == "/quota":
                return self._send(200, b"Reopen quota details from the monitor.", "text/plain")
            if not self._quota_authorised():
                return self._json({"error": "bad or missing token"}, 403)
            if route == "/api/quota":
                try:
                    return self._json({"providers": quota.view(self.paths, load_config(self.paths))})
                except Exception:
                    return self._json({"error": "quota details unavailable"})
            return self._json({"error": "method not allowed"}, 405)
        if route in ("/", "/index.html"):
            if not self._authorised(query):
                return self._send(403, b"Open the URL printed when the monitor started.",
                                  "text/plain; charset=utf-8")
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

    def do_HEAD(self) -> None:                # noqa: N802
        if not self._host_is_ours():
            self.send_response(403)
        else:
            self.send_response(405)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self) -> None:                # noqa: N802
        parsed = urlparse(self.path)
        if not self._host_is_ours():
            return self._send(403, b"bad host", "text/plain")
        if parsed.path in ("/", "/index.html"):
            return self._send(405, b"method not allowed", "text/plain")
        if parsed.path in QUOTA_ROUTES:
            if not self._origin_is_ours():
                return self._json({"error": "bad origin"}, 403)
            if parsed.path == "/quota":
                try:
                    form = parse_qs(self._quota_body().decode())
                    given = (form.get("token") or [""])[0]
                except (ValueError, OSError):
                    return self._json({"error": "bad request"}, 400)
                if not given or not secrets.compare_digest(given.encode(), self.token.encode()):
                    return self._json({"error": "bad or missing token"}, 403)
                page = QUOTA_PAGE.read_text().replace("__TOKEN_JSON__", json.dumps(self.token))
                return self._send(200, page.encode(), "text/html; charset=utf-8")
            if not self._quota_authorised():
                return self._json({"error": "bad or missing token"}, 403)
            if parsed.path != "/api/quota/identity":
                return self._json({"error": "method not allowed"}, 405)
            try:
                payload = json.loads(self._quota_body())
                if not isinstance(payload, dict):
                    raise ValueError("invalid request")
                identity = quota.reveal(self.paths, load_config(self.paths),
                                        payload.get("provider"), payload.get("account"))
            except quota.IdentityBusy:
                return self._json({"identity": None, "status": "busy"})
            except ValueError:
                return self._json({"error": "invalid provider/account request"}, 400)
            except Exception:
                identity = None
            return self._json({"identity": identity})
        if parsed.path != "/api/action":
            return self._json({"error": "unknown endpoint"}, 404)
        if not self._origin_is_ours():
            return self._json({"error": "bad origin"}, 403)
        if not self._authorised(parse_qs(parsed.query)):
            return self._json({"ok": False, "message": "bad or missing token"}, 403)
        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, OSError) as exc:
            return self._json({"ok": False, "message": f"bad request: {exc}"}, 400)
        name = str(payload.get("action") or "")
        return self._json(actions.perform(self.paths, name, payload))


def allow_host_error(value: str) -> str:
    """Why `--allow-host` cannot take `value`, or "" when it can (MT-R3).

    Only a plain host name. A wildcard, a path, a scheme, a port, whitespace or
    an IP literal each mean something other than the one name the user meant,
    and every one of them is a way to end up serving a wider set of Hosts than
    the one that was typed — so they are refused at start instead.
    """
    if not value:
        return "the value is empty"
    reasons = []
    if any(c in value for c in "*/:"):
        reasons.append("a wildcard, a slash or a colon")
    if any(c.isspace() for c in value):
        reasons.append("whitespace")
    if reasons:
        return "it contains " + " or ".join(reasons)
    try:
        ipaddress.ip_address(value)
    except ValueError:
        pass
    else:
        return "it is an IP address, not a name; loopback is served already"
    if len(value) > 253 or not all(LABEL.fullmatch(part) for part in value.split(".")):
        return "it is not a plain host name"
    return ""


class TokenRefused(Exception):
    """A stored token the monitor will not use — and will not replace (MT-R4)."""


def token_path(paths) -> Path:
    """This project's stored token, one file per project.

    Under the user's state directory and never in the project tree: an agent's
    worktree, a mounted checkout or a `.multiagents/` an agent can read would
    all turn a token that gates every route into a file in a prompt.
    """
    return xdg_state_dir() / f"{paths.slug}.token"


def store_token(paths, token: str) -> str:
    """Put `token` where the next start will find it, 0600 in a 0700 directory."""
    path = token_path(paths)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    try:
        path.chmod(0o600)          # a file left unreadable must still be rotatable
    except OSError:
        pass
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        # chmod rather than trust the open mode: the file may be one that
        # something else, or an older run, created with other permissions.
        os.fchmod(handle.fileno(), 0o600)
        handle.write(token)
    return token


def stored_token(paths, minted: str) -> str:
    """The token to serve with: the stored one, or a mint on the first start.

    A stored token that cannot be trusted raises rather than being quietly
    replaced: the point of the flag is that the bookmark keeps working, and a
    fresh token behind the user's back would look like it did (MT-R4).
    """
    path = token_path(paths)
    if not path.is_file():
        return store_token(paths, minted)
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise TokenRefused(f"{path} is readable by group or others (mode {mode:03o})")
    try:
        token = path.read_text().strip()
    except (OSError, UnicodeDecodeError) as exc:
        raise TokenRefused(f"{path} cannot be read ({exc})") from exc
    if not token:
        raise TokenRefused(f"{path} is empty")
    if len(token) < len(minted):
        raise TokenRefused(f"{path} holds a {len(token)}-character token, shorter than "
                           f"the {len(minted)} this monitor mints")
    return token


def serve(paths, port: int = 8787, open_browser: bool = True,
          host: str = LOOPBACK, allow_hosts=(), persistent_token: bool = False,
          rotate_token: bool = False) -> int:
    """Run the monitor until interrupted. Returns an exit code.

    `allow_hosts` are extra Host names to answer to, for a reverse proxy that
    forwards the public Host (`tailscale serve --bg 8787`): the bind stays
    127.0.0.1 and the token is still required on every route (MT-R1..R3).
    `persistent_token` reuses the token stored in the user's state directory, so
    a bookmark on another device keeps working across restarts, and
    `rotate_token` replaces it (MT-R4, MT-R5).
    """
    names, refused = [], []
    for value in allow_hosts:
        why = allow_host_error(value)
        if why:
            refused.append((value, why))
        elif value.lower() not in {n.lower() for n in names}:
            names.append(value)
    if refused:
        for value, why in refused:
            print(f"--allow-host {value!r}: {why}", file=sys.stderr)
        print("nothing is serving; --allow-host takes one plain host name, "
              "such as <host>.<tailnet>.ts.net", file=sys.stderr)
        return 2

    minted = secrets.token_urlsafe(TOKEN_BYTES)
    token = minted
    if rotate_token:
        try:
            token = store_token(paths, minted)
        except OSError as exc:
            print(f"could not store the monitor token — {exc}", file=sys.stderr)
            return 1
    elif persistent_token:
        try:
            token = stored_token(paths, minted)
        except TokenRefused as exc:
            print(f"stored monitor token unusable — {exc}", file=sys.stderr)
            print("repair that file, or start with --rotate-token to replace it",
                  file=sys.stderr)
            return 1
        except OSError as exc:
            # A path the monitor may not write — the token path being a
            # directory, the state directory read-only — is the same kind of
            # refusal as an unusable token, and `--rotate-token` above already
            # treats it as one rather than as a crash (MT-R4).
            print(f"could not store the monitor token at {token_path(paths)} — {exc}",
                  file=sys.stderr)
            print("check that this path is writable, or start with --rotate-token "
                  "to replace it", file=sys.stderr)
            return 1

    Handler.paths = paths
    Handler.token = token
    Handler.bound_host = host
    Handler.allowed_hosts = frozenset(name.lower() for name in names)

    try:
        httpd = MonitorHTTPServer((host, port), Handler)
    except OSError as exc:
        print(f"could not listen on {host}:{port} — {exc}")
        print("another monitor may already be running; --port picks a different one")
        return 1

    url = f"http://{host}:{httpd.server_port}/?token={Handler.token}"
    print(f"monitor      {paths.root.name}")
    print(f"             {url}")
    for name in names:
        # The proxy terminates TLS, so the phone's URL has no port of ours in it.
        print(f"             https://{name}/?token={Handler.token}")
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
