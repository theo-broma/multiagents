# Handoff — tester, phase 3 item 1 (R16/R17/R18 of `context/specs/phase3-cache-aliasing.md`)

Interrupted by quota after step 1 of 5. Working tree is clean; everything below
that is "done" is committed on `agents/tester/329af1`.

## Done

**F150 inversion — committed as `1826faf`.** The only change so far.
`tests/test_c2_budget_characterization.py:341` is now
`test_cache_hit_merges_spent_onto_a_copy_and_leaves_the_cached_object_alone`,
with a comment naming F122 and F150, saying the inversion is deliberate, and
correcting the old `F120` citation to `F122`. Verified red against today's
`budget.py` on its **own** assertion (`assert second is not first`), not on
collection:

    uv run --frozen python -m pytest tests/test_c2_budget_characterization.py \
      -k cache_hit_merges_spent_onto_a_copy -p no:randomly -q

Its assertions were deliberately weakened from exact-dict to
`second.spent["b"] == 2` / `"b" not in third.spent` — see the open question
below. The exact dicts belong in the new file, where the first call passes no
`spent` and the ambiguity disappears.

## Left

`tests/test_phase3_cache_aliasing.py` — **not created yet.** Nothing of R16,
R17 or R18 is written. The plan below is the whole of what the interrupted run
worked out; it is not visible in the diff and is the expensive part to redo.

## What was worked out (do not re-derive)

### 1. The one real ambiguity in the contract — raise this as NEED_INFO

R17 says "a call passing no `spent` does not inherit a previous caller's". Two
readings survive the contract:

- **A** — the cache entry holds only what the *reader* reported; a caller's
  `spent` is merged onto the returned copy and never persists.
- **B** — the fresh path caches the already-merged budget (today's line
  `_cache[name] = (now_, budget)` after `budget.spent = {**budget.spent,
  **spent}`), so the *first* caller's keys do persist; only the cache-hit
  caller's do not.

R17's headline ("the same `spent` produces the same `Budget.spent` on both
paths") implies **A**. The task prompt's claim that *all three* of the pinned
test's assertions invert implies **B** (under A, `second.spent == {"b": 2}`
stays true). The amendment allows "copying at insertion" as an implementation
choice, which does not settle it either.

**Do not pin it.** Every test below is written so it holds under both, and the
trick that achieves that is: **make the first (fresh) call pass no `spent`.**
Then the cache entry holds exactly the reader's keys under either reading, and
exact-dict assertions become unambiguous.

### 2. R16 needs the copy in *both* directions, and F171 is the proof

Copy-on-cache-hit alone does **not** close F171. `read_all`'s first read of a
provider is a cache miss: it gets the object that is then stored in `_cache`,
and decorates it (`severity = "critical"`, note append). The cache entry is
poisoned from the fresh path, so reads 2 and 3 still accumulate the note and
still report `critical` after the cooldown lapses. So the suite must contain a
test that mutates the budget returned by a **fresh** read and shows the next
call is unaffected. That is the bullet "mutating any field of a returned
`Budget`" read strictly, and F171's three-reads scenario enforces it anyway.

### 3. The reader cannot contribute `spent` through a script

`_from_script` never reads a `spent` key — a script-backed provider always has
reader-spent `{}`. R18's required shape (`extra_credits_used` from the reader,
`total`/`cost_usd` from the caller) is therefore only reachable through the
built-in reader path. Driving the real `read_claude` through `read_provider` is
barred: the builtin branch calls `builtin()` with no `config_dir` (F120, out of
scope), so it would read the developer's real `~/.claude.json` and fetch.

**The seam to use:** `monkeypatch.setitem(budget_mod._BUILTIN, "reader", fake)`
with a made-up provider name `"reader"` — no shipped `reader.sh` exists, so
`_from_script` returns `None` and the fake is called. Define it as
`def fake(*args, **kwargs)` returning a **new** `Budget(provider="reader",
known=..., spent={"extra_credits_used": 7}, windows={...})` each call, so it
survives either dispatch shape (`builtin()` or `builtin(spent)`) and any F120
fix. `_BUILTIN` is the only private name the suite should touch, it is a seam
and not an assertion, and the file's docstring should say so.

