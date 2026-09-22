# Phase 3 contract — the cache that defends its own defect

The interface contract for `BRIEF.md` phase 3 item 1: **F150**, **F122**,
**F154**, and **F171**, which I filed while writing this. Written by the
orchestrator.

The findings are files, not tools. Read them in `context/review/C2-budget.md`
and `context/review/C2-budget-adversary.md`. You have neither `read_finding`
nor `list_tickets`; do not try them.

---

## What is actually wrong, which is not quite what the findings say

`read_provider` has two return paths that disagree about `spent`:

```python
# cache hit  (budget.py:680-683)
budget = cached[1]
budget.spent = spent or budget.spent      # replace, on the CACHED object
return budget

# fresh read (budget.py:697-698)
if spent:
    budget.spent = {**budget.spent, **spent}   # merge
```

**F122 justifies this with a scenario that cannot happen.** Its text says
`driver.py`, `runner.py`, `watchdog.py`, `server.py` and `cli.py` "all pass
their own `spend_by_provider`/`spent`", so two callers inside one cache window
erase each other. That is false, and I checked before writing this:

| caller | passes `spent`? | cached? |
|---|---|---|
| `monitor/snapshot.py:133` → `read_all` | **yes** | yes |
| `runner.py:311` `_sample_headroom` | no | yes |
| `runner.py:1719` | no | `use_cache=False` |
| `watchdog.py:279` | no | `use_cache=False`, after `invalidate_cache()` |

Exactly one caller passes `spent`, and what it passes is a **complete snapshot**
recomputed from the whole tree (`spend_by_provider`, `snapshot.py:255`), not a
delta. Between two snapshot callers, merge and replace are indistinguishable.

**So here is the real defect, and it is worse than the one described.**
`budget.spent` has two sources, not one. `read_claude` writes its own keys into
it (`budget.py:489-490`):

```python
budget.spent["extra_credits_used"]  = int(used)
budget.spent["extra_credits_limit"] = int(limit)
```

and the caller adds `total` and `cost_usd`. On a fresh read the merge keeps all
four. **On a cache hit the replacement destroys the two extra-credits keys**,
permanently, in the object the cache is holding — and the monitor reads
`budget["spent"]` at `snapshot.py:113`. The figure being lost is the monthly
extra-credits pool, which the comment immediately below the assignment
describes as the thing whose exhaustion "stops work dead".

That is the justification to carry into the commit message. Not "two callers
race"; "a cache hit throws away the reader's own figures and keeps only the
caller's".

---

## R16 — a cache hit never hands out the cache's own object

**Observable behaviour required.** The `Budget` returned by `read_provider` is
one the caller may mutate freely without changing what any later call sees.

This is the root cause, and closing it closes F171 as a side effect rather than
as a second fix.

**What must be true afterwards:**

- Two successive cache hits return objects that are not the same object.
- Mutating any field of a returned `Budget` — `spent`, `note`, `severity`,
  `cooldown_until` — leaves the next call's result unaffected.
- **F171 specifically**: `read_all` decorates the budget it gets back with
  `cooldown_until`, `severity = "critical"` and an appended `note`
  (`budget.py:736-740`). Reading a cooling provider three times inside the TTL
  must produce the reason in the note **once**, not three times; and when the
  cooldown lapses, the next read must not still report `critical`.

**Returning a copy is safe, and I checked the thing that would have made it
unsafe.** `runner.py` writes `cooldown_until` and `note` onto budgets at lines
339, 377, 392, 398 and 399, which looks like state expected to persist. It is
not: the durable cooldown lives in the tree (`set_cooldown`, `clear_cooldown`),
and `read_all` re-applies it from the `cooldowns` argument on **every** call,
after `read_provider` returns. Nothing depends on the aliasing.

---

## R17 — both paths treat `spent` identically

**Observable behaviour required.** Passing `spent` to `read_provider` produces
the same `Budget.spent` whether the read was cached or fresh.

Merge is the behaviour to converge on: it is what the fresh path does, it is
what keeps `read_claude`'s extra-credits keys, and it is what F122 asks for.

**What must be true afterwards:**

- A cache hit merges the caller's `spent` over what the reader reported, rather
  than replacing it. `extra_credits_used` survives a call that passes only
  `total` and `cost_usd`.
- A call passing **no** `spent` does not inherit a previous caller's `spent`.
  Today's third assertion in the pinned test — a spentless read returning
  `{"b": 2}` — must stop being true. This one is a consequence of R16, but it
  is the visible symptom, so pin it.

---

