"""Slice 1 regression contract for surviving lifecycle/scheduler mutants.

Only lifecycle operations, run environments and scheduler observations are
asserted. Crash tests kill their own supervisor, never edit backend records.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))
import ic_support as ic

WAIT = 15
OP = 15


def call(coro):
    return ic.run(coro, timeout=OP)


def until(predicate, description):
    deadline = time.monotonic() + WAIT
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {description}")


@pytest.fixture
def backend(tmp_path):
    root = tmp_path / "backend"
    be = ic.lifecycle().ReferenceBackend(root)
    yield be, root
    for identity in call(be.list_owned("ic-test")):
        call(be.remove(identity, False))


def provision(backend, **fields):
    be, root = backend
    identity = call(be.provision(ic.launch_spec(root, **fields), "resource"))
    assert isinstance(identity, ic.lifecycle().ResourceIdentity), identity
    return identity


def launch(be, identity, code, key="invocation", **fields):
    reply = call(be.run(identity, ic.invocation(ic.py(code), **fields), key))
    assert reply == key, reply
    return key


def exited(be, identity, key):
    def finished():
        state = call(be.inspect_invocation(identity, key))
        return state if ic.kind_of(state) == "exited" else False
    return until(finished, "invocation exit")


@pytest.mark.parametrize("version", [0, 2, -1])
def test_m3_ic_c4_non_v1_launch_is_invalid(backend, version):
    be, root = backend
    reply = call(be.provision(ic.launch_spec(root, spec_version=version), "bad-version"))
    assert isinstance(reply, ic.lifecycle().InvalidSpec)
    assert isinstance(call(be.find("bad-version")), ic.lifecycle().Absent)


def test_m7_ic_c4_stopped_resource_refuses_run(backend, tmp_path):
    be, _ = backend
    identity = provision(backend)
    assert ic.kind_of(call(be.stop(identity, 0.1))) == "exited"
    marker = tmp_path / "must-not-run"
    reply = call(be.run(identity, ic.invocation(ic.py(
        f"from pathlib import Path; Path({str(marker)!r}).touch()")), "late"))
    assert ic.kind_of(reply) in {"exited", "already_invoked"}
    assert not marker.exists()
    assert ic.kind_of(call(be.inspect_invocation(identity, "late"))) == "absent"


@pytest.mark.parametrize("cwd", ["/", "/etc", "/work/repository", "/work/repo/../escape"])
def test_m9_ic_c4_cwd_outside_guest_paths_is_invalid(backend, cwd):
    be, _ = backend
    identity = provision(backend)
    reply = call(be.run(identity, ic.invocation(ic.py("pass"), cwd=cwd), "bad-cwd"))
    assert isinstance(reply, ic.lifecycle().InvalidSpec)
    assert ic.kind_of(call(be.inspect_invocation(identity, "bad-cwd"))) == "absent"


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_m11_ic_c4_negative_output_offset_is_refused(backend, stream):
    be, _ = backend
    identity = provision(backend)
    key = launch(be, identity, "import sys; print('out'); print('err', file=sys.stderr)")
    exited(be, identity, key)
    assert isinstance(call(be.output(identity, key, stream, -1)), ic.lifecycle().InvalidSpec)
    assert ic.data_of(call(be.output(identity, key, stream, 0))) == (
        b"out\n" if stream == "stdout" else b"err\n")


def crash_supervisor(backend):
    be, _ = backend
    identity = provision(backend)
    # The child deliberately crashes its supervisor; no exit evidence can be
    # published. The child then exits too, leaving no live workload behind.
    key = launch(be, identity,
                 "import os, signal; print('crashing', flush=True); "
                 "os.kill(os.getppid(), signal.SIGKILL); os._exit(0)")
    until(lambda: ic.kind_of(call(be.inspect(identity))) == "unknown",
          "unconfirmable supervisor crash")
    return identity, key


def test_m12_m17_ic_c4_unconfirmed_stop_and_inspect_are_unknown(backend):
    be, _ = backend
    identity, key = crash_supervisor(backend)
    assert isinstance(call(be.stop(identity, 0.1)), ic.lifecycle().Unknown)
    assert isinstance(call(be.inspect(identity)), ic.lifecycle().Unknown)
    assert isinstance(call(be.inspect_invocation(identity, key)), ic.lifecycle().Unknown)


@pytest.mark.parametrize("keep", [True, False])
def test_m14_ic_c4_remove_preserves_unknown_resource_and_ownership(backend, keep):
    be, _ = backend
    identity, key = crash_supervisor(backend)
    assert isinstance(call(be.stop(identity, 0.1)), ic.lifecycle().Unknown)
    before = ic.data_of(call(be.output(identity, key, "stdout", 0)))
    assert before == b"crashing\n"
    assert isinstance(call(be.remove(identity, keep)), ic.lifecycle().Unknown)
    assert identity in call(be.list_owned("ic-test"))
    assert isinstance(call(be.find("resource")), ic.lifecycle().ResourceIdentity)
    assert isinstance(call(be.inspect(identity)), ic.lifecycle().Unknown)
    assert ic.data_of(call(be.output(identity, key, "stdout", 0))) == before


def test_m20_ic_c4_graceful_term_handler_runs_before_force(backend, tmp_path):
    be, _ = backend
    identity = provision(backend)
    ready, marker = tmp_path / "ready", tmp_path / "term"
    key = launch(be, identity,
                 "import signal; from pathlib import Path; "
                 f"signal.signal(signal.SIGTERM, lambda *_: (Path({str(marker)!r}).write_text('TERM'), exit(0))); "
                 f"Path({str(ready)!r}).touch(); signal.pause()")
    until(ready.exists, "TERM handler installation")
    assert ic.kind_of(call(be.stop(identity, 5))) == "exited"
    assert marker.read_text() == "TERM"
    assert ic.kind_of(call(be.inspect_invocation(identity, key))) == "exited"


@pytest.mark.parametrize("paths", [
    {"guest_home": "/work/repo"},
    {"guest_home": "/work/repo/home"},
    {"guest_repo": "/work/home/repo"},
    {"guest_temp": "/work/home/tmp"},
])
def test_m19_ic_c4_overlapping_guest_paths_are_invalid(backend, paths):
    be, root = backend
    assert isinstance(call(be.provision(ic.launch_spec(root, **paths), "overlap")),
                      ic.lifecycle().InvalidSpec)
    assert ic.kind_of(call(be.find("overlap"))) == "absent"


def test_m1_ic_c5_stop_never_signals_an_unrelated_real_child(backend):
    """Partial M1 coverage: public identity cannot confer authority over a PID.

    There is no public API for installing a fake recorded process start time;
    this exercises the specified fallback with a child the backend never ran.
    """
    be, _ = backend
    child = subprocess.Popen(ic.py("import signal; print('ready', flush=True); signal.pause()"),
                             stdout=subprocess.PIPE, text=True, start_new_session=True)
    try:
        # A child-originated pipe event ensures the process has really started.
        import selectors
        with selectors.DefaultSelector() as selector:
            selector.register(child.stdout, selectors.EVENT_READ)
            assert selector.select(WAIT), "unrelated child did not become ready"
        assert child.stdout.readline() == "ready\n"
        identity = ic.lifecycle().ResourceIdentity(str(child.pid))
        assert ic.kind_of(call(be.stop(identity, 0.1))) == "absent"
        assert child.poll() is None
    finally:
        child.kill()
        child.wait(timeout=WAIT)
        child.stdout.close()


def test_b1_k3_host_strip_removes_backend_root_without_backend_help():
    from multiagents.executor.base import strip_run_environment
    env = {"IC_BACKEND_ROOT": "/host/private", "MULTIAGENTS_TEST_ISOLATED_BACKEND": "x:y",
           "IC_TEST_PLAN": "/host/plan", "PATH": "/bin", "APP_VALUE": "keep"}
    result = strip_run_environment(env)
    assert "IC_BACKEND_ROOT" not in result
    assert not set(ic.INJECTED).intersection(result)
    assert result["PATH"] == "/bin" and result["APP_VALUE"] == "keep"


# A protocol double loaded by the scheduler through the contract's K3 seam.
# Its control plane uses the existing per-test plan file and never run state.
class MutantBackend:
    version = "mutant-contract-1"

    def __init__(self, root):
        self.inner = ic.lifecycle().ReferenceBackend(Path(os.environ["IC_BACKEND_ROOT"]))

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def plan(self):
        return json.loads(Path(os.environ[ic.PLAN_ENV]).read_text())

    def observed(self, event):
        Path(os.environ[ic.PLAN_ENV] + "." + event).touch()

    async def provision(self, spec, key):
        reply = await self.inner.provision(spec, key)
        if self.plan().get("mode") == "find_unknown":
            raise RuntimeError("lost provision reply")
        return reply

    async def find(self, key):
        if self.plan().get("mode") == "find_unknown":
            self.observed("unknown")
            return ic.lifecycle().Unknown("lookup temporarily unavailable")
        return await self.inner.find(key)

    async def inspect_invocation(self, identity, key):
        if self.plan().get("mode") == "invocation_unknown":
            self.observed("unknown")
            return ic.lifecycle().Unknown("invocation outcome unavailable")
        return await self.inner.inspect_invocation(identity, key)

    async def run(self, identity, invocation, key):
        mode = self.plan().get("mode")
        if mode == "invalid_run":
            return ic.lifecycle().InvalidSpec("invocation refused")
        if mode == "unsupported_run":
            return ic.lifecycle().Unsupported("invocation")
        reply = await self.inner.run(identity, invocation, key)
        if mode == "invocation_unknown":
            raise RuntimeError("lost invocation reply")
        return reply


    async def inspect(self, identity):
        if self.plan().get("mode") == "invocation_unknown":
            return ic.lifecycle().Unknown("resource outcome unavailable")
        return await self.inner.inspect(identity)

    async def stop(self, identity, grace):
        reply = await self.inner.stop(identity, grace)
        if self.plan().get("mode") == "invocation_unknown":
            return ic.lifecycle().Unknown("termination not confirmed")
        return reply


def make_mutant_backend(root):
    return MutantBackend(root)


@pytest.fixture
def scheduler(tmp_path, monkeypatch):
    s = ic.IcSched(tmp_path, monkeypatch)
    monkeypatch.setenv(ic.BACKEND_ENV, "test_ic_c4_mutants:make_mutant_backend")
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join([
        str(Path(__file__).resolve().parent), os.environ.get("PYTHONPATH", "")]))
    s.p.project["limits"] = {"max_depth": 7, "max_concurrent": 1}
    s.p.write()
    s._reset_server()
    yield s
    s.plan({})
    s.close()


@pytest.mark.parametrize("mode,mutant", [("invocation_unknown", "I3"), ("find_unknown", "I15")])
def test_i3_i15_k6_k7_unknown_holds_slot_and_prevents_retry(scheduler, mode, mutant):
    s = scheduler
    s.plan({"mode": mode})
    s.start()
    first = s.create()["id"]
    s.wait_runs(first, 1, timeout=WAIT)
    until(Path(str(s.plan_file) + ".unknown").exists, "backend uncertainty")
    second = s.create()["id"]
    s.wait(lambda: "max_concurrent" in ic.text_of(s.blocked(second)), timeout=WAIT,
           what="the reservation to block a competing attempt")
    assert len(s.runs(first)) == 1, mutant
    assert s.runs(second) == [], mutant
    assert s.get(first)["state"] not in {"done", "failed", "cancelled"}
    assert len(s.owned()) == 1
    s.plan({})
    s.wait_runs(second, 1, timeout=WAIT)


@pytest.mark.parametrize("mode", ["invalid_run", "unsupported_run"])
def test_i9_ic_c3_run_refusal_is_other_not_launch(scheduler, mode):
    s = scheduler
    s.plan({"mode": mode})
    s.start()
    node = s.create()["id"]
    def refusal():
        view = s.get(node)
        causes = [c for c in ic.causes(view) if "isolation_unavailable" in ic.text_of(c)]
        return (view, causes) if causes and view["state"] == "held" else False
    view, causes = s.wait(refusal, timeout=WAIT, what="the nonretryable run refusal")
    assert {c["class"] for c in causes} == {"other"}
    assert all(c.get("retryable", False) is False for c in causes)
    assert len(view["runs"]) == 1
    assert s.fake.spawns() == 0


def test_i12_i14_i16_k6_normal_exit_confirms_termination_and_retains_artifacts(scheduler):
    s = scheduler
    s.start()
    node = s.create()["id"]
    s.wait(lambda: s.get(node)["state"] == "done", timeout=WAIT, what="normal completion")
    attempt = s.runs(node)[0]["attempt_id"]
    evidence = s.evidence(attempt)["executor"]
    assert evidence.get("termination_confirmed") is True
    be = ic.lifecycle().ReferenceBackend(s.backend_root)
    identity = ic.lifecycle().ResourceIdentity(evidence["identity"])
    key = evidence["invocation_id"]
    state = call(be.inspect_invocation(identity, key))
    assert isinstance(state, ic.lifecycle().Exited) and state.code == 0
    assert state.finished_at is not None
    stdout = ic.data_of(call(be.output(identity, key, "stdout", 0)))
    stderr = ic.data_of(call(be.output(identity, key, "stderr", 0)))
    assert stdout, "positive control: the provider emitted output"
    assert isinstance(call(be.remove(identity, True)), ic.lifecycle().Exited)
    assert call(be.inspect_invocation(identity, key)) == state
    assert ic.data_of(call(be.output(identity, key, "stdout", 0))) == stdout
    assert ic.data_of(call(be.output(identity, key, "stderr", 0))) == stderr
    assert isinstance(call(be.inspect(identity)), ic.lifecycle().Exited)
    next_node = s.create()["id"]
    s.wait_runs(next_node, 1, timeout=WAIT)
