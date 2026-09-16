"""Characterization of authproxy logic."""

import json
import urllib.request
import urllib.error
import pytest
import sys
from pathlib import Path
from http.server import BaseHTTPRequestHandler

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))
import c1_harness as h

class EchoHandler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_POST(self):
        length = int(self.headers.get("content-length", 0))
        body = self.rfile.read(length)
        resp = {
            "path": self.path,
            "headers": {k.lower(): v for k, v in self.headers.items()},
            "body": body.decode()
        }
        resp_bytes = json.dumps(resp).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(resp_bytes)))
        self.end_headers()
        self.wfile.write(resp_bytes)

def test_authproxy_forwards_headers_body_and_query_string(tmp_path, monkeypatch):
    """Confirm Hop Headers drop, Authorization replaces, and body/query preserve."""
    with h.fake_http_server(EchoHandler) as upstream_url:
        with h.authproxy_server(tmp_path, monkeypatch, accounts={"acc": "real_token"}, upstream=upstream_url) as proxy:
            req = urllib.request.Request(
                proxy.base_url + "/v1/messages?foo=bar",
                data=b'{"msg": "hi"}',
                headers={"Authorization": "Bearer " + proxy.mint("agent1"), "X-Custom": "val", "Host": "proxy.local"}
            )
            resp = urllib.request.urlopen(req)
            data = json.loads(resp.read())
            assert data["path"] == "/v1/messages?foo=bar"
            assert data["headers"]["authorization"] == "Bearer real_token"
            assert data["headers"]["x-custom"] == "val"
            assert data["body"] == '{"msg": "hi"}'

def test_authproxy_scrub_leaves_pii_unscrubbed(tmp_path, monkeypatch):
    """F21: The scrub method leaves non-credential PII unredacted."""
    body_with_org = b'{"error": {"message": "Invalid token", "organization": "MyCorp", "email": "admin@example.invalid"}}'
    with h.fake_http_server(h.fixed_response_upstream(400, body=body_with_org)) as upstream_url:
        with h.authproxy_server(tmp_path, monkeypatch, accounts={"acc": "token"}, upstream=upstream_url) as proxy:
            req = urllib.request.Request(
                proxy.base_url + "/v1/messages",
                data=b"{}",
                headers={"Authorization": "Bearer " + proxy.mint("agent1")}
            )
            with pytest.raises(urllib.error.HTTPError) as exc:
                urllib.request.urlopen(req)
            
            error_body = exc.value.read()
            # It passes through unchanged, exposing organization and email.
            assert error_body == body_with_org

def test_authproxy_upstream_error_emits_no_events(tmp_path, monkeypatch):
    """F20: Ordinary upstream errors (like 500 or 502) emit no events."""
    events = []
    def on_event(kind, fields): events.append(kind)
    
    with h.authproxy_server(tmp_path, monkeypatch, accounts={"acc": "token"}, upstream=h.closed_port_url(), on_event=on_event) as proxy:
        req = urllib.request.Request(
            proxy.base_url + "/v1/messages",
            data=b"{}",
            headers={"Authorization": "Bearer " + proxy.mint("agent1")}
        )
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req)
        
        assert exc.value.code == 502
        # No event is emitted!
        assert events == []

def test_authproxy_post_redirect_is_not_auto_followed(tmp_path, monkeypatch):
    """POST 307 redirects are returned as errors, not auto-followed."""
    with h.fake_http_server(h.fixed_response_upstream(307, headers={"Location": "/new"})) as upstream_url:
        with h.authproxy_server(tmp_path, monkeypatch, accounts={"acc": "token"}, upstream=upstream_url) as proxy:
            req = urllib.request.Request(
                proxy.base_url + "/v1/messages",
                data=b"{}",
                headers={"Authorization": "Bearer " + proxy.mint("agent1")}
            )
            with pytest.raises(urllib.error.HTTPError) as exc:
                urllib.request.urlopen(req)
            
            assert exc.value.code == 307

def test_authproxy_missing_accounts_causes_exhaustion(tmp_path, monkeypatch):
    """Empty JSON or missing token causes account to be marked limited and exhaust."""
    vault = tmp_path / "vault"
    vault.mkdir(parents=True)
    d = vault / "accounts" / "bad"
    d.mkdir(parents=True)
    (d / ".credentials.json").write_text("{bad json")
    
    events = []
    def on_event(kind, fields): events.append((kind, fields))
    
    handler_cls = h.fixed_response_upstream(200)
    with h.fake_http_server(handler_cls) as upstream_url:
        with h.authproxy_server(tmp_path, monkeypatch, upstream=upstream_url, on_event=on_event) as proxy:
                req = urllib.request.Request(
                    proxy.base_url + "/v1/messages",
                    data=b"{}",
                    headers={"Authorization": "Bearer " + proxy.mint("agent1")}
                )
                with pytest.raises(urllib.error.HTTPError) as exc:
                    urllib.request.urlopen(req)
                assert exc.value.code == 429
                
                assert len(events) == 1
                assert events[0][0] == "exhausted"

def test_authproxy_large_non_json_body_is_passed_through(tmp_path, monkeypatch):
    """Proxy does not require body to be JSON, passes opaque bytes."""
    with h.fake_http_server(EchoHandler) as upstream_url:
        with h.authproxy_server(tmp_path, monkeypatch, accounts={"acc": "token"}, upstream=upstream_url) as proxy:
            large_body = b"x" * 10000
            req = urllib.request.Request(
                proxy.base_url + "/v1/messages",
                data=large_body,
                headers={"Authorization": "Bearer " + proxy.mint("agent1"), "Content-Type": "application/octet-stream"}
            )
            resp = urllib.request.urlopen(req)
            data = json.loads(resp.read())
            assert data["body"] == large_body.decode()
