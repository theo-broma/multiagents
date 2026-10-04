"""M2 — order, starvation, named locks and the derived node view:
NC-R22/R52 (the `lock`, `admission:*` and `held` codes), NC-R23, NC-R24 (+R53),
NC-R26 (+R68's atomic lock sets of simple nodes).

Assumptions where the contract is silent (kept loose):
- node `created_at` may have a resolution of one second: nodes whose order must
  follow it are deposited 1.1 s apart.
- `starving` is a transition kind `node.starving` in `events.jsonl` (NC-R14).
- "the holder's death is confirmed" is observed from the second run: the
  fixture records whether the first run's pid is still alive when it starts.
- the unconfirmed-termination hold (`termination_unconfirmed`) cannot be forced
  from outside the process (it needs a stop that does not complete); only the
  invariant "a lock is not released while the holder lives" is tested.
"""
from __future__ import annotations

import re
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from nc_fixture.agent import alive  # noqa: E402
from nc_fixture.world import World, blocked_codes, run_id_of  # noqa: E402


@pytest.fixture
def w(tmp_path, monkeypatch):
    world = World(tmp_path, monkeypatch, scheduler={"starvation_after_seconds": 5})
    world.pc = world.provider("pcfx", max_concurrent=1)
    world.agent("pcworker", "pcfx")
    yield world
    world.close()


def holder(w: World, tag="H") -> str:
    node = w.simple(tag, "pcworker", fx={"gate": f"g{tag}"})
    w.wait_running(node)
    return node


def blocked_on(w: World, node: str, code: str) -> dict:
    return w.until(lambda: code in blocked_codes(w.get(node)) and w.get(node),
                   what=f"{node} blocked with {code}")


# ----------------------------------------------------------------- NC-R22 / R23

def test_nc_r22_a_lock_held_by_a_run_blocks_with_the_lock_code(w):
    w.start_scheduler()
    a = w.simple("A", locks=["runner.py"], fx={"gate": "ga"})
    w.wait_running(a)
    b = w.simple("B", locks=["runner.py"])
    view = blocked_on(w, b, "lock")
    assert view["state"] == "open" and view["eligible"] is False
    assert view["ready"] is True, "a lock is not a structural condition (NC-R52)"
    entry = [x for x in view["blocked"] if (x.get("code") if isinstance(x, dict) else x) == "lock"][0]
    assert "runner.py" in str(entry)
    assert w.fx.by_tag("B") == []


def test_nc_r23_the_node_view_carries_the_derived_fields(w):
    w.start_scheduler()
    h = holder(w)
    b = w.simple("B", "pcworker")
    view = blocked_on(w, b, "admission:provider_concurrency")
    for key in ("eligible", "blocked", "eligible_since", "active_run", "ready", "ready_since"):
        assert key in view, key
    assert view["active_run"] in (None, {}, "")
    running = w.get(h)
    assert run_id_of(running["active_run"]) in w.tree_nodes()


def test_nc_r22_nothing_derived_is_persisted(w):
    w.start_scheduler()
    a = w.simple("A", locks=["L"], fx={"gate": "ga"})
    w.wait_running(a)
    b = w.simple("B", locks=["L"])
    blocked_on(w, b, "lock")
    for path in w.scheduler_files():
        text = path.read_bytes().decode("utf-8", "replace")
        assert not re.search(r'"(eligible|blocked|ready)"\s*:', text), path


# ----------------------------------------------------------------- NC-R24 / R53

def test_nc_r24_urgent_launches_first_then_oldest_first(w):
    w.start_scheduler()
    holder(w)
    w.simple("B", "pcworker", fx={"gate": "gB"})
    time.sleep(1.1)
    w.simple("C", "pcworker", fx={"gate": "gC"})
    w.simple("D", "pcworker", urgent=True, fx={"gate": "gD"})
    w.quiet(2)
    order = []
    for opened in ("gH", "gD", "gB"):
        w.gate(opened, w.pc)
        n = len(order) + 2
        w.until(lambda: len(w.pc.calls()) >= n, what=f"launch number {n}")
        order.append(w.pc.calls()[n - 1]["tag"])
    assert order == ["D", "B", "C"]


def test_nc_r24_a_blocked_older_node_does_not_hold_up_a_younger_one_that_can_run(w):
    w.start_scheduler()
    holder(w)
    w.simple("B", "pcworker")
    time.sleep(0.2)
    c = w.simple("C", "worker", fx={"gate": "gC"})     # another provider, free
    w.wait_running(c)


def test_nc_r53_a_ready_node_held_off_past_the_threshold_yields_exactly_one_starving(w):
    w.start_scheduler()
    holder(w)
    b = w.simple("B", "pcworker")
    blocked_on(w, b, "admission:provider_concurrency")
    w.quiet(1.0)
    assert [t for t in w.transitions(b) if t == "starving"] == [], "starving before the threshold"
    w.until(lambda: "starving" in w.transitions(b), timeout=15, what="the starving transition")
    w.quiet(5)
    assert w.transitions(b).count("starving") == 1
    assert w.get(b)["urgent"] is False, "starvation never changes a priority"


