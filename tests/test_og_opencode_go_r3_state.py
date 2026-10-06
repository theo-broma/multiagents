"""Black-box contract for OG-R3 of context/specs/opencode-go-rename.md.

OG-R3  state migrates once: durable state keyed by provider `opencode` moves to
       `opencode-go` the first time the new code loads it — breaker counts,
       cooldowns, quota/headroom records, spend history, scheduler pins,
       deferred tasks. Atomic, idempotent, nothing lost or double-counted;
       records already under `opencode-go` are MERGED, not overwritten.

Fixtures are PRE-RENAME files written by hand in the shape `tree.py` and
`scheduler/store.py` write today (breaker `provider_health`, `cooldowns`,
`headroom` series, `claims`, `pc_seq`, the `deferred` queue and `pause`, node
records carrying `provider` and `usage`; and the scheduler's `plan.sqlite3`
`nodes` table with `pins.provider`). They are read back through `Tree` and the
live scheduler, and the budget tool.

Silences (assumptions, also reported in the run result). The spec fixes only
"merged, not overwritten" and "nothing double-counted", so a merge is asserted
only through what ANY sound merge satisfies:
- a count is at least the larger of the two and at most their sum;
- a cooldown / pause lasts at least as long as the longer one;
- a series or list holds every sample of both, once each, in time order;
- a set of providers has no duplicates.
"Loading" the tree is `Tree.read()`; the move may also happen at the first
write. The persisted file is checked after one write transaction, so either
timing passes. The scheduler's load is `scheduler start`.
"""
from __future__ import annotations

import copy
import json
import sqlite3
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent / "support"))
import og_support as og  # noqa: E402
from og_support import GO_MODEL, NEW, OLD  # noqa: E402

from multiagents.tree import PC_CAUSE, Tree  # noqa: E402

T0 = time.time()
FUTURE = T0 + 7200
MODEL_B = "opencode-go/kimi-k2.7-code"


def _node(node_id, provider, model, cost, tokens, agent="worker"):
    return {"id": node_id, "agent": agent, "provider": provider, "model": model,
            "parent": None, "depth": 0, "status": "done", "task": "placeholder task",
            "usage": {"total": tokens, "cost_usd": cost}}


def pre_rename() -> dict:
    """A tree.json as the code before the rename wrote it, with every
    provider-keyed record under `opencode` and unrelated ones under `claude`."""
    return {
        "version": 1,
        "nodes": {
            "a1": _node("a1", OLD, GO_MODEL, 0.25, 1000),
            "a2": _node("a2", OLD, GO_MODEL, 0.50, 2000, agent="reviewer"),
            "a3": {**_node("a3", "claude", "sonnet", 1.0, 500),
                   "segments": [{"provider": OLD, "model": MODEL_B,
                                 "usage": {"total": 300, "cost_usd": 0.1}},
                                {"provider": "claude", "model": "sonnet",
                                 "usage": {"total": 200, "cost_usd": 0.9}}]},
            "c1": _node("c1", "claude", "sonnet", 2.0, 4000),
        },
        "provider_health": {
            OLD: {"consecutive_failures": 2, "last_reason": "placeholder failure",
                  "last_kind": "failed", "tripped": T0 - 30, "last_success": T0 - 900},
            "claude": {"consecutive_failures": 0, "last_reason": ""},
        },
        "cooldowns": {
            OLD: {"until": FUTURE, "reason": "quota window full", "cause": "quota"},
            "claude": {"until": FUTURE + 60, "reason": "other", "cause": "quota"},
        },
        "headroom": {
            OLD: [[T0 - 300, 0.90, 1.0], [T0 - 200, 0.80, 2.0], [T0 - 100, 0.70, 3.0]],
            "claude": [[T0 - 100, 0.5, 9.0]],
        },
        "claims": {OLD: [T0 - 20, T0 - 10], "claude": [T0 - 5]},
        "pc_seq": {OLD: 4, "claude": 2},
        "pause": {"until": FUTURE, "reason": "all full", "since": T0 - 60,
                  "providers": ["claude", OLD], "cause": "quota"},
        "deferred": [
            {"id": "df-aaaa01", "spec": {"op": "start", "agent": "worker", "provider": OLD,
                                         "model": GO_MODEL, "task": "deferred placeholder"},
             "retry_after": FUTURE, "reason": "quota", "queued_at": T0 - 50,
             "status": "waiting", "deferred_by": None},
            {"id": "df-aaaa02", "spec": {"op": "start", "agent": "worker", "provider": OLD,
                                         "model": GO_MODEL, "task": "queued placeholder"},
             "cause": PC_CAUSE, "seq": 4, "retry_after": T0 - 40, "reason": "slots",
             "queued_at": T0 - 40, "status": "waiting", "deferred_by": None},
            {"id": "df-aaaa03", "spec": {"op": "start", "agent": "worker", "provider": "claude",
                                         "model": "sonnet", "task": "someone else's"},
             "retry_after": FUTURE, "reason": "quota", "queued_at": T0 - 30,
             "status": "waiting", "deferred_by": None},
            {"id": "df-aaaa04", "spec": {"op": "start", "agent": "worker", "task": "no provider"},
             "retry_after": FUTURE, "reason": "quota", "queued_at": T0 - 20,
             "status": "waiting", "deferred_by": None},
        ],
        "questions": [], "tickets": [],
    }


