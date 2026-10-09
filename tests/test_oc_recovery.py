"""OC Decisions 5 and 6, through process death and a surviving MCP server.

The fixture extends the existing OC provider, without reading request storage.
Its compact entry point can wait on a test-owned file. Its CLI can also own a
real MCP server, request compaction, and attempt start_agent on that SAME server
after the public compaction_status reports settlement. A real stub agent writes
a marker, as in the CW safepoint tests, to prove admission actually succeeded.

For the crash test the existing attached-driver harness runs in a subprocess,
so SIGKILL kills a real driver rather than simulating a storage phase. The
same R8f seams are retained: terminal-like stdin, shortened polling, and no
detached watchdog. None of the assertions inspect the handshake or storage.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import oc_harness as oc  # noqa: E402
import sv_harness as h  # noqa: E402
from multiagents import server  # noqa: E402

BOUND = 6.0  # includes real Python/MCP startup and the one-second grace
POLL = 0.03
REASON = "recovery contract"
SUPPORT = str(Path(__file__).resolve().parent / "support")


def wait_for(predicate, message: str):
    deadline = time.monotonic() + BOUND
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(POLL)
    assert predicate(), message


# This code runs inside the fake provider CLI. Its server inherits the driver's
# environment, including whatever authenticated safepoint transport it supplied.
SERVER_BOOT = '''
    srv = None
    if me.get("server"):
        sys.path[:0] = [ctl["src"], ctl["support"]]
        import sv_harness as h
        class P:
            base = pathlib.Path(ctl["base"])
            root = pathlib.Path(ctl["root"])
            servers = []
            def env(self, session=""):
                e = dict(os.environ)
                e.update(ctl["server_env"])
                e["MULTIAGENTS_SESSION_ID"] = session
                return e
        srv = h.Server(P(), session=os.environ["MULTIAGENTS_SESSION_ID"], handshake=False)
        record("server_up", server_pid=srv.pid)
        try:
            srv.initialize(timeout=ctl["bound"])
            got = srv.call("request_compaction", timeout=ctl["bound"], reason=ctl["reason"])
            record("requested", result=got)
            assert isinstance(got, dict) and got.get("status") == "pending", got
            request_id = got["request_id"]
            append([ctl["close"]])
            deadline = time.monotonic() + ctl["bound"]
        except BaseException as exc:
            record("protocol_error", error=repr(exc))
            srv.close()
            sys.exit(1)
'''

SERVER_POLL = '''
        if srv is not None:
            try:
                settled = srv.call("compaction_status", timeout=ctl["bound"], request_id=request_id)
                if settled.get("status") in ("completed", "failed"):
                    record("settled", result=settled, server_pid=srv.pid)
                    admitted = srv.call("start_agent", timeout=ctl["bound"],
                                        agent="worker", task=ctl["task"])
                    record("admission", result=admitted, server_pid=srv.pid)
                    if not isinstance(admitted, dict) or not admitted.get("agent_id") or "error" in admitted:
                        break
                    # The marker comes from the real agent, not the tool result.
                    marker_deadline = time.monotonic() + ctl["bound"]
                    while not os.path.isfile(ctl["marker"]) and time.monotonic() < marker_deadline:
                        time.sleep(0.03)
                    break
                if time.monotonic() >= deadline:
                    record("protocol_error", error="request never settled")
                    break
            except BaseException as exc:
                record("protocol_error", error=repr(exc))
                break
'''


DRIVER_CHILD = '''
import json, os, sys
from pathlib import Path
import pytest
from multiagents import driver, scripts
from multiagents.config import AgentSpec, Config
from multiagents.paths import ProjectPaths, global_config_dir
from multiagents.providers import Provider

settings = json.loads(Path(sys.argv[1]).read_text())
paths = ProjectPaths(Path(settings["root"]))
config = Config(project=settings["project"], providers=settings["providers"],
                agents={}, models={}, instruction_dirs=[])
spec = AgentSpec("orchestrator", "fakeprov", "m")
provider = Provider.from_dict("fakeprov", settings["providers"]["fakeprov"])
context = settings["context"]
if settings.get("resume"):
    context["MULTIAGENTS_RESUME"] = "1"
class Tty:
    def isatty(self): return True
    def fileno(self): raise OSError("fixture stdin")
    def read(self, *_): return ""
    readline = read
with pytest.MonkeyPatch.context() as patch:
    patch.setattr(sys, "stdin", Tty())
    patch.setattr(driver, "STALL_POLL_SECONDS", 0.1)
    patch.setattr(driver, "_start_supervisor", lambda *a, **k: None)
    driver._write_pid(paths, "orchestrator", os.getpid())
    argv, env = scripts.exec_action("fakeprov", provider, object(), "launch",
                                    global_config_dir(), paths.config, extra_env=context)
    raise SystemExit(driver._run_supervised(paths, config, "orchestrator", spec,
                                            provider, object(), context, argv, env))
'''


class Recovery(oc.Oc):
    def __init__(self, tmp_path, monkeypatch, capsys, mode):
        super().__init__(tmp_path, monkeypatch, capsys, declare=oc.declaration(mode),
                         limits={"compact_safe_point_seconds": 2,
                                 "compact_timeout_seconds": BOUND})
        self.processes = []
        self.output_files = []
        self.release = tmp_path / "compact-release"
        self.settings = tmp_path / "driver-settings.json"
        source = self.script.read_text()
        anchor = '    me = launches[min(n, len(launches) - 1)]\n'
        assert anchor in source
        source = source.replace(anchor, anchor + SERVER_BOOT, 1)
        source = source.replace('        now = time.time()\n',
                                '        now = time.time()\n' + SERVER_POLL, 1)
        source = source.replace('    record("exit", n=n + 1)\n',
                                '    if srv: srv.close()\n    record("exit", n=n + 1)\n', 1)
        anchor = '    if c.get("sleep"):\n'
        assert anchor in source
        source = source.replace(anchor, '''    if ctl.get("compact_release"):
        deadline = time.monotonic() + ctl["bound"]
        while not os.path.exists(ctl["compact_release"]):
            if time.monotonic() >= deadline:
                sys.exit(1)
            time.sleep(0.03)
''' + anchor, 1)
        # Catch fixture syntax mistakes even while the production tool is absent.
        compile(source, str(self.script), "exec")
        self.script.write_text(source)

    def start_driver(self, *, resume=False):
        self.settings.write_text(json.dumps({"root": str(self.root),
            "project": self.config.project, "providers": self.config.providers,
            "context": self.context, "resume": resume}))
        out = (self.settings.parent / f"driver-{len(self.processes)}.out").open("w")
        self.output_files.append(out)
        child_env = dict(os.environ)
        child_env["PYTHONPATH"] = h.SRC
        proc = subprocess.Popen([sys.executable, "-c", DRIVER_CHILD, str(self.settings)],
                                cwd=self.root, env=child_env, stdout=out,
                                stderr=subprocess.STDOUT, start_new_session=True)
        self.processes.append(proc)
        return proc

    def close(self):
        self.release.touch()
        # Every group here was created by this fixture, never by a PID lookup.
        for proc in self.processes:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait(timeout=BOUND)
        # Script actions may create a separate group. Only kill a still-live
        # process whose environment proves it belongs to this fixture's log.
        owned = {entry.get(key) for entry in self.calls() for key in ("pid", "server_pid")}
        for pid in owned - {None}:
            try:
                environ = Path(f"/proc/{pid}/environ").read_bytes()
                if f"FAKE_LOG={self.log}".encode() + b"\0" in environ:
                    os.kill(pid, signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass
        for out in self.output_files:
            out.close()


@pytest.fixture
def recovery(tmp_path, monkeypatch, capsys):
    made = []
    projects = []

    def build(mode, *, with_agent=False):
        s = Recovery(tmp_path, monkeypatch, capsys, mode)
        made.append(s)
        if with_agent:
            # Same project/config merging used by CW, retaining the OC root
            # roster while adding svstub and disabling network budget readers.
            oc_providers = yaml.safe_load((s.paths.config / "providers.yaml").read_text())
            oc_agents = yaml.safe_load((s.paths.config / "agents.yaml").read_text())
            oc_project = yaml.safe_load((s.paths.config / "project.yaml").read_text())
            oc_project["team"] = ""  # scratch roster, as in the CW/SV fixture
            project = h.Project(tmp_path)
            projects.append(project)
            for filename, saved in (("providers.yaml", oc_providers), ("agents.yaml", oc_agents)):
                path = s.paths.config / filename
                loaded = yaml.safe_load(path.read_text())
                for key, value in saved.items():
                    loaded.setdefault(key, {}).update(value)
                path.write_text(yaml.safe_dump(loaded))
            (s.paths.config / "project.yaml").write_text(yaml.safe_dump(oc_project))
            server._reset()
            return s, project
        return s

    yield build
    for s in made:
        s.close()
    for project in projects:
        project.cleanup()
    server._reset()


def test_oc_d5_a_driver_crash_while_executing_reconciles_the_same_request_id(recovery):
    s = recovery("restart")
    s.reading(oc.UNDER)
    request_id = s.request(REASON)["request_id"]
    s.ctl.write_text(json.dumps({"transcript": str(s.transcript),
        "events": str(s.paths.events_file), "probe": 0, "bound": BOUND,
        "launches": [{"life": BOUND, "appends": [[0.1, [oc.CLOSE]]]}, {"life": 0}],
        "compacts": [{"exit": 0, "stdout": oc.FIGURES + "\n"}],
        "compact_release": str(s.release)}))
    first = s.start_driver()
    wait_for(lambda: bool(s.calls("compact")), "the first driver never invoked the plugin")
    wait_for(lambda: s.status(request_id).get("status") == "executing",
             "the blocked compaction was not observable as executing")
    assert first.poll() is None, "the driver ended before the simulated crash"
    first.kill()
    first.wait(timeout=BOUND)

    replacement = s.start_driver(resume=True)
    wait_for(lambda: len(s.calls("compact")) >= 2,
             "the replacement driver never reconciled through the plugin")
    for call in s.calls("compact"):
        assert request_id in json.dumps({"argv": call["argv"], "env": call["env"]}), (
            "a compaction plugin invocation used a different request_id", call)
    s.release.touch()
    wait_for(lambda: s.status(request_id).get("status") == "completed",
             "the reconciled request never completed")
    assert replacement.wait(timeout=BOUND) == 0
    calls = s.calls("compact")
    assert len(calls) == 2, "compaction was invoked again after reconciliation"
    assert all(request_id in json.dumps({"argv": c["argv"], "env": c["env"]}) for c in calls)
    assert s.status().get("request_id") == request_id, "recovery created a second request"


@pytest.mark.parametrize("exit_code, terminal", [(0, "completed"), (1, "failed")],
                         ids=["after-success", "after-failure"])
def test_oc_d6_in_session_settlement_reopens_admission_on_the_surviving_server(
        recovery, exit_code, terminal):
    s, project = recovery("in_session", with_agent=True)
    s.reading(oc.UNDER)
    marker = project.marker("admitted-after-compaction")
    task = "after compaction " + h.plan_token([["touch", str(marker)], ["exit", 0]])
    code, _ = s.go([{"life": BOUND + 1, "server": True}],
        compacts=[{"exit": exit_code, "stderr": "fixture-compaction-failed" if exit_code else ""}],
        src=h.SRC, support=SUPPORT, base=str(project.base), root=str(project.root),
        server_env={"PYTHONPATH": h.SRC, "HOME": str(project.user_home),
                    "MULTIAGENTS_STATE_DIR": os.environ["MULTIAGENTS_STATE_DIR"],
                    "MULTIAGENTS_CONFIG_DIR": os.environ["MULTIAGENTS_CONFIG_DIR"],
                    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
                    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"},
        bound=BOUND, reason=REASON, close=oc.CLOSE, task=task, marker=str(marker))

    assert not s.calls("protocol_error"), s.calls("protocol_error")
    settled = s.calls("settled")
    assert settled and settled[0]["result"].get("status") == terminal, settled
    admitted = s.calls("admission")
    assert admitted, "no MCP call attempted admission after settlement"
    result = admitted[0]["result"]
    assert isinstance(result, dict) and result.get("agent_id") and "error" not in result, result
    assert marker.is_file(), "the admitted agent never executed"
    assert admitted[0]["server_pid"] == s.calls("server_up")[0]["server_pid"]
    assert len(s.calls("launch")) == 1 and s.stops() == [], "the CLI was replaced"
    assert len(s.calls("compact")) == 1 and code == 0
