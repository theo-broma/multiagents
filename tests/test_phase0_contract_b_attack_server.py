"""Attack on P0-R8a.4 — the `context_wind_down` latch across a config reload.

Contract: `context/specs/phase0-context-and-team.md` § P0-R8a.4: "attached
once, then not again until the reading has dropped below the threshold (a
compaction) and crossed it again ... nothing else may."
Reuses the `served` fixture of `test_phase0_context_window.py`.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import p0_context_harness as ch  # noqa: E402
from test_phase0_context_window import WIND_DOWN, _call, served  # noqa: E402,F401


def _set_wind_down(p, value) -> None:
    path = p.config / "project.yaml"
    data = yaml.safe_load(path.read_text())
    data["limits"]["context_wind_down_tokens"] = value
    path.write_text(yaml.safe_dump(data))


@pytest.mark.xfail(strict=True, reason=(
    "Finding judged acceptable: raising the threshold above the reading and "
    "lowering it again re-arms the latch, so a second notice arrives with no "
    "compaction in between. A threshold moved across the reading is a fresh "
    "crossing in every sense but the letter of R8a.4, and it needs a config "
    "edit to happen."))
def test_attack_r8a_4_a_threshold_moved_up_and_back_is_not_a_second_crossing(served):
    p = served()
    ch.write_transcript(p.transcript, [ch.request(WIND_DOWN + 2_500)])
    assert "context_wind_down" in _call()
    _set_wind_down(p, WIND_DOWN * 10)
    assert "context_wind_down" not in _call()
    _set_wind_down(p, WIND_DOWN)
    _call()
    assert len(p.notices()) == 1