@pytest.fixture
def tree(tmp_path):
    def make(data: dict) -> Tree:
        path = tmp_path / ".multiagents" / "tree.json"
        og.write_tree_json(path, data)
        return Tree(path, tmp_path / ".multiagents" / "events.jsonl")
    return make


def _providers_in(data: dict) -> set[str]:
    """Every provider name a live, provider-keyed record is filed under.
    Node and segment `provider` fields are history and stay as written."""
    found = set(data.get("provider_health", {})) | set(data.get("cooldowns", {}))
    found |= set(data.get("headroom", {})) | set(data.get("claims", {}))
    found |= set(data.get("pc_seq", {})) | set((data.get("pause") or {}).get("providers", []))
    found |= {(d.get("spec") or {}).get("provider") for d in data["deferred"]}
    found.discard(None)
    return found


def _persisted(tree: Tree) -> dict:
    """What is on disk after one write transaction (any load-time or
    first-write migration has happened by then)."""
    with tree.transaction() as data:
        data.setdefault("claims", data.get("claims", {}))
    return json.loads(tree.path.read_text())


# ===========================================================================
# the move
# ===========================================================================

def test_og_r3_breaker_counts_move_to_opencode_go(tree):
    t = tree(pre_rename())
    health = t.provider_health()
    assert OLD not in health
    assert health[NEW]["consecutive_failures"] == 2
    assert health[NEW]["last_reason"] == "placeholder failure"
    assert health[NEW]["last_kind"] == "failed"
    assert health[NEW]["tripped"] == pytest.approx(T0 - 30)
    assert health[NEW]["last_success"] == pytest.approx(T0 - 900)
    assert health["claude"] == {"consecutive_failures": 0, "last_reason": ""}


def test_og_r3_cooldown_moves_and_still_blocks_opencode_go(tree):
    t = tree(pre_rename())
    assert t.cooldown(OLD) is None
    record = t.cooldown(NEW)
    assert record is not None and record["until"] == pytest.approx(FUTURE)
    assert record["reason"] == "quota window full" and record["cause"] == "quota"
    assert t.cooldown("claude")["until"] == pytest.approx(FUTURE + 60)


def test_og_r3_an_auth_cooldown_moves_with_its_login_flag(tree):
    data = pre_rename()
    data["cooldowns"][OLD] = {
        "auth": {"": {"until": FUTURE, "reason": "token revoked"}},
        "until": FUTURE, "reason": "token revoked", "needs_login": True,
        "cause": "auth", "context": ""}
    t = tree(data)
    assert t.cooldown(OLD) is None
    record = t.cooldown(NEW)
    assert record and record["cause"] == "auth" and record.get("needs_login") is True
    assert record["until"] == pytest.approx(FUTURE)


def test_og_r3_headroom_series_moves_intact_and_burn_reads_it(tree):
    t = tree(pre_rename())
    series = t.read()["headroom"]
    assert OLD not in series
    assert series[NEW] == pre_rename()["headroom"][OLD]
    assert t.burn(NEW)["samples"] == 3 and t.burn(OLD)["samples"] == 0


