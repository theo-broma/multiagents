"""M5 — time windows: NC-R38 (spec, half-open ranges, midnight, DST by local
wall time, intersection inheritance, `empty_window` over 14 days), the
`window`/`empty_window` codes of NC-R22/R52, and NC-R2's `timezone`.

Contract: `context/specs/phase7-part1-contract.md`, revision sections included.
Time is the clock seam of NC-R82 (`scheduler start --clock-file`); a test moves
"now" by rewriting that file and waits only for the scheduler's own tick.

How membership is observed (black box): `get_node` returns `ready` and
`blocked` (NC-R52/R23). To read `ready` without the node racing into `running`,
every table node names lock `L`, which a gated holder node keeps: such a node
is `ready` when its window is open but never launches. "Closed" = `ready`
false with `window` in `blocked`; "open" = `ready` true and neither `window`
nor `empty_window` in `blocked`.

Assumptions where the contract is silent (kept loose):
- `get_node` evaluates windows at the clock's current instant (the test waits
  one tick after moving the clock anyway).
- day names are the three-letter lowercase `mon..sun` of the contract; a
  window without `timezone` uses `scheduler.timezone` (default Europe/Paris).
- `empty_window` is a transition `node.empty_window` naming the node.
- composites (`group`) are created with `children: [ids]` (NC-R73); only the
  window of the children is read here, so no M4 behaviour is needed.
"""
from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from nc_fixture.clock import ALL_DAYS, ClockWorld, local, utc  # noqa: E402
from nc_fixture.world import err_code, unwrap  # noqa: E402

NEUTRAL = local(2026, 10, 5, 12, 0)          # Monday noon, Paris

# Every wait is bounded explicitly: a red test must fail in seconds, not on the
# harness's 30 s default. WAIT covers one scheduler reaction (tick_seconds is 1).
WAIT = 8


@pytest.fixture
def w(tmp_path, monkeypatch):
    world = ClockWorld(tmp_path, monkeypatch, now=NEUTRAL,
                       scheduler={"starvation_after_seconds": 10**7})
    world.start_scheduler()
    yield world
    world.close()


def hold_lock(w: ClockWorld, lock: str = "L") -> str:
    """`ClockWorld.hold_lock` with a bounded wait for the holder to run."""
    holder = w.simple("HOLDER", locks=[lock], fx={"gate": "holder"})
    w.wait_running(holder, timeout=WAIT)
    return holder


def win(days, *ranges, tz=None):
    spec = {"days": list(days), "ranges": list(ranges)}
    if tz:
        spec["timezone"] = tz
    return spec


def table(w: ClockWorld, spec: dict, rows: list[tuple]) -> None:
    """Check every `(instant, expected_open)` against one node's window."""
    hold_lock(w, "L")
    node = w.simple("T", locks=["L"], window=spec)
    for instant, expected in rows:
        w.set_clock(instant)
        assert w.is_open(node) is expected, \
            f"window {spec} at {instant.isoformat()}: expected {'open' if expected else 'closed'}"


# ------------------------------------------------------- half-open ranges

def test_nc_r38_ranges_are_half_open_start_inclusive_end_exclusive(w):
    table(w, win(ALL_DAYS, "09:00-17:00"), [
        (local(2026, 10, 5, 8, 59, 59), False),
        (local(2026, 10, 5, 9, 0, 0), True),
        (local(2026, 10, 5, 16, 59, 59), True),
        (local(2026, 10, 5, 17, 0, 0), False),
        (local(2026, 10, 5, 17, 0, 1), False),
    ])


def test_nc_r38_the_days_list_restricts_the_window(w):
    table(w, win(["mon", "wed"], "09:00-17:00"), [
        (local(2026, 10, 5, 10), True),      # Monday
        (local(2026, 10, 6, 10), False),     # Tuesday
        (local(2026, 10, 7, 10), True),      # Wednesday
        (local(2026, 10, 10, 10), False),    # Saturday
    ])


def test_nc_r38_several_ranges_are_a_union(w):
    table(w, win(["mon"], "06:00-08:00", "13:00-14:00"), [
        (local(2026, 10, 5, 5, 59), False),
        (local(2026, 10, 5, 6, 0), True),
        (local(2026, 10, 5, 8, 0), False),
        (local(2026, 10, 5, 12, 59), False),
        (local(2026, 10, 5, 13, 0), True),
        (local(2026, 10, 5, 14, 0), False),
    ])


