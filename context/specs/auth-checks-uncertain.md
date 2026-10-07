# Connection checks say "unknown", never "logged in", when they cannot tell (AU)

Findings: **F130**, **F131**, **F132**, **F140** (context/review/C2-auth.md,
context/review/C2-audit.md). Plan: `context/plans/2026-10-05-scheduler-first-real-use.md`
§2, group *auth*.

The scripts' existing convention (run_action's contract, agy.sh host branch):
uncertain → exit 20 with an `unclear:`/unknown message, never a false positive.
This contract extends it to the cases the review found.

## Behaviours

**AU-R1 (F130).** `claude.sh check`, container branch — both the legacy profile
branch and the vault branch (claude.sh:281, 364): when the credentials file
exists but no expiry can be read from it — invalid JSON, or an access token with
no `expiresAt` — the check exits 20 and its message says the state is unknown
and why (unparseable / no expiry). It no longer prints "container profile is
logged in". The already-handled cases (missing file, empty oauth block, expired
clock, valid clock) keep their current outcome.
Verified by: the characterization tests
`test_claude_sh_check_unparseable_json_is_also_reported_as_logged_in` and
`test_claude_sh_check_an_access_token_with_no_expiresat_is_also_logged_in`
inverted (renamed to the new behaviour), plus a test that a valid, unexpired
file still reports logged in.

**AU-R2 (F132).** `opencode.sh check`: when `$BIN providers list` output matches
neither the "0 credentials" case nor the positive-count pattern — including
empty output with exit 0 — the check exits 20 with an unknown message. It never
defaults to "1 stored credential(s)". A parsed positive count and the
"0 credentials" case keep their current outcome.
Verified by: the silent-success characterization test inverted; tests for
unrecognised wording and for the two recognised cases.
Decision (orchestrator, 2026-10-07): the positive-count pattern is NOT widened. Output it does not match, such as "3 credentials stored" with the digit opening the line, is unknown (exit 20). The characterization test `test_opencode_sh_check_count_extraction_fails_when_the_digit_opens_the_line` is inverted accordingly. The question came from implementer run ag-0299a2.

**AU-R3 (F131) — a message change, not a parsing change.** The agy container token
file has no documented format and agents may not read real credential files, so
presence is all the check can establish. The check's success message states
that the token was found and not verified (wording such as
"container token present (not verified)"), and the script's comment says the
check answers "was a login attempted", not "is the account usable".
Verified by: the garbage-token characterization test updated to assert the new
message (exit code unchanged).

**AU-R4 (F140).** `_claude_token` (the audit's token reader): when `expiresAt`
is present but not a finite number of milliseconds — a string (numeric or
not), a boolean, NaN, infinity, null-like values — the token is treated as
unusable: the function returns what it returns for an expired token.
Redaction registration is unchanged (a token read from disk is still
registered for redaction, whatever its expiry). A numeric past expiry and a numeric future expiry
keep their current outcome; an absent `expiresAt` keeps its current outcome.
Verified by: `test_claude_token_returns_token_when_expires_at_is_non_numeric`
inverted; tests for numeric past/future and absent.

**AU-R5.** Every caller of these checks treats exit 20 as it already treats
"unknown" (no new code path is required if that already holds; if a caller maps
exit 20 to "logged in" or to "not logged in", say so and stop — that is a
decision).
Existing consequences of "unknown" stay as they are and are accepted: doctor
and repair flows list an unknown state as needing attention (cli.py:429, 1741,
2392); the driver permits it (driver.py:1224).
Verified by: review, and the existing auth/doctor suites green.

## Constraints
- Never read or print real credential files (auth.json, .credentials.json,
  vault). Tests use synthetic files under tmp_path.
