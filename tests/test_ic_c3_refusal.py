"""IC-C3 — an isolated launch that cannot happen is refused, never downgraded.

The reference backend is injected through a double (`ic_backends.Double`)
that fails on demand at probe, provision or start. "No shared-mode process
was started" is observed on the fake provider's call log (`fake.spawns()`):
with the backend failing, nothing may have spawned the provider at all.

TF causes are found the way the TF tests find them: class-bearing mappings
in the public JSON views (`get_node`), with the detail code searched in the
same cause mapping.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import ic_support as ic  # noqa: E402


@pytest.fixture
def ics(tmp_path, monkeypatch):
    s = ic.IcSched(tmp_path, monkeypatch)
    yield s
    s.close()


def isolation_causes(view):
    return [c for c in ic.causes(view) if "isolation_unavailable" in ic.text_of(c)]


# ----------------------------------------------------------- probe failure

def test_ic_c3_probe_failure_blocks_the_node_before_any_attempt(ics):
    ics.plan({"probe": ["daemon down"]})
    ics.start()
    node = ics.create()["id"]
    view = ics.wait(lambda: (v := ics.get(node)) and ic.text_of(v.get("blocked")).count(
        "isolation_unavailable") and v, what="the isolation_unavailable block")
    assert "isolation_unavailable: daemon down" in ic.text_of(view["blocked"]) or (
        "isolation_unavailable" in ic.text_of(view["blocked"])
        and "daemon down" in ic.text_of(view["blocked"]))
    assert view["state"] == "open"
    assert view["runs"] == [] and not view.get("active_run")
    time.sleep(ic.QUIET)
    assert ics.fake.spawns() == 0
    assert ics.runs(node) == []


def test_ic_c3_every_problem_the_probe_lists_is_reported(ics):
    ics.plan({"probe": ["no daemon", "image missing"]})
    ics.start()
    node = ics.create()["id"]
    ics.wait(lambda: "isolation_unavailable" in ic.text_of(ics.blocked(node)),
             what="the block")
    text = ic.text_of(ics.blocked(node))
    assert "no daemon" in text and "image missing" in text


def test_ic_c3_the_node_launches_isolated_once_the_probe_passes(ics):
    ics.plan({"probe": ["daemon down"]})
    ics.start()
    node = ics.create()["id"]
    ics.wait(lambda: "isolation_unavailable" in ic.text_of(ics.blocked(node)), what="the block")
    ics.plan({})
    run = ics.wait_runs(node, 1)[0]
    assert run["executor"]["kind"] == "isolated"
    assert "isolation_unavailable" not in ic.text_of(ics.blocked(node))


def test_ic_c3_a_probe_failure_does_not_block_a_local_pinned_agent(ics):
    ics.plan({"probe": ["daemon down"]})
    ics.pin_agent("reviewer", "local")
    ics.start()
    node = ics.create(agent="reviewer")["id"]
    assert ics.wait_runs(node, 1)[0]["executor"]["kind"] == "local"


# -------------------------------------------------- unsupported configuration

@pytest.mark.parametrize("mode", ["unsupported:resources", "invalid_spec"])
def test_ic_c3_an_unsupported_spec_fails_the_attempt_not_retryable(ics, mode):
    ics.plan({"provision": mode})
    ics.start()
    node = ics.create()["id"]
    view = ics.wait(lambda: (v := ics.get(node)) and v["state"] in ("held", "failed", "done")
                    and isolation_causes(v) and v, what="the refused attempt")
    cause = isolation_causes(view)[0]
    assert cause["class"] == "other"
    assert cause.get("retryable", False) is False
    reason = mode.split(":")[-1] if ":" in mode else "invalid_spec"
    assert reason in ic.text_of(view)
    time.sleep(ic.QUIET)
    assert len(ics.runs(node)) == 1                     # no retry, ever
    assert ics.runs(node)[0]["executor"]["kind"] == "isolated"
    assert ics.fake.spawns() == 0
    assert "retry_pending" not in view and "retries" not in ic.text_of(ics.get(node).get("hold"))


# ------------------------------------------------------ operational failure

@pytest.mark.parametrize("step", ["provision", "start"])
def test_ic_c3_an_operational_failure_is_class_launch_and_retried_isolated(ics, step):
    ics.plan({step: "raise", "times": 1})
    ics.start()
    node = ics.create()["id"]
    runs = ics.wait_runs(node, 2)                       # the retry
    assert [r["executor"]["kind"] for r in runs] == ["isolated", "isolated"]
    first_causes = [c for c in ic.causes(ics.get(node)) if c["class"] == "launch"]
    assert first_causes, ic.text_of(ics.get(node))
    assert any("isolation_unavailable" in ic.text_of(c) for c in first_causes)


@pytest.mark.parametrize("step,budget", [("start", 0), ("provision", 1), ("start", 3), ("provision", 3)])
def test_ic_c3_k10_a_persistent_operational_failure_stops_after_budget_plus_one_never_shared(
        tmp_path, monkeypatch, step, budget):
    s = ic.IcSched(tmp_path, monkeypatch, scheduler={"retries": {"budget": budget,
                                                                  "backoff_seconds": [0.1]}})
    try:
        from multiagents.failures import retry_policy
        effective, _ = retry_policy(s.p.project["scheduler"])      # the value TF itself reads
        assert effective == budget
        s.plan({step: "raise"})
        s.start()
        node = s.create()["id"]
        view = s.wait(lambda: (v := s.get(node)) and v["state"] == "held" and v, timeout=6,
                      what="retries_exhausted")
        assert "retries_exhausted" in ic.text_of(view.get("hold"))
        time.sleep(ic.QUIET)
        runs = s.runs(node)
        assert len(runs) == effective + 1
        assert {r["executor"]["kind"] for r in runs} == {"isolated"}
        assert s.fake.spawns() == 0
    finally:
        s.close()


def test_ic_c3_no_downgrade_when_the_agent_is_pinned_isolated_over_a_local_project(ics):
    ics.executor("local")
    ics.pin_agent("worker", "isolated")
    ics.plan({"probe": ["daemon down"]})
    ics.start()
    node = ics.create()["id"]
    ics.wait(lambda: "isolation_unavailable" in ic.text_of(ics.blocked(node)), what="the block")
    time.sleep(ic.QUIET)
    assert ics.fake.spawns() == 0 and ics.runs(node) == []


# ------------------------------------------------------------ K3: the injection seam

SECRET_NAMES = list(ic.INJECTED)


@pytest.mark.parametrize("kind", ["isolated", "local"])
def test_ic_c3_k3_the_injection_variables_never_reach_a_runs_environment(tmp_path, monkeypatch, kind):
    s = ic.IcSched(tmp_path, monkeypatch, kind=kind)
    try:
        log = s.record_env()
        s.start()
        s.wait_runs(s.create()["id"], 1)
        s.wait(lambda: s.spawn_envs(log), what="the provider to be spawned")
        for names in s.spawn_envs(log):
            assert "PATH" in names or "HOME" in names           # the dump is real
            assert not set(names) & set(SECRET_NAMES), names
    finally:
        s.close()


def test_ic_c3_k3_passthrough_cannot_bring_them_back(tmp_path, monkeypatch):
    s = ic.IcSched(tmp_path, monkeypatch)
    try:
        s.p.project["security"] = {"env_passthrough": SECRET_NAMES}
        s.p.write()
        s._reset_server()
        log = s.record_env()
        s.start()
        s.wait_runs(s.create()["id"], 1)
        s.wait(lambda: s.spawn_envs(log), what="the provider to be spawned")
        for names in s.spawn_envs(log):
            assert not set(names) & set(SECRET_NAMES), names
    finally:
        s.close()


@pytest.mark.parametrize("target", ["no_such_module_ic:make", "ic_backends:no_such_factory",
                                    "ic_backends:boom", "not-a-spec"])
def test_ic_c3_k3_a_failing_import_or_factory_refuses_isolated_execution(ics, monkeypatch, target):
    monkeypatch.setenv(ic.BACKEND_ENV, target)
    ics.start()
    node = ics.create()["id"]
    ics.wait(lambda: "isolation_unavailable" in ic.text_of(ics.get(node)), what="the refusal")
    time.sleep(ic.QUIET)
    assert ics.fake.spawns() == 0                      # no shared-mode fallback, ever
    assert all(r["executor"]["kind"] == "isolated" for r in ics.runs(node))


def test_ic_c3_k3_the_variable_is_ignored_for_other_kinds(tmp_path, monkeypatch):
    s = ic.IcSched(tmp_path, monkeypatch, kind="local")
    try:
        monkeypatch.setenv(ic.BACKEND_ENV, "ic_backends:boom")
        s.start()
        run = s.wait_runs(s.create()["id"], 1)[0]
        s.wait(lambda: s.fake.spawns() >= 1, what="the provider to be spawned")
        assert run["executor"]["kind"] == "local" and s.fake.spawns() >= 1
    finally:
        s.close()


# --------------------------------------------- K6: reservations and termination

def slots(tmp_path, monkeypatch):
    s = ic.IcSched(tmp_path, monkeypatch)
    s.p.project["limits"] = {"max_depth": 7, "max_concurrent": 1}
    s.p.write()
    s._reset_server()
    return s


def test_ic_c3_k6_the_compute_slot_is_released_once_the_resource_has_exited(tmp_path, monkeypatch):
    """A retained resource stays `exited` by design: waiting for `absent` would hold the slot forever."""
    s = slots(tmp_path, monkeypatch)
    try:
        s.start()
        first = s.create()["id"]
        s.wait(lambda: s.get(first)["state"] == "done", what="the first node to finish")
        second = s.create()["id"]
        s.wait_runs(second, 1)                          # admitted: the slot was released
        assert "max_concurrent" not in ic.text_of(s.blocked(second))
    finally:
        s.close()


def test_ic_c3_k6_an_unknown_state_keeps_the_reservation_and_the_hold(tmp_path, monkeypatch):
    s = slots(tmp_path, monkeypatch)
    try:
        s.plan({"inspect": "unknown"})
        s.start()
        first = s.create()["id"]
        s.wait_runs(first, 1)
        second = s.create()["id"]
        s.wait(lambda: "max_concurrent" in ic.text_of(s.blocked(second)), what="the second node blocked")
        time.sleep(ic.QUIET)
        assert s.runs(second) == []                     # the slot is still held
        assert s.get(first)["state"] not in ("done", "failed", "cancelled")    # liveness hold: never guessed dead
        s.plan({})                                       # the backend can tell again: both settle
        s.wait_runs(second, 1, timeout=ic.WAIT * 2)
    finally:
        s.close()


# ----------------------------------------- K7: an exception after success never means "nothing exists"

def test_ic_c3_k7_a_provision_that_raised_after_creating_is_resolved_before_the_retry(ics):
    ics.plan({"provision": "raise_after", "times": 1})
    ics.start()
    node = ics.create()["id"]
    runs = ics.wait_runs(node, 2)
    ex = ics.evidence(runs[0]["attempt_id"])["executor"]
    assert ex.get("creation_key")
    assert ex.get("identity"), "the caller did not resolve the creation key (find) before retrying"
    ics.wait(lambda: ics.get(node)["state"] in ("done", "held", "failed"), what="the node to settle")
    # no resource is left without an owner record: never more resources than attempts
    assert len(ics.owned()) <= len(ics.runs(node))


def test_ic_c3_k7_a_run_that_raised_after_starting_is_resolved_before_the_retry(ics):
    ics.plan({"start": "raise_after", "times": 1})
    ics.start()
    node = ics.create()["id"]
    runs = ics.wait_runs(node, 2)
    ex = ics.evidence(runs[0]["attempt_id"])["executor"]
    assert ex.get("invocation_key")
    assert ex.get("invocation_id"), "the caller did not resolve the invocation key before retrying"
    ics.wait(lambda: ics.get(node)["state"] in ("done", "held", "failed"), what="the node to settle")
    assert ics.fake.spawns() <= len(ics.runs(node))     # at most one invocation per attempt
    assert len(ics.owned()) <= len(ics.runs(node))