def test_og_r3_claims_and_concurrency_sequence_move(tree):
    data = tree(pre_rename()).read()
    assert OLD not in data["claims"] and OLD not in data["pc_seq"]
    assert data["claims"][NEW] == [T0 - 20, T0 - 10]
    assert data["pc_seq"][NEW] == 4
    assert data["pc_seq"]["claude"] == 2 and data["claims"]["claude"] == [T0 - 5]


def test_og_r3_pause_names_opencode_go(tree):
    pause = tree(pre_rename()).read()["pause"]
    assert sorted(pause["providers"]) == sorted(["claude", NEW])
    assert pause["until"] == pytest.approx(FUTURE) and pause["cause"] == "quota"


def test_og_r3_deferred_tasks_move_and_keep_everything_else(tree):
    before = pre_rename()["deferred"]
    after = tree(pre_rename()).read()["deferred"]
    assert [d["id"] for d in after] == [d["id"] for d in before]      # none lost, same order
    by_id = {d["id"]: d for d in after}
    assert by_id["df-aaaa01"]["spec"]["provider"] == NEW
    assert by_id["df-aaaa02"]["spec"]["provider"] == NEW
    assert by_id["df-aaaa02"]["seq"] == 4 and by_id["df-aaaa02"]["cause"] == PC_CAUSE
    assert by_id["df-aaaa03"]["spec"]["provider"] == "claude"
    assert "provider" not in by_id["df-aaaa04"]["spec"]
    for old, new in zip(before, after):                       # only the provider changed
        stripped = lambda d: {**d, "spec": {k: v for k, v in d["spec"].items() if k != "provider"}}  # noqa: E731
        assert stripped(old) == stripped(new)


def test_og_r3_a_queued_entry_is_still_counted_for_opencode_go(tree):
    from multiagents.tree import pc_waiting
    after = tree(pre_rename()).read()["deferred"]
    assert [d["id"] for d in pc_waiting(after, NEW)] == ["df-aaaa02"]
    assert pc_waiting(after, OLD) == []


def test_og_r3_spend_history_is_attributed_to_opencode_go_and_not_lost(tree):
    t = tree(pre_rename())
    rows = {(r["provider"], r["model"]): r for r in t.usage_by_model()}
    assert not [k for k in rows if k[0] == OLD], rows
    go = rows[(NEW, GO_MODEL)]
    assert go["runs"] == 2 and go["tokens"] == 3000
    assert go["cost_usd"] == pytest.approx(0.75)
    assert go["agents"] == ["reviewer", "worker"]
    segment = rows[(NEW, MODEL_B)]                            # a segment of a multi-provider node
    assert segment["tokens"] == 300 and segment["cost_usd"] == pytest.approx(0.1)
    assert rows[("claude", "sonnet")]["cost_usd"] == pytest.approx(2.9)
    total = sum(r["cost_usd"] for r in rows.values())
    assert total == pytest.approx(0.25 + 0.5 + 0.1 + 0.9 + 2.0)   # nothing lost or doubled


def test_og_r3_nothing_but_provider_keys_changes(tree):
    before = pre_rename()
    after = tree(copy.deepcopy(before)).read()
    assert NEW in after["provider_health"], "the move did not happen"
    assert set(after["nodes"]) == set(before["nodes"])
    for key in ("questions", "tickets", "version"):
        assert after[key] == before[key]
    assert after["nodes"]["c1"] == before["nodes"]["c1"]
    assert after["provider_health"]["claude"] == before["provider_health"]["claude"]
    assert after["cooldowns"]["claude"] == before["cooldowns"]["claude"]
    assert after["headroom"]["claude"] == before["headroom"]["claude"]


def test_og_r3_a_tree_with_no_old_records_is_left_as_it_is(tree):
    t = tree(pre_rename())
    t.read()
    clean = {"version": 1, "nodes": {"c1": _node("c1", "claude", "sonnet", 2.0, 4000)},
             "deferred": [], "cooldowns": {}, "pause": {}, "provider_health": {},
             "questions": [], "tickets": []}
    t2 = tree(clean)
    assert t2.read() == {**clean}


def test_og_r3_no_record_is_left_under_the_old_name_on_disk(tree):
    t = tree(pre_rename())
    assert OLD not in _providers_in(_persisted(t))


# ===========================================================================
# once: idempotence and repetition
# ===========================================================================

def test_og_r3_loading_twice_changes_nothing(tree):
    t = tree(pre_rename())
    first = t.read()
    assert NEW in first["provider_health"], "the move did not happen"
    second = t.read()
    assert first == second
    assert Tree(t.path, t.events_path).read() == first            # a fresh process


