"""M3 — the git parts of NC-R17/NC-R61: a run that ends while the scheduler is
dead, and a scheduler killed around result capture and integration.

The points between "result captured" and "integrated" cannot be hit from
outside deterministically; the sweep kills the scheduler at a range of delays
after the run's end is recorded and checks the invariant at each: exactly one
generation, integrated once, no extra commits, one launch.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from nc_fixture.gitworld import GitWorld  # noqa: E402
from nc_fixture.world import run_id_of  # noqa: E402


# Upper bound on every poll-until-condition wait (run to start, node to settle,
# scheduler restart to converge). A red test hits its assertion within this
# bound instead of the harness defaults of 30-60 s.
WAIT_BOUND = 8.0
# Same, for the few waits that span several runs or a scheduler restart (the
# test asked for 60 s).
LONG_BOUND = 20.0


def bound(world: GitWorld) -> None:
    """Cap each `world.until` deadline (the base of wait_state, done,
    wait_running, wait_spawn) at WAIT_BOUND (LONG_BOUND where 60 s or more was asked)."""
    until = world.until

    def capped(pred, timeout: float = 30, *args, **kw):
        return until(pred, min(timeout, LONG_BOUND if timeout >= 60 else WAIT_BOUND), *args, **kw)

    world.until = capped


@pytest.fixture
def w(tmp_path, monkeypatch):
    world = GitWorld(tmp_path, monkeypatch)
    bound(world)
    yield world
    world.close()


def commits_beyond_main(w: GitWorld, node: str) -> int:
    return int(w.git("rev-list", "--count", f"{w.base}..{w.nodes_ref(node)}"))


def test_nc_r61_a_run_that_ends_while_the_scheduler_is_dead_is_captured_and_integrated_at_restart(w):
    w.start_scheduler()
    a = w.coder("A", {"a.txt": "a\n"}, fx={"gate": "ga"})
    run = run_id_of(w.wait_running(a)["active_run"])
    w.kill9()
    w.gate("ga")
    w.until(lambda: w.tree_nodes()[run]["status"] == "done", what="the run to end")
    assert w.tip(a) == w.main_tip(), "integration happened without a scheduler"
    w.start_scheduler()
    done = w.done(a)
    assert done["outcome"] == "completed"
    assert len(done["runs"]) == 1 and len(done["generations"]) == 1
    assert done["generations"][0]["run_id"] == run
    assert w.tip(a) == done["generations"][0]["commit"]
    assert w.blob(w.tip(a), "a.txt") == "a"
    assert commits_beyond_main(w, a) == 1
    assert len(w.fx.by_tag("A")) == 1


def test_nc_r61_a_dependent_launched_after_the_restart_sees_the_recovered_result(w):
    w.start_scheduler()
    a = w.coder("A", {"a.txt": "from A\n"}, fx={"gate": "ga"})
    b = w.coder("B", {"b.txt": "b"}, inputs=[{"node": a}],
                fx={"shell": ["cat a.txt > $FX_DIR/seen.B"]})
    run = run_id_of(w.wait_running(a)["active_run"])
    w.kill9()
    w.gate("ga")
    w.until(lambda: w.tree_nodes()[run]["status"] == "done", what="the run to end")
    w.start_scheduler()
    w.done(b)
    assert w.fx_file("seen.B") == "from A\n"
    assert len(w.fx.by_tag("B")) == 1


def test_nc_r61_a_failed_run_that_ends_while_the_scheduler_is_dead_integrates_nothing(w):
    w.start_scheduler()
    a = w.coder("A", {"a.txt": "a"}, fx={"gate_after": "ga", "exit": 1})
    run = run_id_of(w.wait_running(a)["active_run"])
    w.kill9()
    w.gate("ga")
    w.until(lambda: w.tree_nodes()[run]["status"] != "running", what="the run to end")
    w.start_scheduler()
    done = w.done(a)
    assert done["outcome"] == "failed"
    assert done["generations"] == []
    assert w.main_tip() == w.git("rev-parse", w.base)


@pytest.mark.parametrize("delay", [0.0, 0.02, 0.06, 0.12, 0.25, 0.5])
def test_nc_r61_crash_sweep_after_run_end_a_result_is_integrated_exactly_once(w, delay):
    w.start_scheduler()
    a = w.coder("A", {"a.txt": "a\n"}, fx={"gate": "ga"})
    run = run_id_of(w.wait_running(a)["active_run"])
    w.gate("ga")
    w.until(lambda: w.tree_nodes()[run]["status"] != "running", timeout=30,
            step=0.01, what="the run to end")
    time.sleep(delay)
    w.kill9()
    w.start_scheduler()
    done = w.done(a, timeout=60)
    assert done["outcome"] == "completed"
    assert len(done["generations"]) == 1
    assert [g["seq"] for g in done["generations"]] == [1]
    assert w.tip(a) == done["generations"][0]["commit"]
    assert commits_beyond_main(w, a) == 1, "the result was integrated twice or not at all"
    assert len(w.fx.by_tag("A")) == 1 and len(done["runs"]) == 1
    w.quiet(2)
    assert len(w.generations(a)) == 1 and commits_beyond_main(w, a) == 1


def test_nc_r61_a_restart_after_integration_does_not_integrate_again(w):
    w.start_scheduler()
    a = w.coder("A", {"a.txt": "a\n"})
    done = w.done(a)
    tip = w.tip(a)
    w.restart_scheduler()
    w.quiet(3)
    assert w.tip(a) == tip
    assert w.get(a)["generations"] == done["generations"]
    assert w.kinds().count("node.integrated") == 1
