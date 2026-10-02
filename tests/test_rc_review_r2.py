"""RC review round 2 (reviewer ag-2d1240) — regression test for its finding.

`context/specs/refusal-classification.md` (bug-1213a0), RC-R3: a refusal
triggers no automatic retry. Round 1 stopped the commit-fix LOOP on a refused
fix turn; this covers the gate itself: when the ORIGINAL turn is classified
`refused`, its hook-refused commit is not fed back to the agent at all. The
commit failure stays visible, as in the fix-turn case.

The fix-loop fixture (fake CLI plans, refusing hook) is test_commit_identity_r5's.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_commit_identity_r5 import (  # noqa: E402
    SID, events, fake_provider, invocations, make, result_of, run_to_end, text,
)

PATTERN = "zq-blocked-[a-z]+"

# The original turn does real work it leaves uncommitted — so the end-of-run
# commit is refused by the BLOCK-conditional hook — then refuses: its final
# message matches a declared refusal marker, and it exits non-zero.
REFUSED_WITH_WORK = [["touch", "work.txt", "agent output\n"],
                     ["touch", "BLOCK", "x"],
                     text("zq-blocked-filter"),
                     ["exit", 1]]


def test_a_refused_original_turn_gets_no_automatic_fix_turn(tmp_path, monkeypatch):
    prov, probe = fake_provider(tmp_path, [REFUSED_WITH_WORK],
                                refusal_markers=[PATTERN])
    r, project = make(tmp_path, monkeypatch, prov,
                      limits={"commit_fix_attempts": 3})

    agent = run_to_end(r)

    assert len(invocations(probe)) == 1, (
        f"a refused turn must not be resumed to fix its commit: "
        f"{len(invocations(probe)) - 1} fix launch(es)")
    assert events(r, agent, "commit_fix_attempt") == []
    assert events(r, agent, "retrying") == []
    node = r.tree.get(agent)
    assert node.status == "refused", (node.status, node.reason)
    # The commit failure stays visible, as in the fix-turn case.
    assert events(r, agent, "commit_failed"), "the hook refusal must be reported"
    result = result_of(r, agent)
    assert result["status"] == "refused", result["status"]
    assert "the work-in-progress commit was refused" in result["text"], result["text"]
    assert result["refusal"]["pattern"] == PATTERN, result.get("refusal")
    assert result["refusal"]["source"] == "assistant message", result.get("refusal")