def test_og_r3_persisted_state_is_a_fixed_point(tree):
    t = tree(pre_rename())
    once = _persisted(t)
    twice = _persisted(Tree(t.path, t.events_path))
    assert once == twice
    assert len(twice["headroom"][NEW]) == 3                       # not appended again
    assert twice["provider_health"][NEW]["consecutive_failures"] == 2   # not doubled
    assert len(twice["deferred"]) == 4


def test_og_r3_new_activity_after_the_move_lands_on_opencode_go(tree):
    t = tree(pre_rename())
    t.read()
    t.note_run_outcome(NEW, ok=False, threshold=99, reason="again")
    health = t.provider_health()
    assert health[NEW]["consecutive_failures"] == 3 and OLD not in health
    t.note_headroom(NEW, 0.6, 4.0)
    assert len(t.read()["headroom"][NEW]) == 4


def test_og_r3_a_late_write_naming_the_old_provider_does_not_resurrect_it(tree):
    """A process still running the old code can write under `opencode` after
    the move; the next load folds it in instead of leaving two keys."""
    t = tree(pre_rename())
    _persisted(t)
    with t.transaction() as data:
        data["provider_health"][OLD] = {"consecutive_failures": 1, "last_reason": "late"}
    data = t.read()
    assert OLD not in data["provider_health"]
    assert 2 <= data["provider_health"][NEW]["consecutive_failures"] <= 3


# ===========================================================================
# merge: records already under opencode-go
# ===========================================================================

def merge_case() -> dict:
    data = pre_rename()
    data["provider_health"][NEW] = {"consecutive_failures": 1, "last_reason": "newer",
                                    "last_success": T0 - 100}
    data["cooldowns"][NEW] = {"until": FUTURE + 600, "reason": "weekly", "cause": "quota"}
    data["headroom"][NEW] = [[T0 - 250, 0.85, 1.5], [T0 - 150, 0.75, 2.5]]
    data["claims"][NEW] = [T0 - 15]
    data["pc_seq"][NEW] = 7
    data["pause"]["providers"] = ["claude", OLD, NEW]
    data["nodes"]["g1"] = _node("g1", NEW, GO_MODEL, 0.125, 500, agent="worker")
    data["deferred"].append({
        "id": "df-aaaa05", "spec": {"op": "start", "agent": "worker", "provider": NEW,
                                    "model": GO_MODEL, "task": "already new"},
        "retry_after": FUTURE, "reason": "quota", "queued_at": T0 - 10,
        "status": "waiting", "deferred_by": None})
    return data


def test_og_r3_merge_breaker_keeps_both_histories(tree):
    health = tree(merge_case()).provider_health()
    assert OLD not in health
    merged = health[NEW]
    assert 2 <= merged["consecutive_failures"] <= 3
    assert merged.get("tripped"), "one side had tripped; the merged breaker is still open"
    assert merged["last_success"] == pytest.approx(T0 - 100)       # the later success


def test_og_r3_merge_cooldown_lasts_as_long_as_the_longer(tree):
    t = tree(merge_case())
    record = t.cooldown(NEW)
    assert record is not None and record["until"] >= FUTURE + 600 - 1
    assert t.cooldown(OLD) is None


def test_og_r3_merge_headroom_holds_every_sample_once_in_time_order(tree):
    series = tree(merge_case()).read()["headroom"][NEW]
    expected = sorted(pre_rename()["headroom"][OLD] + [[T0 - 250, 0.85, 1.5], [T0 - 150, 0.75, 2.5]])
    assert series == expected
    assert OLD not in tree(merge_case()).read()["headroom"]


def test_og_r3_merge_claims_pause_and_sequence(tree):
    data = tree(merge_case()).read()
    assert sorted(data["claims"][NEW]) == sorted([T0 - 20, T0 - 10, T0 - 15])
    assert 7 <= data["pc_seq"][NEW] <= 11
    assert OLD not in data["claims"] and OLD not in data["pc_seq"]
    assert sorted(data["pause"]["providers"]) == sorted(["claude", NEW])   # no duplicate


