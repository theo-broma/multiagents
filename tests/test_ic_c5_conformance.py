"""IC-C4 / IC-C5 — the backend lifecycle protocol, exercised through the
protocol only, parametrised by backend.

Slice 1 runs it on the reference backend (processes on the host under a
private directory). A Docker backend joins by appending to `BACKENDS`; the
containment cases are marked `ic_milestone(3)` and skip on the reference
backend, which is never accepted as isolation evidence (IC-C5).

Test ids name IC-C4 clauses. Import of `multiagents.executor.lifecycle`
happens inside the fixture, so a missing module fails these tests only.
"""
from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import ic_backends  # noqa: E402
import ic_support as ic  # noqa: E402
from ic_support import data_of, identity_of, invocation, kind_of, launch_spec, py, run  # noqa: E402

BACKENDS = ["reference"]
GRACE = 0.3


@pytest.fixture(params=BACKENDS)
def be(request, tmp_path):
    """(backend, host root, name). A real backend's factory joins here."""
    root = tmp_path / "backend-root"
    root.mkdir()
    assert request.param == "reference"
    backend = ic_backends.make_reference(root)
    yield backend, root, request.param
    # finalizer: nothing a test provisioned outlives it
    async def cleanup():
        for ident in await backend.list_owned("ic-test"):
            try:
                await backend.stop(identity_of(ident), 0.1)
                await backend.remove(identity_of(ident), False)
            except Exception:
                pass
    try:
        run(cleanup())
    except Exception:
        pass


def provisioned(be, key="key-1", **over):
    backend, root, _ = be
    spec = launch_spec(root, **over)
    return spec, identity_of(run(backend.provision(spec, key)))


def wait_for(fn, what, timeout=ic.OP):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = fn()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


def started(be, code, key="inv-1", ident=None, **kw):
    backend, _, _ = be
    run(backend.run(ident, invocation(py(code), **kw), key))
    return key


def state_of_invocation(be, ident, inv):
    return kind_of(run(be[0].inspect_invocation(ident, inv)))


def finished(be, ident, inv):
    wait_for(lambda: state_of_invocation(be, ident, inv) == "exited", "the invocation to exit")
    return run(be[0].inspect_invocation(ident, inv))


def out(be, ident, inv, stream="stdout", offset=0):
    return data_of(run(be[0].output(ident, inv, stream, offset)))


# ------------------------------------------------------------- the spec digest

def test_ic_c4_digest_is_sha256_of_canonical_json(be):
    spec = launch_spec(be[1])
    digest = ic.digest_of(spec)
    import dataclasses
    assert digest == ic.canonical_digest(dataclasses.asdict(spec) if dataclasses.is_dataclass(spec)
                                         else spec.to_dict())


def test_ic_c4_digest_ignores_key_order_and_changes_with_any_field(be):
    root = be[1]
    a = launch_spec(root, labels={"a": "1", "b": "2"})
    b = launch_spec(root, labels={"b": "2", "a": "1"})
    assert ic.digest_of(a) == ic.digest_of(b)
    for over in ({"owner": "att_other"}, {"lifetime": "conversation"},
                 {"labels": {"a": "1"}}, {"guest_repo": "/work/other"}):
        assert ic.digest_of(launch_spec(root, **over)) != ic.digest_of(launch_spec(root)), over


# ----------------------------------------------------------------- probe

def test_ic_c4_probe_is_a_list_empty_when_ready_and_has_no_side_effects(be):
    backend, root, _ = be
    before = sorted(p.relative_to(root) for p in root.rglob("*"))
    problems = run(backend.probe())
    assert list(problems) == []
    assert sorted(p.relative_to(root) for p in root.rglob("*")) == before


# ------------------------------------------------------------- provision / find

def test_ic_c4_provision_returns_an_immutable_identity_in_state_created(be):
    spec, ident = provisioned(be)
    assert ident
    assert kind_of(run(be[0].inspect(ident))) == "created"
    with pytest.raises((AttributeError, TypeError, ValueError)):
        ident.id = "other"                              # immutable (frozen) identity
    hash(ident)