def test_nc_r38_00_00_to_24_00_is_the_whole_day_and_not_the_next(w):
    table(w, win(["wed"], "00:00-24:00"), [
        (local(2026, 10, 6, 23, 59, 59), False),
        (local(2026, 10, 7, 0, 0, 0), True),
        (local(2026, 10, 7, 23, 59, 59), True),
        (local(2026, 10, 8, 0, 0, 0), False),
    ])


def test_nc_r38_a_range_ending_at_24_00_closes_at_midnight(w):
    table(w, win(["mon"], "12:00-24:00"), [
        (local(2026, 10, 5, 11, 59), False),
        (local(2026, 10, 5, 12, 0), True),
        (local(2026, 10, 5, 23, 59, 59), True),
        (local(2026, 10, 6, 0, 0), False),
    ])


# ------------------------------------------------------ crossing midnight

def test_nc_r38_a_range_crossing_midnight_belongs_to_the_start_day(w):
    table(w, win(["fri"], "22:00-06:00"), [
        (local(2026, 10, 8, 23, 0), False),      # Thursday night: Thursday is not listed
        (local(2026, 10, 9, 3, 0), False),       # Friday 03:00 is the tail of Thursday's
        (local(2026, 10, 9, 21, 59, 59), False),
        (local(2026, 10, 9, 22, 0, 0), True),
        (local(2026, 10, 9, 23, 59, 59), True),
        (local(2026, 10, 10, 0, 0, 0), True),    # Saturday 00:00 is Friday's night
        (local(2026, 10, 10, 5, 59, 59), True),
        (local(2026, 10, 10, 6, 0, 0), False),
        (local(2026, 10, 10, 22, 0), False),     # Saturday night: Saturday is not listed
    ])


def test_nc_r38_sunday_night_runs_into_monday_and_is_not_a_monday_window(w):
    table(w, win(["sun"], "23:00-02:00"), [
        (local(2026, 10, 11, 22, 59), False),
        (local(2026, 10, 11, 23, 0), True),
        (local(2026, 10, 12, 1, 59, 59), True),    # Monday 01:59
        (local(2026, 10, 12, 2, 0), False),
        (local(2026, 10, 12, 23, 30), False),      # Monday night is Monday's
        (local(2026, 10, 5, 1, 0), True),          # the Monday before is last Sunday's tail
    ])


def test_nc_r38_a_range_ending_at_00_00_crosses_midnight_and_ends_at_it(w):
    table(w, win(["fri"], "23:00-00:00"), [
        (local(2026, 10, 9, 22, 59), False),
        (local(2026, 10, 9, 23, 0), True),
        (local(2026, 10, 9, 23, 59, 59), True),
        (local(2026, 10, 10, 0, 0), False),
    ])


# --------------------------------------------------------------- the zone

def test_nc_r38_membership_is_decided_in_the_windows_own_timezone(w):
    # Monday 2026-10-05: New York is UTC-4 (EDT), Paris UTC+2.
    table(w, win(["mon"], "09:00-17:00", tz="America/New_York"), [
        (utc(2026, 10, 5, 12, 59), False),     # 08:59 in New York (14:59 in Paris)
        (utc(2026, 10, 5, 13, 0), True),       # 09:00 in New York
        (utc(2026, 10, 5, 20, 59), True),
        (utc(2026, 10, 5, 21, 0), False),      # 17:00 in New York
    ])


def test_nc_r38_a_window_without_a_zone_uses_the_scheduler_timezone_key(tmp_path, monkeypatch):
    world = ClockWorld(tmp_path, monkeypatch, now=utc(2026, 10, 5, 12, 0),
                       scheduler={"timezone": "Asia/Tokyo",
                                  "starvation_after_seconds": 10**7})
    try:
        world.start_scheduler()
        table(world, win(["tue"], "09:00-17:00"), [
            (utc(2026, 10, 5, 23, 59), False),     # Tue 08:59 in Tokyo (UTC+9)
            (utc(2026, 10, 6, 0, 0), True),        # Tue 09:00 in Tokyo
            (utc(2026, 10, 6, 7, 59), True),
            (utc(2026, 10, 6, 8, 0), False),       # Tue 17:00 in Tokyo
        ])
    finally:
        world.close()


def test_nc_r38_the_default_zone_is_europe_paris(w):
    table(w, win(["mon"], "09:00-17:00"), [
        (utc(2026, 10, 5, 6, 59), False),      # 08:59 CEST
        (utc(2026, 10, 5, 7, 0), True),        # 09:00 CEST
        (utc(2026, 10, 5, 14, 59), True),
        (utc(2026, 10, 5, 15, 0), False),
    ])


