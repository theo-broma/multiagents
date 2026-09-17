# Phase 1 contract — the budget cache that bleeds between tests

The interface contract for `BRIEF.md` phase 1 item 8, finding **F100**. Written
by the orchestrator.

**This one needs no test engineer.** The test that defines done already exists,
has been red since the review, and is the repository's one deliberate failure:

```
tests/test_c2_provider_harness.py::test_read_provider_caches_until_invalidated
```

It **passes in isolation and fails in the full suite**. That asymmetry is the
defect, not a flake.

---

## R10 — a cached `Budget` cannot bleed between unrelated callers

**Observable behaviour required.** Two callers reading a budget for the same
provider *name* but different configuration do not receive each other's cached
result.

Today `budget._cache` is a plain module-level dict keyed only by provider name.
Nothing resets it between tests, `pytest-randomly` is active, so a test can read
a `Budget` another test cached under a colliding name — and which test wins
depends on the run's ordering.

**What must be true afterwards.**

- The named test passes in the full suite, not only in isolation.
- It still passes in isolation.
- Repeated full-suite runs agree with each other. A fix that works for one
  ordering and not another has not fixed this.

**The finding's own note proposes including `config_dir` in the cache key.** That
is a reasonable shape and you may take it. If you see a better one, take that and
say why — but a fix that makes the cache correct is worth more than one that only
makes this test green, and the difference between them is whether two callers
with genuinely different configuration can still collide.

---

## Why a conftest fixture is the wrong fix, and was already tried

Twice, during the review, an agent added an autouse fixture calling
`budget.invalidate_cache()` between tests. Both times it was reverted at the
merge gate, because that agent could not write `conftest.py` — which is `bug-08f9b3`,
now fixed.

It could be written today. **Do not.** It would hide the defect rather than
repair it: the cache would still be keyed wrongly, and the next caller outside a
test would still collide. The fixture was a workaround for an agent that could
not reach the real cause; you can.

---

## What is NOT in scope

- **Do not modify `token_count()`, `sum_usage()`, or anything on the budget-tag
  accounting path.** That was phase 1 item 2, it is done, and its tests are
  green. This is a different defect in the same file.
- **Do not change what `invalidate_cache()` does.** Tests call it deliberately
  and the C2 characterization suite pins its behaviour.
- **Do not touch any existing test file.** They are read-only to your tier and
  the merge gate reverts the change silently. The one test that matters here is
  already written and already red; you do not need to alter it, and if you
  believe it is wrong that is `NEED_INFO(<test name>)` back to me.

---

## What "done" looks like

The confirmed baseline is **967 passed, 2 failed**. The two are this finding's
test, which you are fixing, and
`tests/test_core.py::test_the_claude_script_uses_the_container_profile_only_where_it_should`,
which fails on a host where `docker` is present and passes inside a container —
recorded in `docs/open-questions.md`, and not yours.

So after this lands, a full run inside a container should show **968 passed, 0
failed**, and that is the first time in this project's history that green will
mean green. `BRIEF.md` says so in its own words.

Because the whole point is behaviour across a full run, **verify with a full
suite run**, not a targeted selection — this is the one requirement in phase 1
where a targeted run proves nothing, since the test already passes that way.
