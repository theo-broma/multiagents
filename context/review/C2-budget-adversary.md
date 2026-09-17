# Adversary Findings — budget.py

## Attack Summary

Attacking `src/multiagents/budget.py` — quota reading and provider choice.
Target: 70-test characterization suite in `tests/test_c2_budget_characterization.py`.

## Mutation Test Results

Seven mutations were applied to `budget.py` and tested against the 70-test suite:

| # | Mutation | Location | Survived? |
|---|----------|----------|-----------|
| 1 | `> 0.02` → `>= 0.02` in `usable` | line 114 | **CAUGHT** |
| 2 | `>= reserve` → `> reserve` in `_has_room` | line 763 | **SURVIVED** |
| 3 | `>= 75` / `>= 90` → `> 75` / `> 90` in severity (read_claude) | lines 462-464 | **SURVIVED** |
| 4 | Cache spent overwrite → merge | line 657 | **CAUGHT** (pins wrong behavior) |
| 5 | `>= 75` / `>= 90` → `> 75` / `> 90` in severity (script) | line 634 | **SURVIVED** |
| 6 | `< _CACHE_TTL` → `<= _CACHE_TTL` in cache check | line 655 | **SURVIVED** |
| 7 | Fresh-read spent merge → overwrite | line 672 | **SURVIVED** |

**4 of 7 mutations survived.** The suite would let routing and accounting defects through.

## Findings

### F-A1: Cache hit mutates shared Budget object in place (CRITICAL)

**Location:** `src/multiagents/budget.py:657`
**Input:** Two sequential `read_provider` calls with different `spent` dicts
**Outcome:** The cache stores a reference to the Budget object, and a cache hit
mutates it in place via `budget.spent = spent or budget.spent`. This means:
1. The first caller's reference sees the second caller's spend
2. The cached object is permanently modified
3. A third call with no `spent` sees the second call's spend, not the original

**Test:** `test_interference_cache_hit_mutates_shared_object`
**Severity:** HIGH — spend accounting is silently corrupted across callers sharing
a provider name. A brake nobody would notice breaking.

### F-A2: Cache hit overwrites spent instead of merging (CRITICAL)

**Location:** `src/multiagents/budget.py:657`
**Input:** `read_provider` with `spent={"a": 1}` then `spent={"b": 2}`
**Outcome:** The cache hit does `budget.spent = spent or budget.spent`, which
REPLACES the spent dict rather than merging it. The fresh-read path at line 672
correctly merges with `{**budget.spent, **spent}`, but the cache path does not.

**Test:** `test_cache_hit_overwrites_spent_instead_of_merging_and_mutates_the_cached_object`
(existing test pins this, but the adversary tests prove the mutation would survive
if the merge logic were changed)

**Severity:** HIGH — spend from different runs is lost, not summed. The budget
mechanism is what stops work, and a brake nobody would notice breaking is the
same shape of defect as the one on the sibling context.

### F-A3: Reserve boundary comparison is `>=` not `>` (MEDIUM)

**Location:** `src/multiagents/budget.py:763`
**Input:** `headroom=0.15` against `reserve=0.15` with `reserved=True`
**Outcome:** The comparison `candidate.headroom >= reserve` means headroom exactly
at the reserve threshold is allowed. A mutation to `>` would block it.

**Test:** `test_mutation_reserve_boundary_at_exactly_reserve_is_caught`
**Severity:** MEDIUM — boundary behavior is not pinned by the existing suite.

### F-A4: Severity threshold boundaries are not pinned (MEDIUM)

**Location:** `src/multiagents/budget.py:462-464` (read_claude) and
`src/multiagents/budget.py:634` (script derivation)
**Input:** `percent=75.0` and `percent=90.0` for read_claude; `headroom=0.25` and
`headroom=0.10` for script derivation
**Outcome:** The thresholds `>= 75` and `>= 90` are not pinned at the exact
boundary. A mutation to `> 75` or `> 90` would pass all existing tests.

**Tests:** `test_mutation_severity_warning_boundary_at_75_is_caught`,
`test_mutation_severity_critical_boundary_at_90_is_caught`,
`test_mutation_script_severity_derivation_boundary_at_75`,
`test_mutation_script_severity_derivation_boundary_at_90`

