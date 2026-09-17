"""Supplementary pins for bug-565863 (context/specs/phase1-budget-accounting.md).

R4/R5/R6 in test_core.py sample the tag path through `budget_tag_status` and
the tag-ceiling refusal. Three things the fix touched are not on that path and
had no test at all:

- the tree-wide `budget_tokens` limit (runner.py), which reads the same
  rollup figure and would have kept refusing on claude-free numbers alone;
- the `render()` totals line, which used to ADD `total` and `total_tokens`
  and so would double-count every agy run against the normalised figure;
- `sum_usage`'s rule that a run's total-shaped keys are folded INTO `total`
  (token_count prefers them) rather than summed beside it.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import c3_harness as h  # noqa: E402


CLAUDE_SHAPED = {  # no `total`, no `total_tokens` — what claude actually sends
    "input_tokens": 400, "output_tokens": 100,
    "cache_read_input_tokens": 700,
}


def test_the_tree_wide_budget_limit_counts_claude_shaped_spend(
        tmp_path, monkeypatch):
    """runner.py's `Tree token budget` refusal reads the same normalised
    figure as the tag path, so claude spend exhausts it too."""
    spec = h.AgentSpec("worker", "p", "m")
    r = h.make_runner(tmp_path, monkeypatch, agents={"worker": spec},
                      project={"limits": {"budget_tokens": 1_000}})
    r.tree.add(h.Node(id="ag-1", agent="worker", provider="claude", model="m",
                      parent=None, depth=1, status="done"))
    r.tree.update("ag-1", usage=dict(CLAUDE_SHAPED, input_tokens=1_200))

    with pytest.raises(RuntimeError, match="Tree token budget exhausted"):
        asyncio.run(r.start("worker", "go"))


def test_render_totals_do_not_double_count_an_agy_run(tmp_path):
    """The totals line used to read `total + total_tokens`; against a
    normalised `total` that counts agy's spend twice."""
    tree = h.make_tree(tmp_path)
    tree.add(h.Node(id="ag-1", agent="a", provider="agy", model="m",
                    parent=None, depth=1, status="done"))
    tree.update("ag-1", usage={"total_tokens": 219_669})

    out = tree.render()
    assert "total: 219,669 tokens" in out
    assert "439,338" not in out


def test_a_run_with_a_total_and_parts_is_counted_once(tmp_path):
    """token_count prefers a run's total-shaped key; the parts must not be
    added on top of it, and the raw total-shaped keys must not survive as
    detail keys that a reader could add back."""
    from multiagents.tree import sum_usage

    total = sum_usage([{"usage": {"total": 100, "input_tokens": 5,
                                  "output_tokens": 5}}])
    assert total["total"] == 100, "the parts are already inside the total"
    assert "total_tokens" not in total, "no raw total-shaped detail to add back"
    assert total["input_tokens"] == 5, "the parts stay as raw detail"


def test_rollup_sums_all_three_provider_shapes(tmp_path):
    """The whole-tree rollup is the same mixed-provider sum as the tag path:
    each shape counts, none replaces another."""
    tree = h.make_tree(tmp_path)
    shapes = (
        {"total": 1_000, "cost_usd": 0.5},                    # opencode
        {"total_tokens": 2_000},                              # agy
        {"input_tokens": 1, "output_tokens": 2,
         "cache_read_input_tokens": 3_997, "cost_usd": 1.5},  # claude
    )
    for index, usage in enumerate(shapes):
        tree.add(h.Node(id=f"ag-{index}", agent="a", provider=f"p{index}",
                        model="m", parent=None, depth=1, status="done"))
        tree.update(f"ag-{index}", usage=usage)

    rolled = tree.rollup_usage()
    assert rolled["total"] == 7_000, "all three shapes, none dropped"
    assert rolled["cost_usd"] == 2.0
