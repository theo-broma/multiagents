# Handoff — C1 authproxy characterization (interrupted, quota cutoff)

**Status: nothing committed yet in the repo.** All work so far was exploratory,
run from `/tmp/explore.py` and `/tmp/explore2.py` (outside the repo, not
recoverable, but reproduced below). No test file or findings file has been
created under `tests/` or `context/review/` yet. This file is the first
commit for this task.

## Task recap

Characterize `authproxy.py`'s request handling (token minting, forwarding,
every failure path) into `tests/test_c1_authproxy_characterization.py`,
findings into `context/review/C1-sandbox-authproxy.md` starting at **F20**.
Harness: `tests/support/c1_harness.py` (`h.authproxy_server`, `h.seed_account`,
`h.fixed_response_upstream`, `h.fake_http_server`, `h.closed_port_url`).

## Critical safety note for whoever resumes

**`h.authproxy_server(..., upstream=None)` (the default) leaves
`authproxy.UPSTREAM` pointed at the REAL `https://api.anthropic.com`.** In my
first exploration script I minted a token, seeded a fake account credential,
and sent a request without passing `upstream=`. It went out over the real
network and got a real 401 back from Anthropic's actual API ("Invalid bearer
token"). Always pass `upstream=<fake_http_server url>` or
`upstream=h.closed_port_url()` — never call `authproxy_server` without it in a
test that reaches `do_POST`/`_forward`.

## What I'd confirmed by direct experiment before being cut off

Using `tests/support/c1_harness.py` against the real `authproxy.serve()`:

1. **No `Authorization` header** → 401, body
   `{"type":"error","error":{"type":"multiagents","message":"this proxy serves multiagents agents only"}}`,
   and `on_event` fires `("rejected", {"reason": "unsigned token"})`.
2. **Malformed token string** (`"garbage"`) → identical 401 + identical
   `rejected`/`unsigned token` event. Same code path as no token at all
   (`read_token` returns `""` for both).
3. **Token minted for a different agent id than the one "acting"** — there is
   no separate "caller identity" to compare against; the token's agent id
   *is* the identity. A token minted via `srv.mint("agentB")` used standalone
   is accepted as agentB and proceeds to account pinning — it is not rejected
   for being "the wrong agent," because there is no wrong agent, only an
   unsigned one. (This request actually reached the real upstream in my
   flawed first script — see safety note above — so the 401 I saw there was
   from Anthropic's real API rejecting a fake access token, NOT from
   authproxy. Needs re-verification with a stubbed upstream.)
4. **Token signed with a different secret** (`mint_token("agentA",
   "wrong-secret")`) → same 401 + same `rejected`/`unsigned token` event as
   no token. `read_token`'s `hmac.compare_digest` check fails closed,
   indistinguishable from "no token" to the caller and in the event log.
5. My second script (`explore2.py`), testing header forwarding, query string
   preservation, and body echo through a stubbed upstream (`Echo` handler
   under `h.fake_http_server`), was killed (exit 137, resource/OOM-looking,
   not a logic error) before producing output. **Not yet verified**: which
   headers reach upstream vs get dropped (`HOP_HEADERS` in `authproxy.py`
   lines 66-68 lists `host, authorization, content-length, connection,
   accept-encoding, proxy-connection, keep-alive, transfer-encoding, upgrade,
   te, trailer` as dropped — everything else should pass through, and
   `Authorization` should be *replaced* with `Bearer <real token>`, not just
   dropped), whether query strings on `self.path` survive, and how the body
   round-trips.

## Read but not yet exercised via the harness — reasoning from source only

(`src/multiagents/authproxy.py`, already read in full this session)

