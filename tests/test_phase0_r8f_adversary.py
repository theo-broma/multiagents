"""Adversary attack suite for P0-R8f: interactive compaction (stop -> compact -> resume).

Contract: `context/specs/phase0-context-and-team.md` § P0-R8f and its amendments.
Implementation: `src/multiagents/driver.py`, `src/multiagents/scripts.py`,
`src/multiagents/defaults/providers/claude.sh`.

Attacks:
1. Tree busy mid-grace permanently locks out future compaction proposals for the
   remainder of the rest episode because `self.probed` is not reset on cancellation.
2. `claude.sh compact` reports success (exit 0) when compaction did not shrink
   token count (postTokens == preTokens).
3. `claude.sh compact` reports success (exit 0) when token count grew (postTokens > preTokens).
4. Compaction succeeding without shrinking tokens causes headless mode (`_supervise`)
   to loop compactions repeatedly on every turn.
5. `claude.sh compact` check mode probe exits 0 on an unreadable transcript (chmod 000),
   tricking the driver into stopping the live session before compaction crashes.
6. `claude.sh compact` hardcodes `$HOME/.claude/projects`, ignoring `CLAUDE_CONFIG_DIR`.
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import p0_context_harness as ch  # noqa: E402
from test_phase0_interactive_compact import (  # noqa: E402
    OVER,
    session,
)
from test_phase0_provider_compact import Scratch  # noqa: E402


def manual_record(pre: int = 10000, post: int = 1000) -> str:
    rec = ch.compaction(pre, post, trigger="manual")
    rec["compactMetadata"]["preTokens"] = pre
    rec["compactMetadata"]["postTokens"] = post
    return json.dumps(rec)


# ==============================================================================
# Attack 1: Tree busy mid-grace permanently blocks compaction once quiet
# ==============================================================================

def test_adversary_tree_busy_mid_grace_permanently_blocks_compaction_once_quiet(session):
    """Attack on P0-R8f.2 / P0-R8f.3:
    When compaction conditions hold, `due()` runs the probe and schedules compaction.
    If the tree becomes busy during grace (e.g. background agent starts), `due()` cancels
    compaction and prints:
      'compaction cancelled — the session is in use; it will be proposed again once it is quiet.'
    However, `_cancel()` does NOT clear `self.probed`.
    Because the cancellation was caused by tree activity rather than a user message,
    the session transcript has not changed (state == self.probed).
    When the tree becomes quiet again, `due()` evaluates `if self._busy() or state == self.probed: return False`.
    Compaction is NEVER proposed again once quiet for the remainder of this rest episode.
    """
    s = session()
    s.reading(OVER)

    from multiagents.driver import _AttachedCompaction

    comp = _AttachedCompaction(
        s.paths, s.config, s.spec, s.provider, object(), dict(s.context)
    )
    comp.since = time.time() - 300  # at rest

    with patch("multiagents.driver.sys.stdin.isatty", return_value=True), \
         patch("multiagents.driver.session_context", return_value=OVER), \
         patch("multiagents.driver.scripts.run_action", return_value=(0, "", "")):

        # Poll 1: schedules compaction
        res = comp.due()
        assert res is False
        assert comp.scheduled is not None, "compaction should be scheduled"

        # Tree becomes busy mid-grace
        s.node("running")  # adds active node to tree
        # Poll 2: cancels because tree is busy
        res = comp.due()
        assert res is False
        assert comp.scheduled is None, "compaction should be cancelled while tree is busy"

        # Tree becomes quiet again (node finishes)
        nodes = s.tree.read()["nodes"]
        for nid in nodes:
            s.tree.update(nid, status="merged")
        assert not s.tree.active()

        # Session is still at rest and over threshold; tree is quiet.
        # Compaction must be proposed again once quiet!
        comp.due()
        assert comp.scheduled is not None, (
            "DEFECT: compaction was never proposed again after the tree became quiet! "
            "self.probed was not cleared on busy-tree cancellation, permanently suppressing compaction."
        )


# ==============================================================================
# Attack 2: claude.sh compact succeeds when tokens did not shrink
# ==============================================================================

def test_adversary_claude_compact_succeeds_when_tokens_did_not_shrink(tmp_path):
    """Attack on P0-R8d.2:
    `claude.sh compact` verifies that a manual compaction record was appended,
    but only checks `if pre is not None and post is not None:`.
    It fails to verify that `post < pre`!
    When `claude -p '/compact'` produces a record where `postTokens >= preTokens`
    (token count did not shrink), `claude.sh` exits 0 with figures, reporting false success.
    """
    s = Scratch(tmp_path)
    s.session()

    fake_cli = s.bin / "claude"
    fake_cli.write_text(f"""#!/bin/sh
cat >> "{s.transcript}" << 'EOF'
{manual_record(pre=10000, post=10000)}
EOF
echo '{{"type":"result","subtype":"success","session_id":"{s.transcript.stem}"}}'
""")
    fake_cli.chmod(0o755)

    got = s.run("claude", "compact")
    assert got.returncode != 0, (
        f"DEFECT: claude.sh compact exited 0 ({got.stdout.strip()!r}) when compaction "
        "failed to shrink the token count (preTokens=10000, postTokens=10000)."
    )


# ==============================================================================
# Attack 3: claude.sh compact succeeds when tokens grew
# ==============================================================================

def test_adversary_claude_compact_succeeds_when_tokens_grew(tmp_path):
    """Attack on P0-R8d.2:
    When `claude -p '/compact'` produces a record where `postTokens > preTokens`,
    `claude.sh` exits 0, reporting a token increase as a successful compaction.
    """
    s = Scratch(tmp_path)
    s.session()

    fake_cli = s.bin / "claude"
    fake_cli.write_text(f"""#!/bin/sh
