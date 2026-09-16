import json
import urllib.request
import urllib.error
import sys
from pathlib import Path

# Adjust path for harness
sys.path.insert(0, str(Path("/home/theobroma/.multiagents/worktrees/multiagents-58c9de87/ag-7e48db/tests/support")))
import c1_harness as h
from multiagents import authproxy

# Minimal monkeypatch since we are outside pytest
class FakeMonkeypatch:
    def __init__(self):
        self.orig = {}
    def setattr(self, obj, name, val):
        self.orig[(obj, name)] = getattr(obj, name)
        setattr(obj, name, val)
    def undo(self):
        for (obj, name), val in self.orig.items():
            setattr(obj, name, val)

def run_tests():
    tmp_path = Path("/tmp/authproxy_explore")
    tmp_path.mkdir(exist_ok=True)
    monkeypatch = FakeMonkeypatch()
    
    # 4. Scrubbing behavior
    body_with_org = b'{"error": {"message": "Invalid token", "organization": "MyCorp", "email": "admin@mycorp.com"}}'
    with h.fake_http_server(h.fixed_response_upstream(400, body=body_with_org)) as upstream_url:
        with h.authproxy_server(tmp_path, monkeypatch, accounts={"acc": "token"}, upstream=upstream_url) as proxy:
            req = urllib.request.Request(
                proxy.base_url + "/v1/messages",
                data=b"{}",
                headers={"Authorization": "Bearer " + proxy.mint("agent1")}
            )
            try:
                urllib.request.urlopen(req)
            except urllib.error.HTTPError as exc:
                print("400 Scrubbed Body:", exc.read())

    # 5. Redirect handling
    with h.fake_http_server(h.fixed_response_upstream(307, headers={"Location": "/new"})) as upstream_url:
        with h.authproxy_server(tmp_path, monkeypatch, accounts={"acc": "token"}, upstream=upstream_url) as proxy:
            req = urllib.request.Request(
                proxy.base_url + "/v1/messages",
                data=b"{}",
                headers={"Authorization": "Bearer " + proxy.mint("agent1")}
            )
            try:
                urllib.request.urlopen(req)
            except urllib.error.HTTPError as exc:
                print("307 Redirect Response:", exc.code, exc.read())
            except Exception as e:
                print("307 Redirect Exception:", type(e), str(e))
                
    # 6. Unreachable upstream & no events
    events = []
    def on_event(kind, fields): events.append(kind)
    with h.authproxy_server(tmp_path, monkeypatch, accounts={"acc": "token"}, upstream=h.closed_port_url(), on_event=on_event) as proxy:
        req = urllib.request.Request(
            proxy.base_url + "/v1/messages",
            data=b"{}",
            headers={"Authorization": "Bearer " + proxy.mint("agent1")}
        )
        try:
            urllib.request.urlopen(req)
        except urllib.error.HTTPError as exc:
            print("502 Unreachable Body:", exc.code, exc.read())
        print("Events on 502:", events)
        
    monkeypatch.undo()

if __name__ == "__main__":
    run_tests()