def test_nc_r53_a_lock_wait_also_starves(w):
    w.start_scheduler()
    a = w.simple("A", locks=["L"], fx={"gate": "ga"})
    w.wait_running(a)
    b = w.simple("B", locks=["L"])
    blocked_on(w, b, "lock")
    w.until(lambda: "starving" in w.transitions(b), timeout=15, what="starving for a lock wait")
    w.quiet(4)
    assert w.transitions(b).count("starving") == 1


def test_nc_r53_a_node_that_launches_in_time_never_starves(w):
    w.start_scheduler()
    n = w.simple("A", "worker")
    w.wait_state(n, "done")
    w.quiet(5)
    assert "starving" not in w.transitions(n)


def test_nc_r53_the_episode_survives_a_scheduler_restart_with_one_notification(w):
    w.start_scheduler()
    holder(w)
    b = w.simple("B", "pcworker")
    blocked_on(w, b, "admission:provider_concurrency")
    since = w.get(b)["ready_since"]
    assert since
    w.restart_scheduler()
    assert w.get(b)["ready_since"] == since, "ready_since is persisted"
    w.until(lambda: "starving" in w.transitions(b), timeout=20, what="starving")
    w.restart_scheduler()
    w.quiet(5)
    assert w.transitions(b).count("starving") == 1


# ----------------------------------------------------------------- NC-R26 / R68

def test_nc_r26_two_nodes_on_one_lock_never_overlap(w):
    w.start_scheduler()
    a = w.simple("A", locks=["runner.py"], fx={"gate": "ga"})
    w.wait_running(a)
    pid_a = w.wait_spawn("A")["pid"]
    b = w.simple("B", locks=["runner.py"], fx={"watch_pid": pid_a})
    blocked_on(w, b, "lock")
    w.quiet(2)
    assert w.fx.by_tag("B") == []
    w.gate("ga")
    w.wait_state(b, "done")
    assert w.fx.by_tag("B")[0]["watch_alive"] is False
    assert len(w.fx.by_tag("B")) == 1


def test_nc_r26_a_different_lock_or_none_does_not_block(w):
    w.start_scheduler()
    a = w.simple("A", locks=["runner.py"], fx={"gate": "ga"})
    w.wait_running(a)
    other = w.simple("B", locks=["tree.py"], fx={"gate": "gb"})
    free = w.simple("C", fx={"gate": "gc"})
    w.wait_running(other)
    w.wait_running(free)


def test_nc_r26_a_crashed_holder_releases_the_lock(w):
    w.start_scheduler()
    a = w.simple("A", locks=["L"], fx={"crash": True})
    b = w.simple("B", locks=["L"])
    assert w.wait_state(a, "done")["outcome"] == "failed"
    assert w.wait_state(b, "done")["outcome"] == "completed"


def test_nc_r26_cancelling_the_holder_releases_the_lock_only_after_it_is_dead(w):
    w.start_scheduler()
    a = w.simple("A", locks=["L"], fx={"hang": True})
    w.wait_running(a)
    pid_a = w.wait_spawn("A")["pid"]
    b = w.simple("B", locks=["L"], fx={"watch_pid": pid_a})
    blocked_on(w, b, "lock")
    reply = w.cancel(a)
    assert reply["ok"], reply
    w.wait_state(b, "done", timeout=60)
    assert w.fx.by_tag("B")[0]["watch_alive"] is False
    assert w.get(a)["state"] == "cancelled"


def test_nc_r26_a_holder_that_ignores_sigterm_keeps_the_lock_until_it_is_really_dead(w):
    w.start_scheduler()
    a = w.simple("A", locks=["L"], fx={"hang": True, "ignore_term": True})
    w.wait_running(a)
    pid_a = w.wait_spawn("A")["pid"]
    b = w.simple("B", locks=["L"], fx={"watch_pid": pid_a})
    blocked_on(w, b, "lock")
    w.cancel(a)
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline and not w.fx.by_tag("B"):
        if alive(pid_a):
            assert "lock" in blocked_codes(w.get(b)) or w.get(b)["state"] != "open", \
                "lock released while the holder is alive"
        time.sleep(0.2)
    if w.fx.by_tag("B"):
        assert w.fx.by_tag("B")[0]["watch_alive"] is False


def test_nc_r26_r57_a_lock_survives_a_scheduler_restart(w):
    w.start_scheduler()
    a = w.simple("A", locks=["L"], fx={"gate": "ga"})
    w.wait_running(a)
    b = w.simple("B", locks=["L"])
    blocked_on(w, b, "lock")
    w.restart_scheduler()
    w.quiet(3)
    assert w.fx.by_tag("B") == []
    assert "lock" in blocked_codes(w.get(b))


def test_nc_r68_a_lock_set_is_acquired_whole_or_not_at_all(w):
    w.start_scheduler()
    a = w.simple("A", locks=["a"], fx={"gate": "ga"})
    w.wait_running(a)
    x = w.simple("X", locks=["a", "b"], fx={"gate": "gx"})
    blocked_on(w, x, "lock")
    # X waits for `a` but must not be sitting on `b`
    only_b = w.simple("W", locks=["b"], fx={"gate": "gw"})
    w.wait_running(only_b)
    w.gate("ga")
    w.wait_state(a, "done")
    w.quiet(3)
    assert w.fx.by_tag("X") == [], "X launched while W still holds b"
    assert "lock" in blocked_codes(w.get(x))
    w.gate("gw")
    w.wait_state(only_b, "done")
    w.wait_running(x)
    assert len(w.fx.by_tag("X")) == 1
