# Phase 3 contract — the multi-account reader nothing can reach

The interface contract for `BRIEF.md` phase 3 item 2: **F120**, critical.
Written by the orchestrator.

The finding is a file: `context/review/C2-budget.md`. You have neither
`read_finding` nor `list_tickets`; do not try them.

---

## The defect, in one dispatch expression

`read_provider`'s built-in fallback, `src/multiagents/budget.py:688-691`:

```python
builtin = _BUILTIN.get(name)
budget = builtin() if builtin is read_claude else (
    builtin(spent) if builtin else Budget(..., source="none", ...))
```

Two things are wrong with it, and `claude.sh`'s `budget` action deliberately
`exit 64`s so that **every real claude budget reading in this system goes
through this line**.

1. **`read_claude` is called with no arguments at all**, so its `config_dir`
   parameter — the entire multi-account feature, documented at length in its own
   docstring — is dead from every caller that exists. A second subscription's
   quota is never read.
2. **`_BUILTIN` is keyed on the literal string `"claude"`** and looked up by
   instance `name`. A second account configured the documented way
   (`extends: claude` under a name like `claude-work`) matches nothing, gets
   neither the real reading nor the home fallback, and is stuck at
   `known=False, source="none"` forever.

The second is the worse half. `Budget.usable` treats unknown headroom as *not*
no headroom, so a permanently-unreadable sibling looks exactly as healthy as a
fresh one, and `choose_provider` will confidently route work onto it.

---

## The trap, which is why this is not the one-line fix it looks like

**F120's own proposed fix would break claude budget reading outright.** Do not
apply it.

It suggests `builtin(config_dir=config_dir) if builtin is read_claude else ...`.
But `config_dir` here is whatever `read_provider`'s caller passed, and in
production that is **always `global_config_dir()`** — `~/.config/multiagents`,
multiagents' own configuration directory. `read_claude` reads
`<config_dir>/.claude.json`. There is no `.claude.json` there and there never
will be, so the result is `known=False` with a `"... unreadable"` note for
every claude reading on every machine.

That is a working feature turned into a broken one by a fix for a feature that
never worked. The finding half-sees this — its last clause notes the caller
"isn't `global_config_dir()` and doesn't obviously exist yet either" — and then
proposes the fix anyway.

**The per-account path does exist, just not there.** It is on the provider
instance: `Provider.env` (`providers.py:170`) carries `CLAUDE_CONFIG_DIR`, which
is what `providers.yaml:415-447` documents as the way to separate two accounts,
and `read_provider` already receives the `provider` object.

---

## R21 — the built-in reader is found by family, not by instance name

**Observable behaviour required.** A provider that inherits an integration gets
that integration's built-in reader.

`Provider.family` is already computed as `family or extends or name`
(`providers.py:200`), so `claude-work` with `extends: claude` already carries
`family == "claude"`. The dispatch simply does not consult it.

**What must be true afterwards:**

- A provider named `claude-work` with `extends: claude` is read by
  `read_claude`, not by the `source="none"` branch.
- A provider whose name matches a `_BUILTIN` key directly is unaffected.
- A provider with neither — no script, no family match — still lands on
  `known=False, source="none"` with its existing note. That branch is correct
  and is the honest answer.

---

## R22 — the reader is told which account it is reading

**Observable behaviour required.** When a provider instance declares its own
`CLAUDE_CONFIG_DIR`, the built-in reader reads *that* profile. When it declares
none, the reader behaves exactly as it does today.

**What must be true afterwards:**

- A provider whose `env` carries `CLAUDE_CONFIG_DIR: <path>` causes
  `read_claude` to read `<path>/.claude.json`, expanded for `~` and for
  variables the way `scripts.build_env` already expands that block.
- **A provider declaring no `CLAUDE_CONFIG_DIR` is read exactly as today** —
  `read_claude` with no `config_dir`, falling back to `CLAUDE_STATE` in the home
  directory. **This is the regression guard that matters most in this
  contract.** Single-account is the overwhelmingly common case, it works today,
  and a change that improves the two-account case by breaking the one-account
  case is a net loss. Pin it explicitly.
- Two instances with different `CLAUDE_CONFIG_DIR` values produce different
  readings rather than the same one twice.
- `config_dir`, the argument `read_provider` receives, keeps meaning what it
  means everywhere else in the function — where multiagents' own configuration
  lives. **Do not repurpose it.** It is passed to `_from_script` and must keep
  arriving there unchanged.

---

## R23 — the dispatch stops special-casing one reader

**Observable behaviour required.** Adding a built-in reader for a new provider
requires no change to `read_provider`.

The `builtin is read_claude` identity check is the wart `BRIEF.md` names as
"where the fallback table leaks into the dispatcher": the dispatcher knows one
reader's signature by identity and every other reader's by a different one.

**What must be true afterwards:** the three built-in readers are called through
one uniform path. How you get there — a common signature, keyword arguments, a
small adapter in the table — is yours. What is not yours is leaving an identity
comparison against a specific function in the dispatcher.

**`read_opencode` and `read_agy` take `spent` and must keep receiving it.**
Whatever the uniform call looks like, that must not be quietly dropped; it is
the kind of loss that shows up as a blank column in the monitor and nowhere
else.

---

## What is NOT in scope

- **F122, F150, F154, F171** — the cache aliasing, phase 3 item 1, **in this
  same function** and landing before you start. Your branch will already carry
  it. Do not touch the copy or the `spent` merge, and do not "tidy" them.
- **`claude.sh`'s deliberate `exit 64`.** It is correct and documented; the
  built-in reader is the intended path, not a workaround.
- **Whether a second account should be configured at all.** Not a question this
  contract opens.
- **`read_claude`'s parsing.** It is right; it is simply never given the chance.

---

## What "done" looks like

Run the suite as `uv run --frozen python -m pytest`, never
`uv run --frozen pytest`. 18 pre-existing `PermissionError: can_spawn is false`
failures in `test_core.py` are environmental, and
`test_the_claude_script_uses_the_container_profile_only_where_it_should` is F130
— phase 3 item 4, red only where `docker` is present.

**Expect the two F120 reproductions to go red**, and that is correct:
`test_claude_builtin_fallback_ignores_the_callers_config_dir` and
`test_a_second_named_claude_account_gets_no_builtin_at_all` pin today's
behaviour. The implementer does not touch them; a test engineer inverts them
afterwards, renamed to state what is now true, with a comment naming F120. That
has been done five times in this project now.

R21, R22 and R23 are done when an inherited account is read by its family's
reader, a declared profile is the one read, a provider declaring nothing behaves
exactly as today, and no identity check against a named function remains in the
dispatch.
