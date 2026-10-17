"""Slice 4C on a REAL Docker daemon (SG_TEST_DOCKER=1): a projection provider's
guest reads its key from the private HOME, no projected secret appears in the
Docker logs, and the projection is deleted after the attempt ends.

Fake-CLI passes are never evidence of containment; these are the cases that are.
With the switch set a missing prerequisite FAILS (`dk.require_docker`), it never
skips. Without it the module is skipped.

NOT RUN by the author when no Docker daemon exists in the authoring sandbox: the
module is then verified to collect only.

Probes never print a secret: they report salted hashes the host recomputes from
the fictional values it wrote itself. The one deliberate exception is the `echo`
probe, whose whole purpose is to try to put the key in the Docker logs; the
redactor must stop it. No allowlist entry or external host is added. Seams
assumed: tests/support/ic_s4c.py (NEED_INFO(s4c-seams)).
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import ic_docker as dk  # noqa: E402
import ic_probe  # noqa: E402
import ic_s4c as c4  # noqa: E402
import ic_s4c_refresh as rf  # noqa: E402
import ic_workload as wl  # noqa: E402

pytestmark = pytest.mark.skipif(not dk.OPT_IN, reason="real Docker is opt-in: SG_TEST_DOCKER=1")

LOG_WAIT = 60

EXTRA = r'''
import hashlib as _h
_AUTH = os.path.join(HOME, ".local/share/fakecode/auth.json")
def _sha(v):
    return _h.sha256((ins.get("salt", "") + v).encode()).hexdigest()
def _read():
    try:
        return json.load(open(_AUTH))
    except Exception as e:
        return {"error": type(e).__name__}
def _facts(d):
    return {"access_sha": _sha(d.get("fakecode", {}).get("access", "")),
            "key_sha": _sha(d.get("fakekey", {}).get("key", "")),
            "has_refresh": "refresh" in json.dumps(d), "error": d.get("error")}
def _ino():
    try:
        return os.stat(_AUTH).st_ino
    except OSError:
        return None
def _file_bind():
    try:
        return any(line.split()[4] == _AUTH for line in open("/proc/self/mountinfo"))
    except OSError:
        return None
if probe == "projection":
    d = _read()
    say(dict(common(), phase="read", facts=_facts(d), ino=_ino(), file_bind=_file_bind(), home=HOME))
    if ins.get("echo"):
        key = d.get("fakekey", {}).get("key", "")
        sys.stdout.write("echo-key=" + key + "\n"); sys.stdout.flush()
        sys.stdout.write("echo-split=" + key[:9]); sys.stdout.flush(); time.sleep(0.3)
        sys.stdout.write(key[9:] + "\n"); sys.stdout.flush()
        print("echo-access=" + d.get("fakecode", {}).get("access", ""), file=sys.stderr, flush=True)
        print("echo-done", flush=True)
    if ins.get("watch"):
        end = time.time() + ins["watch"]
        last = _facts(d)["access_sha"]
        while time.time() < end:
            f = _facts(_read())
            if f["access_sha"] != last and not f["error"]:
                last = f["access_sha"]
                say(dict(common(), phase="changed", facts=f, ino=_ino(), file_bind=_file_bind()))
            time.sleep(0.1)
    if ins.get("hold"):
        import signal
        signal.pause()
elif probe == "plant":
'''


class ProjectionWorld(dk.IsolationWorld):
    """The isolated world whose `acme` provider runs the projection probe, declares
    `isolated_credentials: projection` and has an opencode-like vault."""

    AGENT = "s4c"

    def __init__(self, tmp_path, monkeypatch, owner):
        self.ma_state = tmp_path / "ma-state"
        self.refresh_log = tmp_path / "refresh.jsonl"
        monkeypatch.setenv("MULTIAGENTS_STATE_DIR", str(self.ma_state))
        monkeypatch.setenv(rf.LOG_ENV, str(self.refresh_log))
        monkeypatch.setenv(c4.ENV_REFRESH, rf.SPEC)
        monkeypatch.setenv("PYTHONPATH", os.pathsep.join(
            p for p in (str(Path(__file__).resolve().parent / "support"), os.environ.get("PYTHONPATH", "")) if p))
        super().__init__(tmp_path, monkeypatch, owner)
        self.tmp_root = tmp_path
        self.vault = self.ma_state / "container-state" / "shared" / c4.PROVIDER / "vault"
        c4.write_projection_vault(self.vault)
        probe = self.p.fakes["acme"]
        script = tmp_path / "ic-s4c-probe.py"
        script.write_text(ic_probe.PROBE_CLI.replace('if probe == "plant":\n', EXTRA, 1))
        spawn = dict(probe.entry["spawn"])
        spawn["args"] = spawn["args"][2:]
        probe.entry["bin"], probe.entry["spawn"] = str(script), spawn
        wl.guest_runnable(probe.entry)
        probe.entry["needs"] = {"credentials": True, "network": False, "rpc": False}
        probe.entry["isolated_credentials"] = "projection"
        probe.entry["container_private_home"] = [c4.PROJECTED_DIR]
        self.p.add_agent(self.AGENT, "acme", "acme/m1")
        executor = self.p.project.setdefault("executor", {})
        isolated = executor.setdefault("isolated", {"backend": "docker"})
        isolated["credential_renew_interval"] = 0.5
        self.p.write()
        self._reset_server()

    def probe_node(self, probe: str, agent: str = AGENT, **fields) -> str:
        return super().probe_node(probe, agent, **fields)

    def container_id(self, node: str) -> str:
        attempt = self.runs(node)[-1]["attempt_id"]
        ids = dk.docker("ps", "-aq", "--no-trunc", "--filter", f"label=multiagents.owner={attempt}").stdout.split()
        assert len(ids) == 1, (attempt, ids)
        return ids[0]

    def host_projection(self, node: str) -> Path:
        """The host file behind the guest's `$HOME/<projected file>`, from Docker's own mount list."""
        info = json.loads(dk.docker("inspect", self.container_id(node)).stdout)[0]
        home = next((e.split("=", 1)[1] for e in info["Config"]["Env"] if e.startswith("HOME=")), "/work/home")
        guest = home.rstrip("/") + "/" + c4.PROJECTED_FILE
        covering = [m for m in info["Mounts"] if guest.startswith(m["Destination"].rstrip("/") + "/")]
        assert covering, "no mount covers the guest path of the projected file"
        best = max(covering, key=lambda m: len(m["Destination"]))
        assert best["Type"] == "bind" and Path(best["Source"]).is_dir(), "the projection must be a directory mount"
        return Path(best["Source"]) / guest[len(best["Destination"].rstrip("/")):].lstrip("/")

    def logs(self, node: str) -> str:
        done = dk.docker("logs", self.container_id(node))
        return done.stdout + done.stderr


@pytest.fixture
def world(tmp_path, monkeypatch, st_live_docker, request):
    dk.require_docker()
    w = ProjectionWorld(tmp_path, monkeypatch, dk.test_owner(request))
    w.start()
    yield w
    dk.cleanup(w)


# ----------------------------------------------------------------- the guest reads its key

def test_s4c_docker_the_guest_reads_its_key_from_the_private_home(world):
    node = world.probe_node("projection", salt="s4", hold=True)
    r = world.report(node, phase="read", timeout=dk.START_WAIT)
    facts = r["facts"]
    assert facts["error"] is None, f"the guest could not read the projection: {facts['error']}"
    assert facts["key_sha"] == c4.salted("s4", c4.API_KEY)
    assert facts["access_sha"] == c4.salted("s4", c4.OAUTH_ACCESS)
    assert facts["has_refresh"] is False, "refresh material reached the guest"
    assert r["file_bind"] is False, "the projected file is itself a mount point (a file bind)"
    world.stop_victim(node)


def test_s4c_docker_a_renewal_replaces_the_file_and_the_guest_sees_it(world):
    node = world.probe_node("projection", salt="s5", watch=40, hold=True)
    first = world.report(node, phase="read", timeout=dk.START_WAIT)
    data = json.loads((world.vault / "auth.json").read_text())
    data["fakecode"]["expires"] = c4.PAST
    tmp = world.vault / "auth.json.swap"
    tmp.write_text(json.dumps(data))
    os.replace(tmp, world.vault / "auth.json")
    changed = world.wait(lambda: next((r for r in world.reports(node) if r.get("phase") == "changed"), None),
                         dk.PROBE_WAIT, "the guest to see the renewed token")
    renewed = json.loads((world.vault / "auth.json").read_text())["fakecode"]["access"]
    assert changed["facts"]["access_sha"] == c4.salted("s5", renewed) != first["facts"]["access_sha"]
    assert changed["facts"]["has_refresh"] is False
    assert changed["file_bind"] is False and changed["ino"] != first["ino"], "renewal must replace the file"
    assert world.get(node)["state"] == "running", "a rotation must never cut the attempt"
    world.stop_victim(node)


# ----------------------------------------------------------------- the logs

def test_s4c_docker_no_projected_secret_appears_in_the_docker_logs(world):
    node = world.probe_node("projection", salt="s6", echo=True, hold=True)
    world.report(node, phase="read", timeout=dk.START_WAIT)
    world.wait(lambda: "echo-done" in world.logs(node), LOG_WAIT, "the guest's last output line")
    logs = world.logs(node)
    for secret in (c4.API_KEY, c4.OAUTH_ACCESS):
        assert secret not in logs, "a projected secret reached `docker logs` (the redactor missed it)"
    assert "echo-key=" in logs, "positive control: the guest's line is there, with the secret replaced"
    inspect = dk.docker("inspect", world.container_id(node)).stdout
    assert c4.API_KEY not in inspect and c4.OAUTH_ACCESS not in inspect, "a projected secret is in `docker inspect`"
    assert c4.leaks_of({"texts": world.texts(node)}, [c4.API_KEY, c4.OAUTH_ACCESS]) == {}, \
        "a projected secret is in the host's recorded output"
    world.stop_victim(node)


# ----------------------------------------------------------------- deletion

def test_s4c_docker_the_projection_is_deleted_after_the_attempt_ends(world):
    node = world.probe_node("projection", salt="s7", hold=True)
    world.report(node, phase="read", timeout=dk.START_WAIT)
    path = world.host_projection(node)
    assert path.is_file()
    world.stop_victim(node)
    world.wait(lambda: not path.exists(), dk.PROBE_WAIT, "the projection to be deleted")
    skip = (str(world.vault), str(world.refresh_log))
    survivors = {k: v for k, v in c4.tree_texts(world.tmp_root).items() if not k.startswith(skip)}
    assert c4.leaks_of(survivors, [c4.API_KEY, c4.OAUTH_ACCESS]) == {}, "a projected secret survives on the host"


def test_s4c_docker_the_projection_is_deleted_when_the_container_is_already_gone(world):
    node = world.probe_node("projection", salt="s8", hold=True)
    world.report(node, phase="read", timeout=dk.START_WAIT)
    path = world.host_projection(node)
    dk.docker("rm", "-f", world.container_id(node))            # the container disappears behind the scheduler's back
    world.wait(lambda: not path.exists() and world.get(node)["state"] in ("held", "failed", "done", "cancelled"),
               dk.PROBE_WAIT, "the projection to be deleted after the container vanished")
