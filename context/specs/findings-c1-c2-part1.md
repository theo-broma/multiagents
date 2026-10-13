# Review findings C1/C2, part 1: the high-severity ones

Source: `context/plans/2026-10-13-findings-c1-c2-part1.md` (items 1 to 5).
Findings: F24 (with its duplicate F71), F30, F62, F121, F135. Every commit
cites the requirement ids below and the finding id.

Where a characterization test pins the defect, a tester reverses it
deliberately (F150's lesson). An implementer never edits it.

Grouping, for routing only:
- **Group C1** (HF1 to HF3): F24, F30, F62. These are sandbox and
  credential-boundary items, so the adversary runs on this group before
  merge.
- **Group B** (HF4): F121, in `budget.py`.
- **Group A** (HF5): F135, in `defaults/providers/opencode.sh`.

## HF1 — F24/F71: a non-JSON upstream error body is redacted

`authproxy._scrub(payload: bytes) -> bytes`.

**HF1.1.** A body that parses as JSON keeps its current behaviour:
`redact.scrub` is applied to the parsed value and the result is re-serialised.
Verified by: the existing JSON scrub tests, unchanged.

**HF1.2.** A body that does not parse as JSON is no longer returned
untouched. It gets the same text redaction that `redact.scrub` applies to a
string value: the registered literals and the secret patterns are each
replaced by `redact.MASK`. Verified by: a test registering a literal, then
passing an HTML error page that contains it and a bearer-token-shaped
string; neither appears in the output. The characterization test
`test_scrub_falls_back_to_original_bytes_on_non_json_error_body` is reversed.

**HF1.3.** A non-JSON body that is not valid UTF-8 is still redacted where it
contains a secret: it is decoded and re-encoded as UTF-8 with
`surrogateescape`, without normalisation, so every byte outside a redacted
span is preserved exactly, including the invalid sequences. `_scrub` never
raises on any input. If the redaction itself fails, the upstream content is
withheld: the result is a short fixed body that contains none of the
upstream bytes, never the original payload. Redaction must not fail because
a literal is registered concurrently. Verified by: a
body with invalid UTF-8 bytes on both sides of a registered literal; the
output keeps those bytes and masks the literal. Also a fuzz-style test over
arbitrary bytes that asserts no exception is raised.

**HF1.4.** A **non-JSON** body with nothing to redact comes out byte-identical
(a JSON body is re-serialised as today, HF1.1). Verified
by: a plain-text and a binary body, compared with `==`.

**HF1.5.** The response's `content-length` matches the scrubbed body that
is sent. It already holds for JSON, and it must also hold when the
non-JSON redaction changes the length. Verified by: a handler-level test
with an upstream HTTPError whose body shrinks or grows under redaction.

**Out of scope:**
- the `content-type` header, which F22 handles in part 2;
- what counts as personal data, which F21 handles in part 2.

The fallback branch that returns the payload when `redact` cannot be
imported is unchanged.

## HF2 — F30: a mount collision is never resolved silently

`DockerExecutor.mounts()` and `DockerExecutor.configuration_problems()`.

**HF2.1.** When an `extra_mounts` entry names the same path as another mount
in the list, and the two disagree on `read_only`, the configuration is
**refused**. It does not matter whether the other mount is built-in or
another `extra_mounts` entry. `configuration_problems()` returns a message
that names the path and says which entry asked for read-only. `mounts()`
therefore raises `ValueError`, as it does for every configuration problem.
Read-only does not quietly win either: the user wrote two contradictory
things, and is told so.
Verified by:
- the characterization tests
  `test_extra_mounts_read_only_is_silently_dropped_when_path_collides_with_root`
  and `test_duplicate_extra_mounts_entries_first_one_wins`, reversed;
- a test for an `extra_mounts` read-only entry on a built-in writable path,
  such as the worktrees directory.

**HF2.2.** Entries at the same path that agree on `read_only` are not a
problem. They are deduplicated as today. Verified by: two identical entries
yield one mount and no problem.

**HF2.3.** Collisions among built-in mounts alone are unchanged by HF2.1.
Paths are compared after `expanduser`, the same way they are mounted.
Verified by: an executor with no `extra_mounts` reports no new problem, on
the existing fixtures.

**HF2.1a — what is compared.** The `extra_mounts` entries are compared with
the complete final mount list, including what is appended after the
deduplication: CLI binaries, provider homes, private-state projections,
transcript mounts, adapters. Comparison is exact path equality after
`expanduser`, lexical, whether or not the path exists. A parent/child overlap
is not a collision: the deepest mount wins by design, and the adapter
overlays rely on it. The check is pure: it creates no directory, calls no
docker command and does not recurse through `mounts()`'s own call to
`configuration_problems()`. The existing transport symlink and ancestor
protections (`tests/test_nc_m1_adv3_docker.py`) stay as they are; HF2 does
not replace them.
Verified by: a read-only `extra_mounts` entry equal to a provider home path
is refused, and `test_extra_mounts_wins_over_a_colliding_cli_binary_mount_because_it_comes_first_in_out`
is reversed (a disagreeing collision with a CLI binary mount is refused, an
agreeing one is valid).

**HF2.4.** The rule is recorded as the one phase 7 part 2 carries over: a
collision at the same path between a user-configured mount and any other
mount, with different `read_only` values, is refused, never resolved
silently. Built-in overlays are not concerned. Verified by: this paragraph. It has no test of its own.

## HF3 — F62: the auth proxy's bridge connect is checked

`DockerExecutor.ensure_auth_proxy() -> dict`.

**HF3.1.** When `docker network connect bridge <auth container>` exits
non-zero after a successful create, `ensure_auth_proxy`:
- removes the half-created auth container;
- returns `{"ok": False, "error": <stderr, truncated as for the create>}`.

It never answers `ok`. (ST2's F61 fix on the egress proxy returns the error
but does not remove the container; HF3 goes further and does not copy it.)
Verified by: a test with a fake `_run` failing only the connect call. It
asserts `ok` is false, the error is carried, and a `docker rm -f` of that
container was issued.

**HF3.2.** A failed connect is judged by the container's actual bridge
membership (an inspect of its networks), never by matching the error text.
If the container is on `bridge` after all, the connect counts as success.
Verified by: a fake connect failing while the inspect shows `bridge`, after
which `ok` is true and nothing is removed.

**HF3.4.** When `ensure_auth_proxy` finds an existing running auth proxy that
is not attached to `bridge`, it connects it. If that connect fails, it
returns the error and does not remove a running container. Verified by: a
fake running proxy without `bridge`, connect succeeding then failing.

**HF3.3.** If removing the container also fails, the returned error
mentions both failures. Verified by: a fake where both calls fail.

## HF4 — F121: an unread provider is usable in either position

`budget.choose_provider(...)`.

**HF4.1.** A provider with no reading is a usable candidate. No reading
means: no `Budget` entry, `known=False`, a stale reading as
`_known_reading()` already defines it, or a known reading whose `headroom`
is `None`. An active cooldown still makes a provider unusable, and so does
anything `Budget.usable` already refuses; "no reading" never overrides those.
A provider with no reading This holds in the preferred slot and
in any fallback position (Tier B routes and Tier C chain). It is still
subject to:
- `allowed`;
- the family rules;
- and anything else that is not about headroom.

The rule is the module's own: unknown headroom is not no headroom.
Verified by: the three characterization tests named in F121, reversed to
this decision, and a test with an unread provider as the only fallback.

**HF4.2.** When an unread provider is chosen, the returned reason says that
it has no quota reading. It never says it "has headroom". Verified by:
asserting the reason text contains no "headroom" claim and says there is
no reading.

**HF4.3.** Tier A keeps its reservation precedence and its instance
strategy (`soonest_reset`, tolerance, load, last use) as today. Within the
eligible pool that results, and within one tier, a candidate with a fresh
reading and usable headroom is preferred to an unread one. An unread candidate is preferred to
one whose reading shows it is exhausted or below the reserve. Verified by:
a test with a known, an unread and an exhausted provider in the same tier.

**HF4.4.** Tier order wins over readings. An unread Tier B route is chosen
before a Tier C entry that does have a reading. Short-reset waiting
(`wait_for_reset_within`) keeps its current place in the order. Verified
by: a test with these two candidates.

## HF5 — F135: a credential count at line start is parsed

`opencode.sh check`. The user's decision of 2026-10-13 supersedes the
AU-R2 decision of 2026-10-07 in `context/specs/auth-checks-uncertain.md`.

**HF5.0 — what is parsed.** opencode's `providers list` prints provider rows,
a stored-credential total, then a separate environment section with its own
count. Only the stored-credential total counts; provider rows and the
environment count are never read as it. ANSI colour codes and decoration
are ignored. A command failure exits 20, as today. The other checks the
script already does, such as plan-specific ones, are unchanged.

**HF5.1.** The total `N credential` or `N credentials`, with N plain decimal
digits and greater than zero, is parsed as N, whether the number opens the line or follows any
non-digit. The check prints `N stored credential(s)` and exits 0. Verified
by:
- `1 credentials` and `3 credentials stored` at line start;
- the same with a prefix, such as `Found 2 credentials`.

The characterization test at `tests/test_c2_auth_characterization.py:560`,
and `test_opencode_sh_check_count_extraction_fails_when_the_digit_opens_the_line`,
are reconciled with this decision.

**HF5.2.** Zero means no stored credentials. The check exits 10 when the
count is exactly zero, and only then. It no longer matches the substring
`0 credentials`, which also matches `10 credentials` and `20 credentials`.
It exits 10 even when the environment section reports keys. Verified by:
`10 credentials` exits 0 reporting 10, `0 credentials` exits 10, and
`0 credentials` followed by an environment section with a count exits 10.

**HF5.3.** Output that contains no count at all exits 20 with the unknown
message, as today. This includes empty output. So does ambiguous output:
two different credential totals, or a malformed count such as `-1` or
`1.5`. Verified by: the existing
AU-R2 tests for unrecognised wording.

**HF5.4.** The decision note in `auth-checks-uncertain.md` (AU-R2) gains a
line saying it is superseded by HF5, with this path. Verified by: review.

Done when `tests/test_zai_provider.py`, `tests/test_au_*.py` and the full
suite are green against the baseline.

## HF6 — the auth proxy sends the account token only to the model API

Found by the adversary on 2026-10-13 (run ag-198e9b), outside the review's
findings. It predates HF1. `authproxy` builds the upstream URL as
`UPSTREAM + self.path`. A request target that does not start with `/`, such
as `.attacker.invalid/v1/messages`, therefore makes the proxy call
`https://api.anthropic.com.attacker.invalid/...` with the account token in
`Authorization`. The auth container reaches the internet through `bridge`.

**HF6.1.** Before any account is chosen or any token is read, the proxy
checks the URL it would request. Its scheme and host must be exactly those
of `UPSTREAM`: `https`, the same hostname, and the default port. It must
have no userinfo. Checking the URL actually built, not the raw target, is
what makes the check hold for every spelling. Verified by: request targets
`.attacker.invalid/x`, `-x.attacker.invalid/x`, `@attacker.invalid/x`,
`:8443/x` and an absolute-form `http://attacker.invalid/x`. None of them
causes an upstream request, and no token appears in any request a fake
opener records.

**HF6.2.** A refused target gets a `400` response with a short fixed body
and no upstream content. The refusal is recorded as a proxy event naming the
agent, never the target text verbatim. The proxy keeps serving the requests
that follow. Verified by: a handler-level test that sends a refused target
and then a normal `/v1/messages`, which is forwarded.

**HF6.3.** Normal targets are unchanged. That covers an origin-form path
starting with `/`, with or without a query string such as
`/v1/messages?beta=true`. Verified by: the existing authproxy tests, unchanged.

## Decisions

1. **The F30 choice (orchestrator, 2026-10-13).** The plan offered two
   options: read-only wins, or the configuration is refused. I chose
   refusal.
   - **The root is already read-only.** Since SG-R2 it is mounted read-only,
     so the original direction of F30 no longer exists.
   - **What collides today.** The remaining collisions are against writable
     built-ins: the worktrees, the homes, the `.git` subdirectories and the
     provider homes.
   - **Why not read-only wins.** There, letting read-only win would break
     agents in a way that is hard to diagnose, the same kind of silent
     surprise as F30 itself.
2. **Cleanup (plan item 6).** The orchestrator disposes of the old nodes
   itself, with no delegation:
   - nd-74628b22 and its reviewer nd-65004b6a;
   - nd-7866cea2;
   - nd-0950c9d2, which is never merged;
   - the tester and adversary leftovers.

   `/var/tmp/main-host` is removed.
3. **The advisor's review of this contract (2026-10-13)** is folded in:
   - HF1.3 and HF1.4 are scoped to non-JSON bodies, and the failure policy
     is to withhold, never to pass the bytes through;
   - HF2.1a: what is compared, and the pure check;
   - HF2.4 is scoped to user-configured mounts;
   - HF3 judges by inspected membership, not by the error text, and adds
     HF3.4;
   - HF4.1: the cases with no reading, including stale ones; HF4.3 keeps
     Tier A's precedence;
   - HF5.0 and HF5.3: what output is parsed, and ambiguity.
4. **The adversary's pass on HF1 to HF3 (2026-10-13, run ag-198e9b).**
   - **The token sent to an arbitrary host** is a new requirement, HF6.
   - **A server install path:** an `extra_mounts` entry equal to one, set
     writable, silently replaced the read-only mount. HF2.1a already covers
     this, because the comparison is over the complete final list, which
     includes `server_mounts`. It is an implementation gap, not a contract
     change.
   - **A non-JSON error body in UTF-16 or UTF-32** is not redacted, because
     HF1.3 decodes as UTF-8. This is accepted, not fixed:
     - the body comes from `api.anthropic.com`, and only that host chooses
       the encoding;
     - the agent controls neither the encoding nor the body;
     - guessing an encoding without a BOM would trade a theoretical leak for
       mangled bytes.

     The adversary's three tests for it are not merged.
   - **Two leaks that predate HF1 and that HF1.1 keeps:** a registered
     literal used as a JSON key, and JSON nested more than 24 levels deep.
     They go to part 2 with F21, which decides what counts as personal data.
