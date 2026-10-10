"""OC-D5: the shipped plugin reconciles its own durable compaction receipt."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import time

from test_phase0_provider_compact import SID, SCRIPTS, Scratch


def request(scratch, receipt, request_id="first", **env):
    return scratch.run("claude", "compact",
                       MULTIAGENTS_COMPACTION_REQUEST_ID=request_id,
                       MULTIAGENTS_COMPACT_RECEIPT=str(receipt), **env)


def wait_for(predicate):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("compaction did not reach the expected boundary")


def test_oc_d5_completed_receipt_replays_without_another_compaction(tmp_path):
    scratch = Scratch(tmp_path)
    scratch.session()
    receipt = tmp_path / "first.receipt.json"
    first = request(scratch, receipt)
    before = scratch.transcript.read_bytes()
    again = request(scratch, receipt)
    assert first.returncode == again.returncode == 0
    assert first.stdout == again.stdout == "27729 -> 1607 tokens\n"
    assert len(scratch.calls()) == 1
    assert scratch.transcript.read_bytes() == before
    assert json.loads(receipt.read_text())["status"] == "completed"
    fresh = request(scratch, tmp_path / "next.receipt.json", "next")
    assert fresh.returncode == 0
    assert len(scratch.calls()) == 2


def test_oc_d5_failed_receipt_replays_and_a_new_request_can_try_again(tmp_path):
    scratch = Scratch(tmp_path)
    scratch.session()
    receipt = tmp_path / "first.receipt.json"
    first = request(scratch, receipt, mode="fail")
    again = request(scratch, receipt)
    assert first.returncode == again.returncode == 1
    assert first.stderr == again.stderr
    assert len(scratch.calls()) == 1
    assert json.loads(receipt.read_text())["status"] == "failed"
    assert request(scratch, tmp_path / "next.receipt.json", "next").returncode == 0


def test_oc_d5_request_without_a_receipt_never_invokes_the_cli(tmp_path):
    scratch = Scratch(tmp_path)
    scratch.session()
    got = scratch.run("claude", "compact", MULTIAGENTS_COMPACTION_REQUEST_ID="first")
    assert got.returncode == 1
    assert "MULTIAGENTS_COMPACT_RECEIPT" in got.stderr
    assert scratch.calls() == []


def test_oc_d5_a_receipt_cannot_be_used_for_a_different_request(tmp_path):
    scratch = Scratch(tmp_path)
    scratch.session()
    receipt = tmp_path / "first.receipt.json"
    assert request(scratch, receipt).returncode == 0
    assert request(scratch, receipt, "another").returncode == 1
    assert len(scratch.calls()) == 1


def paused_request(scratch, receipt, gate):
    env = {"PATH": f"{scratch.bin}:/usr/bin:/bin", "HOME": str(scratch.home),
           "MULTIAGENTS_BIN": str(scratch.fake), "MULTIAGENTS_SESSION_ID": SID,
           "MULTIAGENTS_COMPACTION_REQUEST_ID": "first",
           "MULTIAGENTS_COMPACT_RECEIPT": str(receipt), "COMPACT_GATE": str(gate)}
    return subprocess.Popen(["sh", str(SCRIPTS / "claude.sh"), "compact"],
                            cwd=scratch.cwd, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, start_new_session=True)


def paused_cli(scratch):
    # Pause before the manual boundary is appended. A CLI may close unknown
    # descriptors on startup; the plugin must retain its own lock holder.
    source = scratch.fake.read_text()
    source = source.replace('mode = os.environ.get("FAKE_MODE", "manual")', '''
import time
os.closerange(3, 256)
pathlib.Path(str(log) + '.pid').write_text(str(os.getpid()))
while not pathlib.Path(os.environ['COMPACT_GATE']).exists():
    time.sleep(0.02)
mode = "manual"
''')
    scratch.fake.write_text(source)


def reap(*processes):
    for process in processes:
        if process is not None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.communicate(timeout=10)


def test_oc_d5_crash_reconciliation_waits_for_the_first_cli_and_compacts_once(tmp_path):
    scratch = Scratch(tmp_path)
    scratch.session()
    paused_cli(scratch)
    receipt, gate = tmp_path / "first.receipt.json", tmp_path / "release"
    first, again = None, None
    try:
        first = paused_request(scratch, receipt, gate)
        wait_for(lambda: scratch.log.with_suffix('.log.pid').is_file())
        assert json.loads(receipt.read_text())["status"] == "executing"
        first.kill()
        first.wait(timeout=10)
        again = paused_request(scratch, receipt, gate)
        time.sleep(0.15)
        assert again.poll() is None
        assert len(scratch.calls()) == 1
        gate.touch()
        out, err = again.communicate(timeout=10)
        assert again.returncode == 0, err
        assert out == "27729 -> 1607 tokens\n"
        assert len(scratch.calls()) == 1
        assert json.loads(receipt.read_text())["status"] == "completed"
    finally:
        reap(first, again)


def test_oc_d5_a_crash_before_compaction_does_not_claim_an_older_boundary(tmp_path):
    scratch = Scratch(tmp_path)
    scratch.session()
    # The old successful boundary predates this request's durable baseline.
    assert scratch.run("claude", "compact").returncode == 0
    paused_cli(scratch)
    receipt, gate = tmp_path / "first.receipt.json", tmp_path / "release"
    first = None
    try:
        first = paused_request(scratch, receipt, gate)
        wait_for(lambda: len(scratch.calls()) == 2)
        os.killpg(first.pid, signal.SIGKILL)
        first.communicate(timeout=10)
        gate.touch()
        again = request(scratch, receipt, COMPACT_GATE=str(gate))
        assert again.returncode == 0, again.stderr
        assert len(scratch.calls()) == 3
        assert json.loads(receipt.read_text())["status"] == "completed"
    finally:
        reap(first)


def test_oc_d5_a_new_boundary_reconciles_an_executing_receipt_after_a_crash(tmp_path):
    scratch = Scratch(tmp_path)
    scratch.session()
    source = scratch.fake.read_text()
    source = source.replace('print(json.dumps({"type": "result"', '''
import time
while not pathlib.Path(os.environ['COMPACT_GATE']).exists():
    time.sleep(0.02)
print(json.dumps({"type": "result"''')
    scratch.fake.write_text(source)
    receipt, gate = tmp_path / "first.receipt.json", tmp_path / "release"
    first = None
    before = scratch.transcript.read_bytes()
    try:
        first = paused_request(scratch, receipt, gate)
        wait_for(lambda: scratch.transcript.read_bytes() != before)
        assert json.loads(receipt.read_text())["status"] == "executing"
        os.killpg(first.pid, signal.SIGKILL)
        first.communicate(timeout=10)
        gate.touch()
        again = request(scratch, receipt, COMPACT_GATE=str(gate))
        assert again.returncode == 0, again.stderr
        assert again.stdout == "27729 -> 1607 tokens\n"
        assert len(scratch.calls()) == 1
        assert json.loads(receipt.read_text())["status"] == "completed"
    finally:
        reap(first)
