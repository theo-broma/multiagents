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
    """Attack on P0-R8f.2 / P0-R8f.3 / R8f.14:
    When compaction conditions hold, `due()` runs the probe and schedules compaction.
    If the tree becomes busy during grace, `due()` cancels compaction and prints:
      'compaction cancelled — the session is in use; it will be proposed again once it is quiet.'
    The attack: `_cancel()` not clearing `self.probed`. The cancellation was caused
    by tree activity rather than a user message, so the transcript is unchanged
    (state == self.probed), and once the tree is quiet again compaction would never
    be proposed again for the remainder of this rest episode.

    Busy is SV-R11's (context/specs/agent-survival.md): a finished result the
    orchestrator has not yet seen. A running agent deliberately does not block
    compaction, so this makes the tree busy with an unseen `done` result, and quiet
    again by having the orchestrator see it through check_agent.
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

        # Tree becomes busy mid-grace (SV-R11): an agent finishes, result unseen
        agent_id = s.finished("done")
        # Poll 2: cancels because tree is busy
        res = comp.due()
        assert res is False
        assert comp.scheduled is None, "compaction should be cancelled while tree is busy"

        # Tree becomes quiet again: the orchestrator sees the result
        s.see(agent_id, "check_agent")

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


# Attack 4 withdrawn (8493889): in headless mode R8c.3 wins; a compaction that fails, including one that did not shrink (R8f.15), is retried next qualifying turn.


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
