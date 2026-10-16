"""Adversary pass, slice 3: guards for lines that survived mutation.

Each test below passes on the approved branch and fails under the mutation
named in its docstring (made and reverted during the adversary pass in
`src/multiagents/executor/docker_lifecycle.py` and
`src/multiagents/scheduler/orphans.py`). Real Docker backend over the fake CLI.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import ic_s3 as s3  # noqa: E402
import ic_support as ic  # noqa: E402
from ic_support import kind_of  # noqa: E402


@pytest.fixture
def w(tmp_path, monkeypatch):
    return s3.DockerWorld(tmp_path, monkeypatch)


def test_guard_output_of_another_invocation_id_is_absent_never_this_containers_logs(w):
    """Mutation: `output` drops the recorded-invocation check (IC-C4: output
    is per invocation; a wrong id is not this invocation's bytes)."""
    w.fd.workload(stdout=b"secret-of-inv-1\n")
    _, identity = w.identity()
    w.invoke(identity, key="inv-1")
    got = ic.run(w.backend.output(identity, "inv-other", "stdout", 0))
    assert kind_of(got) == "absent", got
    assert ic.data_of(ic.run(w.backend.output(identity, "inv-1", "stdout", 0))) == b"secret-of-inv-1\n"


def test_guard_an_identity_that_is_another_containers_name_is_never_that_container(w):
    """Mutation: `metadata` drops the `Id == identity.id` check. Docker's
    inspect also resolves names and id prefixes (D-S3.4: every inspect names
    the recorded immutable id; a name is never an identity)."""
    _, identity = w.identity()
    w.invoke(identity)
    impostor_name = "f" * 64
    w.fd.inject(impostor_name, {ic.PROJECT_LABEL: s3.PROJECT}, status="running")
    got = ic.run(w.backend.inspect(ic.lifecycle().ResourceIdentity(impostor_name)))
    assert kind_of(got) in ("absent", "unknown"), got


def test_guard_two_containers_with_one_creation_key_is_unknown_never_one_of_them(w):
    """Mutation: `find` returns the first of several matches (K5: the key names
    one resource; ambiguity is uncertainty, not an answer)."""
    _, identity = w.identity("key-dup")
    w.fd.inject("dup", {ic.PROJECT_LABEL: s3.PROJECT, "multiagents.creation_key": "key-dup"})
    assert kind_of(ic.run(w.backend.find("key-dup"))) == "unknown"


def test_guard_stop_whose_graceful_command_failed_still_forces_termination_and_confirms(w):
    """Mutation: `stop` loses its forced-kill fallback (IC-C14: forced
    termination after `grace`, confirmed by inspect)."""
    w.fd.workload(runs=True)
    _, identity = w.identity()
    w.invoke(identity)
    w.fd.rule("stop", effect="fail", stderr="Error response from daemon: timeout", times=1)
    got = ic.run(w.backend.stop(identity, 0.1), timeout=20)
    assert kind_of(got) == "exited", got
    assert w.fd.calls_of("kill")


def test_guard_remove_orphan_stops_a_live_orphan_with_the_cancellation_grace(tmp_path, monkeypatch):
    """Mutation: `remove-orphan` skips its own stop and lets `remove` stop with
    its short internal grace (K17.6: stopped with the cancellation grace)."""
    s = s3.FakeDockerSched(tmp_path, monkeypatch)
    try:
        s.start()
        cid = s.fd.inject("live-orphan", {ic.PROJECT_LABEL: s.slug, "multiagents.owner": "att_x",
                                          "multiagents.lifetime": "attempt"}, status="running")
        reply = s.cli("scheduler", "remove-orphan", cid, timeout=ic.WAIT)
        assert json.loads(reply.stdout) == {"ok": True, "container_id": cid, "state": "absent"}
        graces = [int(c[c.index("--time") + 1]) for c in s.fd.calls_of("stop") if "--time" in c]
        assert graces and max(graces) >= 3, s.fd.calls_of("stop")
    finally:
        s.close()
