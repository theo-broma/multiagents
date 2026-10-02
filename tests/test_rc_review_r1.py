"""RC review round 1 (reviewer ag-997df9) — regression tests for its three
findings, against `context/specs/refusal-classification.md` (bug-1213a0).

1. A refused commit-fix turn ends the loop on the first refusal and the run's
   verdict carries it (RC-R3: a refusal is never retried; never `done`).
2. The refusal excerpt redacts the FULL match before it is bounded to 200
   chars, so a registered secret is never left half-visible.
3. `result.json` carries the refusal evidence — {source, pattern, excerpt} —
   as well as the node reason and the events.

The fix-loop fixture (fake CLI plans, refusing hook) is `test_commit_identity_r5`'s;
the stderr-refusal runner is `support/rc_harness`. Both are imported, not copied.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

from test_commit_identity_r5 import (  # noqa: E402
    FIRST, SID, attempt_numbers, events, fake_provider, invocations, make,
    result_of, run_to_end,
)

import rc_harness as rc  # noqa: E402  (tests/support, as test_rc_refusal_* does)

from multiagents import redact  # noqa: E402
from multiagents.runner import Runner  # noqa: E402

PATTERN = "zq-blocked-[a-z]+"
STDERR_LINE = "zq-blocked-content: request declined\n"


# --------------------------- finding 1: refused fix turn -------------------

REFUSED_FIX = [["emit", {"type": "text", "text": "zq-blocked-filter",
                         "session_id": SID}], ["exit", 1]]


def test_a_refused_fix_turn_ends_the_loop_and_the_run_ends_refused(
        tmp_path, monkeypatch):
    """commit_fix_attempts=3 and every fix turn refused: exactly ONE fix turn
    runs, and the run's verdict is the refusal — not `done` over a commit the
    provider refused to let the agent fix."""
    prov, probe = fake_provider(tmp_path, [FIRST, REFUSED_FIX],
                                refusal_markers=[PATTERN])
    r, project = make(tmp_path, monkeypatch, prov,
                      limits={"commit_fix_attempts": 3})

    agent = run_to_end(r)

    calls = invocations(probe)
    assert len(calls) == 2, (
        f"a refused fix turn must end the loop on the first refusal, "
        f"got {len(calls) - 1} fix turn(s)")
    assert attempt_numbers(events(r, agent, "commit_fix_attempt")) == [1]
    node = r.tree.get(agent)
    assert node.status == "refused", (node.status, node.reason)
    assert "refusal marker" in (node.reason or ""), node.reason
    assert events(r, agent, "commit_failed"), "the commit failure is still reported"
    result = result_of(r, agent)
    assert result["status"] == "refused", result["status"]
    # The evidence lives on the fix turn's Run; this run's record carries it.
    assert result["refusal"]["source"] == "assistant message", result.get("refusal")
    assert result["refusal"]["pattern"] == PATTERN, result.get("refusal")


# --------------------------- finding 2: scrub before bound -----------------

def test_the_excerpt_scrubs_the_full_match_before_it_is_bounded(monkeypatch):
    """A registered literal with no recognizable token shape, starting inside
    the last 200 chars of the match: slicing first would leave its head
    half-visible where scrub can no longer see a whole secret."""
    secret = "Xy3qZt9vLm2nKd8sRf5jHw7uC4aGb6eT"   # matches none of scrub's shapes
    assert redact._scrub_text(secret) == secret, "fixture: unshapeable literal"
    monkeypatch.setattr(redact, "_literals", {secret})
    matched = "zq-blocked-" + "p" * 185 + secret   # the secret starts at char 196

    record = Runner._refusal_record("stderr", PATTERN, matched)

    assert record["source"] == "stderr"
    assert record["pattern"] == PATTERN
    excerpt = record["excerpt"]
    assert len(excerpt) <= 200
    assert secret[:4] not in excerpt, (
        f"a half secret survived the bound: {excerpt[-20:]!r}")
    assert excerpt == "zq-blocked-" + "p" * 185 + "[red", excerpt


# --------------------------- finding 3: result.json evidence ---------------

def test_result_json_carries_the_refusal_object(tmp_path, monkeypatch):
    """For a stderr-classified refusal, result.json itself — not only the node
    reason and the event log — records source, pattern and bounded excerpt."""
    cli = rc.Cli(tmp_path, "p", markers=[PATTERN])
    cli.set(stderr=STDERR_LINE, exit=1)
    r = rc.runner(tmp_path, monkeypatch, {"p": cli.config})

    result = rc.start(r)
    assert rc.status(r, result) == "refused"

    data = json.loads(
        (r.paths.run_dir(result["agent_id"]) / "result.json").read_text())
    refusal = data["refusal"]
    assert refusal["source"] == "stderr", refusal
    assert refusal["pattern"] == PATTERN, refusal
    assert "zq-blocked-content" in refusal["excerpt"], refusal
    assert len(refusal["excerpt"]) <= 200, refusal