def test_nc_r38_a_node_without_a_window_is_never_blocked_by_one(w):
    hold_lock(w, "L")
    node = w.simple("T", locks=["L"])
    for instant in (local(2026, 10, 5, 3), local(2026, 10, 10, 23), local(2026, 10, 11, 12)):
        w.set_clock(instant)
        assert w.is_open(node)


# ---------------------------------------------------- DST: 2026 Paris
# Spring forward: Sun 2026-03-29, 02:00 CET -> 03:00 CEST (01:00Z).
# Fall back:      Sun 2026-10-25, 03:00 CEST -> 02:00 CET (01:00Z).

def test_nc_r38_dst_spring_a_range_inside_the_gap_never_occurs(w):
    # Local 02:00-03:00 does not exist on 2026-03-29.
    table(w, win(["sun"], "02:00-03:00"), [
        (utc(2026, 3, 29, 0, 59), False),      # 01:59 CET
        (utc(2026, 3, 29, 1, 0), False),       # 03:00 CEST
        (utc(2026, 3, 29, 1, 30), False),
        (utc(2026, 3, 22, 0, 59), False),      # an ordinary Sunday: 01:59 CET
        (utc(2026, 3, 22, 1, 0), True),        # 02:00 CET
        (utc(2026, 3, 22, 1, 59), True),
        (utc(2026, 3, 22, 2, 0), False),
    ])


def test_nc_r38_dst_spring_a_range_straddling_the_gap_opens_at_the_local_wall_time(w):
    table(w, win(["sun"], "02:30-04:00"), [
        (utc(2026, 3, 29, 0, 59), False),      # 01:59 CET: before 02:30
        (utc(2026, 3, 29, 1, 0), True),        # wall time 03:00 CEST is inside [02:30, 04:00)
        (utc(2026, 3, 29, 1, 59), True),       # 03:59 CEST
        (utc(2026, 3, 29, 2, 0), False),       # 04:00 CEST
    ])


def test_nc_r38_dst_spring_the_all_day_window_of_the_short_day(w):
    table(w, win(["sun"], "00:00-24:00"), [
        (utc(2026, 3, 28, 22, 59), False),     # Sat 23:59 CET
        (utc(2026, 3, 28, 23, 0), True),       # Sun 00:00 CET
        (utc(2026, 3, 29, 1, 0), True),        # across the gap
        (utc(2026, 3, 29, 21, 59), True),      # Sun 23:59 CEST
        (utc(2026, 3, 29, 22, 0), False),      # Mon 00:00 CEST
    ])


def test_nc_r38_dst_spring_a_night_window_across_the_change_is_one_hour_shorter(w):
    table(w, win(["sat"], "22:00-06:00"), [
        (utc(2026, 3, 28, 20, 59), False),     # Sat 21:59 CET
        (utc(2026, 3, 28, 21, 0), True),       # Sat 22:00 CET
        (utc(2026, 3, 29, 3, 59), True),       # Sun 05:59 CEST
        (utc(2026, 3, 29, 4, 0), False),       # Sun 06:00 CEST
    ])


def test_nc_r38_dst_fall_the_repeated_hour_counts_both_times(w):
    # Local 02:00-03:00 occurs twice on 2026-10-25: 00:00Z-01:00Z (CEST), 01:00Z-02:00Z (CET).
    table(w, win(["sun"], "02:00-03:00"), [
        (utc(2026, 10, 24, 23, 59), False),    # 01:59 CEST
        (utc(2026, 10, 25, 0, 0), True),       # 02:00 CEST
        (utc(2026, 10, 25, 0, 59), True),      # 02:59 CEST
        (utc(2026, 10, 25, 1, 0), True),       # 02:00 CET (the second one)
        (utc(2026, 10, 25, 1, 59), True),      # 02:59 CET
        (utc(2026, 10, 25, 2, 0), False),      # 03:00 CET
    ])


def test_nc_r38_dst_fall_a_range_inside_the_repeated_hour_opens_twice(w):
    table(w, win(["sun"], "02:30-02:45"), [
        (utc(2026, 10, 25, 0, 29), False),
        (utc(2026, 10, 25, 0, 30), True),      # 02:30 CEST
        (utc(2026, 10, 25, 0, 45), False),     # 02:45 CEST
        (utc(2026, 10, 25, 1, 0), False),      # 02:00 CET
        (utc(2026, 10, 25, 1, 30), True),      # 02:30 CET
        (utc(2026, 10, 25, 1, 44), True),
        (utc(2026, 10, 25, 1, 45), False),
    ])