def test_ic_c4_provision_is_idempotent_by_key_and_digest(be):
    spec, first = provisioned(be)
    second = identity_of(run(be[0].provision(spec, "key-1")))
    assert second == first
    owned = run(be[0].list_owned("ic-test"))
    assert len(owned) == 1


def test_ic_c4_same_key_with_a_different_digest_is_an_identity_conflict_and_changes_nothing(be):
    spec, first = provisioned(be)
    other = launch_spec(be[1], owner="att_conf2")
    reply = run(be[0].provision(other, "key-1"))
    assert kind_of(reply) == "identity_conflict"
    assert identity_of(run(be[0].find("key-1"))) == first
    assert kind_of(run(be[0].inspect(first))) == "created"
    assert len(run(be[0].list_owned("ic-test"))) == 1


def test_ic_c4_distinct_keys_give_distinct_resources(be):
    _, a = provisioned(be, "key-a", owner="att_a")
    _, b = provisioned(be, "key-b", owner="att_b")
    assert a != b
    assert len(run(be[0].list_owned("ic-test"))) == 2


def test_ic_c4_find_returns_the_identity_created_under_a_key_or_absent(be):
    assert kind_of(run(be[0].find("never-used"))) == "absent"
    _, ident = provisioned(be)
    assert identity_of(run(be[0].find("key-1"))) == ident


def test_ic_c4_find_works_for_a_backend_instance_that_did_not_provision(be):
    """The crash between recording the key and recording the identity."""
    _, ident = provisioned(be)
    fresh = ic_backends.make_reference(be[1])
    assert identity_of(run(fresh.find("key-1"))) == ident
    assert kind_of(run(fresh.inspect(ident))) == "created"


