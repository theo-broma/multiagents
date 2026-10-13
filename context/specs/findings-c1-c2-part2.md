# Review findings C1/C2, part 2: the medium and low ones

Source: `context/plans/2026-10-13-findings-c1-c2-part2.md`. Every commit cites
the requirement ids below and the finding id.

Where a characterization test pins the defect, a tester reverses it
deliberately (F150's lesson). An implementer never edits it.

This file covers the plan's items 2, 4, 5 and 6 (part 2a). Items 1 and 3
(sandbox/egress and the auth proxy: F2, F11, F21, F63, F66, F67, F23, F22) and
F20, which lives in `authproxy.py`, are specified in a second pass once HF6
of part 1 has merged, because they touch the same files.

Routing:
- **Group FC** (FC1 to FC3): `auth.py`, `failures.py`, `scripts.py`.
- **Group QB** (QB1 to QB5): `budget.py`, `server.py`, `tree.py`,
  `models.py`. Lock `budget.py`.
- **Group BT** (BT1): tests only, `budget.py`. A tester and a reviewer, no
  implementer. Runs after QB has merged.
- **Group PS** (PS1, PS2): `scripts.py`, `providers.py`. Runs after FC, which
  also touches `scripts.py`.

## FC — failure classification (plan item 2)

`auth.looks_like_auth_failure(status: str, stderr: str) -> bool` decides the
`auth` failure class (`failures.py:160`) and the runner's `unauthenticated`
status (`runner.py:7652`). An `auth` failure is not retried.

**FC1 — F136.** `401` counts only as an HTTP status, never as any substring.
It matches only in an HTTP context:
- preceded by `http`, `http/<version>`, `httperror`, `status`, `status code`
  or `status_code` (case-insensitive), with optional separators `:`, `=`,
  quotes or whitespace between; or
- as the JSON value of a key named `status` or `status_code`; or
- followed (after whitespace) by `unauthorized`.

The character after `401` must not be a letter, digit, `.` or `_`, nor may the
one before it be a digit, `.` or `_`. A bare `code 401` or `error 401` does
not match. `unauthorized` stays a marker on its own. Verified by:
`40105`, `401 items`, `v4.0.1`, `id_401`, `took 401ms`, `HTTP 401ms`,
`exit code 401`, `compile error 401` do not match; `HTTP 401`, `HTTP/1.1 401`,
`HTTPError: 401`, `status: 401`, `status_code=401`, `"status": 401`,
`401 Unauthorized` match; the existing samples in `tests/test_core.py`
(around 1682) still match.

**FC2 — F137.** These phrasings are recognised, case-insensitively:
`session expired`, `session has expired`, `sign in again`, `api key missing`,
`missing api key`. Every phrase recognised today still is. The permission
veto (`permission`, `auto-denied`, `dangerously-skip` without a login word)
is kept, but its login-word list gains `sign in`, `session` and `api key`,
so a mixed message such as `permission denied; sign in again` is an auth
failure. Verified by: the four misses characterized in
`tests/test_c2_auth_characterization.py` (reversed), each new phrase alone,
and a quota/rate-limit message that contains none of them staying
non-auth.

**FC3 — F114.** `scripts.run_action` returns distinct codes for a run that
timed out and a script that could not be started, defined as named module
constants and listed in its docstring:
- `EXIT_TIMEOUT = 124`: the script started and ran past its timeout;
- `EXIT_NOT_STARTED = 126`: the script could not be started at all (any
  `OSError` at start: not executable, bad format, missing interpreter).

127 keeps its current meaning, unchanged: `run_action` returns it when no
script resolves for the provider (`scripts.py:285`, "no script for
provider ..."), and a script may return it for an action it does not
implement. `budget._from_script` treats 127 as "no script" and answers with
the built-in reader. A script that exists but cannot be started is a broken
installation, gets 126, and must never take that path. stderr keeps its current reason text. The docstring
says that a script may itself exit with any of these codes, so a code alone
does not prove where it came from. No other caller changes behaviour.
Verified by: the two characterization tests pinning 124 for both cases
(reversed); a script with a missing interpreter returning 126 and the budget
reader reporting it as a failure with the reason, not falling back.

## QB — quotas and budget (plan item 4)

**QB1 — F171, verify only.** A cooldown decoration applied by `read_all`
never reaches the cached `Budget`: within the cache TTL, repeated reads
carry the cooldown note exactly once, and once the cooldown has lapsed the
severity is the one derived from the reading, not `critical`. The researcher
found this already holds on main (decoration applies to a per-call copy).
Verified by: a tester's tests; if they are green on main, no implementer
runs and the ledger cites the tests' merge commit.

**QB2 — F160/F161.** A usage record **reports tokens** when at least one of
`total`, `total_tokens`, `input_tokens`, `output_tokens`,
`cache_read_input_tokens`, `cache_creation_input_tokens` is a number (not a
boolean). Its count is `tree.token_count()`'s. A record with none of them,
but with other usage (for example only `cost_usd`), is **unreported**. A run
with no usage record at all is neither, as today.

Everywhere `server.budget_status()` reports tokens (provider `spent`
aggregates, `by_model` rows, `tree_usage`) and in `Tree.render()`'s per-node
label:
- `tokens` is the sum over reporting records;
- a `tokens_unreported` field gives the number of unreported records (0 when
  none);
- when no record reports and at least one is unreported, `tokens` is `null`
  rather than 0; `by_model` sorts a `null` as below every number;
- `render()` shows `tok?` for a node whose usage is unreported, and the count
  otherwise; a record reporting 0 shows 0.

This is computed on read. Stored usage, token-budget enforcement and
`cost_usd` are unchanged; existing stored zeros are not reinterpreted.
Verified by: a Claude-shaped record (no `total` key) counted in each place;
a cost-only record giving `tokens: null`, `tokens_unreported: 1` and `tok?`;
a mix of both giving the reporting sum and `tokens_unreported: 1`.

**QB3 — F123.** In a script reading, an absent or `null` `headroom` keeps
its current meaning (unknown). A supplied `headroom` that is not a finite
number in [0, 1] (a boolean, a string, NaN, an infinity, a value outside the
range) refuses the whole reading: the `Budget` has `known=False`,
`headroom=None`, `severity="unknown"`, no windows, and a note naming the
value received. 0 and 1 themselves are accepted. Verified by: `-0.1`,
`1.5`, `85`, `true`, `"0.5"`, NaN refused, with windows in the same reading
ignored; `0`, `1`, `0.5`, `null` and absent accepted as today; the
characterization tests pinning out-of-range acceptance reversed.

**QB4 — F124/F142.** An empty successful script response (blank stdout)
gives a `Budget` with `known=False` and the note `script returned no
reading`; a script that supplies its own note keeps it. `read_claude` names
`cachedUsageUtilization` as its `source` only when a utilization bucket was
actually read from it; when nothing readable was found (no cache, a failed
fetch, or utilization present with no readable bucket), `source` is
`"none"`. Verified by: a blank stdout; `read_claude` with no cache and a
failing fetch; utilization with no readable bucket.

**QB5 — F143.** `models.refresh_models` writes `target` atomically: it
writes a temporary file with a unique name in the same directory, then
replaces `target` with it in one rename. If anything fails before the
rename, `target` is unchanged, the temporary file is removed, and the error
propagates as today. Concurrent refreshes each publish a whole file; the
last rename wins. An existing `target`'s permission bits are kept. Verified
by: a failure injected during serialisation leaving the previous content
intact and no stray temporary file; the mode of an existing target kept.

## BT — tests that protect nothing (plan item 5)

**BT1 — F151, F152, F153, F155, F156.** Tests only. Each boundary below is
pinned so that the named mutation makes at least one test fail:
- `reserve` admission `headroom >= reserve` (mutation: `>`);
- the severity thresholds at 75 and 90, in `read_claude` and in the script
  headroom conversion (mutations: each threshold moved by one, `>=` to `>`);
- the cache TTL hit `age < _CACHE_TTL` (mutation: `<=`);
- `Budget.usable`'s exhaustion `headroom <= 0.02` (mutations: `<`, 0.02
  changed);
- the two untested `usable` behaviours: a stale reading with exhausted
  headroom is usable; a cooldown ending exactly now is not active.

Verified by: the reviewer applies each mutation and sees a test fail.

## PS — provider seam (plan item 6)

**PS1 — F113.** One identity per script run. `MULTIAGENTS_PROVIDER` in a
script's environment names the configured provider instance the action runs
for (for example `codex-b`), including when its script is inherited through
`extends:` or borrowed from an owner account. It is never the parent's name:
scripts and the docker executor select account secrets from it. It is set
after the provider and action environment overlays, so no overlay can
change it.

The `Provider` object passed to `run_action`, `exec_action`, `resolve` and
`build_env` carries that instance identity as its `name`, while keeping the
script, credentials and account pin it inherits. The callers that copy an
owner's `Provider` for a borrowed account (`auth.py` around 99 and 189) build
the copy that way. A `provider_name` argument that differs from
`provider.name` is then a programming error: these functions raise
`ValueError` naming both, before running anything. That is the one
exception to `run_action`'s "never raises", and its docstring says so. A
provider-like object without `name` falls back to the `provider_name`
argument, as today. Verified by: the two characterization tests in
`tests/test_c2_seam_characterization.py` (reversed); a child extending a
parent seeing its own name; a borrowed-account auth check seeing the
instance name and running the owner's script; an env overlay setting
`MULTIAGENTS_PROVIDER` being overridden.

**PS2 — F133.** Loading refuses an `extends:` that names a provider not
defined, at any link of the inheritance chain, with
`ValueError("provider 'child': extends: no provider named 'ghost'")`, the same
form as the other provider-load refusals. A parent that is defined but
disabled is accepted. No family is formed from an unknown name. The
`resolve_inheritance` docstring describing the old degradation is updated.
An existing configuration with such an entry now fails to load with this
diagnostic; nothing rewrites it. Verified by: the missing-parent
characterization tests in `tests/test_c2_auth_characterization.py`
(reversed); a missing grandparent; a disabled parent; the shipped providers
still load.

## Decisions

1. **Part 2 is split.** Items 2, 4, 5 and 6 are specified here and run
   while HF6 is in flight. Items 1 and 3, and F20, follow once HF6 has merged
   (same files: `authproxy.py`, `executor/docker.py`).
2. **F113 reads "the provider whose script runs" as the instance**, not the
   parent whose script it inherits, to keep account separation.
3. **F171 is verify-only:** main already decorates a copy.
4. **F123 refuses rather than clamps:** a value outside [0, 1] says the
   script is wrong, and clamping would hide it. The whole reading is refused,
   so its windows cannot reintroduce a headroom.
5. **F114 leaves 127 alone** (advisor, 2026-10-13; corrected the same day
   after the FC tester found `run_action` itself returns 127 for "no script"):
   127 means "no script / action not implemented" to the budget reader, which
   then falls back to its built-in reader. Every start failure of an existing
   script is 126.
6. **F136 requires HTTP context** for `401`; bare `code 401`/`error 401`
   are not enough.
