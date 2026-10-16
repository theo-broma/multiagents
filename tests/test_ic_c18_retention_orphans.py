"""IC-C18 / K17.5–6: retained evidence and host-only orphan management."""
from pathlib import Path
import datetime
import json
import sys
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))
import ic_s3 as s3
import ic_support as ic
from ic_support import kind_of, identity_of


@pytest.fixture
def fs(tmp_path, monkeypatch):
    s = s3.FakeDockerSched(tmp_path, monkeypatch)
    s.start()
    yield s
    s.close()


def listing(s):
    reply = s.cli("scheduler", "orphans", timeout=ic.WAIT)
    assert reply.returncode == 0, reply.stderr
    value = json.loads(reply.stdout)
    assert set(value) == {"orphans", "orphans_error"}
    return value


def orphan(s, *, project=None, status="exited"):
    return s.fd.inject("unrecorded", {ic.PROJECT_LABEL: project or s.slug,
                                     "multiagents.owner": "att_unrecorded",
                                     "multiagents.lifetime": "attempt"}, status=status)


def remove(s, cid, error=None):
    reply = s.cli("scheduler", "remove-orphan", cid, timeout=ic.WAIT)
    value = json.loads(reply.stdout)
    if error:
        assert reply.returncode != 0
        assert value.get("ok") is False and value.get("error") == error and value.get("reason")
    else:
        assert reply.returncode == 0, reply.stderr
        assert value == {"ok": True, "container_id": cid, "state": "absent"}
    assert value["container_id"] == cid
    return value


def test_ic_c18_retention_keeps_exit_output_and_repository_until_discard(tmp_path, monkeypatch):
    w = s3.DockerWorld(tmp_path, monkeypatch)
    w.fd.workload(exit_code=7, stdout=b"retained\n", stderr=b"diagnostic\n")
    _, identity = w.identity()
    repository = ic.run(w.backend.checkout_path(identity))
    marker = repository / "result.txt"
    marker.write_bytes(b"private result")
    w.invoke(identity)
    ic.run(w.backend.remove(identity, True))
    state = ic.run(w.backend.inspect(identity))
    assert kind_of(state) == "exited" and ic.field(state, "code") == 7
    assert marker.read_bytes() == b"private result"
    assert any(identity_of(i) == identity for i in ic.run(w.backend.list_owned(s3.PROJECT)))
    assert w.fd.stdout_of(identity.id) == b"retained\n"
    ic.run(w.backend.remove(identity, False))
    assert not repository.exists()
    assert not any(identity_of(i) == identity for i in ic.run(w.backend.list_owned(s3.PROJECT)))


def test_k17_5_orphan_listing_matches_status_sorted_and_never_deletes(fs):
    ids = sorted([orphan(fs), orphan(fs)])
    value = listing(fs)
    status = fs.status()
    assert {k: status[k] for k in value} == value
    assert value["orphans_error"] is None
    assert [v["container_id"] for v in value["orphans"]] == ids
    for item in value["orphans"]:
        assert set(item) == {"container_id", "name", "labels", "created_at", "project", "attempt_id", "reason"}
        assert item["reason"] == "unrecorded_attempt" and item["project"] == fs.slug
        assert item["labels"][ic.PROJECT_LABEL] == fs.slug
        datetime.datetime.fromisoformat(item["created_at"].replace("Z", "+00:00"))
    assert sorted(fs.fd.containers()) == ids


def test_k17_5_other_projects_are_never_listed(fs):
    foreign = orphan(fs, project="somebody-else")
    assert listing(fs) == {"orphans": [], "orphans_error": None}
    assert fs.fd.container(foreign) is not None


def test_k17_5_daemon_failure_is_unknown_not_empty(fs):
    orphan(fs)
    fs.fd.daemon_down()
    value = listing(fs)
    assert value["orphans"] is None and value["orphans_error"].get("reason")


@pytest.mark.parametrize("state", ["exited", "running"])
def test_k17_6_explicit_removal_stops_live_orphan_and_is_repeatable(fs, state):
    cid = orphan(fs, status=state)
    remove(fs, cid)
    assert fs.fd.container(cid) is None
    remove(fs, cid)


@pytest.mark.parametrize("form", ["name", "prefix", "empty", "malformed"])
def test_k17_6_names_prefixes_and_malformed_ids_are_refused(fs, form):
    cid = orphan(fs)
    candidate = {"name": "unrecorded", "prefix": cid[:12], "empty": "", "malformed": "g" * 64}[form]
    remove(fs, candidate, "invalid_id")
    assert fs.fd.container(cid) is not None