def test_nc_r38_dst_fall_the_all_day_window_of_the_long_day(w):
    table(w, win(["sun"], "00:00-24:00"), [
        (utc(2026, 10, 24, 21, 59), False),    # Sat 23:59 CEST
        (utc(2026, 10, 24, 22, 0), True),      # Sun 00:00 CEST
        (utc(2026, 10, 25, 1, 30), True),
        (utc(2026, 10, 25, 22, 59), True),     # Sun 23:59 CET
        (utc(2026, 10, 25, 23, 0), False),     # Mon 00:00 CET
    ])


def test_nc_r38_dst_fall_a_night_window_across_the_change_is_one_hour_longer(w):
    table(w, win(["sat"], "22:00-06:00"), [
        (utc(2026, 10, 24, 19, 59), False),    # Sat 21:59 CEST
        (utc(2026, 10, 24, 20, 0), True),      # Sat 22:00 CEST
        (utc(2026, 10, 25, 4, 59), True),      # Sun 05:59 CET
        (utc(2026, 10, 25, 5, 0), False),      # Sun 06:00 CET
    ])


# ------------------------------------------------------- invalid windows

INVALID = {
    "unknown_zone": win(ALL_DAYS, "09:00-17:00", tz="Mars/Base"),
    "malformed_range": win(ALL_DAYS, "9-17"),
    "no_colon": win(ALL_DAYS, "0900-1700"),
    "hour_out_of_range": win(ALL_DAYS, "09:00-25:00"),
    "minute_out_of_range": win(ALL_DAYS, "09:60-17:00"),
    "start_equals_end": win(ALL_DAYS, "09:00-09:00"),
    "midnight_to_midnight_zero": win(ALL_DAYS, "00:00-00:00"),
    "empty_days": win([], "09:00-17:00"),
    "unknown_day": win(["funday"], "09:00-17:00"),
    "one_bad_range_among_good": win(ALL_DAYS, "09:00-12:00", "13:00-13:00"),
    "not_a_string_range": win(ALL_DAYS, 900),
}


@pytest.mark.parametrize("name", sorted(INVALID))
def test_nc_r38_an_invalid_window_is_refused_on_create_and_changes_nothing(w, name):
    before = w.ok("list_nodes", {})
    reply = w.create({"kind": "simple", "agent": "worker", "task": "t",
                      "window": INVALID[name]})
    assert not reply.get("ok") and err_code(reply) == "invalid", reply
    after = w.ok("list_nodes", {})
    assert after["nodes"] == before["nodes"]
    assert after["plan_revision"] == before["plan_revision"]


@pytest.mark.parametrize("name", ["unknown_zone", "start_equals_end", "empty_days"])
def test_nc_r38_an_invalid_window_is_refused_on_update_and_leaves_the_node_alone(w, name):
    hold_lock(w, "L")
    node = w.simple("T", locks=["L"], window=win(ALL_DAYS, "09:00-17:00"))
    before = w.get(node)
    reply = w.rpc("update_node", {"id": node, "revision": before["revision"],
                                  "window": INVALID[name]})
    assert not reply.get("ok") and err_code(reply) == "invalid", reply
    after = w.get(node)
    assert after["window"] == before["window"]
    assert after["revision"] == before["revision"]


def test_nc_r38_a_window_can_be_removed_with_update_and_the_node_is_then_always_open(w):
    hold_lock(w, "L")
    node = w.simple("T", locks=["L"], window=win(["sat"], "09:00-10:00"))
    w.set_clock(local(2026, 10, 5, 12))
    assert not w.is_open(node)
    rev = w.get(node)["revision"]
    w.ok("update_node", {"id": node, "revision": rev, "window": None})
    assert w.get(node)["window"] is None
    assert w.is_open(node)


def test_nc_r38_updating_a_window_takes_effect_on_the_next_evaluation(w):
    hold_lock(w, "L")
    node = w.simple("T", locks=["L"], window=win(["mon"], "09:00-10:00"))
    w.set_clock(local(2026, 10, 5, 12))
    assert not w.is_open(node)
    rev = w.get(node)["revision"]
    w.ok("update_node", {"id": node, "revision": rev,
                         "window": win(["mon"], "11:00-13:00")})
    w.set_clock(local(2026, 10, 5, 12))
    assert w.is_open(node)


