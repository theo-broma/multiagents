"""Adversary: TF retry of an isolated node downgrades to local after config change.

IC-C2: "Automatic retries of the same node (TF) keep the recorded selection;
only an explicit root action (relaunch with a changed config) produces a new
attempt under a new selection, and that attempt records it."
IC-C3: "No process of an isolated attempt ever starts in the shared mode, and no
retry switches kind."

When a node's first attempt fails with an operational failure (TF retryable),
and project.yaml is changed to `executor.kind: local` before the retry runs:
`engine.py:647` calls `self.context(node, probe=True)` with `attempt=None`.
Because `retry_route` is only set on quota_release (engine.py:2007) and not on
TF failure retries, `context.executor_kind` is empty. The admission probe and
result["executor_selection"] read the new configuration (`local`), admitting and
launching Attempt 2 under the local executor rather than the recorded isolated
selection.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import ic_support as ic  # noqa: E402


@pytest.fixture
def ics(tmp_path, monkeypatch):
    s = ic.IcSched(tmp_path, monkeypatch)
    yield s
    s.close()


def test_ic_adv2_tf_retry_never_downgrades_to_local_after_config_change(ics):
    # Attempt 1 fails at provision, retry will be scheduled
    ics.plan({"provision": "raise", "times": 1})
    ics.start()
    node = ics.create()["id"]
    # Wait for attempt 1 to be recorded and fail
    ics.wait_runs(node, 1)

    # Change host configuration to local while attempt 1 has failed / retrying
    ics.executor("local")

    # Attempt 2 runs
    runs = ics.wait_runs(node, 2)
    assert len(runs) == 2

    # Both attempts must remain isolated per IC-C2 and IC-C3
    assert runs[0]["executor"]["kind"] == "isolated"
    assert runs[1]["executor"]["kind"] == "isolated", (
        f"TF retry downgraded to {runs[1]['executor']['kind']} after project config changed to local"
    )