### 4. Budget equality

`Budget` is a plain `@dataclass`, so `==` is already value equality. Do **not**
rely on it — compare field tuples or `to_dict()` instead, so the suite never
quietly assumes `__eq__` semantics the contract did not grant. (That was the
orchestrator's own flagged question; answering it is not needed if no test uses
`==` on two `Budget`s.)

### 5. The test list, named and ready to write

All against `h` = `tests/support/c2_harness.py`; follow that file's convention
of `h.invalidate_cache()` on the way in.

R16:
- `test_r16_two_successive_cache_hits_are_not_the_same_object`
- `test_r16_the_copy_carries_the_same_reading_as_the_cached_one` — guards
  against satisfying "not the same object" with an empty `Budget`; assert field
  by field (or `to_dict()`) and that the script ran once.
- `test_r16_item_assignment_into_a_returned_spent_does_not_reach_the_next_call`
  — **`returned.spent["k"] = v`, never `returned.spent = {...}`.** The
  amendment's load-bearing case: rebinding passes against a shallow copy.
- `test_r16_item_assignment_into_returned_windows_does_not_reach_the_next_call`
  — same shape; use a script reporting a real `windows` object.
- `test_r16_mutating_a_returned_budgets_fields_does_not_reach_the_next_call`
  — the scalars: `note`, `severity`, `cooldown_until`.
- `test_r16_mutating_the_budget_from_a_fresh_read_does_not_reach_the_next_call`
  — point 2 above.
- `test_r16_f171_a_cooling_provider_read_three_times_carries_the_reason_once`
  — three `read_all(..., cooldowns={"a": {"until": now+100, "reason": "rate
  limited"}})` inside one TTL, `use_cache` on; assert `note == "rate limited"`
  every time (script reports no note of its own).
- `test_r16_f171_severity_returns_to_the_script_reading_when_the_cooldown_lapses`
  — same TTL, fourth call with the cooldown gone: `severity == "normal"`,
  `cooldown_until is None`.

R17 (through `read_provider`):
- `test_r17_a_cache_hit_merges_the_callers_spent_over_the_readers` — fresh call
  with **no** `spent`, then a cache hit with `{"total": 100, "cost_usd": 2}`;
  assert exactly `{"extra_credits_used": 7, "total": 100, "cost_usd": 2}`.
- `test_r17_a_spentless_call_does_not_inherit_a_previous_callers_spent` — third
  call, no `spent`, must equal exactly `{"extra_credits_used": 7}`.
- `test_r17_the_same_spent_gives_the_same_result_on_both_paths` — R17's
  headline: one `use_cache=False` call and one cache-hit call with the same
  `spent`, equal `.spent`. (Note a `use_cache=False` read still *writes* the
  cache.)

R18 (through `read_all` with `spend_by_provider`, the one production caller
that passes `spent` — `monitor/snapshot.py:133`):
- `test_r18_a_fresh_read_keeps_both_the_readers_spend_and_the_callers`
- `test_r18_a_cache_hit_keeps_both_the_readers_spend_and_the_callers`

### 6. The mutation map the contract asks for in the result

- return the cached object instead of a copy → every R16 test, and the F150
  inversion's `second is not first`.
- cache path back to `budget.spent = spent or budget.spent` → R17's merge test
  and R18's cache test.
- fresh path's merge back to a bare assignment → R18's fresh test (and nothing
  else — that is F154 exactly).

## Environment notes

Run as `uv run --frozen python -m pytest`, never `uv run --frozen pytest`.
`-p no:randomly` is useful for a single-test loop. Do not run the full suite
between groups; it is expensive and red is expected throughout.