def test_og_r3_merge_spend_is_summed_not_overwritten_or_doubled(tree):
    rows = {(r["provider"], r["model"]): r for r in tree(merge_case()).usage_by_model()}
    go = rows[(NEW, GO_MODEL)]
    assert go["runs"] == 3 and go["tokens"] == 3500
    assert go["cost_usd"] == pytest.approx(0.25 + 0.5 + 0.125)


def test_og_r3_merge_keeps_every_deferred_entry(tree):
    after = tree(merge_case()).read()["deferred"]
    assert sorted(d["id"] for d in after) == [f"df-aaaa0{i}" for i in range(1, 6)]
    providers = {d["id"]: (d["spec"] or {}).get("provider") for d in after}
    assert providers["df-aaaa05"] == NEW and providers["df-aaaa01"] == NEW


def test_og_r3_merge_is_idempotent_and_leaves_nothing_old(tree):
    t = tree(merge_case())
    once = _persisted(t)
    twice = _persisted(Tree(t.path, t.events_path))
    assert once == twice
    assert OLD not in _providers_in(twice)


# ===========================================================================
# scheduler pins
# ===========================================================================

def _plan_db(sched) -> Path:
    return sched.state_dir / "plan.sqlite3"


def _rewrite_pins(sched, mapping: dict[str, dict]) -> None:
    """Edit stored node records as the code before the rename wrote them."""
    db = sqlite3.connect(_plan_db(sched), timeout=30)
    try:
        for node_id, pins in mapping.items():
            (raw,) = db.execute("SELECT record FROM nodes WHERE id=?", (node_id,)).fetchone()
            record = json.loads(raw)
            record["pins"] = pins
            db.execute("UPDATE nodes SET record=? WHERE id=?",
                       (json.dumps(record, sort_keys=True, separators=(",", ":")), node_id))
        db.commit()
    finally:
        db.close()


def _stored_pins(sched) -> dict[str, dict]:
    db = sqlite3.connect(_plan_db(sched), timeout=30)
    try:
        return {i: json.loads(r)["pins"] for i, r in db.execute("SELECT id, record FROM nodes")}
    finally:
        db.close()


@pytest.fixture
def scheduler(tmp_path, monkeypatch):
    from nc_harness import Sched
    sched = Sched(tmp_path, monkeypatch, enabled=True)
    yield sched
    sched.close()


def test_og_r3_scheduler_pins_move_once_and_nothing_else_in_the_plan_changes(scheduler):
    s = scheduler
    s.start()
    a = s.create(task="pinned to the old name")
    b = s.create(task="already new", pins={"provider": NEW, "model": GO_MODEL})
    c = s.create(task="other vendor", pins={"provider": "acme2", "model": "acme2/m1"})
    d = s.create(task="no pin")
    s.stop()
    _rewrite_pins(s, {a["id"]: {"provider": OLD, "model": GO_MODEL, "effort": "high"}})

    s.start()
    first = s.snapshot()
    nodes = {n["id"]: n for n in first["nodes"]}
    assert nodes[a["id"]]["pins"] == {"provider": NEW, "model": GO_MODEL, "effort": "high"}
    assert nodes[b["id"]]["pins"] == {"provider": NEW, "model": GO_MODEL}
    assert nodes[c["id"]]["pins"] == {"provider": "acme2", "model": "acme2/m1"}
    assert nodes[d["id"]]["pins"] == {}
    assert {n["task"] for n in nodes.values()} == {
        "pinned to the old name", "already new", "other vendor", "no pin"}
    assert OLD not in [p.get("provider") for p in _stored_pins(s).values()]

    s.restart()                                           # loaded a second time
    assert s.snapshot() == first
    assert OLD not in [p.get("provider") for p in _stored_pins(s).values()]


# ===========================================================================
# Clarified merge rules (2026-10-06), where records exist under both keys:
#   breaker failures -> the larger value; cooldown/pause -> the later end;
#   samples and history -> united, de-duplicated, in time order.
# These are exact, unlike the range checks above.
# ===========================================================================

def test_og_r3c_merged_breaker_failures_are_the_larger_not_the_sum(tree):
    data = pre_rename()                                  # old side: 2 failures
    data["provider_health"][NEW] = {"consecutive_failures": 1, "last_reason": "newer"}
    assert tree(data).provider_health()[NEW]["consecutive_failures"] == 2