# ------------------------------------------------------------ inheritance

def group_with(w: ClockWorld, children: list[str], window: dict | None = None,
               parent_children_ok: bool = True) -> str:
    fields = {"kind": "group", "children": children}
    if window is not None:
        fields["window"] = window
    reply = w.create(fields)
    assert reply.get("ok"), reply
    return unwrap(reply["result"])["id"]


def test_nc_r38_the_effective_window_is_the_intersection_with_the_parents(w):
    hold_lock(w, "L")
    child = w.simple("C", locks=["L"], window=win(ALL_DAYS, "12:00-20:00"))
    group_with(w, [child], win(["mon", "tue", "wed", "thu", "fri"], "09:00-17:00"))
    # Effective: Mon-Fri 12:00-17:00
    for instant, expected in [
        (local(2026, 10, 5, 11, 59), False),    # inside parent, before child
        (local(2026, 10, 5, 12, 0), True),
        (local(2026, 10, 5, 16, 59, 59), True),
        (local(2026, 10, 5, 17, 0), False),     # inside child, after parent
        (local(2026, 10, 5, 19, 0), False),
        (local(2026, 10, 10, 13, 0), False),    # Saturday: outside the parent's days
    ]:
        w.set_clock(instant)
        assert w.is_open(child) is expected, instant.isoformat()


def test_nc_r38_a_child_without_a_window_inherits_the_parents(w):
    hold_lock(w, "L")
    child = w.simple("C", locks=["L"])
    group_with(w, [child], win(["mon"], "09:00-10:00"))
    for instant, expected in [(local(2026, 10, 5, 9, 30), True),
                              (local(2026, 10, 5, 10, 0), False),
                              (local(2026, 10, 6, 9, 30), False)]:
        w.set_clock(instant)
        assert w.is_open(child) is expected, instant.isoformat()


def test_nc_r38_the_intersection_runs_through_every_ancestor(w):
    hold_lock(w, "L")
    leaf = w.simple("C", locks=["L"], window=win(ALL_DAYS, "08:00-18:00"))
    mid = group_with(w, [leaf], win(ALL_DAYS, "10:00-20:00"))
    group_with(w, [mid], win(ALL_DAYS, "06:00-14:00"))
    # Effective: 10:00-14:00 every day
    for instant, expected in [(local(2026, 10, 5, 9, 59), False),
                              (local(2026, 10, 5, 10, 0), True),
                              (local(2026, 10, 5, 13, 59), True),
                              (local(2026, 10, 5, 14, 0), False)]:
        w.set_clock(instant)
        assert w.is_open(leaf) is expected, instant.isoformat()


def test_nc_r38_intersection_is_of_instants_not_of_day_labels(w):
    # Parent: Friday 22:00 -> Saturday 02:00. Child: Saturday 00:00-01:00.
    # Saturday 00:00-01:00 is in both (the parent's tail), so the window is
    # not empty and it is open exactly there.
    hold_lock(w, "L")
    child = w.simple("C", locks=["L"], window=win(["sat"], "00:00-01:00"))
    group_with(w, [child], win(["fri"], "22:00-02:00"))
    for instant, expected in [(local(2026, 10, 9, 23, 30), False),
                              (local(2026, 10, 10, 0, 0), True),
                              (local(2026, 10, 10, 0, 59, 59), True),
                              (local(2026, 10, 10, 1, 0), False)]:
        w.set_clock(instant)
        assert w.is_open(child) is expected, instant.isoformat()
    assert "empty_window" not in w.codes(child)


def test_nc_r38_a_narrow_nonempty_intersection_is_window_not_empty_window(w):
    hold_lock(w, "L")
    child = w.simple("C", locks=["L"], window=win(["mon"], "09:30-09:45"))
    group_with(w, [child], win(["mon"], "09:00-10:00"))
    w.set_clock(local(2026, 10, 5, 12))
    codes = w.codes(child)
    assert "window" in codes and "empty_window" not in codes
    w.set_clock(local(2026, 10, 5, 9, 40))
    assert w.is_open(child)
    assert w.kinds().count("node.empty_window") == 0


# ---------------------------------------------------------- empty_window

def empty_pair(w: ClockWorld, parent_win: dict, child_win: dict) -> str:
    hold_lock(w, "L")
    child = w.simple("C", locks=["L"], window=child_win)
    group_with(w, [child], parent_win)
    return child