def test_ic_c4_a_host_path_outside_the_host_issued_root_is_invalid_spec(be, tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    for over in ({"writable_paths": [str(outside)]}, {"readonly_paths": [str(outside)]},
                 {"writable_paths": [str(be[1] / ".." / "elsewhere")]},
                 {"readonly_paths": ["/etc"]}):
        reply = run(be[0].provision(launch_spec(be[1], **over), "key-bad"))
        assert kind_of(reply) == "invalid_spec", over
        assert kind_of(run(be[0].find("key-bad"))) == "absent"          # nothing was created


def test_ic_c4_a_symlink_out_of_the_root_is_invalid_spec(be, tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    link = be[1] / "link"
    link.symlink_to(outside)
    reply = run(be[0].provision(launch_spec(be[1], writable_paths=[str(link)]), "key-link"))
    assert kind_of(reply) == "invalid_spec"


def test_ic_c4_inspect_of_an_unknown_identity_is_absent_never_unknown(be):
    _, ident = provisioned(be)
    run(be[0].remove(ident, False))
    assert kind_of(run(be[0].inspect(ident))) == "absent"


# ------------------------------------------------------------------ run / inspect

def test_ic_c4_an_invocation_runs_and_reports_exited_with_code_and_time(be):
    _, ident = provisioned(be)
    inv = started(be, "import sys; sys.exit(7)", ident=ident)
    state = finished(be, ident, inv)
    assert ic.field(state, "code") == 7
    assert ic.field(state, "oom") in (False, None, 0)
    assert ic.field(state, "finished_at") is not None


def test_ic_c4_a_running_invocation_is_running_and_the_resource_too(be):
    _, ident = provisioned(be)
    inv = started(be, "import time; time.sleep(30)", ident=ident)
    wait_for(lambda: state_of_invocation(be, ident, inv) == "running", "running")
    assert kind_of(run(be[0].inspect(ident))) == "running"


def test_ic_c4_run_is_idempotent_by_key(be):
    _, ident = provisioned(be)
    code = "print('once', flush=True)"
    started(be, code, key="inv-1", ident=ident)
    finished(be, ident, "inv-1")
    again = run(be[0].run(ident, invocation(py(code)), "inv-1"))
    assert kind_of(again) not in ("already_invoked", "exited", "invalid_spec", "unknown")
    time.sleep(0.2)
    assert out(be, ident, "inv-1") == b"once\n"                 # one process ran, not two


def test_ic_c4_an_attempt_resource_refuses_a_second_agent_invocation(be):
    _, ident = provisioned(be, lifetime="attempt")
    started(be, "import time; time.sleep(30)", key="inv-1", ident=ident)
    second = run(be[0].run(ident, invocation(py("print(1)")), "inv-2"))
    assert kind_of(second) == "already_invoked"


def test_ic_c4_an_exited_attempt_resource_refuses_run_with_exited(be):
    _, ident = provisioned(be, lifetime="attempt")
    started(be, "pass", key="inv-1", ident=ident)
    finished(be, ident, "inv-1")
    wait_for(lambda: kind_of(run(be[0].inspect(ident))) == "exited", "the resource to exit")
    assert kind_of(run(be[0].run(ident, invocation(py("print(1)")), "inv-2"))) == "exited"


def test_ic_c4_a_conversation_resource_carries_one_invocation_per_turn(be):
    _, ident = provisioned(be, owner="conv_1", lifetime="conversation")
    for n in (1, 2, 3):
        started(be, f"print({n}, flush=True)", key=f"turn-{n}", ident=ident, kind="turn")
        finished(be, ident, f"turn-{n}")
        assert out(be, ident, f"turn-{n}") == f"{n}\n".encode()
    assert kind_of(run(be[0].inspect(ident))) != "absent"


def test_ic_c4_a_user_shell_is_an_invocation_of_its_own_kind(be):
    _, ident = provisioned(be, owner="conv_2", lifetime="conversation")
    started(be, "print('hi')", key="sh-1", ident=ident, kind="user_shell")
    state = finished(be, ident, "sh-1")
    assert ic.field(state, "code") == 0
    assert kind_of(run(be[0].inspect_invocation(ident, "never-started"))) == "absent"


def test_ic_c4_the_working_directory_is_a_guest_path_and_env_reaches_the_process(be):
    _, ident = provisioned(be)
    started(be, "import os; print(os.environ['IC_X'], os.getcwd().endswith('repo'))",
            ident=ident, env={"IC_X": "marker"})
    finished(be, ident, "inv-1")
    assert out(be, ident, "inv-1") == b"marker True\n"


# ------------------------------------------------------------------ output

CHATTY = ("import sys,time\n"
          "sys.stdout.write('o1\\n'); sys.stdout.flush()\n"
          "sys.stderr.write('e1\\n'); sys.stderr.flush()\n"
          "sys.stdout.write('o2\\n'); sys.stdout.flush()\n"
          "sys.stderr.write('e2\\n'); sys.stderr.flush()\n")


def test_ic_c4_stdout_and_stderr_are_separate_byte_streams(be):
    _, ident = provisioned(be)
    started(be, CHATTY, ident=ident)
    finished(be, ident, "inv-1")
    assert out(be, ident, "inv-1", "stdout") == b"o1\no2\n"
    assert out(be, ident, "inv-1", "stderr") == b"e1\ne2\n"


def test_ic_c4_output_replays_from_any_offset(be):
    _, ident = provisioned(be)
    started(be, CHATTY, ident=ident)
    finished(be, ident, "inv-1")
    whole = b"o1\no2\n"
    for offset in range(len(whole) + 1):
        assert out(be, ident, "inv-1", "stdout", offset) == whole[offset:]


def test_ic_c4_output_is_readable_while_running_and_grows_without_loss_or_duplication(be):
    _, ident = provisioned(be)
    started(be, "import sys,time\nprint('first',flush=True)\ntime.sleep(2)\nprint('second',flush=True)",
            ident=ident)
    wait_for(lambda: out(be, ident, "inv-1") == b"first\n", "first line")
    mark = len(b"first\n")
    finished(be, ident, "inv-1")
    assert out(be, ident, "inv-1", "stdout", mark) == b"second\n"


def test_ic_c4_output_survives_exit_and_binary_data_is_kept_exactly(be):
    _, ident = provisioned(be)
    started(be, "import sys; sys.stdout.buffer.write(bytes(range(256)) + b'\\r\\n\\x00end')",
            ident=ident)
    finished(be, ident, "inv-1")
    time.sleep(0.2)
    assert out(be, ident, "inv-1") == bytes(range(256)) + b"\r\n\x00end"


# ------------------------------------------------------------------ stop

def test_ic_c4_stop_ends_a_running_resource_and_is_idempotent(be):
    _, ident = provisioned(be)
    inv = started(be, "import time; time.sleep(60)", ident=ident)
    wait_for(lambda: state_of_invocation(be, ident, inv) == "running", "running")
    first = run(be[0].stop(ident, GRACE))
    assert kind_of(first) != "unknown"
    assert kind_of(run(be[0].inspect(ident))) == "exited"
    assert kind_of(run(be[0].stop(ident, GRACE))) != "unknown"            # again: no error, no change
    assert kind_of(run(be[0].inspect(ident))) == "exited"


def test_ic_c4_stop_forces_termination_after_the_grace_period(be):
    _, ident = provisioned(be)
    stubborn = ("import signal,time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                "print('armed', flush=True)\ntime.sleep(60)")
    inv = started(be, stubborn, ident=ident)
    wait_for(lambda: out(be, ident, inv) == b"armed\n", "the handler to be armed")
    t0 = time.monotonic()
    run(be[0].stop(ident, 0.5))
    assert time.monotonic() - t0 < 6
    assert kind_of(run(be[0].inspect(ident))) == "exited"


def test_ic_c4_stop_of_an_absent_resource_is_absent(be):
    _, ident = provisioned(be)
    run(be[0].remove(ident, False))
    assert kind_of(run(be[0].stop(ident, GRACE))) == "absent"


def test_ic_c4_stop_signals_only_processes_the_backend_started(be):
    """C15: never a host process it did not start, even one in the same session."""
    bystander = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        _, ident = provisioned(be)
        inv = started(be, "import time; time.sleep(60)", ident=ident)
        wait_for(lambda: state_of_invocation(be, ident, inv) == "running", "running")
        run(be[0].stop(ident, GRACE))
        assert bystander.poll() is None
    finally:
        bystander.kill()
        bystander.wait(5)


def test_ic_c4_a_forged_pid_file_cannot_redirect_a_stop(be):
    """Signals go through the recorded identity, not through files the agent writes."""
    bystander = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        _, ident = provisioned(be)
        forge = (f"import pathlib,os\n"
                 f"for n in ('wrapper.pid','container.pid','agent.pid','pid'):\n"
                 f"    for d in (os.getcwd(), os.environ.get('HOME','.'), os.environ.get('TMPDIR','/tmp')):\n"
                 f"        try: pathlib.Path(d, n).write_text('{bystander.pid}')\n"
                 f"        except OSError: pass\n"
                 f"import time; time.sleep(60)")
        inv = started(be, forge, ident=ident)
        wait_for(lambda: state_of_invocation(be, ident, inv) == "running", "running")
        time.sleep(0.3)
        run(be[0].stop(ident, GRACE))
        assert bystander.poll() is None
    finally:
        bystander.kill()
        bystander.wait(5)


@pytest.mark.ic_milestone(3)
def test_ic_c4_stop_reaches_a_descendant_that_left_the_session(be):
    """Escaped descendants (setsid) die with the resource. Real backends only."""
    if be[2] == "reference":
        pytest.skip("containment is not claimed by the reference backend (IC-C5)")
    _, ident = provisioned(be)
    code = ("import os,time\nif os.fork()==0:\n    os.setsid()\n    open('/work/tmp/escaped','w').write(str(os.getpid()))\n"
            "    time.sleep(60)\nelse:\n    time.sleep(60)")
    inv = started(be, code, ident=ident)
    wait_for(lambda: state_of_invocation(be, ident, inv) == "running", "running")
    run(be[0].stop(ident, GRACE))
    assert kind_of(run(be[0].inspect(ident))) == "exited"
    assert run(be[0].stop(ident, GRACE)) is not None


@pytest.mark.ic_milestone(3)
def test_ic_c4_a_sibling_resource_is_not_readable(be):
    if be[2] == "reference":
        pytest.skip("containment is not claimed by the reference backend (IC-C5)")
    _, a = provisioned(be, "key-a", owner="att_a")
    _, b = provisioned(be, "key-b", owner="att_b")
    started(be, "open('/work/home/secret','w').write('B-SECRET'); import time; time.sleep(60)",
            key="inv-b", ident=b)
    started(be, "import glob,sys\nprint([p for p in glob.glob('/**/secret', recursive=True)])",
            key="inv-a", ident=a)
    finished(be, a, "inv-a")
    assert b"B-SECRET" not in out(be, a, "inv-a")


# ------------------------------------------------------------------ remove

def test_ic_c4_remove_keeping_artifacts_leaves_output_and_exit_evidence(be):
    _, ident = provisioned(be)
    started(be, "import sys; print('kept'); sys.exit(3)", ident=ident)
    finished(be, ident, "inv-1")
    run(be[0].remove(ident, True))
    state = run(be[0].inspect(ident))
    assert kind_of(state) == "exited"
    assert out(be, ident, "inv-1") == b"kept\n"
    assert ic.field(run(be[0].inspect_invocation(ident, "inv-1")), "code") == 3


def test_ic_c4_remove_without_artifacts_discards_everything(be):
    _, ident = provisioned(be)
    started(be, "print('gone')", ident=ident)
    finished(be, ident, "inv-1")
    run(be[0].remove(ident, False))
    assert kind_of(run(be[0].inspect(ident))) == "absent"
    assert kind_of(run(be[0].find("key-1"))) == "absent"
    assert run(be[0].list_owned("ic-test")) == [] or len(run(be[0].list_owned("ic-test"))) == 0


def test_ic_c4_remove_is_idempotent_and_removing_a_running_resource_stops_it(be):
    _, ident = provisioned(be)
    inv = started(be, "import time; time.sleep(60)", ident=ident)
    wait_for(lambda: state_of_invocation(be, ident, inv) == "running", "running")
    run(be[0].remove(ident, False))
    run(be[0].remove(ident, False))                                       # again: no error
    assert kind_of(run(be[0].inspect(ident))) == "absent"


# ----------------------------------------------------------------- list_owned

def test_ic_c4_list_owned_reports_every_resource_and_deletes_nothing(be):
    _, a = provisioned(be, "key-a", owner="att_a")
    _, b = provisioned(be, "key-b", owner="att_b")
    owned = [identity_of(x) for x in run(be[0].list_owned("ic-test"))]
    assert a in owned and b in owned
    run(be[0].list_owned("ic-test"))
    assert kind_of(run(be[0].inspect(a))) == "created"
    assert kind_of(run(be[0].inspect(b))) == "created"


def test_ic_c4_list_owned_finds_resources_of_a_previous_backend_instance(be):
    """Orphans: a crashed host left them; a new instance reports them."""
    _, ident = provisioned(be)
    fresh = ic_backends.make_reference(be[1])
    assert ident in [identity_of(x) for x in run(fresh.list_owned("ic-test"))]


# ------------------------------------------------------------------ cancellation

def test_ic_c4_cancelling_provision_leaves_a_state_inspect_reports_truthfully(be):
    backend, root, _ = be
    spec = launch_spec(root)

    async def go():
        task = asyncio.ensure_future(backend.provision(spec, "key-cancel"))
        await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        found = identity_of(await backend.find("key-cancel"))
        if kind_of(found) == "absent":
            return "absent"
        return kind_of(await backend.inspect(found))
    assert run(go()) in ("absent", "created")


def test_ic_c4_cancelling_run_leaves_the_invocation_in_a_truthful_state(be):
    _, ident = provisioned(be)
    backend = be[0]

    async def go():
        task = asyncio.ensure_future(backend.run(ident, invocation(py("import time; time.sleep(30)")), "inv-c"))
        await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await asyncio.sleep(0.2)
        return kind_of(await backend.inspect_invocation(ident, "inv-c"))
    assert run(go()) in ("absent", "running", "exited")                  # never unknown, never a lie


# ================================================================ K1: names

def test_ic_c5_k1_the_named_types_and_outcomes_exist():
    lc = ic.lifecycle()
    for name in ("ReferenceBackend", "LaunchSpec", "InvocationSpec", "ResourceIdentity", "InvalidSpec",
                 "Unsupported", "IdentityConflict", "AlreadyInvoked", "Absent", "Created", "Running",
                 "Exited", "Unknown"):
        assert isinstance(getattr(lc, name, None), type), name


def test_ic_c5_k1_outcome_fields():
    lc = ic.lifecycle()
    assert lc.InvalidSpec("why").reason == "why"
    assert lc.Unknown("because").reason == "because"
    assert lc.Unsupported("resources").field == "resources"
    exited = lc.Exited(code=3, oom=False, finished_at=12.5)
    assert (exited.code, exited.oom, exited.finished_at) == (3, False, 12.5)


def test_ic_c5_k1_launch_spec_defaults(tmp_path):
    lc = ic.lifecycle()
    spec = lc.LaunchSpec(owner="att_x", lifetime="attempt")
    assert spec.spec_version == 1
    assert (spec.guest_repo, spec.guest_home, spec.guest_temp) == ("/work/repo", "/work/home", "/work/tmp")
    for name in ("labels", "writable_paths", "readonly_paths"):
        assert not getattr(spec, name)
    for name in ("seed", "session_state", "resources", "credential_handle", "transport_handle"):
        assert not getattr(spec, name)
    assert ic.digest_of(spec) == ic.digest_of(lc.LaunchSpec(owner="att_x", lifetime="attempt"))


def test_ic_c5_k1_the_reference_backend_has_a_version_and_typed_outcomes(be):
    lc = ic.lifecycle()
    backend = be[0]
    assert isinstance(backend.version, str) and backend.version
    spec, ident = provisioned(be)
    assert isinstance(ident, lc.ResourceIdentity)
    assert isinstance(ident.id, str) and ident.id
    assert isinstance(run(backend.inspect(ident)), lc.Created)
    inv = started(be, "import time; time.sleep(30)", ident=ident)
    wait_for(lambda: isinstance(run(backend.inspect_invocation(ident, inv)), lc.Running), "Running")
    run(backend.stop(ident, GRACE))
    assert isinstance(run(backend.inspect(ident)), lc.Exited)
    run(backend.remove(ident, False))
    assert isinstance(run(backend.inspect(ident)), lc.Absent)
    assert isinstance(run(backend.find("nothing")), lc.Absent)


def test_ic_c5_k1_conflict_and_refusal_classes(be):
    lc = ic.lifecycle()
    spec, ident = provisioned(be)
    assert isinstance(run(be[0].provision(launch_spec(be[1], owner="att_z"), "key-1")), lc.IdentityConflict)
    started(be, "import time; time.sleep(30)", ident=ident)
    assert isinstance(run(be[0].run(ident, invocation(py("pass")), "inv-2")), lc.AlreadyInvoked)
    assert isinstance(run(be[0].provision(launch_spec(be[1], writable_paths=["/etc"]), "key-bad")),
                      lc.InvalidSpec)


def test_ic_c5_k1_the_invocation_id_run_returns_is_the_invocation_key(be):
    _, ident = provisioned(be)
    reply = run(be[0].run(ident, invocation(py("print(1)")), "inv-key-77"))
    got = reply if isinstance(reply, str) else (ic.field(reply, "invocation_id") or ic.field(reply, "id"))
    assert got == "inv-key-77"
    assert kind_of(run(be[0].inspect_invocation(ident, "inv-key-77"))) in ("running", "exited")


# ======================================== K2: the invocation is idempotent by key too

def test_ic_c5_k2_same_key_different_invocation_spec_is_an_identity_conflict_and_starts_nothing(be):
    _, ident = provisioned(be, owner="conv_k2", lifetime="conversation")
    started(be, "print('one', flush=True)", key="turn-1", ident=ident, kind="turn")
    finished(be, ident, "turn-1")
    for other in (invocation(py("print('two', flush=True)"), kind="turn"),
                  invocation(py("print('one', flush=True)"), kind="turn", env={"X": "1"}),
                  invocation(py("print('one', flush=True)"), kind="user_shell")):
        reply = run(be[0].run(ident, other, "turn-1"))
        assert kind_of(reply) == "identity_conflict"
    time.sleep(0.3)
    assert out(be, ident, "turn-1") == b"one\n"                        # nothing was started
    assert ic.field(run(be[0].inspect_invocation(ident, "turn-1")), "code") == 0


def test_ic_c5_k2_a_conflict_while_the_first_still_runs_starts_nothing_and_leaves_it_running(be):
    _, ident = provisioned(be)
    inv = started(be, "import time; time.sleep(60)", ident=ident)
    wait_for(lambda: state_of_invocation(be, ident, inv) == "running", "running")
    reply = run(be[0].run(ident, invocation(py("print('x')")), inv))
    assert kind_of(reply) == "identity_conflict"
    assert state_of_invocation(be, ident, inv) == "running"


# ============================================ K9: what the reference backend implements

UNIMPLEMENTED = {"session_state": "ss-handle", "resources": {"memory_mb": 256},
                 "credential_handle": "cred-handle", "transport_handle": "tr-handle"}


@pytest.mark.parametrize("name", sorted(UNIMPLEMENTED))
def test_ic_c5_k9_a_non_empty_unimplemented_field_is_unsupported_naming_the_field(be, name):
    reply = run(be[0].provision(launch_spec(be[1], **{name: UNIMPLEMENTED[name]}), "key-u"))
    assert kind_of(reply) == "unsupported"
    assert ic.field(reply, "field") == name
    assert kind_of(run(be[0].find("key-u"))) == "absent"                # nothing created


def test_ic_c5_k9_invalid_spec_is_checked_before_unsupported(be):
    reply = run(be[0].provision(launch_spec(be[1], writable_paths=["/etc"], seed="seed-ref-1"), "key-u"))
    assert kind_of(reply) == "invalid_spec"


def test_ic_c5_k9_an_unset_resources_field_does_not_make_the_launch_unsupported(be):
    spec = launch_spec(be[1])                                           # resources unset
    assert not spec.resources
    assert kind_of(run(be[0].provision(spec, "key-r"))) not in ("unsupported", "invalid_spec", "unknown")


def test_ic_c5_k9_each_invocation_gets_a_private_home_and_tmpdir(be):
    _, ident = provisioned(be)
    probe = "import os; print(os.environ['HOME'], os.environ['TMPDIR'])"
    started(be, probe, key="inv-1", ident=ident)
    finished(be, ident, "inv-1")
    home, tmp = out(be, ident, "inv-1").decode().split()
    assert home != tmp and home not in ("", "/") and tmp not in ("", "/tmp")
    _, ident2 = provisioned(be, "key-2", owner="att_other")
    started(be, probe, key="inv-1", ident=ident2)
    finished(be, ident2, "inv-1")
    assert out(be, ident2, "inv-1").decode().split() != [home, tmp]


def test_ic_c5_k9_declared_writable_path_is_writable_and_readonly_path_is_not(be):
    backend, root, _ = be
    rw, ro = root / "rw", root / "ro"
    rw.mkdir()
    ro.mkdir()
    (ro / "f").write_text("x")
    _, ident = provisioned(be, writable_paths=[str(rw)], readonly_paths=[str(ro)])
    code = ("import os\nfor p in os.environ['IC_RW'], os.environ['IC_RO']:\n"
            "    try: open(p + '/new','w').write('x'); print('wrote', flush=True)\n"
            "    except OSError: print('denied', flush=True)")
    started(be, code, ident=ident, env={"IC_RW": str(rw), "IC_RO": str(ro)})
    finished(be, ident, "inv-1")
    assert out(be, ident, "inv-1").split() == [b"wrote", b"denied"]


# ============================================== K12: the project label and list_owned

def test_ic_c5_k12_list_owned_keeps_two_projects_apart(be):
    backend, root, _ = be
    a = launch_spec(root, owner="att_a", labels={ic.PROJECT_LABEL: "proj-a"})
    b = launch_spec(root, owner="att_b", labels={ic.PROJECT_LABEL: "proj-b"})
    ida = identity_of(run(backend.provision(a, "key-a")))
    idb = identity_of(run(backend.provision(b, "key-b")))
    assert [identity_of(x) for x in run(backend.list_owned("proj-a"))] == [ida]
    assert [identity_of(x) for x in run(backend.list_owned("proj-b"))] == [idb]
    assert run(backend.list_owned("proj-c")) == []
    assert kind_of(run(backend.inspect(ida))) == "created"             # reporting deleted nothing