- **Missing/empty/malformed account credentials**: `Accounts.token(label)`
  returns `""` on `OSError`/`ValueError` from `json.loads`, or if no block in
  the JSON has a truthy `accessToken` key. In `do_POST`, an empty token causes
  `mark_limited(label, 300)` and the loop retries — with only one account
  seeded, the next `for_agent()` call finds no free label and returns `""`,
  which triggers the `exhausted` event and a 429. **Not yet run** — should
  confirm with `h.seed_account` writing malformed/empty JSON directly (the
  harness's `seed_account` always writes valid JSON, so a malformed-JSON test
  needs to write `.credentials.json` directly via `Path.write_text`, which is
  fine — that's the test file, not the harness or production code).
- **Upstream 429/529** → `_forward` calls `mark_limited(label, retry_after)`
  and returns `"limited"`; `do_POST`'s loop then either retries another
  account (emitting a `switch` event) or exhausts (`exhausted` event, 429 to
  client). **Not yet run.**
- **Upstream other error codes** (401, 500, etc. via `HTTPError`) → response
  body is passed through `_scrub()` (imports `.redact.scrub`) before being
  relayed verbatim with the same status code. Since `redact.scrub` only
  touches known secret-shaped keys/patterns, an upstream error body that
  contains e.g. an organization name or email (per the code comment at
  authproxy.py:315-318) would pass through scrubbed only for
  recognizable-shaped secrets — worth testing with a body containing
  something scrub-shaped (e.g. `"api_key": "..."`) vs something
  scrub does NOT catch (e.g. a plain email address or org name) to see
  whether that comment's claim ("scrubbed first") actually protects
  non-credential PII or only credential-shaped strings. **This is probably
  the single highest-value untested case** — the module docstring's whole
  thesis is "the credential never enters the sandbox," so what actually
  survives `_scrub` on a realistic upstream error body is worth pinning
  precisely and is a strong F20 candidate if anything credential-shaped
  leaks through unscrubbed.
- **Upstream redirect (3xx)**: `urllib.request.urlopen` follows redirects
  itself by default for GET, but this is always a POST — need to check
  whether `urllib.request`'s default redirect handling applies to POST 3xx
  responses (it generally does NOT auto-follow 307/308 POST redirects the
  same way, and 301/302/303 via urllib can convert POST to GET on redirect
  and follow it under the hood) — this changes what `_forward` even sees.
  **Not yet run**, and genuinely uncertain from reading alone — must observe.
- **Unreachable upstream** (`h.closed_port_url()`): the bare
  `except Exception as exc` branch sends 502 with body
  `f"upstream unreachable: {type(exc).__name__}"` — i.e. only the exception
  *class name* leaks (e.g. `ConnectionRefusedError`), not a message/path/host.
  Confirmed by reading, not yet by running. Worth pinning exactly.
- **Large / empty / non-JSON body**: `do_POST` reads exactly
  `content-length` bytes and passes them as opaque `bytes` to `_forward` —
  nothing parses or validates the body as JSON anywhere in this file, so a
  non-JSON or empty body should just forward unchanged. **Not yet run.**
- **Successful passthrough (`"ok"` outcome)**: uses chunked transfer-encoding
  regardless of what the upstream sent, rewrites/drops
  `transfer-encoding`/`connection`/`content-length` from the upstream's
  response headers, forwards everything else verbatim. No `on_event` call at
  all on plain success — only `rejected`, `exhausted`, and `switch` emit
  events; a normal 200 (or a passed-through 401/500 that isn't 429/529)
  produces **no event**. This asymmetry (errors that reach the account-limit
  logic are observable via events; ordinary pass-through errors like a raw
  401/500 from upstream are not) is worth flagging as a finding, if
  confirmed — it means an operator watching only `on_event` cannot see
  ordinary upstream failures, only rate-limit-driven ones.

## What's left to do (in priority order per the task's own instructions)

1. Re-run the header/body forwarding experiment (`explore2.py`'s intent) with
   a stubbed upstream (`h.fake_http_server` + a handler that echoes back
   received headers/body/path as JSON) — confirm `HOP_HEADERS` behavior,
   `Authorization` replacement, query-string survival on `self.path`.
2. Confirm the missing-account / malformed-JSON / empty-JSON /
   missing-accessToken-key paths against a running server (write the bad
   `.credentials.json` directly, since `h.seed_account` only writes valid
   JSON — that's fine, it's test-file code, not new harness code).
3. Confirm 429/529 → `switch`/`exhausted` event sequence and retry-after
   parsing (`_retry_after`), including the epoch-vs-duration branch
   (`value > 1e9`).
4. Confirm scrubbing behavior on a realistic non-429 upstream error body —
   this is the highest-value untested case, directly on-mission for C1
   ("can a credential leave").
5. Confirm redirect handling (3xx from upstream) — genuinely unknown,
   must observe rather than assume.
6. Confirm the unreachable-upstream 502 body exactly, and the "no event on
   plain error passthrough" asymmetry noted above.
7. Confirm large/empty/non-JSON body passthrough.
8. Write `tests/test_c1_authproxy_characterization.py` covering all
   confirmed behaviors, following the style of the harness-builder's
   `tests/test_c1_sandbox_harness.py` (module docstring, one test function
   per behavior cluster, F-numbered comments referencing findings).
9. Write `context/review/C1-sandbox-authproxy.md` with any findings
   (F20+), same format as `context/review/C1-sandbox.md` (Class/Severity/
   File/Trace/Evidence/Reasoning/Recommendation). Leading candidates
   identified so far (neither confirmed yet):
   - **Candidate F20**: ordinary upstream error passthrough (any status
     that isn't 429/529) produces no `on_event` at all — an operator
     monitoring only events is blind to real upstream failures.
   - **Candidate F21**: whatever `_scrub` does or doesn't catch in a
     realistic (non-token-shaped) upstream error body containing account/org
     identifying info, given the code comment explicitly claims this body
     "is scrubbed first" for exactly that reason.

## Files touched so far

None inside the repo except this handoff file — `git status --short` was
clean before writing this. `/tmp/explore.py` and `/tmp/explore2.py` are
outside the worktree and will not persist; their content is reproduced above
in enough detail to redo them quickly (they use a small hand-rolled
`FakeMonkeypatch` shim since these were run outside pytest — under pytest,
just use the real `monkeypatch` fixture with `authproxy_server`).