## R18 — the merge is defended on both paths

**Observable behaviour required.** Replacing either path's merge with a
wholesale assignment turns a test red.

This is F154, and it is the requirement most likely to be skipped because the
code will already be correct when you get here.

**The existing test does not do this.**
`test_fresh_read_merges_spent_rather_than_replacing_it` passes `spent={"a": 1}`
against a script that reports no spend at all, so `{**{}, **{"a": 1}}` and
`{"a": 1}` are the same dict and the assertion cannot tell merge from replace.
It is named after a behaviour it does not check.

**So the test for both paths must have both sources reporting spend at once** —
a reader contributing keys and a caller contributing different ones — and assert
all of them survive. Use the real shape rather than a synthetic one:
`extra_credits_used` from the reader, `total`/`cost_usd` from the caller.

---

## F150 comes first, and is not a surprise

`tests/test_c2_budget_characterization.py:340`,
`test_cache_hit_overwrites_spent_instead_of_merging_and_mutates_the_cached_object`,
asserts today's broken behaviour under a name that reads like an invariant. All
three of its assertions invert under R16 and R17.

**Deal with it as the opening move**, before touching `budget.py`, exactly as
this project has done three times already: rename it so the name states what is
now true, keep a comment naming F122/F150 and saying the inversion is
deliberate, and do not simply delete it — what it asserted is now a different
behaviour and that behaviour is worth pinning.

Its comment also says `F120` where it means `F122`. Fix that while you are in
there.

---

## What is NOT in scope

- **F120**, the discarded `config_dir` — phase 3 item 2, adjacent in the same
  file and a different defect. Do not fold it in.
- **`_CACHE_TTL` itself.** Whether 60 seconds is right is a separate question.
- **`read_claude`'s extra-credits parsing.** It is correct; it is only the
  victim here.
- **Reconciling `spend_by_provider` with the tree's own accounting.** Out of
  scope and larger.

---

## What "done" looks like

Baseline: **985 passed, 1 failed** on a host, that one being
`test_the_claude_script_uses_the_container_profile_only_where_it_should` —
finding F130, phase 3 item 4, red only where `docker` is present. Green in a
container, plus whatever phase 2 has added by the time this starts.

Run the suite as `uv run --frozen python -m pytest`, never
`uv run --frozen pytest`.

**I will run the mutation check myself after the merge**, as I did for R13:
revert the cache path to `budget.spent = spent or budget.spent`, revert the
fresh path's merge to a bare assignment, and return the cached object instead of
a copy — three mutations, each of which must turn a test red. Green after your
change is not evidence here, because green was already wrong. That is F150.

Say in your result which test guards which of the three, so that check is a list
to walk rather than a hunt.

---

# Amendment — after the advisor read this

## The copy must be deep enough to cover `spent` and `windows`

I had left shallow-versus-deep to the implementer as an implementation choice.
**That was wrong, and it is the load-bearing part.** `Budget.spent` and
`Budget.windows` are mutable dicts, so a shallow `copy.copy()` or
`dataclasses.replace()` hands out a new `Budget` whose `.spent` is still the
cache's own dict — and `read_claude` writes into it by item assignment
(`budget.spent["extra_credits_used"] = ...`), which is exactly the shape that
would still reach the cache.

**So R16 requires that `spent` and `windows` are themselves copied**, not only
the `Budget` around them. A caller doing `returned.spent["k"] = v` must not
change what the next call sees. Test it that way — by item assignment, not by
rebinding the attribute — or the guard passes against a shallow copy.

Whether that is `copy.deepcopy`, `replace(b, spent=dict(b.spent),
windows=dict(b.windows))`, or copying at insertion instead of on read remains
the implementer's choice. What is no longer optional is the observable
behaviour.

## An objection I considered and rejected, with the evidence

The advisor's reading was that `runner.py` **depends** on the aliasing: that
`_wind_down` (line 339) and `_half_open` (line 377) set `cooldown_until` without
ever writing it to the tree, so a copy would make those cooldowns "instantly
vanish on the next read" and break wind-down.

I checked, and it does not hold. The whole sequence lives inside one
`start_agent` call at `runner.py:1026-1046`:

```
cooldowns = self.tree.read().get("cooldowns", {})     # durable state
budgets   = read_all(..., cooldowns)                  # fresh, re-applies them
self._half_open(budgets, cooldowns)                   # decorates
self._wind_down(budgets)                              # decorates
chosen, why = choose_provider(spec.provider, budgets, ...)   # consumes
```

