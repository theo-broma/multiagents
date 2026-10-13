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

## Part 2b — sandbox, egress and the auth proxy (plan items 1 and 3)

Specified once HF6 had passed review. Findings: F2, F11, F63 (group SE,
`executor/docker.py`) and F21, F66, F67, F23, F22, F20 plus the two leaks
deferred by part 1's Decision 4 (group AP, `authproxy.py`, `redact.py`). Both
groups are credential or egress boundaries, so the adversary runs on each
before merge.

### SE — sandbox and egress

`DockerExecutor.allowlist_entries() -> tuple[list[str], list[str]]` returns
the accepted hosts and the diagnostics. A diagnostic reads `refused
allowlist entry {entry!r}: {reason}`. Diagnostics stay non-fatal (ST9): they
are printed to stderr wherever, and in whichever network modes, they are
printed today; this part changes which entries are refused, not where the
refusal is shown.

**SE1 — F2.** An `egress_allowlist` entry that is not a string (a number, a
boolean, `null`, a mapping, a list) is refused with the reason `not a
string`. It never raises, and the other entries are still read. A whole
`egress_allowlist: null` stays an empty list. A whole value that is not a
list (a string, a number, a boolean, a mapping) is refused as a whole with
one diagnostic, `refused egress_allowlist {value!r}: not a list`, and allows
no host; it is never iterated, so a string is not read character by
character (Decision 13). Verified by: `[5]`, `[true]`,
`[null]`, `[{}]`, `[[]]` each giving one diagnostic and no exception; a list
mixing one of them with `a.example` still allowing `a.example`.

**SE2 — F11.** Each string entry is checked in this order, and the first
refusal wins:
1. the refusals made today on the raw entry (blank, control character),
   with today's wording;
2. new raw-shape refusals: any whitespace character that is not already a
   control character (`contains whitespace`), exactly one `:` followed by
   digits at the end (`contains a port`; an IPv6 address, having several
   `:`, keeps today's IPv6 refusal and wording), a leading `.`
   (`leading dot`);
3. today's normalisation (IDNA to ASCII, lower case, one trailing dot
   removed, in that order), then today's refusals on the normalised name
   (IPv6, non-canonical IPv4, single label, public suffix), with today's
   wording.

The unused `_validate_allowlist_entry` is not wired in unchanged: it would
refuse trailing dots and non-ASCII labels that are accepted today. The
deferred R15 reds in `tests/test_phase2_entry_semantics.py` stay deferred.
Verified by: `" a.com"`, `"a.com "`, `"a .com"`, `"a\u00a0.com"` (whitespace),
`"b.com:443"` (port), `".c.com"` (leading dot) refused with those reasons
and none of them in the filter; `"d.com."` accepted as `d.com`; `"a\t.com"` and an entry
with both a control character and a space refused as today (control
character); `"::1"` and `"[::1]:443"` refused with today's IPv6 wording; the existing ST9
tests unchanged.

**SE3 — F63.** `credential_drift` never puts a path inside a command
string. Paths reach the container only as separate argv elements, either one
`docker exec` per path or one constant command that takes the paths as
arguments and answers in their order; no shell ever parses a path. What it
reports is unchanged: an inode per readable path; a missing or unreadable
file is not reported as drift, as today; in the container a symlink is not
followed, as today (`stat` without `-L`). Failures are as today: a nonzero
return from any `docker exec` makes the whole result `[]`, discarding paths
already read; docker missing (`FileNotFoundError`) and a timeout
propagate. Verified by:
a fake `docker exec` recording the argv, with paths containing a space,
`;`, `$(...)`, a quote and a leading `-`, each arriving as exactly one
argument and appearing in no command string; drift results for an
existing, a missing and a changed file matching today's; a nonzero exec
on the second of three paths giving `[]`.

### AP — the auth proxy

**AP1 — F21 and part 1's Decision 4 leaks.** In an upstream body passed
back to an agent (`authproxy._scrub`), on top of what HF1 masks:
- every e-mail address is masked, in JSON values, in JSON keys and in
  non-JSON text. An e-mail address is ASCII `local@domain`: `local` is one or
  more of letters, digits and `._%+-`; `domain` is two or more dot-separated
  labels of letters, digits and `-`, the last of two or more letters. It is
  not preceded by a `local` character nor followed by a label character, so
  the mask covers the whole address and only it. `user@host` with no dot
  in the domain is not an e-mail address here;
