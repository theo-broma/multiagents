"""Slice 4B.2 on a REAL Docker daemon (SG_TEST_DOCKER=1): the auth gateway's
reachability, authority and replacement, and redaction of the bearer from the
logs.

Fake-CLI passes are never evidence of containment (4B work split); these are
the cases that are. With the switch set a missing prerequisite FAILS
(`dk.require_docker`), it never skips. Without it the module is skipped.

NOT RUN by the author: no Docker daemon exists in the authoring sandbox. The
module is verified to collect only.

Probes never print a credential: the bearer is taken host-side from the
invocation file the guest is started from, the probe reports only statuses and
a salted hash the host recomputes. Credentials are fiction; no allowlist entry
or external host is added. The gateway's synthetic upstream comes from
MULTIAGENTS_TEST_GATEWAY_BACKEND=ic_s4b2_backend:factory, set on the host.

K4B2.8: with the variable set host-side the registry resolves the factory module
file and mounts it read-only into the auth gateway container, and sets the
variable for the gateway process only; it never reaches an attempt. These tests
require that "the guest reaches ma-auth with its bearer" is visible as a 200
carrying the synthetic marker.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import ic_docker as dk  # noqa: E402
import ic_probe  # noqa: E402
import ic_s4b2_backend as be  # noqa: E402
import ic_workload as wl  # noqa: E402

pytestmark = pytest.mark.skipif(not dk.OPT_IN, reason="real Docker is opt-in: SG_TEST_DOCKER=1")

MARK = be.MARKER.decode()

EXTRA = r'''
import hashlib as _h
MARK = %(mark)r
def _facts(token):
    return {"present": bool(token), "length": len(token or ""),
            "salted_sha256": _h.sha256((ins.get("salt", "") + (token or "")).encode()).hexdigest()}
def _http(host, port, raw, timeout=4):
    try:
        c = socket.create_connection((host, port), timeout=timeout); c.settimeout(timeout)
        c.sendall(raw.encode()); data = b""
        while len(data) < 65536:
            chunk = c.recv(4096)
            if not chunk: break
            data += chunk
        c.close()
        head = data.split(b"\r\n")[0].decode("latin-1")
        status = int(head.split()[1]) if head.startswith("HTTP/") and len(head.split()) > 1 else 0
        return {"status": status, "marker": MARK.encode() in data}
    except OSError as e:
        return {"status": 0, "error": type(e).__name__}
def _post(authorization, extra=""):
    lines = ["POST /v1/messages HTTP/1.1", "Host: ma-auth:8930", "Content-Length: 2", "Connection: close"]
    if authorization: lines.append("Authorization: " + authorization)
    return "\r\n".join(lines) + "\r\n" + extra + "\r\n{}"
def _egress(proxy_auth):
    raw = "GET http://evil.test/ HTTP/1.1\r\nHost: evil.test\r\nConnection: close\r\n"
    if proxy_auth: raw += "Proxy-Authorization: " + proxy_auth + "\r\n"
    return _http("ma-egress", 8888, raw + "\r\n")
def _creds():
    token = ins.get("credential") or os.environ.get("ANTHROPIC_AUTH_TOKEN", "")
    m = re.match(r"http://([^:]+):([^@]+)@", os.environ.get("HTTP_PROXY", ""))
    return token, (m.group(1) if m else ""), (m.group(2) if m else "")
if probe == "auth":
    token, user, pw = _creds()
    own = os.environ.get("ANTHROPIC_AUTH_TOKEN", "")
    variants = {
        "own": _post("Bearer " + token),
        "none": _post(None),
        "forged": _post("Bearer ma-auth-v1." + "c" * 64),
        "egress_password_as_bearer": _post("Bearer " + pw),
        "egress_basic": _post("Basic " + base64.b64encode((user + ":" + pw).encode()).decode()),
        "legacy_mxa": _post("Bearer mxa_fiction_" + "0" * 32),
        "legacy_mxa2": _post("Bearer mxa2_v2.ZmljdGlvbg.acme_" + "0" * 32),
        "forged_forwarding": _post("Bearer ma-auth-v1." + "c" * 64, "X-Forwarded-For: 10.0.0.2\r\nForwarded: for=10.0.0.2\r\n"),
        "put": _http("ma-auth", 8930, _post("Bearer " + token).replace("POST ", "PUT ", 1)),
    }
    out = {k: (v if isinstance(v, dict) and "status" in v else _http("ma-auth", 8930, v)) for k, v in variants.items()}
    out["put"] = variants["put"]
    egress = {"bearer_as_basic_password": _egress("Basic " + base64.b64encode((user + ":" + own).encode()).decode()),
              "bearer_as_bearer": _egress("Bearer " + own),
              "own_password": _egress("Basic " + base64.b64encode((user + ":" + pw).encode()).decode())}
    say(dict(common(), base_url=os.environ.get("ANTHROPIC_BASE_URL"), api_key_present="ANTHROPIC_API_KEY" in os.environ,
             token=_facts(own), no_proxy=os.environ.get("NO_PROXY"), variants=out, egress=egress))
    if ins.get("echo"):
        sys.stdout.write("bearer="); sys.stdout.flush()
        sys.stdout.write(own[:30]); sys.stdout.flush(); time.sleep(0.3); sys.stdout.write(own[30:] + "\n"); sys.stdout.flush()
        print("Authorization: Bearer " + own, file=sys.stderr, flush=True)
    if ins.get("hold"):
        import signal
        signal.pause()
elif probe == "authpoll":
    token, user, pw = _creds()
    say(dict(common(), phase="ready"))
    import time as _t
    end = _t.time() + ins["seconds"]
    seen = {"none": [], "forged": [], "own": []}
    while _t.time() < end:
        seen["none"].append(_http("ma-auth", 8930, _post(None), 2)["status"])
        seen["forged"].append(_http("ma-auth", 8930, _post("Bearer ma-auth-v1." + "c" * 64), 2)["status"])
        seen["own"].append(_http("ma-auth", 8930, _post("Bearer " + token), 2)["status"])
        _t.sleep(0.05)
    summary = {k: {"n": len(v), "ok": sum(1 for s in v if 200 <= s < 300), "s401": v.count(401), "failed": v.count(0),
                   "other": sorted({s for s in v if s not in (0, 401) and not 200 <= s < 300})} for k, v in seen.items()}
    say(dict(common(), phase="poll", summary=summary, tail=seen["own"][-10:]))
elif probe == "legacy":
    gw = None
    try:
        for line in open("/proc/net/route").read().splitlines()[1:]:
            f = line.split()
            if f[1] == "00000000":
                gw = socket.inet_ntoa(__import__("struct").pack("<I", int(f[2], 16))); break
    except (OSError, ValueError, IndexError): pass
    def can(host, port):
        try:
            c = socket.create_connection((host, port), timeout=2); c.close(); return True
        except OSError as e: return type(e).__name__
    names = ins["names"]
    hosts = sorted(set(ins.get("addresses", []) + ([gw] if gw else []) + ["host.docker.internal", "172.17.0.1"]))
    ports = [8930] + ([ins["canary_port"]] if ins.get("canary_port") else [])
    def resolve(n):
        try: return socket.gethostbyname(n)
        except OSError as e: return type(e).__name__
    say(dict(common(), gateway=gw, control=can("ma-auth", 8930),
             reach={"%%s:%%d" %% (h, p): can(h, p) for h in hosts for p in ports},
             names={n: resolve(n) for n in names}))
    if ins.get("hold"):
        import signal
        signal.pause()
elif probe == "plant":
''' % {"mark": MARK}


class AuthWorld(dk.IsolationWorld):
    """The isolated world whose `acme` provider runs the s4b2 probes and declares
    network + `gateway_auth: anthropic`, reached through the writable `s4b2` agent."""

    AGENT = "s4b2"

    def __init__(self, tmp_path, monkeypatch, owner):
        monkeypatch.setenv("MULTIAGENTS_TEST_GATEWAY_BACKEND", be.SPEC)
        monkeypatch.setenv("PYTHONPATH", os.pathsep.join(
            p for p in (str(Path(__file__).resolve().parent / "support"), os.environ.get("PYTHONPATH", "")) if p))
        super().__init__(tmp_path, monkeypatch, owner)
        probe = self.p.fakes["acme"]
        source = ic_probe.PROBE_CLI.replace('if probe == "plant":\n', EXTRA, 1)
        if "import base64" not in source:
            source = "import base64\n" + source
        spawn = dict(probe.entry["spawn"])
        spawn["args"] = spawn["args"][2:]
        script = tmp_path / "ic-s4b2-probe.py"
        script.write_text(source)
        probe.entry["bin"], probe.entry["spawn"] = str(script), spawn
        wl.guest_runnable(probe.entry)
        probe.entry["needs"] = {"credentials": False, "network": True, "rpc": False}
        probe.entry["gateway_auth"] = "anthropic"
        self.p.add_agent(self.AGENT, "acme", "acme/m1")
        self.p.project.setdefault("executor", {})
        self.p.write()
        self._reset_server()

    def probe_node(self, probe: str, agent: str = AGENT, **fields) -> str:
        return super().probe_node(probe, agent, **fields)

    def set_gateway_image(self, image: str) -> None:
        executor = self.p.project.setdefault("executor", {})
        isolated = executor.setdefault("isolated", {"backend": "docker"})
        isolated.setdefault("docker", {})["gateway_image"] = image
        self.p.write()
        self._reset_server()


@pytest.fixture
def world(tmp_path, monkeypatch, st_live_docker, request):
    dk.require_docker()
    w = AuthWorld(tmp_path, monkeypatch, dk.test_owner(request))
    w.start()
    yield w
    dk.cleanup(w)


def issued_env(w, node) -> dict:
    """The guest environment the scheduler issued to `node`'s latest attempt, read
    host-side from the invocation file (never from anything the guest prints)."""
    attempt = w.runs(node)[-1]["attempt_id"]
    ids = dk.docker("ps", "-aq", "--no-trunc", "--filter", f"label=multiagents.owner={attempt}").stdout.split()
    assert len(ids) == 1, (attempt, ids)
    info = json.loads(dk.docker("inspect", ids[0]).stdout)[0]
    mounts = [m for m in info["Mounts"] if m["Destination"] == "/multiagents-input"]
    assert mounts, "the invocation mount is missing"
    return json.loads((Path(mounts[0]["Source"]) / "invocation.json").read_text())["env"]


def bearer_of(w, node) -> str:
    token = issued_env(w, node).get("ANTHROPIC_AUTH_TOKEN", "")
    assert re.fullmatch(r"ma-auth-v1\.[0-9a-f]{64}", token), "the issued bearer has the wrong shape"
    return token


def salted(salt: str, value: str) -> str:
    return hashlib.sha256((salt + value).encode()).hexdigest()


def auth(w, **fields):
    node = w.probe_node("auth", **fields)
    return node, w.report(node, timeout=dk.START_WAIT)


# ------------------------------------------------------------ reaching ma-auth

def test_k4b2_docker_the_guest_reaches_ma_auth_with_its_bearer_and_nothing_else_is_accepted(world):
    node, r = auth(world, salt="s2", hold=True)
    token = bearer_of(world, node)
    assert r["base_url"] == "http://ma-auth:8930" and r["api_key_present"] is False
    assert r["token"]["length"] == len(token) and r["token"]["salted_sha256"] == salted("s2", token), \
        "the guest holds a different bearer than the one issued"
    v = r["variants"]
    assert v["own"]["status"] == 200 and v["own"]["marker"], f"the guest's own bearer was not served: {v['own']}"
    for name in ("none", "forged", "egress_password_as_bearer", "egress_basic", "legacy_mxa", "legacy_mxa2",
                 "forged_forwarding"):
        assert v[name]["status"] == 401 and not v[name]["marker"], (name, v[name])
    assert v["put"]["status"] == 405, v["put"]
    assert "ma-auth" in r["no_proxy"].split(",")
    world.stop_victim(node)


def test_k4b2_docker_an_auth_bearer_is_refused_by_the_egress_gateway(world):
    node, r = auth(world, hold=True)
    for name, answer in r["egress"].items():
        if name == "own_password":
            assert answer["status"] == 403, ("positive control: the egress credential still works", answer)
        else:
            assert answer["status"] == 407, (name, answer)
    world.stop_victim(node)


def test_k4b2_docker_a_bearer_copied_to_another_attempt_network_is_401(world):
    a, ra = auth(world, hold=True)
    assert ra["variants"]["own"]["status"] == 200, "positive control: the owner's bearer works on its own network"
    b, rb = auth(world, credential=bearer_of(world, a), hold=True)
    assert rb["variants"]["own"]["status"] == 401 and not rb["variants"]["own"]["marker"], \
        f"another network's copied bearer was accepted: {rb['variants']['own']}"
    assert rb["variants"]["forged_forwarding"]["status"] == 401
    world.stop_victim(a), world.stop_victim(b)


def test_k4b2_docker_two_attempts_have_separate_bearers(world):
    a, ra = auth(world, salt="sa", hold=True)
    b, rb = auth(world, salt="sb", hold=True)
    ta, tb = bearer_of(world, a), bearer_of(world, b)
    assert ta != tb
    assert ra["token"]["salted_sha256"] == salted("sa", ta) and rb["token"]["salted_sha256"] == salted("sb", tb)
    assert ra["variants"]["own"]["status"] == 200 and rb["variants"]["own"]["status"] == 200
    world.stop_victim(a), world.stop_victim(b)


# -------------------------------------------------------------- the legacy listener

class Canary:
    """A TCP listener on the host's every interface. A guest that connects to it
    reached the host; the test fails then. It stands in for the legacy auth proxy."""

    def __init__(self):
        self.hits = 0
        self.server = socket.socket()
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server.bind(("0.0.0.0", 0))
        self.server.listen(8)
        self.port = self.server.getsockname()[1]
        self.server.settimeout(0.2)
        self.stop = threading.Event()
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while not self.stop.is_set():
            try:
                conn, _ = self.server.accept()
            except (TimeoutError, OSError):
                continue
            self.hits += 1
            conn.close()

    def close(self):
        self.stop.set()
        self.server.close()


def test_k4b2_docker_the_legacy_listener_and_the_host_are_unreachable_from_the_attempt_network(world):
    canary = Canary()
    try:
        names = [f"multiagents-auth-{world.slug}", f"multiagents-proxy-{world.slug}", "multiagents-auth"]
        node = world.probe_node("legacy", names=names, canary_port=canary.port, hold=True)
        r = world.report(node, timeout=dk.START_WAIT)
        assert r["control"] is True, "positive control: ma-auth:8930 must be reachable"
        assert [k for k, v in r["reach"].items() if v is True] == [], r["reach"]
        assert canary.hits == 0, "the guest reached a listener on the host"
        assert not any(re.fullmatch(r"\d+\.\d+\.\d+\.\d+", str(v)) for v in r["names"].values()), \
            f"a legacy auth or proxy name resolves inside the run: {r['names']}"
        world.stop_victim(node)
    finally:
        canary.close()


# ------------------------------------------------------------------- redaction

def test_k4b2_docker_logs_hold_only_sanitised_frames_without_the_bearer(world):
    node, r = auth(world, echo=True, hold=True)
    token = bearer_of(world, node)
    world.stop_victim(node)
    world.wait(lambda: world.get(node)["state"] in ("done", "held", "failed", "cancelled"), dk.PROBE_WAIT, "the end")
    ids = dk.docker("ps", "-aq", "--filter", f"label=multiagents.project={world.slug}").stdout.split()
    assert ids
    for cid in ids:
        logs = dk.docker("logs", cid)
        assert token not in logs.stdout + logs.stderr, "a raw Docker log copy holds the issued bearer"
        assert token.removeprefix("ma-auth-v1.") not in logs.stdout + logs.stderr
    out, err = world.host_output(world.runs(node)[-1]["run_id"])
    assert token.encode() not in out + err
    assert token not in world.texts(node)


# ----------------------------------------------------------------- replacement

def retag(world, request) -> str:
    """A second tag of the workspace image, local only (no pull, no new host), so that
    changing `gateway_image` replaces both gateways."""
    tag = "multiagents/workspace:ic-s4b2-" + dk.test_token(dk.test_owner(request))
    assert dk.docker("tag", dk.default_image(), tag).returncode == 0
    request.addfinalizer(lambda: dk.docker("rmi", tag))
    return tag


def test_k4b2_docker_auth_gateway_replacement_leaves_no_unauthenticated_window(world, request):
    node = world.probe_node("authpoll", seconds=30)
    world.report(node, phase="ready", timeout=dk.START_WAIT)
    token = bearer_of(world, node)
    before = dk.docker("ps", "-q", "--filter", "label=multiagents.gateway=auth").stdout.split()
    assert len(before) == 1
    world.set_gateway_image(retag(world, request))
    second = world.probe_node("auth", hold=True)                # a new launch reconciles and replaces the gateways
    world.report(second, timeout=dk.START_WAIT)
    after = dk.docker("ps", "-q", "--filter", "label=multiagents.gateway=auth").stdout.split()
    assert len(after) == 1 and after != before, "the auth gateway was not replaced"
    done = world.report(node, phase="poll", timeout=dk.PROBE_WAIT)
    summary = done["summary"]
    for kind in ("none", "forged"):
        assert summary[kind]["ok"] == 0, f"an unauthenticated request was served during replacement: {summary[kind]}"
        assert summary[kind]["other"] == [], f"{kind} requests got a status other than 401: {summary[kind]}"
    assert summary["own"]["ok"] > 0
    assert done["tail"] and all(200 <= s < 300 for s in done["tail"]), \
        f"the unchanged bearer did not reconnect after replacement: {done['tail']}"
    assert bearer_of(world, node) == token, "replacement changed the issued bearer"
    world.stop_victim(second)
