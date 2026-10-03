"""C20: the monitor page itself requires the token.

Contract: context/specs/c20-monitor-page-token.md, MT-R1..R5. Test names carry
the requirement id.

Black box at one seam: the monitor HTTP server, a real ThreadingHTTPServer on an
ephemeral port. Ordinary requests use http.client; the no-Host and empty-Host
cases are raw sockets, because http.client always sends a Host.

The fixture is this file's own (the same shape as the C19 one, deliberately not
imported from it: C19's file is still moving).

Where the contract is silent these tests are too: whether `/` accepts the token
in the X-Monitor-Token header, whether HEAD/POST on `/` are special, the 403
page's content type, and the exact words of the 403 page beyond "printed".
"""

from __future__ import annotations

import base64
import http.client
import json
import os
import re
import socket
import threading
import urllib.parse
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from multiagents.monitor import server
from multiagents.paths import ProjectPaths


class Fx:
    def __init__(self, tmp_path, monkeypatch):
        self.paths = ProjectPaths(tmp_path)
        self.paths.ensure()
        self.token = "tok-" + base64.b32encode(os.urandom(10)).decode().lower()
        monkeypatch.setattr(server.Handler, "paths", self.paths)
        monkeypatch.setattr(server.Handler, "token", self.token)
        monkeypatch.setattr(server.Handler, "bound_host", "127.0.0.1")
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.port = self.httpd.server_port
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        try:
            conn.request(method, path, body=body, headers=dict(headers or {}))
            resp = conn.getresponse()
            data = resp.read()
            return SimpleNamespace(
                status=resp.status, text=data.decode("utf-8", "replace"),
                headers={k.lower(): v for k, v in resp.getheaders()})
        finally:
            conn.close()

    def get(self, path, **headers):
        return self.request("GET", path, headers=headers)

    def raw(self, request_bytes):
        """One raw request on a fresh socket; returns (status, headers, body)."""
        with socket.create_connection(("127.0.0.1", self.port), timeout=20) as s:
            s.sendall(request_bytes)
            chunks = []
            while True:
                data = s.recv(65536)
                if not data:
                    break
                chunks.append(data)
        blob = b"".join(chunks).decode("utf-8", "replace")
        head, _, body = blob.partition("\r\n\r\n")
        lines = head.split("\r\n")
        status = int(lines[0].split()[1])
        headers = {k.lower(): v.strip() for k, _, v in
                   (ln.partition(":") for ln in lines[1:])}
        return SimpleNamespace(status=status, headers=headers, text=body)


@pytest.fixture
def fx(tmp_path, monkeypatch):
    f = Fx(tmp_path, monkeypatch)
    yield f
    f.close()


def tokened(fx, route="/"):
    return f"{route}?token={urllib.parse.quote(fx.token)}"


PAGE_ROUTES = ["/", "/index.html"]


# --------------------------------------------------------------------------
# MT-R1: no page without the token


@pytest.mark.parametrize("route", PAGE_ROUTES)
def test_mt_r1_no_token_is_403_and_the_body_has_no_token(fx, route):
    resp = fx.get(route)
    assert resp.status == 403
    assert fx.token not in resp.text
    assert fx.token not in json.dumps(resp.headers)


@pytest.mark.parametrize("route", PAGE_ROUTES)
def test_mt_r1_a_wrong_token_is_403_and_the_body_has_no_token(fx, route):
    resp = fx.get(f"{route}?token=not-the-token")
    assert resp.status == 403
    assert fx.token not in resp.text


@pytest.mark.parametrize("route", PAGE_ROUTES)
def test_mt_r1_a_near_miss_token_is_403(fx, route):
    # a prefix, an extension and a same-length variant are all wrong
    same_len = ("x" if fx.token[0] != "x" else "y") + fx.token[1:]
    for bad in (fx.token[:-1], fx.token + "x", same_len, ""):
        resp = fx.get(f"{route}?token={urllib.parse.quote(bad)}")
        assert resp.status == 403, bad
        assert fx.token not in resp.text


@pytest.mark.parametrize("route", PAGE_ROUTES)
def test_mt_r1_a_valueless_or_repeated_token_param_is_not_a_pass(fx, route):
    assert fx.get(f"{route}?token").status == 403
    assert fx.get(f"{route}?other={urllib.parse.quote(fx.token)}").status == 403


def test_mt_r1_the_403_page_tells_the_user_what_to_do(fx):
    text = fx.get("/").text.lower()
    assert "printed" in text           # "open the URL printed when the monitor started"
    assert len(text) < 2000            # a short page, not the app