EMPTY_CASES = {
    "disjoint_days": (win(["mon", "tue", "wed"], "09:00-12:00"), win(["thu", "fri"], "09:00-12:00")),
    "disjoint_times": (win(ALL_DAYS, "09:00-12:00"), win(ALL_DAYS, "13:00-15:00")),
    "adjacent_half_open": (win(ALL_DAYS, "09:00-12:00"), win(ALL_DAYS, "12:00-15:00")),
}


@pytest.mark.parametrize("name", sorted(EMPTY_CASES))
def test_nc_r38_an_empty_intersection_blocks_the_node_empty_window_and_notifies_once(w, name):
    child = empty_pair(w, *EMPTY_CASES[name])
    w.set_clock(local(2026, 10, 5, 10))
    codes = w.codes(child)
    assert "empty_window" in codes, codes
    assert w.get(child)["ready"] is False
    # many evaluations, in several instants: still one notification
    for hours in (30, 77, 120):
        w.set_clock(local(2026, 10, 5, 10) + timedelta(hours=hours))
    transitions = [t for t in w.events() if w.kind_of(t) == "node.empty_window"
                   and child in str(t)]
    assert len(transitions) == 1, transitions
    assert "empty_window" in w.codes(child)


def test_nc_r38_an_empty_window_node_never_launches_even_when_everything_else_is_free(w):
    child = w.simple("C", window=win(["mon"], "09:00-10:00"))
    group_with(w, [child], win(["tue"], "09:00-10:00"))
    for instant in (local(2026, 10, 5, 9, 30), local(2026, 10, 6, 9, 30)):
        w.set_clock(instant)
    assert w.fx.by_tag("C") == []
    assert w.get(child)["state"] == "open"


def test_nc_r38_a_far_but_nonempty_window_is_window_not_empty_window(w):
    # Opens once a week, on Sunday: 6 days away is inside the 14-day horizon.
    hold_lock(w, "L")
    node = w.simple("C", locks=["L"], window=win(["sun"], "03:00-03:30"))
    w.set_clock(local(2026, 10, 5, 12))      # Monday
    codes = w.codes(node)
    assert "window" in codes and "empty_window" not in codes


def test_nc_r38_repairing_an_empty_window_unblocks_the_node(w):
    child = empty_pair(w, win(["tue"], "09:00-10:00"), win(["mon"], "09:00-10:00"))
    w.set_clock(local(2026, 10, 5, 9, 30))
    assert "empty_window" in w.codes(child)
    rev = w.get(child)["revision"]
    w.ok("update_node", {"id": child, "revision": rev, "window": win(["mon", "tue"], "09:00-10:00")})
    w.set_clock(local(2026, 10, 6, 9, 30))
    assert w.is_open(child)
    assert "empty_window" not in w.codes(child)


def test_nc_r38_empty_window_notification_survives_a_scheduler_restart_without_repeating(w):
    child = empty_pair(w, *EMPTY_CASES["disjoint_days"])
    w.set_clock(local(2026, 10, 5, 10))
    assert "empty_window" in w.codes(child)
    w.restart_scheduler()
    w.set_clock(local(2026, 10, 6, 10))
    assert "empty_window" in w.codes(child)
    assert [w.kind_of(e) for e in w.events() if child in str(e)].count("node.empty_window") == 1


# --------------------------------------------- windows gate launching

def test_nc_r38_a_node_outside_its_window_does_not_launch_and_launches_when_it_opens(w):
    node = w.simple("W1", window=win(["mon"], "13:00-14:00"))
    w.set_clock(local(2026, 10, 5, 12, 59))
    assert w.fx.by_tag("W1") == []
    assert w.get(node)["state"] == "open"
    assert "window" in w.codes(node)
    w.set_clock(local(2026, 10, 5, 13, 0, 5))
    done = w.wait_state(node, "done", timeout=WAIT)
    assert done["outcome"] == "completed"
    assert len(w.fx.by_tag("W1")) == 1


def test_nc_r69_a_node_deposited_while_the_scheduler_is_down_with_a_closed_window_stays_open(w):
    w.stop_scheduler()
    w.write_clock(local(2026, 10, 5, 8))
    w.start_scheduler()
    node = w.simple("W3", window=win(["mon"], "09:00-17:00"))
    w.quiet(2.5)
    assert w.fx.by_tag("W3") == []
    w.set_clock(local(2026, 10, 5, 9, 0, 1))
    assert w.wait_state(node, "done", timeout=WAIT)["outcome"] == "completed"
