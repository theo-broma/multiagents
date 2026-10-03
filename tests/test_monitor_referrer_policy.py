"""MT-R2b: same-origin POSTs retain their Origin; null remains refused."""

import http.client
import threading
from http.server import ThreadingHTTPServer

import pytest

from multiagents.monitor import server


@pytest.fixture
def monitor(monkeypatch):
    monkeypatch.setattr(server.Handler, "token", "referrer-token")
    monkeypatch.setattr(server.Handler, "bound_host", "127.0.0.1")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd
    httpd.shutdown()
    httpd.server_close()
    thread.join()


def request(monitor, method, path, origin=None):
    headers = {"X-Monitor-Token": "referrer-token"}
    if origin is not None:
        headers["Origin"] = origin
    conn = http.client.HTTPConnection("127.0.0.1", monitor.server_port, timeout=10)
    try:
        conn.request(method, path, body="token=referrer-token", headers=headers)
        response = conn.getresponse()
        return response.status, response.getheader("Referrer-Policy"), response.read()
    finally:
        conn.close()


@pytest.mark.parametrize("method,path", [("GET", "/"), ("POST", "/quota")])
def test_mt_r2b_html_responses_use_same_origin(monitor, method, path):
    origin = f"http://127.0.0.1:{monitor.server_port}"
    status, policy, _ = request(monitor, method, path, origin)
    assert status == 200
    assert policy == "same-origin"


def test_mt_r2b_pages_do_not_override_same_origin_policy():
    quota = server.QUOTA_PAGE.read_text()
    assert 'name="referrer" content="same-origin"' in quota
    assert "no-referrer" not in quota
    assert "no-referrer" not in server.PAGE.read_text()


def test_mt_r2b_null_origin_is_still_refused(monitor):
    status, _, body = request(monitor, "POST", "/quota", "null")
    assert status == 403
    assert b"bad origin" in body
