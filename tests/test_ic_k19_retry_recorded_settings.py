"""K19 — a TF retry of an isolated attempt keeps its recorded settings
(`context/specs/phase7-part2-contract.md`, K19; builds on IC-C15 "launch
evidence is the only authority").

Through the scheduler's public entry points, on the real Docker backend over
the fake `docker` CLI (no real Docker). The first attempt ends in a retryable
TF class (a crash); while the retry waits out its backoff the project's
configuration changes; the retry must run on what the first attempt RECORDED.
Observed through what the backend is asked to create (image, memory) and
through `get_node` (`state`, `hold.reason`, `hold.detail`).

The contract does not fix the hold reason's spelling ("a reason naming the
refusal"). The code already uses `admission_refused`, and
`isolation_unavailable` when the refusal is the isolation prerequisite
itself; the tests accept those two and require the detail to say why.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import ic_s3 as s3  # noqa: E402
import ic_support as ic  # noqa: E402

RECORDED_IMAGE = "example/recorded:1"
NEW_IMAGE = "example/changed:2"
RECORDED_MEMORY, NEW_MEMORY = "512m", "256m"
BACKOFF = 4          # long enough to reconfigure between the crash and its retry
HOLD_REASONS = ("admission_refused", "isolation_unavailable")


@pytest.fixture
def ks(tmp_path, monkeypatch):
    s = s3.FakeDockerSched(tmp_path, monkeypatch,
                           scheduler={"retries": {"backoff_seconds": [BACKOFF] * 3}})
    s.fd.set_images([RECORDED_IMAGE, NEW_IMAGE])
    configure(s, RECORDED_IMAGE, RECORDED_MEMORY)
    yield s
    s.close()


def configure(s, image, memory):
    s.executor("isolated", isolated={
        "backend": "docker",
        "docker": {"cli": str(s.fd.docker), "image": image},
        "resources": s3.resources(memory=memory)})


def images(s):
    return [c["Config"]["Image"] for c in s.fd.containers().values()]


def memories(s):
    return [c["HostConfig"]["Memory"] for c in s.fd.containers().values()]


def mib(n):
    return n * 2 ** 20


def crash_then(s):
    """The next container exits nonzero with no provider error text (TF class
    crash); later ones succeed (the default workload)."""
    s.fd.workload(exit_code=1, queue=True)


def wait_first_attempt_ended(s, node):
    """The attempt crashed and is awaiting its retry (one container so far)."""
    s.wait(lambda: len(s.runs(node)) == 1 and s.tf_causes(node) and len(s.fd.containers()) == 1,
           what="the first attempt to end retryably")


def test_k19_a_retry_after_the_image_changed_runs_on_the_recorded_image(ks):
    crash_then(ks)
    ks.start()
    node = ks.create()["id"]
    wait_first_attempt_ended(ks, node)
    assert images(ks) == [RECORDED_IMAGE]
    configure(ks, NEW_IMAGE, NEW_MEMORY)
    view = s3.wait_done(ks, node, BACKOFF + ic.WAIT)
    assert view["state"] == "done", ic.text_of(view)[:600]
    assert len(ks.runs(node)) == 2
    assert images(ks) == [RECORDED_IMAGE, RECORDED_IMAGE], "the retry used the current image"
    assert NEW_IMAGE not in images(ks)


def test_k19_a_retry_keeps_the_other_recorded_executor_settings_too(ks):
    crash_then(ks)
    ks.start()
    node = ks.create()["id"]
    wait_first_attempt_ended(ks, node)
    configure(ks, NEW_IMAGE, NEW_MEMORY)
    assert s3.wait_done(ks, node, BACKOFF + ic.WAIT)["state"] == "done"
    assert sorted(memories(ks)) == [mib(512), mib(512)], memories(ks)


def test_k19_the_retry_records_the_settings_it_ran_on(ks):
    crash_then(ks)
    ks.start()
    node = ks.create()["id"]
    wait_first_attempt_ended(ks, node)
    configure(ks, NEW_IMAGE, NEW_MEMORY)
    assert s3.wait_done(ks, node, BACKOFF + ic.WAIT)["state"] == "done"
    first, second = ks.runs(node)
    assert second["executor"]["kind"] == first["executor"]["kind"] == "isolated"
    assert second["executor"]["backend"] == first["executor"]["backend"] == "docker"


def test_k19_a_new_node_after_the_change_uses_the_new_configuration(ks):
    """The record binds the retry of that attempt only; it is not a freeze of
    the project's configuration."""
    crash_then(ks)
    ks.start()
    node = ks.create()["id"]
    wait_first_attempt_ended(ks, node)
    configure(ks, NEW_IMAGE, NEW_MEMORY)
    assert s3.wait_done(ks, node, BACKOFF + ic.WAIT)["state"] == "done"
    other = ks.create()["id"]
    assert s3.wait_done(ks, other, BACKOFF + ic.WAIT)["state"] == "done"
    assert images(ks).count(NEW_IMAGE) == 1 and images(ks).count(RECORDED_IMAGE) == 2


# ------------------------------------------------------- refused admission

def assert_held_with_refusal(ks, node, expect_in_detail):
    view = ks.wait(lambda: (v := ks.get(node)) and v["state"] == "held" and v,
                   BACKOFF + ic.WAIT, "the refused retry to be held")
    hold = view["hold"]
    assert hold["reason"] in HOLD_REASONS, hold
    detail = ic.text_of(hold.get("detail")).lower()
    assert detail not in ("null", '""', "[]", "{}"), "a hold without a detail"
    assert expect_in_detail in detail, hold
    return view


def test_k19_a_retry_whose_recorded_image_is_no_longer_admissible_is_held_with_its_reason(ks):
    crash_then(ks)
    ks.start()
    node = ks.create()["id"]
    wait_first_attempt_ended(ks, node)
    configure(ks, NEW_IMAGE, NEW_MEMORY)
    ks.fd.set_images([NEW_IMAGE])           # the recorded image is gone; the new one exists
    view = assert_held_with_refusal(ks, node, "image")
    assert len(ks.runs(node)) == 1
    # It did not fall back to the current configuration.
    time.sleep(ic.QUIET)
    assert images(ks) == [RECORDED_IMAGE]
    assert ks.get(node)["state"] == "held"
    assert view["hold"]["reason"] in HOLD_REASONS


def test_k19_a_refused_retry_never_stays_open_or_ready(ks):
    crash_then(ks)
    ks.start()
    node = ks.create()["id"]
    wait_first_attempt_ended(ks, node)
    ks.fd.set_images([])                    # nothing is admissible
    seen = set()
    end = time.monotonic() + BACKOFF + ic.WAIT
    while time.monotonic() < end:
        state = ks.get(node)["state"]
        seen.add(state)
        if state == "held":
            break
        time.sleep(0.1)
    assert ks.get(node)["state"] == "held", f"states seen: {sorted(seen)}"


def test_k19_a_held_refused_retry_stays_held_across_a_scheduler_restart(ks):
    crash_then(ks)
    ks.start()
    node = ks.create()["id"]
    wait_first_attempt_ended(ks, node)
    ks.fd.set_images([])
    before = ks.wait(lambda: (v := ks.get(node)) and v["state"] == "held" and v,
                     BACKOFF + ic.WAIT, "the hold")
    ks.restart()
    time.sleep(ic.QUIET)
    after = ks.get(node)
    assert after["state"] == "held" and after["hold"]["reason"] == before["hold"]["reason"]
    assert len(ks.fd.containers()) == 1 and len(ks.runs(node)) == 1