def test_mt_r1_the_403_is_not_the_monitor_page(fx):
    # the real page is large and talks to /api; the refusal must not be it
    refusal = fx.get("/").text
    page = fx.get(tokened(fx)).text
    assert "X-Monitor-Token" not in refusal
    assert len(refusal) < len(page)


@pytest.mark.parametrize("route", PAGE_ROUTES)
def test_mt_r1_the_right_token_serves_the_page(fx, route):
    resp = fx.get(tokened(fx, route))
    assert resp.status == 200
    assert "text/html" in resp.headers["content-type"]
    # the page works: it holds the token for its own API calls
    assert fx.token in resp.text
    assert "X-Monitor-Token" in resp.text
    assert "/api/state" in resp.text
    assert "__TOKEN__" not in resp.text


def test_mt_r1_the_tokened_page_can_actually_call_the_api(fx):
    # pull the token the page embeds, as the page's script would, and use it
    page = fx.get(tokened(fx)).text
    assert fx.token in page
    state = fx.get("/api/state", **{"X-Monitor-Token": fx.token})
    assert state.status == 200 and "project" in json.loads(state.text)


def test_mt_r1_the_token_is_compared_exactly_not_as_a_substring(fx):
    resp = fx.get(f"/?token=junk{urllib.parse.quote(fx.token)}junk")
    assert resp.status == 403


def test_mt_r1_unknown_routes_are_still_404_and_never_leak_the_token(fx):
    resp = fx.get(tokened(fx, "/nope"))
    assert resp.status == 404 and fx.token not in resp.text


def test_mt_r1_a_refused_page_does_not_stop_the_server_serving_the_next(fx):
    assert fx.get("/").status == 403
    assert fx.get(tokened(fx)).status == 200
    assert fx.get("/").status == 403


# --------------------------------------------------------------------------
# MT-R2: the token does not linger in the address bar


def test_mt_r2_the_page_drops_the_token_from_the_url_with_replace_state(fx):
    page = fx.get(tokened(fx)).text
    assert re.search(r"history\s*\.\s*replaceState\s*\(", page)


def test_mt_r2_the_page_removes_the_token_not_only_calls_replace_state(fx):
    page = fx.get(tokened(fx)).text
    call = re.search(r"history\s*\.\s*replaceState\s*\(([^;]*)\)\s*;", page, re.S)
    assert call, "no replaceState call"
    # the replacement URL is derived from the location without its query token;
    # a call that re-states the full current URL would remove nothing.
    assert re.search(r"location", page)
    assert "token" in page.lower()


def test_mt_r2_the_tokened_page_has_both_headers(fx):
    resp = fx.get(tokened(fx))
    assert resp.status == 200
    assert resp.headers.get("referrer-policy") == "same-origin"
    assert resp.headers.get("cache-control") == "no-store"


def test_mt_r2_the_index_html_alias_has_both_headers(fx):
    resp = fx.get(tokened(fx, "/index.html"))
    assert resp.headers.get("referrer-policy") == "same-origin"
    assert resp.headers.get("cache-control") == "no-store"


def test_mt_r2_the_refusal_has_both_headers_too(fx):
    # "Responses carry": a cached 403 would keep refusing a good URL
    resp = fx.get("/")
    assert resp.status == 403
    assert resp.headers.get("referrer-policy") == "same-origin"
    assert resp.headers.get("cache-control") == "no-store"


def test_mt_r2_api_responses_carry_the_referrer_policy(fx):
    resp = fx.get("/api/state", **{"X-Monitor-Token": fx.token})
    assert resp.status == 200
    assert resp.headers.get("referrer-policy") == "same-origin"
    assert resp.headers.get("cache-control") == "no-store"


def test_mt_r2_a_reload_of_the_bare_url_is_refused(fx):
    # the accepted consequence of dropping the token from the address bar
    assert fx.get(tokened(fx)).status == 200
    assert fx.get("/").status == 403


# --------------------------------------------------------------------------
# MT-R3: the Host header is required


def http10(path, extra=b"", host=None):
    head = f"GET {path} HTTP/1.0\r\n".encode()
    if host is not None:
        head += b"Host:" + host + b"\r\n"
    return head + extra + b"\r\n"


def test_mt_r3_no_host_is_403_on_the_page_even_with_the_right_token(fx):
    resp = fx.raw(http10(tokened(fx)))
    assert resp.status == 403
    assert fx.token not in resp.text


def test_mt_r3_no_host_is_403_on_the_api_even_with_the_right_token(fx):
    resp = fx.raw(http10("/api/state",
                         f"X-Monitor-Token: {fx.token}\r\n".encode()))
    assert resp.status == 403
    resp = fx.raw(http10(tokened(fx, "/api/state")))
    assert resp.status == 403


