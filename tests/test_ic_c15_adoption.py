"""IC-C15 — Runner on isolated attempts: adoption and output without loss or
duplication, the replay rule, the five launch boundaries (IC-R7, C2b).

Everything runs through the scheduler's public entry points on the REFERENCE
backend injected per K3 (`ic_s3_backends`, which adds injected crashes), and
again, parametrised, on the real Docker backend under SG_TEST_DOCKER=1 (a
missing prerequisite is then a failure; not runnable in the authoring sandbox).

The workload is `ic_workload.WorkloadProvider`: deterministic output, so "byte
identical to an uninterrupted run" is checked against a control run in the
same test. The host run output is read from the run directory (G-S3.4).
How the checkpoint is stored is the developer's choice (the contract says so),
so the crash between an append and its checkpoint is made through the
public artifact: after a hard kill the host output is given the bytes an
append that outran its checkpoint would have left; recovery must discard them.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import ic_docker as dk  # noqa: E402
import ic_s3 as s3  # noqa: E402
import ic_support as ic  # noqa: E402
import ic_workload as wl  # noqa: E402
from ic_support import kind_of  # noqa: E402

LINES, GAP, ERRS = 70, 0.03, 12
STREAM_OPS = [{"lines": LINES, "gap": GAP}, {"err": ERRS, "gap": 0.02}]
QUICK = [{"lines": 5, "gap": 0.01}]
RESTART_WAIT = 12


@pytest.fixture(params=["reference", pytest.param("docker", marks=pytest.mark.skipif(
    not dk.OPT_IN, reason="real Docker is opt-in: SG_TEST_DOCKER=1"))])
def sx(request, tmp_path, monkeypatch, st_live_docker):
    owner = None
    if request.param == "docker":
        dk.require_docker()
        owner = request.config.st_docker_owner
    s = s3.S3Sched(tmp_path, monkeypatch, backend=request.param, owner=owner)
    s.counts_spawns = request.param == "reference"
    yield s
    s.close()
    if owner:
        dk.remove_session_resources(owner)


def finish(s, node, timeout=RESTART_WAIT):
    return s3.wait_done(s, node, timeout)


def output_lines(s, node):
    (rid, *_) = s.run_ids(node) or [None]
    return s.host_output(rid)[0].count(b"\n") if rid else 0


def newest_output(s, node):
    return s.host_output(s.run_ids(node)[-1])


def control(s, ops):
    """An uninterrupted run of `ops`: its host output is the reference."""
    node = s.node(*ops)
    assert finish(s, node)["state"] == "done"
    return newest_output(s, node)


def same_output(got, expected):
    """Byte equality of (stdout, stderr), with a message that says where they part."""
    for name, a, b in (("stdout", got[0], expected[0]), ("stderr", got[1], expected[1])):
        if a != b:
            at = next((i for i, (x, y) in enumerate(zip(a, b)) if x != y), min(len(a), len(b)))
            raise AssertionError(f"host {name} differs from the uninterrupted run: {len(a)} bytes against "
                                 f"{len(b)}, first difference at {at}: {a[at - 20:at + 30]!r} vs {b[at - 20:at + 30]!r}")


def kill_while_streaming(s, node, lines=12):
    s.wait(lambda: output_lines(s, node) >= lines, 15, f"{lines} lines of host output")
    run_id = s.run_ids(node)[0]
    s.kill9()
    return run_id


def restart(s):
    s.start()


def spawns(s):
    return s.fake.spawns() if s.counts_spawns else None


# ======================================================== the replay rule

def test_ic_c15_an_uninterrupted_run_leaves_its_whole_output_on_the_host(sx):
    sx.start()
    out, err = control(sx, STREAM_OPS)
    assert out == wl.lines_stdout(LINES)
    assert err == b"".join(b"E%04d\n" % i for i in range(1, ERRS + 1))


def test_ic_c15_a_scheduler_kill_while_streaming_loses_and_duplicates_no_byte(sx):
    sx.start()
    expected = control(sx, STREAM_OPS)
    node = sx.node(*STREAM_OPS)
    run_id = kill_while_streaming(sx, node)
    restart(sx)
    assert finish(sx, node)["state"] == "done"
    same_output(sx.host_output(run_id), expected)
    assert spawns(sx) in (2, None)                       # the control run and this one: never re-run


ERR_FIRST = [{"err": 60, "gap": 0.03}, {"lines": 10, "gap": 0.0}]


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
@pytest.mark.parametrize("tail", ["whole_lines", "partial_line"])
def test_ic_c15_replay_an_append_that_outran_its_checkpoint_is_discarded_and_replayed(sx, stream, tail):
    """The crash between an append and its checkpoint: the host output is
    longer than what the checkpoint says was consumed."""
    from multiagents.paths import ProjectPaths
    ops = STREAM_OPS if stream == "stdout" else ERR_FIRST
    name = "output.ndjson" if stream == "stdout" else "stderr.log"
    sx.start()
    expected = control(sx, ops)
    node = sx.node(*ops)
    def lines_of(n):
        rids = sx.run_ids(node)
        return rids and (sx.host_output(rids[0])[0 if stream == "stdout" else 1]).count(b"\n") >= n
    sx.wait(lambda: lines_of(12), 15, f"12 lines of host {stream}")
    run_id = sx.run_ids(node)[0]
    sx.kill9()
    path = ProjectPaths(sx.root).run_dir(run_id) / name
    have = path.read_bytes() if path.exists() else b""
    full = expected[0] if stream == "stdout" else expected[1]
    assert full.startswith(have), "what the host has recorded so far is a prefix of the stream"
    rest = full[len(have):].splitlines(True)
    extra = b"".join(rest[:2]) if tail == "whole_lines" else rest[0][:len(rest[0]) // 2]
    path.write_bytes(have + extra)                       # what an append that outran its checkpoint leaves
    restart(sx)
    assert finish(sx, node)["state"] == "done"
    same_output(sx.host_output(run_id), expected)


def test_ic_c15_whole_lines_only_a_trailing_partial_line_waits_for_its_newline(sx):
    sx.start()
    node = sx.node({"lines": 4, "gap": 0.01}, {"half": "the-split-line", "gap": 1.6}, {"lines": 1, "gap": 0.0})
    sx.wait(lambda: output_lines(sx, node) >= 4, 10, "the whole lines")
    run_id = sx.run_ids(node)[0]
    end = time.monotonic() + 1.1                          # inside the 1.6 s the line is half written
    seen = 0
    while time.monotonic() < end:
        data = sx.host_output(run_id)[0]
        assert data == b"" or data.endswith(b"\n"), f"a partial line reached the host output: {data[-60:]!r}"
        seen += 1
        time.sleep(0.02)
    assert seen > 10
    assert finish(sx, node)["state"] == "done"
    final = sx.host_output(run_id)[0]
    assert final.endswith(b"\n") and b"the-split-line" in final and final.count(b"\n") == 6


def test_ic_c15_a_partial_last_line_at_the_end_of_the_stream_is_appended_once(sx):
    sx.start()
    node = sx.node({"lines": 3, "gap": 0.0}, {"raw": "no-newline-at-the-end"})
    assert finish(sx, node)["state"] == "done"
    out = sx.host_output(sx.run_ids(node)[0])[0]
    assert out == wl.lines_stdout(3) + b"no-newline-at-the-end"


def test_ic_c15_a_partial_line_at_the_kill_is_not_doubled_after_recovery(sx):
    sx.start()
    ops = [{"lines": 6, "gap": 0.02}, {"half": "split-across-the-kill", "gap": 3.0}, {"lines": 4, "gap": 0.0}]
    expected = control(sx, ops)
    node = sx.node(*ops)
    sx.wait(lambda: output_lines(sx, node) >= 6, 10, "the whole lines")
    run_id = sx.run_ids(node)[0]
    time.sleep(0.5)                                       # the half line is on the wire now
    sx.kill9()
    restart(sx)
    assert finish(sx, node)["state"] == "done"
    same_output((sx.host_output(run_id)[0], b""), (expected[0], b""))


# ================================================ the five launch boundaries

def evidence_key(s, node, field="creation_key"):
    return s.evidence(s.runs(node)[-1]["attempt_id"]).get("executor", {}).get(field)


def crashed(s, point, ops=QUICK):
    s.start()
    pid = s.crash_at(point)
    node = s.node(*ops)
    s.wait_dead(pid, timeout=15)
    return node


def test_ic_c15_boundary_1_before_creation_nothing_exists_and_the_attempt_is_an_unlaunched_claim(sx):
    node = crashed(sx, "provision_entry")
    key = evidence_key(sx, node)
    assert key, "the creation key is recorded before provision is called (K5)"
    assert kind_of(ic.run(sx.be().find(key))) == "absent"
    assert spawns(sx) in (0, None)
    restart(sx)
    assert finish(sx, node)["state"] == "done"
    assert spawns(sx) in (1, None)


@pytest.mark.parametrize("point", ["provision_done", "run_entry"])
def test_ic_c15_boundary_2_a_created_resource_is_removed_never_started_and_the_attempt_is_unlaunched(sx, point):
    node = crashed(sx, point)
    key = evidence_key(sx, node)
    be = sx.be()
    found = ic.run(be.find(key))
    assert kind_of(found) != "absent"
    assert kind_of(ic.run(be.inspect(ic.identity_of(found)))) == "created"
    assert spawns(sx) in (0, None)
    restart(sx)
    assert finish(sx, node)["state"] == "done"
    assert kind_of(ic.run(be.find(key))) == "absent"       # removed, artifacts not kept
    assert spawns(sx) in (1, None)                          # the invocation ran exactly once, in a new resource


def test_ic_c15_boundary_3_started_but_the_invocation_id_was_not_recorded_is_adopted_not_rerun(sx):
    ops = [{"lines": 40, "gap": 0.03}]
    sx.start()
    expected = control(sx, ops)
    pid = sx.crash_at("run_done")
    node = sx.node(*ops)
    sx.wait_dead(pid, timeout=15)
    restart(sx)
    assert finish(sx, node)["state"] == "done"
    runs = sx.runs(node)
    assert len(runs) == 1                                   # adopted: no second attempt
    same_output(sx.host_output(runs[0]["run_id"]), expected)
    ex = sx.evidence(runs[0]["attempt_id"])["executor"]
    assert ex["invocation_id"] == ex["invocation_key"]


def test_ic_c15_boundary_4_exit_seen_before_its_evidence_is_recorded_drains_and_ends_like_an_uninterrupted_run(sx):
    ops = [{"lines": 8, "gap": 0.01}, {"err": 3, "gap": 0.0}, {"exit": 3}]
    sx.start()
    ref_node = sx.node(*ops)
    ref = finish(sx, ref_node)
    ref_out = newest_output(sx, ref_node)
    pid = sx.crash_at("exited_seen")
    node = sx.node(*ops)
    sx.wait_dead(pid, timeout=15)
    restart(sx)
    got = finish(sx, node)
    assert got["state"] == ref["state"]
    assert sorted({c["class"] for c in ic.causes(got)}) == sorted({c["class"] for c in ic.causes(ref)})
    same_output(sx.host_output(sx.run_ids(node)[0]), ref_out)    # drained to the end, exactly once
    assert spawns(sx) in (2, None)


def test_ic_c15_boundary_5_a_committed_capture_is_recognised_and_no_second_generation_appears(sx):
    sx.start()
    node = sx.node({"commit": "result.txt"}, {"lines": 3, "gap": 0.0})
    done = finish(sx, node)
    assert done["state"] == "done" and len(done["generations"]) == 1
    sx.kill9()
    restart(sx)
    time.sleep(ic.QUIET * 2)
    again = sx.get(node)
    assert again["state"] == "done" and again["generations"] == done["generations"]
    assert len(sx.runs(node)) == 1 and spawns(sx) in (1, None)


def test_ic_c15_boundary_5_a_crash_at_the_first_removal_still_gives_exactly_one_generation(sx):
    sx.start()
    pid = sx.crash_at("remove_entry")
    node = sx.node({"commit": "result.txt"}, {"lines": 3, "gap": 0.0})
    sx.wait_dead(pid, timeout=15)
    restart(sx)
    done = finish(sx, node)
    time.sleep(ic.QUIET)
    assert done["state"] == "done" and len(sx.get(node)["generations"]) == 1
    assert len(sx.runs(node)) == 1 and spawns(sx) in (1, None)


# ================================================ adoption after a restart

def test_ic_c15_adopt_running_the_attempt_finishes_normally_and_its_output_is_whole(sx):
    sx.start()
    ops = [{"lines": 50, "gap": 0.04}]
    expected = control(sx, ops)
    node = sx.node(*ops)
    run_id = kill_while_streaming(sx, node, lines=8)
    restart(sx)
    assert finish(sx, node)["state"] == "done"
    assert len(sx.runs(node)) == 1
    same_output(sx.host_output(run_id), expected)


def test_ic_c15_adopt_exited_the_exit_is_collected_and_the_attempt_ends_normally(sx):
    sx.start()
    ops = [{"lines": 12, "gap": 0.02}, {"exit": 0}]
    expected = control(sx, ops)
    node = sx.node(*ops)
    run_id = kill_while_streaming(sx, node, lines=1)
    key = evidence_key_of(sx, node)
    be = sx.be()
    ident = ic.identity_of(ic.run(be.find(key)))
    end = time.monotonic() + 15
    while kind_of(ic.run(be.inspect(ident))) != "exited" and time.monotonic() < end:
        time.sleep(0.1)                                    # the workload ends while the scheduler is down
    assert kind_of(ic.run(be.inspect(ident))) == "exited"
    restart(sx)
    assert finish(sx, node)["state"] == "done"
    assert len(sx.runs(node)) == 1
    same_output(sx.host_output(run_id), expected)


def evidence_key_of(s, node):
    return evidence_key(s, node)


def test_ic_c15_adopt_absent_a_resource_gone_while_down_is_a_lost_launch_class_launch_never_another_kind(sx):
    sx.start()
    node = sx.node({"hold": 30})
    sx.wait(lambda: sx.runs(node), 8, "the run")
    time.sleep(0.5)
    sx.kill9()
    key = evidence_key_of(sx, node)
    be = sx.be()
    ident = ic.identity_of(ic.run(be.find(key)))
    ic.run(be.stop(ident, 0.2), timeout=20)
    ic.run(be.remove(ident, False))
    assert kind_of(ic.run(be.find(key))) == "absent"
    restart(sx)
    view = sx.wait(lambda: any(c["class"] == "launch" for c in ic.causes(sx.get(node))) and sx.get(node),
                   RESTART_WAIT, "the lost launch")
    assert any(c["class"] == "launch" for c in ic.causes(view))
    assert {r["executor"]["kind"] for r in sx.runs(node)} == {"isolated"}


def test_ic_c15_adopt_unknown_holds_the_attempt_and_keeps_its_reservation_until_the_backend_answers(sx):
    sx.start()
    node = sx.node({"hold": 4}, {"lines": 3, "gap": 0.0})
    sx.wait(lambda: sx.runs(node), 8, "the run")
    time.sleep(0.5)
    sx.kill9()
    sx.plan({"calls": True, "inspect": "unknown"})
    restart(sx)
    time.sleep(ic.QUIET * 2)
    view = sx.get(node)
    assert view["state"] not in ("done", "failed", "cancelled"), ic.text_of(view)[:400]
    assert len(sx.runs(node)) == 1 and spawns(sx) in (1, None)    # not relaunched, not abandoned
    sx.plan({"calls": True})
    assert finish(sx, node)["state"] == "done"
    assert len(sx.runs(node)) == 1


def test_ic_c15_no_recorded_identity_is_resolved_through_the_creation_key_first(sx):
    sx.start()
    pid = sx.crash_at("provision_done")                    # the resource exists; its identity was never recorded
    node = sx.node(*QUICK)
    sx.wait_dead(pid, timeout=15)
    assert not evidence_key(sx, node, "identity")
    sx.plan({"calls": True})
    restart(sx)
    finish(sx, node)
    assert sx.get(node)["state"] == "done"
    assert len(sx.runs(node)) == 1


def test_ic_c15_adoption_uses_the_recorded_backend_even_when_the_config_changed_meanwhile(sx):
    sx.start()
    node = sx.node({"hold": 3}, {"lines": 3, "gap": 0.0})
    sx.wait(lambda: sx.runs(node), 8, "the run")
    sx.executor("local")                                   # the config says local now
    sx.kill9()
    restart(sx)
    assert finish(sx, node)["state"] == "done"
    assert {r["executor"]["kind"] for r in sx.runs(node)} == {"isolated"}
    assert spawns(sx) in (1, None)


def test_ic_c15_no_isolated_path_reads_a_pid_file_or_a_process_group(sx):
    """Liveness comes from the backend. A run directory that carries a pid
    file of a LIVE decoy does not keep a finished attempt alive, and one that
    names a dead pid does not end a running one (see also IC-C16)."""
    import subprocess
    decoy = subprocess.Popen(["sleep", "60"], start_new_session=True)
    try:
        sx.start()
        node = sx.node({"forge": {"pid": decoy.pid}}, {"lines": 3, "gap": 0.0})
        assert finish(sx, node)["state"] == "done"
        assert decoy.poll() is None
    finally:
        decoy.kill()
        decoy.wait(5)