- in JSON, the value under a key named, case-insensitively,
  `organization`, `organization_id`, `organization_name`, `org`, `org_id`,
  `org_name`, `email`, `user_email` is masked whatever its shape (a string,
  a number, an object, a list). Only that value is masked: its siblings
  (an error's `type`, `code`, `message`) are kept and scrubbed as usual;
- a registered literal used as a JSON key is masked in the key too. When
  masking makes two keys of one object equal, the later one wins;
- a JSON value nested deeper than the scrubber's depth limit (`_depth > 24`)
  is replaced whole by the mask, never passed through raw.

This applies to `_scrub` only. `redact.scrub` keeps its current behaviour
for every other caller (logs, results), so ordinary e-mail addresses in an
agent's own output are not touched. "Unchanged" for a JSON body means the
same parsed value: HF1 re-serialises it. Verified by: an e-mail in a JSON
value, in a JSON key, in a non-JSON page and inside a longer string with
punctuation around it; `user@host` left alone; each listed key with a
string, a number and an object value, with its siblings kept; a literal as
a key; two keys colliding after masking; a 30-level body with a literal at
the bottom; a body with none of these parsing to the same value.

**AP2 — F66.** An exception raised by the `on_event` callback is reported
on the proxy's stderr, once per occurrence, naming the event kind and the
exception type, with its message passed through `redact.scrub`. Reporting is
best-effort: a failure to write it is swallowed. The proxy keeps serving the
request and later ones. Verified by: a callback raising an exception whose
message contains a registered literal; the stderr line contains the kind
and the type, not the literal; the request still completes; a stderr that
raises on write does not break the request.

**AP3 — F67.** The per-account cooldown is the backoff:
- a 429 or 529 marks the account limited for `max(60 s, the upstream
  hint)`, or 3600 s when there is no usable hint. The hint is `retry-after`
  as delta-seconds or as an HTTP-date, else the unified-reset header as epoch
  seconds or as an ISO 8601 timestamp. A hint that does not parse, is not
  finite, or is in the past is not usable;
- a new mark never shortens a cooldown already running on that account,
  whichever request sets it;
- no attempt admitted after the mark uses that account before its cooldown
  ends (attempts already sent upstream are not recalled);
- within one request, each account is tried at most once;
- the proxy never sleeps.

Cooldowns live in the proxy's memory and are lost when the proxy restarts
(Decision 11). TF retries (launch, crash, transient) and quota holds sit
above this and are unchanged. Verified by: tests pinning each bullet,
including an HTTP-date `retry-after`, an ISO reset, `inf` and `nan`, a past
date, and two marks in either order.

**AP4 — F23.** The proxy forwards the request's own method for `GET`,
`HEAD` and `POST` (Decision 10). Each is sent upstream with the same method
and the same HF6 target check. A request carrying a `Content-Length` (even
`0`) is forwarded with that body; one without is forwarded with none.
A request with a `Transfer-Encoding` header gets `411` and nothing is sent
upstream (the proxy reads only `Content-Length` today and would leave chunk
bytes on the connection). Any other method gets `405` (a deliberate change
from today's `501`) with an `Allow: GET, HEAD, POST` header and nothing sent
upstream. After a `405` or `411` for a request that may carry a body, the
proxy closes the connection (`Connection: close`) rather than reading the
next request from unconsumed bytes. A `HEAD`
response carries no body bytes, whatever produced it (upstream, or the
proxy's own 400, 429, 502). Redirects are never followed: for a 3xx from
upstream the status is preserved, the body scrubbed and the headers
filtered under AP5 (so no `Location`). Verified by: each served method arriving upstream unchanged,
with a body, with an empty body and with none; `PUT`, `DELETE` and
`OPTIONS` giving 405 with nothing upstream and the connection closed; a
chunked `POST` giving 411 with nothing upstream; a refused target on `GET` giving
HF6's 400; `HEAD` against an upstream 200, an upstream error and an
exhausted 429, each with an empty body; a 302 not followed.

**AP5 — F22.** On an upstream non-2xx response passed through to the
agent, including the saved 401 returned after every account was tried,
these upstream headers are forwarded: `retry-after`, `request-id`,
`x-request-id`, and every header whose name starts with
`anthropic-ratelimit-`. No other upstream header is forwarded: never
`set-cookie`, `authorization`, `www-authenticate`, `location`, `proxy-*`,
nor any header whose name contains `organization` or `org-`; where a name
matches both rules, it is not forwarded. `content-type` and
`content-length` are the proxy's and describe the body actually sent:
`application/json; charset=utf-8` for a scrubbed JSON body, upstream's own
`content-type` for scrubbed text, the proxy's own type for a body it
replaced. When every account is limited and the proxy answers 429 itself,
it sets `retry-after` to the whole seconds, rounded up, until the earliest
cooldown end among the accounts that agent may use and that are limited
(not rejected), at least 1, read under the same lock as account selection;
when no such account exists, no `retry-after` is sent. Verified by: a 500,
a 400 and the saved 401 with each allowed and each forbidden header,
including `anthropic-ratelimit-organization-id`; a JSON and a text body with
matching `content-type` and `content-length`; the exhausted 429 carrying
the expected `retry-after`, and none when every account was rejected.

**AP6 — F20.** Each upstream attempt emits at most one of the two new
events below; the existing events (`unauthenticated`, `switch`, ...) are
unchanged and not counted. An upstream
response with a status of 400 or more other than 401, 429 and 529 emits
`upstream_error` with `{"agent", "label", "status"}`, even if reading its
body then fails; in that case the agent gets the proxy's own `502` and the
connection is closed. A failure before any upstream status is received
(connection, DNS, TLS, timeout, sending the request) emits
`upstream_unreachable` with `{"agent", "label", "error"}`, where `error` is
the exception's class name only. A failure while relaying a successful
response also emits `upstream_unreachable`, closes the connection, and
never starts a second response. The agent disconnecting is not an upstream
failure and emits nothing. Neither event carries any part of the body or the
target. The events for 401, 429 and 529, and what the proxy retries, are
unchanged: this item adds events only. Verified by: a 500, a 400, a refused
connection, a timeout and a body read failing mid-relay, each emitting
exactly one event with those fields; a client disconnect emitting none; the
characterization tests pinning "no event" reversed.

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
7. **Part 2b groups AP1 (F21) with the proxy**, since it lives in
   `authproxy._scrub`; e-mail and organisation masking is limited to upstream
   bodies, so an agent's own output is not rewritten.
8. **F67 is fixed in place, not by a sleep:** the per-account cooldown
   already honours `retry-after`; AP3 closes its gaps (date formats, non-finite
   hints, a mark shortening a cooldown). A sleep inside the proxy would hold
   the agent's request and duplicate TF's retry and the quota holds.
9. **The exhausted 429 carries `retry-after`** (AP5), so an agent knows when
   to come back instead of retrying blind.
10. **The proxy serves `GET`, `HEAD` and `POST` only.** This
    is a deliberate compatibility limit, not a security boundary: `POST` can
    already cancel a batch. It keeps the surface to what agent work needs
    (messages, token counting) and blocks `DELETE` of account files, which
    no agent task here needs. `405` with `Allow` says so plainly. Widening
    it is a later, explicit decision.
11. **Cooldowns stay in memory.** A proxy restart forgets them; the next
    requests then meet the upstream 429 again and re-mark the account, which
    costs a few requests per account. Persisting them is not worth a state file.
12. **SE2 extends today's checks rather than adopting
    `_validate_allowlist_entry`**, so no entry accepted today on purpose
    (trailing dot, non-ASCII label) starts being refused.
13. **A non-list `egress_allowlist` is refused whole** (SE tester question,
    2026-10-13): reading `"a.example"` as one entry would guess at intent, and
    iterating it, as today, allows single characters. Refusing allows nothing,
    which is the safe failure for an egress control, and the diagnostic says
    why.