`budgets` is built, decorated and consumed in one synchronous block, and the
next pass rebuilds all of it. `_wind_down`'s conclusion is **recomputed** every
time from `self.tree.burn(name)`, so it does not need to persist; `_half_open`'s
line 377 branch is likewise re-derived from `claim_trial`, and its lines 392 and
398 write the durable half to the tree via `clear_cooldown`/`set_cooldown`
first.

The reading is not merely unnecessary — it is inverted. A wind-down decision
surviving in the cache *past the pass that made it* is a stale conclusion
leaking into unrelated readers, which is F171 exactly. Today `_wind_down` even
skips a provider `if ... budget.cooldown_until`, so an aliased leftover can
suppress the very re-evaluation that would have been correct. Copying fixes
that too.

If an implementer finds a wind-down case that genuinely needs to persist, that
is `NEED_INFO` to me, not a reason to keep the aliasing.

## Not a correction: the second writer

The advisor offered `read_claude`'s `extra_credits` write as something I had
missed. It is the centre of this contract's opening section and the reason R17
exists. Recorded here only so a later reader does not think it arrived late.

---

# Amendment 2 — a gap in R16, and the answer to `NEED_INFO(R17)`

The first R16-R18 run was cut off by quota after committing the F150 inversion,
and left `HANDOFF-R16-R18.md` at the repository root. Two of its three points
are corrections to this contract rather than notes, and both are taken.

## R16 was wrong to say "a cache hit"

R16's headline — *"a cache hit never hands out the cache's own object"* — is not
sufficient, and F171 is the proof. **The fresh path caches the very object it
returns**:

```python
if spent:
    budget.spent = {**budget.spent, **spent}
_cache[name] = (now_, budget)      # the same object
return budget
```

So `read_all`'s **first** read of a provider is a cache *miss*, it decorates
what it gets back with `cooldown_until`, `severity = "critical"` and an appended
note, and that decoration lands in the cache entry anyway. Copy-on-cache-hit
alone leaves the note accumulating and `severity` stuck exactly as before.

**R16 therefore applies to both paths**: what `read_provider` returns is never
the object the cache holds, whether the read was cached or fresh. Whether that
is achieved by copying on the way out or by caching a copy on the way in is
still the implementer's choice; that no caller can ever reach the cached object
is not.

**A test that mutates a fresh read's return and then reads again is required,
not optional.** The handoff is right that without it the suite would pass with
F171 fully intact.

## `NEED_INFO(R17)` — a caller's `spent` does not persist into the cache

The question was whether the cache entry should hold only what the reader
reported, or the already-merged result including the first caller's keys.

**Only what the reader reported.** The caller's `spent` is a per-call overlay,
merged onto the returned copy and never stored.

Three reasons, in order of weight:

1. **The alternative is the defect.** If caller keys persisted, a later caller
   passing no `spent` would inherit them — which is exactly the third assertion
   of the test F150 pins (`third.spent == {"b": 2}` after a spentless read), and
   that assertion is being inverted precisely because it is wrong.
2. **Caching it buys nothing.** `spend_by_provider` recomputes the whole figure
   from the tree on every call, so a cached copy of it is at best redundant and
   at worst stale.
3. It makes R17's second bullet exactly true rather than accidentally true.

So the cached `Budget.spent` carries `read_claude`'s `extra_credits_*` keys and
nothing else, and every caller sees those plus its own. Tests may assert exact
dicts.

## The `_from_script` constraint, which is a seam not an assertion

The handoff records that `_from_script` has no `spent` key, so a *script* reader
cannot contribute to `spent` at all — R18's "both sources reporting at once"
shape is reachable only through the built-in path, and the real `read_claude`
cannot be used because it would read the developer's own `~/.claude.json`.

Its proposed seam is right and is hereby sanctioned:
`monkeypatch.setitem(budget_mod._BUILTIN, "<made-up provider>", fake)` with
`fake(*args, **kwargs)` so it survives either dispatch shape. One private name,
opened deliberately, and stated in the test rather than buried.

This is also worth noticing as a fact about the system rather than a test
inconvenience: **a provider whose budget comes from a script can never report
spend**, only headroom. Not in scope here; recorded so it is not rediscovered.

## `Budget.__eq__`

The handoff notes `Budget` is a dataclass, so `==` is already value equality,
and says it avoided relying on that rather than assume semantics I had not
granted. Correct instinct, and the answer is: **do not rely on `==` for the
copy tests.** Identity (`is not`) and independent mutation are what R16 is
about; value equality would pass against the very aliasing being removed.
