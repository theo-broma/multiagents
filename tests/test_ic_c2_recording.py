"""IC-C2 — each attempt records its backend, frozen before launch.

C2a (slice 1): recording, projection and freezing, through `get_node` and the
attempt's `launch_evidence.executor` (read via `IcSched.evidence`).
C2b (slice 3, `ic_milestone(3)`): adoption, liveness, stop and cleanup act on
the recorded backend, never the current config; a record with no `executor`
evidence is unknown and held; a changed backend version holds adoption unless
the backend declares the recorded version compatible.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import ic_support as ic  # noqa: E402

HEX64 = re.compile(r"^[0-9a-f]{64}$")


@pytest.fixture
def ics(tmp_path, monkeypatch):
    s = ic.IcSched(tmp_path, monkeypatch)
    yield s
    s.close()


def first_run(s, node_id):
    return s.wait_runs(node_id, 1)[0]


# ------------------------------------------------------------------ C2a

def test_ic_c2a_projection_of_an_isolated_attempt(ics):
    ics.start()
    run = first_run(ics, ics.create()["id"])
    ex = run["executor"]
    assert set(ex) >= {"kind", "backend", "backend_version"}
    assert ex["kind"] == "isolated"
    assert ex["backend"] == "reference"              # K4: the injected backend, recorded truthfully
    assert isinstance(ex["backend_version"], str) and ex["backend_version"]


def test_ic_c2a_backend_version_is_what_the_backend_reports(ics):
    ics.plan({"version": "ref-7.3"})
    ics.start()
    assert first_run(ics, ics.create()["id"])["executor"]["backend_version"] == "ref-7.3"


def test_ic_c2a_the_local_kind_records_an_empty_backend(tmp_path, monkeypatch):
    # (docker needs a daemon, so only the local kind is launched here)
    s = ic.IcSched(tmp_path, monkeypatch, kind="local")
    try:
        s.start()
        ex = first_run(s, s.create()["id"])["executor"]
        assert ex["kind"] == "local" and ex["backend"] == ""
    finally:
        s.close()


def test_ic_c2a_launch_evidence_holds_kind_backend_version_schema_and_digest(ics):
    ics.start()
    run = first_run(ics, ics.create()["id"])
    evidence = ics.evidence(run["attempt_id"])["executor"]
    assert evidence["kind"] == "isolated"
    assert evidence["backend"] == "reference"
    assert evidence["backend_version"] == run["executor"]["backend_version"]
    assert evidence["spec_version"] == 1
    assert HEX64.match(evidence["spec_digest"])


def test_ic_c2a_distinct_attempts_have_distinct_digests(ics):
    ics.start()
    a = first_run(ics, ics.create()["id"])
    b = first_run(ics, ics.create(agent="reviewer")["id"])
    assert ics.evidence(a["attempt_id"])["executor"]["spec_digest"] != \
        ics.evidence(b["attempt_id"])["executor"]["spec_digest"]


def test_ic_c2a_k4_the_recorded_version_is_the_reference_backends_own(ics):
    ics.start()
    run = first_run(ics, ics.create()["id"])
    own = ic.importlib.import_module("ic_backends").make_reference(ics.backend_root).version
    assert run["executor"]["backend"] == "reference" and run["executor"]["backend_version"] == own


def test_ic_c2a_k5_keys_are_written_before_use_and_ids_after(ics):
    ics.start()
    run = first_run(ics, ics.create()["id"])
    ex = ics.wait(lambda: (e := ics.evidence(run["attempt_id"]).get("executor") or {})
                  .get("invocation_id") and e, what="invocation_id in the evidence")
    for name in ("creation_key", "invocation_key", "identity", "invocation_id"):
        assert isinstance(ex.get(name), str) and ex[name], (name, ex)
    assert ex["invocation_id"] == ex["invocation_key"]          # K1: the id is the key
    assert ex["creation_key"] != ex["invocation_key"]


def test_ic_c2a_k5_later_evidence_writes_do_not_drop_the_executor_fields(ics):
    ics.start()
    node = ics.create()["id"]
    run = first_run(ics, node)
    ics.wait(lambda: ics.get(node)["state"] in ("done", "held", "failed"), what="the node to finish")
    ex = ics.evidence(run["attempt_id"])["executor"]
    for name in ("kind", "backend", "backend_version", "spec_version", "spec_digest",
                 "creation_key", "invocation_key", "identity", "invocation_id"):
        assert ex.get(name), (name, ex)


def test_ic_c2a_k5_a_provision_failure_leaves_the_creation_key_and_no_identity(ics):
    ics.plan({"provision": "raise", "times": 1})
    ics.start()
    # The failed provision is complete once the retry's attempt exists.
    run = ics.wait_runs(ics.create()["id"], 2)[0]
    ex = ics.evidence(run["attempt_id"])["executor"]
    assert ex.get("creation_key")
    assert not ex.get("identity") and not ex.get("invocation_id")


def test_ic_c2a_k5_a_start_failure_leaves_the_invocation_key_but_no_invocation_id(ics):
    ics.plan({"start": "raise", "times": 1})
    ics.start()
    node = ics.create()["id"]
    # The failed start is complete once the retry's attempt exists; reading
    # earlier can catch the evidence mid-provisioning.
    run = ics.wait_runs(node, 2)[0]
    ex = ics.evidence(run["attempt_id"])["executor"]
    assert ex.get("creation_key") and ex.get("identity") and ex.get("invocation_key")
    assert not ex.get("invocation_id")


@pytest.mark.parametrize("fail", [{"provision": "raise"}, {"start": "raise"}])
def test_ic_c2a_the_record_is_frozen_before_anything_is_created(ics, fail):
    """The launch fails operationally, yet the evidence is already there."""
    ics.plan({**fail, "times": 1})
    ics.start()
    run = first_run(ics, ics.create()["id"])
    ex = ics.evidence(run["attempt_id"]).get("executor") or {}
    assert ex.get("kind") == "isolated" and ex.get("backend") == "reference"
    assert ex.get("spec_version") == 1 and HEX64.match(ex.get("spec_digest", ""))


def test_ic_c2a_an_automatic_retry_keeps_the_recorded_selection(ics):
    ics.plan({"start": "raise"})                     # every start fails: TF retries
    ics.start()
    node = ics.create()["id"]
    ics.wait_runs(node, 2)
    ics.executor("local")                            # a config change in between
    runs = ics.wait_runs(node, 3)
    assert [r["executor"]["kind"] for r in runs[:3]] == ["isolated"] * 3
    assert ics.fake.spawns() == 0                    # and nothing ran in the shared mode


def test_ic_c2a_an_explicit_relaunch_after_a_config_change_records_the_new_selection(ics):
    ics.plan({"provision": "unsupported:resources"})  # not retryable: the node stops, held
    ics.start()
    node = ics.create()["id"]
    ics.wait(lambda: ics.get(node)["state"] in ("held", "failed"), what="the node to settle")
    ics.executor("local")
    view = ics.get(node)
    reply = ics.rpc("relaunch_node", {"id": node, "revision": view["revision"]})
    assert reply.get("ok") is True, reply
    runs = ics.wait_runs(node, 2)
    assert runs[0]["executor"]["kind"] == "isolated"      # the old attempt keeps its record
    assert runs[-1]["executor"]["kind"] == "local"        # the new one records the new selection


# ------------------------------------------------------------------ C2b (slice 3)

def long_agent(s):
    s.fake.script(steps=[{"cost": 0, "sleep": 30}])


@pytest.mark.ic_milestone(3)
def test_ic_c2b_cancel_after_a_config_change_stops_through_the_recorded_backend(ics):
    long_agent(ics)
    ics.start()
    node = ics.create()["id"]
    run = first_run(ics, node)
    assert run["executor"]["kind"] == "isolated"
    ics.wait(lambda: ics.fake.spawns() >= 1, what="the agent to start in the backend")
    ics.executor("local")
    assert ics.cancel_raw(node).get("ok") is True
    ics.wait(lambda: ics.get(node)["state"] == "cancelled", what="the cancellation")
    # the isolated run's process is gone: stopped by the recorded backend
    ics.wait(lambda: not agent_processes(ics), ic.SHORT, "the agent process to be gone")


def agent_processes(s):
    """Live processes running this world's fake provider script."""
    script = s.fake.calls.name.replace(".calls", "-cli.py")
    found = []
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit():
            try:
                cmd = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode()
                state = (entry / "stat").read_text().rsplit(")", 1)[1].split()[0]
            except OSError:
                continue
            if script in cmd and str(s.root.parent) in cmd and state != "Z":
                found.append(int(entry.name))
    return found