cat >> "{s.transcript}" << 'EOF'
{manual_record(pre=10000, post=15000)}
EOF
echo '{{"type":"result","subtype":"success","session_id":"{s.transcript.stem}"}}'
""")
    fake_cli.chmod(0o755)

    got = s.run("claude", "compact")
    assert got.returncode != 0, (
        f"DEFECT: claude.sh compact exited 0 ({got.stdout.strip()!r}) when tokens GREW "
        "after compaction (preTokens=10000, postTokens=15000)."
    )


# ==============================================================================
# Attack 4: Compaction succeeding without shrinking causes headless loop
# ==============================================================================

def test_adversary_compaction_succeeding_without_shrinking_causes_headless_loop(tmp_path):
    """Attack on P0-R8c.1 / P0-R8c.3:
    In headless mode (`_supervise`), `_compact_if_due` checks whether `tokens >= threshold`.
    If `claude.sh compact` reports success (exit 0) but did not shrink the transcript below threshold,
    `_compact_if_due` lacks any threshold-crossing latch and runs compaction again on the next turn,
    looping repeatedly on every turn.
    """
    from multiagents.driver import _compact_if_due

    paths = MagicMock()
    paths.root = tmp_path
    config = MagicMock()
    config.limits = {"compact_at_tokens": 8000}
    spec = MagicMock()
    spec.provider = "claude"
    provider = MagicMock()
    executor = MagicMock()
    context = {"MULTIAGENTS_SESSION_ID": "sid-test"}
    tree = MagicMock()
    tree.active.return_value = []
    tree.read.return_value = {}
    unsupported = []

    compact_calls = 0

    def fake_compact(*a, **k):
        nonlocal compact_calls
        compact_calls += 1
        return 0  # reports success, but tokens remain 9000

    with patch("multiagents.driver.session_context", return_value=9000), \
         patch("multiagents.driver._compact_session", side_effect=fake_compact):

        # Turn 1
        _compact_if_due(paths, config, spec, provider, executor, context, tree, unsupported)
        assert compact_calls == 1

        # Turn 2: tokens are still 9000 because compaction didn't shrink them
        _compact_if_due(paths, config, spec, provider, executor, context, tree, unsupported)
        assert compact_calls == 1, (
            f"DEFECT: _compact_if_due ran compaction {compact_calls} times in a row! "
            "When compaction succeeds without shrinking tokens below threshold, "
            "headless mode loops compactions on every turn."
        )


# ==============================================================================
# Attack 5: claude.sh check mode probe exits 0 on unreadable transcript
# ==============================================================================

def test_adversary_claude_probe_exits_zero_on_unreadable_transcript(tmp_path):
    """Attack on P0-R8f.1:
    P0-R8f.1 states that with `MULTIAGENTS_COMPACT_CHECK=1`, the script exits 0
    if a real compact call could succeed now.
    However, `claude.sh compact` only checks `[ ! -f "$transcript" ]` instead of `[ ! -r "$transcript" ]`.
    If the transcript file is unreadable (chmod 000), check mode exits 0, falsely
    reporting that compaction can succeed and causing the driver to stop the live CLI.
    """
    s = Scratch(tmp_path)
    s.session()
    s.transcript.chmod(0o000)

    try:
        got = s.run("claude", "compact", MULTIAGENTS_COMPACT_CHECK="1")
        assert got.returncode != 0, (
            "DEFECT: claude.sh check mode probe exited 0 on an unreadable transcript (chmod 000)! "
            "It tests `[ ! -f ]` instead of `[ ! -r ]`, misleading the driver into stopping the CLI."
        )
    finally:
        s.transcript.chmod(0o644)


# ==============================================================================
# Attack 6: claude.sh compact ignores CLAUDE_CONFIG_DIR
# ==============================================================================

def test_adversary_claude_compact_ignores_claude_config_dir(tmp_path):
    """Attack on P0-R8d.2 / P0-R8f.1:
    `transcripts.default_root()` respects `CLAUDE_CONFIG_DIR` when sensing tokens.
    However, `claude.sh compact` hardcodes `$HOME/.claude/projects/`.
    When `CLAUDE_CONFIG_DIR` is set to an alternate directory, `claude.sh compact` looks
    in the wrong place and exits 1 ('no transcript for session ...'), breaking compaction.
    """
    custom_claude = tmp_path / "custom_claude"
    s = Scratch(tmp_path)
    s.transcript = custom_claude / "projects" / ch.slug(s.cwd) / f"{s.transcript.stem}.jsonl"
    s.session()

    got = s.run("claude", "compact", CLAUDE_CONFIG_DIR=str(custom_claude),
                MULTIAGENTS_COMPACT_CHECK="1")
    assert got.returncode == 0, (
        f"DEFECT: claude.sh compact exited {got.returncode} with CLAUDE_CONFIG_DIR set. "
        f"Stderr: {got.stderr.strip()!r}. It hardcodes $HOME/.claude/projects."
    )