def test_og_r3c_merged_breaker_failures_take_the_larger_when_it_is_the_new_side(tree):
    data = pre_rename()
    data["provider_health"][NEW] = {"consecutive_failures": 5, "last_reason": "newer"}
    health = tree(data).provider_health()
    assert health[NEW]["consecutive_failures"] == 5 and OLD not in health


def test_og_r3c_merged_breaker_failures_when_equal_stay_equal(tree):
    data = pre_rename()
    data["provider_health"][NEW] = {"consecutive_failures": 2, "last_reason": "same"}
    assert tree(data).provider_health()[NEW]["consecutive_failures"] == 2


def test_og_r3c_merged_cooldown_ends_at_the_later_when_the_new_side_is_later(tree):
    t = tree(merge_case())                               # old: FUTURE, new: FUTURE + 600
    assert t.cooldown(NEW)["until"] == pytest.approx(FUTURE + 600)


def test_og_r3c_merged_cooldown_ends_at_the_later_when_the_old_side_is_later(tree):
    data = merge_case()
    data["cooldowns"][NEW] = {"until": FUTURE - 3000, "reason": "shorter", "cause": "quota"}
    t = tree(data)
    assert t.cooldown(NEW)["until"] == pytest.approx(FUTURE)
    assert t.cooldown(OLD) is None


def test_og_r3c_merged_cooldown_is_not_extended_beyond_the_later_end(tree):
    """Not the sum, not now + the longer: the later of the two ends."""
    t = tree(merge_case())
    t.read()
    assert _persisted(t)["cooldowns"][NEW]["until"] == pytest.approx(FUTURE + 600)


def test_og_r3c_merged_pause_ends_at_the_later_of_the_two_ends(tree):
    data = merge_case()
    data["pause"]["providers"] = ["claude", OLD]
    data["pause"]["until"] = FUTURE
    for ends in (FUTURE + 900, FUTURE - 900):
        d = copy.deepcopy(data)
        d["cooldowns"][NEW] = {"until": ends, "reason": "x", "cause": "quota"}
        pause = tree(d).read()["pause"]
        assert pause["until"] >= FUTURE - 1
        assert sorted(pause["providers"]) == sorted(["claude", NEW])


def test_og_r3c_merged_headroom_deduplicates_a_sample_present_under_both_keys(tree):
    data = pre_rename()
    shared = data["headroom"][OLD][1]                    # also recorded under the new name
    data["headroom"][NEW] = [[T0 - 350, 0.95, 0.5], shared, [T0 - 50, 0.6, 4.0]]
    series = tree(data).read()["headroom"][NEW]
    assert series == [[T0 - 350, 0.95, 0.5], [T0 - 300, 0.90, 1.0], [T0 - 200, 0.80, 2.0],
                      [T0 - 100, 0.70, 3.0], [T0 - 50, 0.6, 4.0]]
    assert len([s for s in series if s == shared]) == 1


def test_og_r3c_merged_claims_are_united_deduplicated_and_in_time_order(tree):
    data = pre_rename()
    data["claims"][NEW] = [T0 - 25, T0 - 10, T0 - 5]     # T0 - 10 is also under the old key
    claims = tree(data).read()["claims"][NEW]
    assert claims == [T0 - 25, T0 - 20, T0 - 10, T0 - 5]


def test_og_r3c_merging_twice_adds_nothing(tree):
    data = pre_rename()
    shared = data["headroom"][OLD][1]
    data["headroom"][NEW] = [shared]
    t = tree(data)
    once = _persisted(t)
    twice = _persisted(Tree(t.path, t.events_path))
    assert once == twice
    assert len(twice["headroom"][NEW]) == 3


# ===========================================================================
# What is NOT migrated: the spend ledger and the scheduler's history
# ===========================================================================

LEDGER_AT = 1_773_576_000.0          # 2026-03-15 12:00 UTC, mid-month, fixed


def _ledger_line(key: str, provider: str, usd: float, ts: float = LEDGER_AT) -> str:
    return json.dumps({"kind": "charge", "ts": ts, "key": key, "provider": provider,
                       "model": GO_MODEL, "agent": "worker", "node": "n1", "usd": usd},
                      sort_keys=True) + "\n"


def _write_ledger(tmp_path: Path) -> Path:
    path = tmp_path / ".multiagents" / "spend-ledger.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"kind": "created", "ts": LEDGER_AT - 86400 * 40}) + "\n"
        + _ledger_line("k1", OLD, 1.25)
        + _ledger_line("k2", NEW, 0.5)
        + _ledger_line("k3", "claude", 4.0))
    return path