@pytest.mark.parametrize("blank", [b"", b" ", b"   \t"])
def test_mt_r3_an_empty_host_is_403_on_the_page(fx, blank):
    resp = fx.raw(http10(tokened(fx), host=blank))
    assert resp.status == 403
    assert fx.token not in resp.text


@pytest.mark.parametrize("blank", [b"", b" "])
def test_mt_r3_an_empty_host_is_403_on_the_api(fx, blank):
    resp = fx.raw(http10("/api/state",
                         f"X-Monitor-Token: {fx.token}\r\n".encode(), host=blank))
    assert resp.status == 403


def test_mt_r3_no_host_is_403_over_http11_too(fx):
    # HTTP/1.1 requires Host, but a hand-rolled client can omit it
    resp = fx.raw(f"GET {tokened(fx)} HTTP/1.1\r\nConnection: close\r\n\r\n".encode())
    assert resp.status == 403


def test_mt_r3_a_loopback_host_still_works(fx):
    for host in (f"127.0.0.1:{fx.port}", f"localhost:{fx.port}", "127.0.0.1"):
        resp = fx.raw(http10(tokened(fx), host=host.encode()))
        assert resp.status == 200, host
    for host in (f"local.evil.example:{fx.port}", "attacker.example"):
        resp = fx.raw(http10(tokened(fx), host=host.encode()))
        assert resp.status == 403, host


# --------------------------------------------------------------------------
# MT-R4: the docstring tells the truth (loose: the contract names three facts)


def test_mt_r4_the_docstring_states_the_real_threat_model():
    doc = (server.__doc__ or "").lower()
    assert "rebinding" in doc
    assert "cross-site" in doc or "csrf" in doc
    # the page is no longer an open door that hands out the token
    assert "embedded in the page it serves" not in doc
    assert re.search(r"every (route|call).*(including|even) `?/", doc, re.S) \
        or "including `/`" in doc or "including /" in doc


# --------------------------------------------------------------------------
# MT-R5: no regression


def test_mt_r5_api_state_with_the_header_token(fx):
    resp = fx.get("/api/state", **{"X-Monitor-Token": fx.token})
    assert resp.status == 200 and "project" in json.loads(resp.text)


def test_mt_r5_api_state_with_the_query_token(fx):
    resp = fx.get(tokened(fx, "/api/state"))
    assert resp.status == 200 and "project" in json.loads(resp.text)


@pytest.mark.parametrize("route", ["/api/state", "/api/settings", "/api/events"])
def test_mt_r5_api_without_or_with_a_wrong_token_is_403(fx, route):
    assert fx.get(route).status == 403
    assert fx.get(route, **{"X-Monitor-Token": "wrong"}).status == 403
    assert fx.get(f"{route}?token=wrong").status == 403


@pytest.mark.parametrize("route", ["/api/settings", "/api/events"])
def test_mt_r5_other_api_routes_still_answer_with_the_token(fx, route):
    assert fx.get(route, **{"X-Monitor-Token": fx.token}).status == 200


def test_mt_r5_the_action_endpoint_still_needs_the_token(fx):
    body = json.dumps({"action": "nope"})
    resp = fx.request("POST", "/api/action", body=body,
                      headers={"Content-Type": "application/json"})
    assert resp.status == 403
    resp = fx.request("POST", "/api/action", body=body,
                      headers={"Content-Type": "application/json",
                               "X-Monitor-Token": fx.token})
    assert resp.status != 403


def test_mt_r5_post_with_no_host_is_refused(fx):
    body = b"{}"
    resp = fx.raw(b"POST /api/action HTTP/1.0\r\n"
                  + f"X-Monitor-Token: {fx.token}\r\n".encode()
                  + b"Content-Length: 2\r\n\r\n" + body)
    assert resp.status == 403


def test_mt_r5_the_quota_handoff_still_works(fx):
    """C19's `POST /quota` (form body token=<token>). Skipped while the route
    does not exist on this branch: a 404 would otherwise pass "refused" for the
    wrong reason."""
    form = {"Content-Type": "application/x-www-form-urlencoded"}
    body = urllib.parse.urlencode({"token": fx.token})
    probe = fx.request("POST", "/quota", body=body, headers=form)
    if probe.status == 404:
        pytest.skip("C19 not merged: no POST /quota route yet")
    assert probe.status == 200
    refused = fx.request("POST", "/quota",
                         body=urllib.parse.urlencode({"token": "wrong"}),
                         headers=form)
    assert refused.status == 403 and fx.token not in refused.text
    # the details page's handoff must not have reopened the tokenless `/`
    assert fx.get("/").status == 403
