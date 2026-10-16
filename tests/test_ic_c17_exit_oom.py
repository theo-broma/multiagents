"""IC-C17: per-container OOM evidence, parsed from Docker and exposed by TF."""
from pathlib import Path
import sys
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))
import ic_s3 as s3
import ic_support as ic
import ic_docker as dk
from ic_support import kind_of

DOCKER_WAIT = 60       # scheduler launch + container start + OOM kill on a real daemon


@pytest.mark.parametrize("code,oom", [(0, False), (7, False), (137, False), (137, True)])
def test_ic_c17_inspect_keeps_exit_code_and_oom_separate(tmp_path, monkeypatch, code, oom):
    w = s3.DockerWorld(tmp_path, monkeypatch)
    w.fd.workload(exit_code=code, oom=oom)
    _, identity = w.identity()
    w.invoke(identity)
    state = ic.run(w.backend.inspect(identity))
    assert kind_of(state) == "exited"
    assert ic.field(state, "code") == code and ic.field(state, "oom") is oom


def test_ic_c17_oom_cause_is_attributed_to_the_attempt(tmp_path, monkeypatch):
    # Retries after an OOM crash are allowed, so the node need not end: the
    # cause is read from the OOM attempt's own record as soon as it exists.
    s = s3.FakeDockerSched(tmp_path, monkeypatch)
    try:
        s.fd.workload(exit_code=137, oom=True)
        s.start()
        node = s.create()["id"]

        def oom_attempt():
            return next((r for r in s.runs(node)
                         if isinstance(r.get("cause"), dict) and r["cause"].get("class") == "crash"), None)

        run = s.wait(oom_attempt, ic.WAIT, f"the crash cause of an attempt of {node}")
        assert run.get("attempt_id"), run
        assert run["cause"].get("oom") is True, run
        assert "oom" in ic.text_of(run["cause"]).lower(), run
    finally:
        s.close()


@pytest.mark.skipif(not dk.OPT_IN, reason="real Docker is opt-in: SG_TEST_DOCKER=1")
def test_ic_c17_real_docker_hard_memory_limit_reports_oom(tmp_path, monkeypatch, request, st_live_docker):
    dk.require_docker()
    s = s3.S3Sched(tmp_path, monkeypatch, backend="docker", owner=dk.test_owner(request))
    try:
        s.executor("isolated", isolated={"backend": "docker", "resources": {"memory": "64m"}})
        s.start()
        node = s.node({"alloc_mb": 256, "hold": 0})
        view = s3.wait_done(s, node, DOCKER_WAIT)
        causes = ic.causes(view)
        assert any(c.get("class") == "crash" and "oom" in ic.text_of(c).lower() for c in causes), causes
    finally:
        s.close()
        dk.remove_session_resources(dk.test_owner(request))