def test_og_r3c_ledger_readers_count_old_entries_as_opencode_go(tmp_path):
    from multiagents.spendcap import Ledger
    ledger = Ledger(_write_ledger(tmp_path))
    ledger.refresh()
    assert ledger.spend(NEW, "", "month", LEDGER_AT) == pytest.approx(1.75)
    assert ledger.spend(NEW, GO_MODEL, "month", LEDGER_AT) == pytest.approx(1.75)
    assert ledger.spend("claude", "", "month", LEDGER_AT) == pytest.approx(4.0)
    assert ledger.providers_seen() == {NEW, "claude"}, ledger.providers_seen()


def test_og_r3c_ledger_file_is_byte_unchanged_by_reading_it(tmp_path):
    from multiagents.spendcap import Ledger
    path = _write_ledger(tmp_path)
    before = path.read_bytes()
    ledger = Ledger(path)
    ledger.refresh()
    ledger.poll()
    ledger.spend(NEW, "", "month", LEDGER_AT)
    ledger.providers_seen()
    assert path.read_bytes() == before
    assert OLD.encode() in before and b'"provider": "opencode"' in before   # still there


def test_og_r3c_ledger_stays_append_only_when_a_charge_is_added(tmp_path):
    from multiagents.spendcap import Cap, Ledger
    path = _write_ledger(tmp_path)
    before = path.read_bytes()
    ledger = Ledger(path)
    ledger.charge(key="k4", provider=NEW, model=GO_MODEL, agent="worker", node="n2",
                  usd=0.25, caps=[Cap(NEW, "", 100.0, "month")], at=LEDGER_AT + 60)
    after = path.read_bytes()
    assert after.startswith(before), "an existing ledger line was rewritten"
    assert len(after) > len(before)
    fresh = Ledger(path)
    fresh.refresh()
    assert fresh.spend(NEW, "", "month", LEDGER_AT + 60) == pytest.approx(2.0)


def test_og_r3c_an_old_entry_counts_toward_a_cap_on_opencode_go(tmp_path):
    from multiagents.spendcap import Cap, Ledger
    ledger = Ledger(_write_ledger(tmp_path))
    ledger.refresh()
    state = ledger.describe(Cap(NEW, "", 1.5, "month"), LEDGER_AT)
    assert state["spend"] == pytest.approx(1.75) and state["reached"] is True


def test_og_r3c_a_charge_is_not_double_counted_when_both_names_carry_one_key(tmp_path):
    from multiagents.spendcap import Ledger
    path = tmp_path / ".multiagents" / "spend-ledger.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(_ledger_line("same", OLD, 1.0) + _ledger_line("same", NEW, 1.0))
    ledger = Ledger(path)
    ledger.refresh()
    assert ledger.spend(NEW, "", "month", LEDGER_AT) == pytest.approx(1.0)


def test_og_r3c_scheduler_attempt_and_history_records_keep_the_old_name(scheduler):
    """Only live routing keys move. An attempt record in the plan database is
    history: it is read back exactly as written."""
    s = scheduler
    s.start()
    a = s.create(task="has history")
    s.stop()
    db = sqlite3.connect(_plan_db(s), timeout=30)
    try:
        (raw,) = db.execute("SELECT record FROM nodes WHERE id=?", (a["id"],)).fetchone()
        record = json.loads(raw)
        record["pins"] = {"provider": OLD, "model": GO_MODEL}
        record["attempts"] = [{"n": 1, "provider": OLD, "model": GO_MODEL, "outcome": "failed"}]
        db.execute("UPDATE nodes SET record=? WHERE id=?",
                   (json.dumps(record, sort_keys=True, separators=(",", ":")), a["id"]))
        db.commit()
    finally:
        db.close()
    s.start()
    db = sqlite3.connect(_plan_db(s), timeout=30)
    try:
        (raw,) = db.execute("SELECT record FROM nodes WHERE id=?", (a["id"],)).fetchone()
    finally:
        db.close()
    stored = json.loads(raw)
    assert stored["pins"]["provider"] == NEW                      # live routing key moved
    assert stored["attempts"] == [{"n": 1, "provider": OLD, "model": GO_MODEL,
                                   "outcome": "failed"}]          # history did not
