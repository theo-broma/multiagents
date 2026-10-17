"""IC-C16 — stop, cancel and steer on isolated attempts (IC-R7), and the
D-S3.2 refusals.

Through the scheduler's public entry points on the reference backend (K3
injection, `ic_s3_backends`) and, parametrised, on the real Docker backend
under SG_TEST_DOCKER=1 (a missing prerequisite is then a failure; not
runnable in the authoring sandbox). The refusals also run on the fake CLI.

Nothing the attempt writes is trusted: the workload forges `wrapper.pid`,
`container.pid` and `agent.pid` wherever it can, naming a live decoy process
of the test or a dead pid.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import ic_docker as dk  # noqa: E402
import ic_s3 as s3  # noqa: E402
import ic_support as ic  # noqa: E402
import ic_workload as wl  # noqa: E402
from ic_support import kind_of  # noqa: E402

DOCKER = pytest.param("docker", marks=pytest.mark.skipif(not dk.OPT_IN, reason="real Docker is opt-in: SG_TEST_DOCKER=1"))


@pytest.fixture(params=["reference", DOCKER])
def sx(request, tmp_path, monkeypatch, st_live_docker):
    owner = None
    if request.param == "docker":
        dk.require_docker()
        owner = dk.test_owner(request)
    s = s3.S3Sched(tmp_path, monkeypatch, backend=request.param, owner=owner)
    yield s
    s.close()
    if owner:
        dk.remove_session_resources(owner)


@pytest.fixture
def fs(tmp_path, monkeypatch):
    s = s3.FakeDockerSched(tmp_path, monkeypatch)
    yield s
    s.close()


def running(s, node):
    run = s.wait_runs(node, 1)[0]
    s.wait(lambda: s.evidence(run["attempt_id"]).get("executor", {}).get("invocation_id"),
           ic.WAIT * 2, "the invocation to be recorded")
    return run


def resource_state(s, run):
    key = s.evidence(run["attempt_id"])["executor"]["creation_key"]
    be = s.be()
    found = ic.run(be.find(key))
    if kind_of(found) == "absent":
        return "absent"
    return kind_of(ic.run(be.inspect(ic.identity_of(found))))


def cancelled(s, node, timeout=ic.WAIT * 2):
    s.wait(lambda: s.get(node)["state"] == "cancelled", timeout, "the cancellation")


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return False


def processes_with(tag: str) -> list[int]:
    found = []
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit() and int(entry.name) != os.getpid():
            try:
                if tag.encode() in (entry / "cmdline").read_bytes() and alive(int(entry.name)):
                    found.append(int(entry.name))
            except OSError:
                continue
    return found


# ============================================================= cancel

def test_ic_c16_cancel_stops_the_resource_through_the_recorded_backend_and_confirms_it(sx):
    sx.start()
    node = sx.node({"hold": 60})
    run = running(sx, node)
    assert sx.cancel_raw(node).get("ok") is True
    cancelled(sx, node)
    assert resource_state(sx, run) in ("exited", "absent")


def test_ic_c16_cancel_after_a_config_change_still_stops_through_the_recorded_backend(sx):
    sx.start()
    node = sx.node({"hold": 60})
    run = running(sx, node)
    sx.executor("local")
    assert sx.cancel_raw(node).get("ok") is True
    cancelled(sx, node)
    assert resource_state(sx, run) in ("exited", "absent")


def test_ic_c16_k6_an_unconfirmed_stop_keeps_the_hold_and_the_attempt_is_not_declared_cancelled(sx):
    sx.start()
    node = sx.node({"hold": 60})
    run = running(sx, node)
    sx.plan({"calls": True, "inspect": "unknown"})               # stop cannot be confirmed
    assert sx.cancel_raw(node).get("ok") is True
    assert sx.get(node)["state"] != "cancelled"
    assert len(sx.runs(node)) == 1
    sx.plan({"calls": True})
    cancelled(sx, node)
    assert resource_state(sx, run) in ("exited", "absent")


@pytest.mark.skipif(not dk.OPT_IN, reason="containment is claimed by real backends only (IC-C5): SG_TEST_DOCKER=1")
def test_ic_c16_cancel_leaves_no_process_of_the_attempt_even_a_setsid_child(tmp_path, monkeypatch, st_live_docker, request):
    dk.require_docker()
    owner = dk.test_owner(request)
    s = s3.S3Sched(tmp_path, monkeypatch, backend="docker", owner=owner)
    tag = "IC-TAG-" + uuid.uuid4().hex
    try:
        s.start()
        node = s.node({"setsid_child": 120, "tag": tag}, {"hold": 120})
        running(s, node)
        s.wait(lambda: processes_with(tag), 15, "the escaped child")
        assert s.cancel_raw(node).get("ok") is True
        cancelled(s, node)
        assert processes_with(tag) == [], "a descendant that left the session survived the confirmed stop"
    finally:
        s.close()
        dk.remove_session_resources(owner)


# ====================================================== forged pid files

def test_ic_c16_forged_pid_files_naming_a_live_decoy_never_make_stop_signal_it(sx):
    decoy = subprocess.Popen(["sleep", "120"], start_new_session=True)
    try:
        sx.start()
        node = sx.node({"forge": {"pid": decoy.pid}}, {"hold": 60})
        run = running(sx, node)
        assert sx.cancel_raw(node).get("ok") is True
        cancelled(sx, node)
        assert resource_state(sx, run) in ("exited", "absent")
        assert decoy.poll() is None, "stop signalled the process a forged pid file named"
        assert alive(decoy.pid)
    finally:
        decoy.kill()
        decoy.wait(5)


def test_ic_c16_forged_pid_files_naming_a_dead_pid_do_not_make_a_live_attempt_look_dead(sx):
    corpse = subprocess.Popen(["true"])
    corpse.wait()
    sx.start()
    node = sx.node({"forge": {"pid": corpse.pid}}, {"hold": 3.5}, {"lines": 3, "gap": 0.0})
    sx.wait_runs(node, 1)
    view = sx.get(node)
    assert view["state"] not in ("done", "failed", "cancelled", "held"), ic.text_of(view)[:300]
    assert len(sx.runs(node)) == 1
    assert s3.wait_done(sx, node, 15)["state"] == "done"
    assert len(sx.runs(node)) == 1


def test_ic_c16_forged_pid_files_naming_a_live_decoy_do_not_keep_a_finished_attempt_alive(sx):
    decoy = subprocess.Popen(["sleep", "120"], start_new_session=True)
    try:
        sx.start()
        node = sx.node({"forge": {"pid": decoy.pid}}, {"lines": 3, "gap": 0.0})
        assert s3.wait_done(sx, node, 12)["state"] == "done"
        assert decoy.poll() is None
    finally:
        decoy.kill()
        decoy.wait(5)


# ================================================== a config change

def test_ic_c16_a_config_change_with_a_running_attempt_lets_it_finish_and_the_next_attempt_uses_the_new_config(sx):
    sx.start()
    node = sx.node({"hold": 3}, {"lines": 3, "gap": 0.0})
    sx.wait_runs(node, 1)
    sx.executor("local")
    assert s3.wait_done(sx, node, 15)["state"] == "done"
    assert len(sx.runs(node)) == 1 and sx.runs(node)[0]["executor"]["kind"] == "isolated"
    nxt = sx.node({"lines": 2, "gap": 0.0})
    run = sx.wait_runs(nxt, 1)[0]
    assert run["executor"]["kind"] == "local"


def test_ic_c16_the_legacy_recreate_on_config_change_path_never_touches_an_isolated_container(fs):
    fs.fd.workload(runs=True)
    fs.start()
    node = fs.create()["id"]
    running(fs, node)
    (container,) = fs.fd.containers().values()
    fs.executor("isolated", isolated={"backend": "docker", "resources": s3.resources(memory="256m")})
    assert fs.fd.container(container["Id"])["State"]["Running"] is True
    assert fs.fd.calls_of("stop") == [] and fs.fd.calls_of("rm") == [] and fs.fd.calls_of("kill") == []
    assert fs.fd.start_count(container["Id"]) == 1 and len(fs.fd.containers()) == 1
    fs.fd.finish(container["Id"], 0, stdout=s3.events_stdout())
    assert s3.wait_done(fs, node, 12)["state"] == "done"
    nxt = fs.create()["id"]
    fs.fd.workload(stdout=s3.events_stdout())
    assert s3.wait_done(fs, nxt, 12)["state"] == "done"
    newest = [c for c in fs.fd.containers().values() if c["Id"] != container["Id"]][0]
    assert newest["HostConfig"]["Memory"] == 256 * 2 ** 20                # the next attempt got the new config
    assert fs.fd.container(container["Id"])["HostConfig"]["Memory"] != 256 * 2 ** 20


def test_ic_c16_cancel_on_the_fake_cli_stops_by_container_id_only(fs):
    fs.fd.workload(runs=True)
    fs.start()
    node = fs.create()["id"]
    running(fs, node)
    (container,) = fs.fd.containers().values()
    assert fs.cancel_raw(node).get("ok") is True
    cancelled(fs, node)
    assert fs.fd.container(container["Id"])["State"]["Running"] is False
    for cmd in fs.fd.calls_of("stop") + fs.fd.calls_of("kill"):
        assert container["Id"] in cmd and container["Name"].strip("/") not in cmd, cmd


# ========================================================= steer

def steer(s, run_id, message):
    reply = s.rpc("steer_run", {"run_id": run_id, "message": message})
    assert reply.get("ok") is True, reply
    command = reply["result"]["command_id"]
    return s.wait(lambda: (s.rpc("steer_result", {"run_id": run_id, "command_id": command})
                           .get("result") or {}).get("result"), 40, "the steer result")


def session_resume_values(view):
    return [o["session_resume"] for o in ic.objects(view) if "session_resume" in o]


def test_ic_c16_steer_stops_the_attempt_starts_a_new_one_and_records_that_the_session_was_not_resumed(sx):
    sx.start()
    node = sx.node({"lines": 1, "gap": 0.0}, {"hold": 60})          # a session id exists once it printed
    run = running(sx, node)
    answer = steer(sx, run["run_id"], "do it differently")
    assert not answer.get("error"), answer
    runs = sx.wait_runs(node, 2, 25)
    assert [r["executor"]["kind"] for r in runs] == ["isolated", "isolated"]
    assert resource_state(sx, run) in ("exited", "absent")               # the first attempt's resource was stopped
    values = session_resume_values(sx.get(node)) + session_resume_values(answer)
    assert values and set(values) == {"unsupported"}, (values, ic.text_of(answer)[:300])


# ========================================== D-S3.2 refusals by declaration

# K17.2 after slice 4A: `needs.rpc` alone is no longer refused (IC-C20 covers it running).
# D-S4B.1 lifts the `network` refusal: only `credentials` stays refused until 4C.
NEEDS = [("credentials", r"credential_handle")]


def refused(view):
    return [c for c in ic.causes(view) if c.get("class") == "other" and "isolation_unavailable" in ic.text_of(c)]


def arm_prepare_marker(s):
    """Public provider script: preparation has an observable filesystem effect."""
    marker = s.tmp / "provider-prepared"
    script = s.tmp / "provider-actions.py"
    script.write_text("#!" + sys.executable + "\nimport sys\nfrom pathlib import Path\n"
                      + "if 'prepare' in sys.argv[1:]: Path(" + repr(str(marker)) + ").write_text('prepared')\n")
    script.chmod(0o700)
    s.fake.entry["script"] = str(script)
    s.p.write()
    s._reset_server()
    return marker


def assert_refused(s, node, pattern):
    view = s.wait(lambda: (v := s.get(node)) and v["state"] in ("held", "failed", "done") and refused(v) and v,
                  ic.WAIT, "the refusal")
    assert not (s.tmp / "provider-prepared").exists(), "provider preparation preceded refusal"
    cause = refused(view)[0]
    text = ic.text_of(cause).lower()
    assert "unsupported" in text and re.search(pattern, text), text
    assert cause.get("retryable", False) is False
    assert len(s.runs(node)) == 1 and s.fake.spawns() == 0


@pytest.mark.parametrize("need,pattern", NEEDS)
def test_ic_c16_d_s3_2_a_provider_declaring_a_need_is_refused_unsupported_not_retried_reference(
        tmp_path, monkeypatch, need, pattern):
    s = s3.S3Sched(tmp_path, monkeypatch)
    try:
        s3.declare(s.fake.entry, **{need: True})
        s.p.write()
        s._reset_server()
        arm_prepare_marker(s)
        s.start()
        assert_refused(s, s.node({"lines": 1}), pattern)
    finally:
        s.close()


@pytest.mark.parametrize("need,pattern", NEEDS)
def test_ic_c16_d_s3_2_a_provider_declaring_a_need_is_refused_unsupported_not_retried_docker_cli(
        tmp_path, monkeypatch, need, pattern):
    s = s3.FakeDockerSched(tmp_path, monkeypatch)
    try:
        s.provider_declares(**{need: True})
        arm_prepare_marker(s)
        s.start()
        assert_refused(s, s.create()["id"], pattern)
        assert s.fd.containers() == {}
    finally:
        s.close()


def test_ic_c16_d_s4b_1_a_provider_needing_only_the_network_is_not_refused_for_it(tmp_path, monkeypatch):
    s = s3.FakeDockerSched(tmp_path, monkeypatch)
    try:
        s.provider_declares(network=True, credentials=False, rpc=False)
        arm_prepare_marker(s)
        s.start()
        node = s.create()["id"]
        view = s.wait(lambda: (v := s.get(node)) and (refused(v) or s.runs(node)) and v, ic.WAIT, "a launch or a refusal")
        assert not [c for c in refused(view) if "network" in ic.text_of(c).lower()], refused(view)
    finally:
        s.close()


def test_ic_c16_d_s3_2_a_provider_without_a_declaration_counts_as_needing_all_three(tmp_path, monkeypatch):
    s = s3.S3Sched(tmp_path, monkeypatch)
    try:
        s.fake.entry.pop(s3.NEEDS_KEY, None)
        s.p.write()
        s._reset_server()
        arm_prepare_marker(s)
        s.start()
        assert_refused(s, s.node({"lines": 1}), r"credential_handle")
    finally:
        s.close()


def test_ic_c16_d_s3_2_a_provider_declaring_none_runs(sx):
    sx.start()
    node = sx.node({"lines": 2, "gap": 0.0})
    assert s3.wait_done(sx, node, 12)["state"] == "done"


def test_ic_c16_d_s3_2_the_refusal_is_for_isolated_only_a_local_pinned_agent_still_runs(tmp_path, monkeypatch):
    s = s3.S3Sched(tmp_path, monkeypatch)
    try:
        s.fake.entry.pop(s3.NEEDS_KEY, None)               # undeclared: needs everything
        s.p.write()
        s.pin_agent("reviewer", "local")
        s.start()
        node = s.node({"lines": 1}, agent="reviewer")
        assert s.wait_runs(node, 1)[0]["executor"]["kind"] == "local"
    finally:
        s.close()


@pytest.mark.parametrize("declaration,field", [
    ({}, "credential_handle"),
    # D-S4B.1: `network` is no longer a refusal ({"credentials": False} alone is not refused).
    ({"credentials": True, "network": True, "rpc": True}, "credential_handle"),
])
def test_k17_2_omitted_members_default_true_and_refusals_follow_precedence(tmp_path, monkeypatch, declaration, field):
    s = s3.FakeDockerSched(tmp_path, monkeypatch)
    try:
        s.fake.entry["needs"] = declaration
        s.p.write()
        s._reset_server()
        arm_prepare_marker(s)
        s.start()
        assert_refused(s, s.create()["id"], field)
        assert s.fd.containers() == {} and s.fake.spawns() == 0
    finally:
        s.close()


def test_k17_2_provider_inherits_needs_and_overrides_one_member(tmp_path, monkeypatch):
    s = s3.FakeDockerSched(tmp_path, monkeypatch)
    try:
        s.p.providers["base"] = {"needs": {"credentials": False, "network": True, "rpc": False}}
        s.fake.entry["extends"] = "base"
        s.fake.entry["needs"] = {"network": False}
        s.p.write()
        s._reset_server()
        s.start()
        assert s3.wait_done(s, s.create()["id"])["state"] == "done"
    finally:
        s.close()
