"""The contract for phase 3 item 1 — the cache that hands out its own object.

`context/specs/phase3-cache-aliasing.md`, R16/R17/R18, including both
amendments. Findings: F122 and F171 (`context/review/C2-budget.md`), F150 and
F154 (`context/review/C2-budget-adversary.md`).

Written against behaviour that does not exist yet; every test here except the
two noted in the result is expected to be red until `budget.py` changes.

Three things this file deliberately does, each of which it would be wrong to
"tidy" later:

- **It never compares two `Budget`s with `==`.** `Budget` is a dataclass, so
  `==` is value equality, which is satisfied by the very aliasing R16 removes.
  Identity (`is not`) and independent mutation are what R16 is about.
- **It mutates `spent` and `windows` by item assignment**
  (`returned.spent["k"] = v`), never by rebinding the attribute. Rebinding
  passes against a shallow `copy.copy()` whose `.spent` is still the cache's
  own dict — and `read_claude` writes by item assignment
  (`budget.spent["extra_credits_used"] = ...`), which is exactly the shape
  that leaks. See the spec's first amendment.
- **It opens one private name, deliberately**: `budget._BUILTIN`, via
  `monkeypatch.setitem`, to register a reader for a made-up provider. This is
  a seam, not an assertion — nothing here asserts anything *about* `_BUILTIN`.
  It is needed because `_from_script` has no `spent` key, so a script-backed
  provider can never report spend, and R18's required shape (reader and caller
  both contributing at once) is reachable only through the built-in path. The
  real `read_claude` cannot be used: it would read the developer's own
  `~/.claude.json`. Sanctioned by Amendment 2 of the spec.

`budget._cache` is a bare module dict (F100's neighbourhood) — every test
below calls `h.invalidate_cache()` on the way in, as the rest of the C2 suite
does.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import c2_harness as h  # noqa: E402


# A script that answers `budget` with a plain, usable reading and nothing else:
# no note, no spend (a script cannot report spend), severity derived from
# headroom as "normal".
_PLAIN = 'budget) printf \'{"known": true, "headroom": 0.5}\'; exit 0 ;;'

# The same, plus one window and a note of its own, for the tests that need
# something in `windows` to mutate or a note to overwrite.
_RICH = ('budget) printf \'{"known": true, "headroom": 0.42, '
         '"resets_at": "2030-01-01T00:00:00Z", '
         '"windows": {"weekly": {"headroom": 0.42}}, '
         '"note": "from the script"}\'; exit 0 ;;')


def _counting(calls: Path, arm: str) -> str:
    """`arm` with a byte appended to `calls` every time the script runs."""
    return arm.replace("budget)", f'budget) echo -n x >> "{calls}";', 1)


def _executor_for(_name):
    return h.FakeExecutor()


# What the fake built-in reader reports as its own spend — the shape
# `read_claude` really writes (`budget.py:489-490`).
READER_SPENT = {"extra_credits_used": 7, "extra_credits_limit": 100}


def _builtin_reader(monkeypatch, calls: list | None = None) -> str:
    """Register a built-in budget reader under a made-up provider name.

    Returns the provider name. No script of that name is shipped or written,
    so `_from_script` answers None and the built-in is what runs.

    `fake(*args, **kwargs)` on purpose: today's dispatch calls a non-claude
    built-in as `builtin(spent)` and `read_claude` as `builtin()`, and F120
    (out of scope here) may change the argument list again. The fake ignores
    whatever it is passed — a reader reports the READER's figures; the
    caller's `spent` is the caller's to contribute.

    A NEW `Budget` with fresh dicts every call, so that any aliasing a test
    observes came from the cache and not from this fake handing out one
    object twice.
    """
    name = "creditreader"

    def fake(*_args, **_kwargs):
        if calls is not None:
            calls.append(1)
        return h.Budget(
            provider=name, known=True, headroom=0.5, severity="normal",
            source="builtin", spent=dict(READER_SPENT),
            windows={"weekly": {"headroom": 0.5}},
        )

    monkeypatch.setitem(h.budget_mod._BUILTIN, name, fake)   # the sanctioned seam
    return name


# ===========================================================================
# R16 — what `read_provider` returns is never the object the cache holds
# ===========================================================================

def test_r16_two_successive_cache_hits_are_not_the_same_object(tmp_path):
    h.invalidate_cache()
    provider = h.make_provider("p")
    h.case_script(tmp_path, "p.sh", _PLAIN)

    first = h.read_provider("p", provider, h.FakeExecutor(), tmp_path)
    second = h.read_provider("p", provider, h.FakeExecutor(), tmp_path)
    third = h.read_provider("p", provider, h.FakeExecutor(), tmp_path)

    assert second is not third
    assert second is not first
    assert third is not first


def test_r16_the_copy_carries_the_same_reading_as_the_cached_one(tmp_path):
    # Guards the cheap way to satisfy "not the same object": handing back a
    # fresh empty `Budget`. The copy must carry the whole reading, and it must
    # still be a cache hit — the script runs once, not twice.
    h.invalidate_cache()
    provider = h.make_provider("p")
    calls = tmp_path / "calls"
    h.case_script(tmp_path, "p.sh", _counting(calls, _RICH))

    first = h.read_provider("p", provider, h.FakeExecutor(), tmp_path)
    second = h.read_provider("p", provider, h.FakeExecutor(), tmp_path)

    assert second is not first
    assert second.to_dict() == first.to_dict()
    assert second.headroom == 0.42
    assert second.windows == {"weekly": {"headroom": 0.42}}
    assert second.note == "from the script"
    assert calls.read_text() == "x"


def test_r16_item_assignment_into_a_returned_spent_does_not_reach_the_next_call(tmp_path):
    # The amendment's load-bearing case: `returned.spent["k"] = v`, never
    # `returned.spent = {...}`. A shallow copy survives the rebinding and
    # fails this.
    h.invalidate_cache()
    provider = h.make_provider("p")
    h.case_script(tmp_path, "p.sh", _PLAIN)

    h.read_provider("p", provider, h.FakeExecutor(), tmp_path)          # fresh
    second = h.read_provider("p", provider, h.FakeExecutor(), tmp_path)  # cache hit
    second.spent["leaked"] = 99

    third = h.read_provider("p", provider, h.FakeExecutor(), tmp_path)
    assert "leaked" not in third.spent
    assert dict(third.spent) == {}        # a script reports no spend at all


def test_r16_item_assignment_into_returned_windows_does_not_reach_the_next_call(tmp_path):
    h.invalidate_cache()
    provider = h.make_provider("p")
    h.case_script(tmp_path, "p.sh", _RICH)

    h.read_provider("p", provider, h.FakeExecutor(), tmp_path)          # fresh
    second = h.read_provider("p", provider, h.FakeExecutor(), tmp_path)  # cache hit
    second.windows["injected"] = {"headroom": 0.0}

    third = h.read_provider("p", provider, h.FakeExecutor(), tmp_path)
    assert "injected" not in third.windows
    assert set(third.windows) == {"weekly"}


def test_r16_mutating_a_returned_budgets_fields_does_not_reach_the_next_call(tmp_path):
    # The scalars R16 names: note, severity, cooldown_until. These are the
    # three `read_all` writes, so this is F171's mechanism in miniature.
    h.invalidate_cache()
    provider = h.make_provider("p")
    h.case_script(tmp_path, "p.sh", _PLAIN)

    h.read_provider("p", provider, h.FakeExecutor(), tmp_path)          # fresh
    second = h.read_provider("p", provider, h.FakeExecutor(), tmp_path)  # cache hit
    second.note = "scribbled on"
    second.severity = "critical"
    second.cooldown_until = time.time() + 999

    third = h.read_provider("p", provider, h.FakeExecutor(), tmp_path)
    assert third.note == ""
    assert third.severity == "normal"
    assert third.cooldown_until is None


def test_r16_mutating_the_budget_from_a_fresh_read_does_not_reach_the_next_call(tmp_path):
    # Amendment 2. The fresh path caches the very object it returns
    # (`_cache[name] = (now_, budget); return budget`), so copy-on-cache-hit
    # alone leaves the cache poisonable by the FIRST caller — which is how
    # F171 survives a fix scoped to the hit path. `read_all`'s first read of a
    # provider is a miss, and it decorates what it gets back.
    h.invalidate_cache()
    provider = h.make_provider("p")
    h.case_script(tmp_path, "p.sh", _RICH)

    first = h.read_provider("p", provider, h.FakeExecutor(), tmp_path)   # cache MISS
    first.spent["leaked"] = 99
    first.windows["injected"] = {"headroom": 0.0}
    first.note = "scribbled on"
    first.severity = "critical"
    first.cooldown_until = time.time() + 999

    second = h.read_provider("p", provider, h.FakeExecutor(), tmp_path)
    assert second is not first
    assert "leaked" not in second.spent
    assert "injected" not in second.windows
    assert second.note == "from the script"
    assert second.severity == "normal"
    assert second.cooldown_until is None


def test_r16_two_callers_holding_results_at_once_do_not_see_each_others_writes(tmp_path):
    # Two results alive at the same time — the interleaving the cache makes
    # possible, and the one F122's scenario is really about.
    h.invalidate_cache()
    provider = h.make_provider("p")
    h.case_script(tmp_path, "p.sh", _PLAIN)

    mine = h.read_provider("p", provider, h.FakeExecutor(), tmp_path)
    theirs = h.read_provider("p", provider, h.FakeExecutor(), tmp_path)

    theirs.spent["theirs"] = 1
    theirs.note = "theirs"
    assert "theirs" not in mine.spent
    assert mine.note == ""


def test_r16_f171_a_cooling_provider_read_three_times_carries_the_reason_once(tmp_path):
    # F171. `read_all` appends the cooldown reason to `budget.note` on every
    # call (`budget.py:736-740`). Against a shared cached object the note grows
    # by one " | rate limited" per read, and the monitor renders all of them.
    h.invalidate_cache()
    providers = {"p": h.make_provider("p")}
    calls = tmp_path / "calls"
    h.case_script(tmp_path, "p.sh", _counting(calls, _PLAIN))
    cooling = {"p": {"until": time.time() + 300, "reason": "rate limited"}}

    reads = [
        h.read_all(providers=providers, executor_for=_executor_for,
                   config_dir=tmp_path, cooldowns=cooling)["p"]
        for _ in range(3)
    ]

    assert [b.note for b in reads] == ["rate limited"] * 3
    assert [b.severity for b in reads] == ["critical"] * 3
    assert calls.read_text() == "x"       # all three inside one TTL: one read


def test_r16_f171_severity_returns_to_the_script_reading_when_the_cooldown_lapses(tmp_path):
    # The worse half of F171: `severity` is force-set to "critical" on the
    # shared object and nothing re-derives it, so a provider whose cooldown
    # has lapsed stays out of routing until the TTL expires.
    h.invalidate_cache()
    providers = {"p": h.make_provider("p")}
    h.case_script(tmp_path, "p.sh", _PLAIN)
    cooling = {"p": {"until": time.time() + 300, "reason": "rate limited"}}
    lapsed = {"p": {"until": time.time() - 1, "reason": "rate limited"}}

    for _ in range(3):
        h.read_all(providers=providers, executor_for=_executor_for,
                   config_dir=tmp_path, cooldowns=cooling)

    after = h.read_all(providers=providers, executor_for=_executor_for,
                       config_dir=tmp_path, cooldowns=lapsed)["p"]

    assert after.severity == "normal"     # what the script itself reported
    assert after.cooldown_until is None
    assert after.note == ""
    assert after.usable is True


# ===========================================================================
# R17 — both paths treat `spent` identically, and the reader's keys survive
# ===========================================================================

def test_r17_a_cache_hit_merges_the_callers_spent_over_the_readers(tmp_path, monkeypatch):
    # The defect in one line: a cache hit does `budget.spent = spent or
    # budget.spent`, so a caller passing only total/cost_usd destroys the
    # extra-credits pool the reader put there — the figure whose exhaustion
    # "stops work dead".
    h.invalidate_cache()
    name = _builtin_reader(monkeypatch)
    provider = h.make_provider(name)

    first = h.read_provider(name, provider, h.FakeExecutor(), tmp_path)
    assert dict(first.spent) == READER_SPENT

    second = h.read_provider(name, provider, h.FakeExecutor(), tmp_path,
                             spent={"total": 100, "cost_usd": 2})
    assert dict(second.spent) == {"extra_credits_used": 7, "extra_credits_limit": 100,
                                  "total": 100, "cost_usd": 2}


def test_r17_a_spentless_call_does_not_inherit_a_previous_callers_spent(tmp_path, monkeypatch):
    # Amendment 2: the caller's `spent` is a per-call overlay, merged onto the
    # returned copy and never stored. The cached reading carries the reader's
    # keys and nothing else.
    h.invalidate_cache()
    name = _builtin_reader(monkeypatch)
    provider = h.make_provider(name)

    h.read_provider(name, provider, h.FakeExecutor(), tmp_path)
    h.read_provider(name, provider, h.FakeExecutor(), tmp_path,
                    spent={"total": 100, "cost_usd": 2})
    third = h.read_provider(name, provider, h.FakeExecutor(), tmp_path)

    assert dict(third.spent) == READER_SPENT


def test_r17_an_empty_spent_is_treated_as_no_spent_on_both_paths(tmp_path, monkeypatch):
    # The boundary between "no spend to report" and "a caller that reports
    # nothing". Both must leave the reader's own figures alone.
    h.invalidate_cache()
    name = _builtin_reader(monkeypatch)
    provider = h.make_provider(name)

    fresh = h.read_provider(name, provider, h.FakeExecutor(), tmp_path,
                            spent={}, use_cache=False)
    assert dict(fresh.spent) == READER_SPENT

    h.read_provider(name, provider, h.FakeExecutor(), tmp_path,
                    spent={"total": 100})
    hit = h.read_provider(name, provider, h.FakeExecutor(), tmp_path, spent={})
    assert dict(hit.spent) == READER_SPENT


def test_r17_a_callers_key_wins_over_the_readers_same_key_on_a_cache_hit(tmp_path, monkeypatch):
    # Merge direction, where the two sources collide: the caller's figure is
    # the later one, so it wins. Pins `{**reader, **caller}` rather than
    # `{**caller, **reader}` — the plausible wrong fix for the hit path.
    h.invalidate_cache()
    name = _builtin_reader(monkeypatch)
    provider = h.make_provider(name)

    h.read_provider(name, provider, h.FakeExecutor(), tmp_path)
    hit = h.read_provider(name, provider, h.FakeExecutor(), tmp_path,
                          spent={"extra_credits_used": 9, "total": 100})

    assert dict(hit.spent) == {"extra_credits_used": 9, "extra_credits_limit": 100,
                               "total": 100}


def test_r17_the_same_spent_gives_the_same_result_on_both_paths(tmp_path, monkeypatch):
    # R17's headline. The snapshot matters: today the cache-hit call mutates
    # the very object the fresh call returned, so comparing the two live
    # `.spent` attributes would compare a dict with itself and pass.
    h.invalidate_cache()
    name = _builtin_reader(monkeypatch)
    provider = h.make_provider(name)
    caller_spend = {"total": 100, "cost_usd": 2}
    both = {"extra_credits_used": 7, "extra_credits_limit": 100,
            "total": 100, "cost_usd": 2}

    fresh = h.read_provider(name, provider, h.FakeExecutor(), tmp_path,
                            spent=dict(caller_spend), use_cache=False)
    fresh_spent = dict(fresh.spent)
    assert fresh_spent == both

    hit = h.read_provider(name, provider, h.FakeExecutor(), tmp_path,
                          spent=dict(caller_spend))
    assert dict(hit.spent) == fresh_spent


# ===========================================================================
# R18 — the merge is defended on both paths, with both sources reporting
# ===========================================================================
# Driven through `read_all(spend_by_provider=...)`, the one production caller
# that passes `spent` (`monitor/snapshot.py:133`), against a reader that
# reports spend of its own. The existing
# `test_fresh_read_merges_spent_rather_than_replacing_it` cannot tell merge
# from replace, because its script reports no spend and `{**{}, **s}` is `s`.

def test_r18_a_fresh_read_keeps_both_the_readers_spend_and_the_callers(tmp_path, monkeypatch):
    h.invalidate_cache()
    name = _builtin_reader(monkeypatch)

    out = h.read_all(providers={name: h.make_provider(name)},
                     executor_for=_executor_for, config_dir=tmp_path,
                     spend_by_provider={name: {"total": 100, "cost_usd": 2}},
                     use_cache=False)

    assert dict(out[name].spent) == {"extra_credits_used": 7, "extra_credits_limit": 100,
                                     "total": 100, "cost_usd": 2}


def test_r18_a_cache_hit_keeps_both_the_readers_spend_and_the_callers(tmp_path, monkeypatch):
    h.invalidate_cache()
    name = _builtin_reader(monkeypatch)
    providers = {name: h.make_provider(name)}

    h.read_all(providers=providers, executor_for=_executor_for, config_dir=tmp_path)
    out = h.read_all(providers=providers, executor_for=_executor_for,
                     config_dir=tmp_path,
                     spend_by_provider={name: {"total": 100, "cost_usd": 2}})

    assert dict(out[name].spent) == {"extra_credits_used": 7, "extra_credits_limit": 100,
                                     "total": 100, "cost_usd": 2}