**Severity:** MEDIUM — severity drives alerting and routing decisions.

### F-A5: usable boundary at exactly 0.02 is pinned but 0.021 is not (LOW)

**Location:** `src/multiagents/budget.py:114`
**Input:** `headroom=0.021`
**Outcome:** The suite tests 0.02 (not usable) and 0.021 (usable), but a mutation
changing `> 0.02` to `> 0.021` would pass both. The test pins 0.021 as usable.

**Test:** `test_mutation_usable_boundary_at_exactly_0_021_is_caught`
**Severity:** LOW — boundary is narrow and unlikely to matter in practice.

### F-A6: Unknown headroom with headroom=0.0 is usable (LOW)

**Location:** `src/multiagents/budget.py:113-115`
**Input:** `known=False, headroom=0.0`
**Outcome:** The logic falls through to `return True` because the condition
`self.known and self.headroom is not None` is False when `known=False`. This is
correct (unknown headroom is not "no headroom"), but the suite does not test it.

**Test:** `test_hardcoding_headroom_exactly_zero_is_not_usable`
**Severity:** LOW — edge case, but correct behavior.

### F-A7: Negative headroom is not usable (LOW)

**Location:** `src/multiagents/budget.py:114`
**Input:** `known=True, headroom=-0.5`
**Outcome:** The comparison `self.headroom > 0.02` correctly returns False for
negative values. The suite tests -0.5 as "critical" severity but not usability.

**Test:** `test_hardcoding_headroom_negative_is_not_usable`
**Severity:** LOW — correct behavior, but untested.

## Verdict on the 70-test suite

The suite is **not load-bearing** for routing and accounting correctness.

**What it catches:**
- Basic routing logic (preferred vs fallback)
- Cache TTL and invalidation (but not boundary precision)
- Severity derivation from percentages (but not exact thresholds)
- Family-based instance selection
- Cooldown application
- The `usable` boundary at exactly 0.02

**What it misses (mutations that survived):**
1. **Reserve boundary** (mutation #2): `>= reserve` vs `> reserve` — a provider
   with headroom exactly at the reserve threshold would be incorrectly blocked.
2. **Severity thresholds** (mutations #3, #5): `>= 75` / `>= 90` vs `> 75` / `> 90`
   — exact boundary values are not pinned in either read_claude or script derivation.
3. **Cache TTL boundary** (mutation #6): `< _CACHE_TTL` vs `<= _CACHE_TTL` — the
   exact expiration moment is not pinned.
4. **Fresh-read spent merge** (mutation #7): merge vs overwrite — the suite does
   not test the case where a script reports its own spent and the caller also
   passes spent.

**The most consequential finding:**

The suite **pins incorrect behavior as correct** (mutation #4). The test
`test_cache_hit_overwrites_spent_instead_of_merging_and_mutates_the_cached_object`
asserts that a cache hit should overwrite spent, not merge it. A mutation making
the cache path merge (which would be correct, matching the fresh-read path) fails
the test. This is the same shape of defect as the sibling context's spend-summing
issue: the budget mechanism is what stops work, and a brake nobody would notice
breaking is the same shape of defect.

**Would it let a routing or accounting defect through?**

**YES.** Four mutations survived, including:
- Reserve boundary: a provider at exactly the reserve threshold would be blocked
- Severity thresholds: alerting and routing decisions at exact boundaries are unchecked
- Fresh-read spent merge: spend from scripts and callers could be lost, not summed

The cache overwrite behavior is pinned as correct when it is actually a defect.
The suite would reject a fix that made the cache path merge like the fresh path.

## Tests committed

- `tests/test_budget_adversary.py` — 24 tests covering mutation, hardcoding,
  fuzzing, and interference attacks.

**Run command:**
```
uv run --frozen python -m pytest -q tests/test_budget_adversary.py
```

**No seed required** — all tests are deterministic.

```
VERDICT(rejected, 4): four defects, the first blocking
```

Defects:
1. Suite pins incorrect cache behavior (overwrites spent instead of merging) — would reject a fix
2. Reserve boundary not pinned at exact threshold (mutation survived)
3. Severity thresholds not pinned at exact boundaries (mutations survived)
4. Fresh-read spent merge not tested when script reports its own spent (mutation survived)