@pytest.mark.ic_milestone(3)
def test_ic_c2b_adoption_after_a_scheduler_restart_follows_the_recorded_backend(ics):
    long_agent(ics)
    ics.start()
    node = ics.create()["id"]
    first_run(ics, node)
    ics.wait(lambda: ics.fake.spawns() >= 1, what="the agent to start")
    ics.executor("local")
    ics.restart(hard=True)
    ics.wait(lambda: ics.sock.exists(), what="the restarted scheduler")
    ic.time.sleep(ic.QUIET)
    runs = ics.runs(node)
    assert len(runs) == 1 and runs[0]["executor"]["kind"] == "isolated"
    assert ics.get(node)["state"] not in ("failed", "cancelled")
    assert ics.fake.spawns() == 1                      # adopted, not relaunched


@pytest.mark.ic_milestone(3)
def test_ic_c2b_a_possibly_launched_record_without_executor_evidence_is_held(ics):
    long_agent(ics)
    ics.start()
    node = ics.create()["id"]
    run = first_run(ics, node)
    ics.wait(lambda: ics.fake.spawns() >= 1, what="the agent to start")
    ics.kill9()
    from multiagents.scheduler.engine import attempts, save_attempt
    from multiagents.scheduler.store import Store
    store = Store(ics.root)
    with store.transaction() as db:
        attempt = attempts(db)[run["attempt_id"]]
        attempt["launch_evidence"].pop("executor", None)
        save_attempt(db, attempt)
    ics.start()
    ic.time.sleep(ic.QUIET)
    # never guessed to be `local`: no second attempt, no kill, the liveness hold
    assert len(ics.runs(node)) == 1
    assert ics.fake.spawns() == 1
    view = ics.get(node)
    assert view["state"] not in ("done", "failed", "cancelled")
    assert ic.text_of(view).count("unknown") >= 1 or view.get("hold") or view.get("blocked")


@pytest.mark.ic_milestone(3)
def test_ic_c2b_a_changed_backend_version_holds_adoption_unless_declared_compatible(ics):
    ics.plan({"version": "ref-1"})
    long_agent(ics)
    ics.start()
    node = ics.create()["id"]
    first_run(ics, node)
    ics.wait(lambda: ics.fake.spawns() >= 1, what="the agent to start")
    ics.plan({"version": "ref-2"})                     # the double declares no compatibility
    ics.restart(hard=True)
    ic.time.sleep(ic.QUIET)
    assert len(ics.runs(node)) == 1 and ics.fake.spawns() == 1
    view = ics.get(node)
    assert view["state"] not in ("done", "failed", "cancelled")
    assert "unknown" in ic.text_of(view).lower() or view.get("hold") or view.get("blocked")