def test_k17_6_wrong_project_is_refused(fs):
    cid = orphan(fs, project="somebody-else")
    remove(fs, cid, "wrong_project")
    assert fs.fd.container(cid) is not None


def test_k17_6_daemon_uncertainty_prevents_removal(fs):
    cid = orphan(fs)
    fs.fd.daemon_down()
    remove(fs, cid, "unknown")
    assert fs.fd.container(cid) is not None


def test_k17_5_retained_recorded_attempt_is_not_an_orphan_and_cannot_be_removed(fs):
    node = fs.create()["id"]
    assert s3.wait_done(fs, node)["state"] == "done"
    (cid,) = fs.fd.containers()
    assert listing(fs) == {"orphans": [], "orphans_error": None}
    remove(fs, cid, "not_orphan")
    assert fs.fd.container(cid) is not None


def test_ic_c18_dispose_removes_the_recorded_container(fs):
    node = fs.create()["id"]
    assert s3.wait_done(fs, node)["state"] == "done"
    (cid,) = fs.fd.containers()
    view = fs.get(node)
    reply = fs.rpc("dispose_node", {"id": node, "revision": view["revision"]}, timeout=ic.WAIT)
    assert reply.get("ok") is True, reply
    fs.wait(lambda: fs.fd.container(cid) is None, what="discarded resource to disappear")
    assert listing(fs) == {"orphans": [], "orphans_error": None}


def test_k17_6_agent_rpc_does_not_expose_orphan_removal(fs):
    cid = orphan(fs)
    reply = fs.rpc("remove_orphan", {"container_id": cid}, timeout=ic.WAIT)
    assert reply.get("ok") is False
    assert fs.fd.container(cid) is not None


def test_k17_5_scoped_scheduler_status_omits_host_orphan_fields(fs):
    node = fs.create()["id"]
    fs.wait_runs(node, 1)
    run = fs.runs(node)[0]
    token = fs.issue(run["run_id"], node)
    value = fs.ok("scheduler_status", {}, token=token)
    assert "orphans" not in value and "orphans_error" not in value


def test_k17_5_discarded_node_resource_is_reported_with_its_reason(fs):
    node = fs.create()["id"]
    assert s3.wait_done(fs, node)["state"] == "done"
    (original,) = fs.fd.containers().values()
    labels = original["Config"]["Labels"]
    reply = fs.rpc("dispose_node", {"id": node, "revision": fs.get(node)["revision"]}, timeout=ic.WAIT)
    assert reply.get("ok") is True, reply
    fs.wait(lambda: fs.fd.container(original["Id"]) is None, what="node disposal")
    cid = fs.fd.inject("discarded-leftover", labels)
    found = listing(fs)
    assert any(item["container_id"] == cid and item["reason"] == "discarded_node"
               for item in found["orphans"]), found


def test_k17_5_creation_before_recording_the_id_is_reconciled_not_orphaned(tmp_path, monkeypatch):
    import fake_docker as fdk
    assert callable(getattr(ic.lifecycle(), "DockerBackend", None)), "K17.1 DockerBackend is missing"
    s = s3.S3Sched(tmp_path, monkeypatch)
    fd = fdk.FakeDocker(tmp_path).install(monkeypatch)
    monkeypatch.setenv(ic.BACKEND_ENV, "ic_s3_backends:make_docker")
    try:
        s.start()
        pid = s.crash_at("provision_done")
        s.node({"lines": 1})
        s.wait_dead(pid, timeout=ic.WAIT)
        assert len(fd.containers()) == 1
        s.start()
        assert listing(s) == {"orphans": [], "orphans_error": None}
    finally:
        s.close()


# The same actual workload/capture is exercised on reference and opt-in Docker.
from test_ic_c16_stop_cancel_steer import sx  # noqa: E402,F401


def test_ic_c18_failed_capture_retains_the_container_and_private_repository(sx):
    sx.start()
    node = sx.node({"corrupt_ref": True}, {"lines": 1})
    view = s3.wait_done(sx, node)
    assert view["state"] == "held" and view["hold"]["reason"] == "result_capture_failed", view
    ev = sx.evidence(sx.runs(node)[-1]["attempt_id"])["executor"]
    backend = sx.be()
    identity = identity_of(ic.run(backend.find(ev["creation_key"])))
    assert kind_of(ic.run(backend.inspect(identity))) == "exited"
    repository = ic.run(backend.checkout_path(identity))
    assert repository.is_dir() and (repository / ".git").is_dir()
