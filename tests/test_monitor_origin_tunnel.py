"""QD-R7: quota origins follow the browser's Host through a port forward."""

import http.client
import threading
from http.server import ThreadingHTTPServer

import pytest

from multiagents.monitor import server


@pytest.fixture
def monitor(monkeypatch):
    monkeypatch.setattr(server.Handler, "token", "tunnel-token")
    monkeypatch.setattr(server.Handler, "bound_host", "127.0.0.1")
    monkeypatch.setattr(server, "load_config", lambda _: None)
    monkeypatch.setattr(server.quota, "view", lambda *args: [])
    monkeypatch.setattr(server.quota, "reveal", lambda *args: None)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd
    httpd.shutdown()
    httpd.server_close()
    thread.join()


ROUTES = [("POST", "/quota", "token=tunnel-token"),
          ("GET", "/api/quota", None),
          ("POST", "/api/quota/identity", '{"provider":"example"}')]


def request(monitor, route, host, origin):
    method, path, body = route
    headers = {"Host": host, "X-Monitor-Token": "tunnel-token"}
    if origin is not None:
        headers["Origin"] = origin
    conn = http.client.HTTPConnection("127.0.0.1", monitor.server_port, timeout=10)
    try:
        conn.request(method, path, body=body, headers=headers)
        response = conn.getresponse()
        return response.status, response.read()
    finally:
        conn.close()


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("host,origin", [
    ("localhost:9999", "http://localhost:9999"),
    ("localhost", "http://localhost:80"),
    ("localhost:80", "http://localhost"),
    ("[::1]:9999", "http://[::1]:9999"),
    ("[::1]", "http://[::1]:80"),
    ("localhost:9999", None),
])
def test_qd_r7_matching_forwarded_authority_is_accepted(monitor, route, host, origin):
    assert request(monitor, route, host, origin)[0] == 200


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("origin", [
    "http://localhost:8787", "http://127.0.0.1:9999",
    "http://foreign.example:9999", "null", "https://localhost:9999",
    "http://user@localhost:9999", "http://@localhost:9999",
    "http://localhost:9999/", "http://localhost:9999?x=1",
    "http://localhost:9999#fragment", "http://localhost:bad",
    "http://localhost:0",
])
def test_qd_r7_mismatching_or_invalid_origin_is_refused(monitor, route, origin):
    status, body = request(monitor, route, "localhost:9999", origin)
    assert status == 403
    assert b"bad origin" in body


@pytest.mark.parametrize("route", ROUTES)
def test_mt_r3_matching_foreign_host_is_still_refused(monitor, route):
    status, body = request(monitor, route, "foreign.example:9999",
                           "http://foreign.example:9999")
    assert status == 403
    assert body == b"bad host"
