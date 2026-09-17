# C1 — Auth proxy request handling characterization

Continues `context/review/HANDOFF-c1-authproxy.md`. Findings F20/F21 were
candidates that handoff named but never filed; both are confirmed here by
running the real `authproxy.serve()` against a fake upstream (never the real
`https://api.anthropic.com` — see the handoff's safety note). F22-F24 are new,
found while confirming the handoff's remaining open items. Proofs are split
across `tests/test_char_c1_authproxy.py` (written by an earlier auditor run,
covers header/body/query forwarding, F21's core case, F20's unreachable-
upstream half, redirects, and large bodies) and
`tests/test_c1_authproxy_characterization.py` (this run: account-credential
edge cases, rate-limit switch/exhaust sequencing, the HTTPError-passthrough
half of F20, the scrub key/shape boundary, and the GET/POST alias).

**F20** — Ordinary upstream errors (anything but 429/529) emit no `on_event` at all
*Class:* correctness
*Severity:* medium
*Where:* `src/multiagents/authproxy.py:301-328` (`Handler._forward`)
*Evidence:* reproduction
*Proof:* `tests/test_char_c1_authproxy.py::test_authproxy_upstream_error_emits_no_events`
(unreachable upstream, the bare `except Exception` branch, 502) and
`tests/test_c1_authproxy_characterization.py::test_ordinary_500_passthrough_preserves_status_and_emits_no_event`
(a live HTTPError from upstream, the `except urllib.error.HTTPError` branch
for a non-429/529 code, 500) — both confirmed with `events == []`.
*What happens:* `_event(...)` is called from exactly three places in the
whole file: `do_POST`'s `rejected` (bad/missing token) and `exhausted`
(no account free) branches, and `_forward`'s `switch` branch (429/529 only,
called from `do_POST` after `_forward` returns `"limited"`). Neither of
`_forward`'s two failure branches that *don't* involve rate limiting — a
genuine HTTPError like a 401/403/500/502 from upstream, or the bare
`except Exception` for an unreachable/DNS-failed/TLS-failed upstream — calls
`_event` at all. An operator or the runner's own supervision, watching only
`on_event`, sees rate-limit churn (`switch`, `exhausted`) and rejected tokens,
but is completely blind to the upstream itself returning errors on otherwise
well-formed, well-authenticated requests.
*Disposition:* fix
*Reasoning:* The module docstring's own stated value proposition for this
proxy beyond credential protection is "it is the only place every agent's
traffic passes through" for measurement — "measuring is not preventing, and
nothing here refuses an expensive request," which presumes something
*is* watching every outcome. A 500 flood from upstream, or an upstream host
that becomes completely unreachable (a real outage, a broken egress rule,
a misconfigured `UPSTREAM`), passes through to every agent as a per-request
HTTP error with zero signal to whatever is consuming `on_event` — the one
thing this file was built to let something outside the container observe.

**F21** — `_scrub` only removes secret-*shaped* content; plain PII (organisation, email) in an upstream error body passes through completely unredacted
*Class:* security
*Severity:* medium
*Where:* `src/multiagents/authproxy.py:301-325` (`_forward`'s HTTPError
branch) calling `authproxy.py:359-367` (`_scrub`) calling
`src/multiagents/redact.py` (`scrub`/`_SECRET_KEYS`/`_PATTERNS`)
*Evidence:* reproduction
*Proof:* `tests/test_char_c1_authproxy.py::test_authproxy_scrub_leaves_pii_unscrubbed`
and, more precisely, `tests/test_c1_authproxy_characterization.py::test_scrub_masks_a_key_named_like_a_secret_but_not_its_sibling_pii`
(same body, same response: `api_key` dropped to `[redacted]`, `organization`
and `email` keys survive byte-for-byte).
*What happens:* `redact.scrub` has exactly two mechanisms: drop a dict value
wholesale when its *key* matches `_SECRET_KEYS` (`access_token`, `api_key`,
`secret`, `password`, `credential`, etc.), or mask a *value* when it matches
one of a fixed set of token-shape regexes (JWT, `sk-...`, `ghp_...`,
`AIza...`, `bearer ...`). Neither mechanism has any notion of "this looks
like an organisation name" or "this looks like an email address" — a value
under an innocuously-named key (`"organization"`, `"email"`, `"message"`)
survives untouched regardless of content.
*Disposition:* fix (or fix the comment)
*Reasoning:* `authproxy.py:315-318`'s own comment says, verbatim: "Upstream
errors name the account, the organisation, sometimes the email. That reply
is about to be handed to a container running somebody else's code with
approvals off, so it is scrubbed first." The code does not do what the
comment says it does for two of the three things the comment names as the
reason for scrubbing. Either the comment overstates what `_scrub` covers (a
documentation bug that will mislead the next person reasoning about this
boundary, exactly as it misled the handoff's author into calling this
"probably the single highest-value untested case"), or `_scrub`/`redact.scrub`
needs a mechanism for org/email-shaped PII, not just credential-shaped
secrets. Lower severity than F24 below because this is PII, not a credential
capable of authenticating anything — but it is exactly the gap the comment
claims does not exist.

**F22** — The error-passthrough branch of `_forward` drops every upstream response header except a proxy-generated `content-type`/`content-length`
*Class:* correctness
*Severity:* low
*Where:* `src/multiagents/authproxy.py:309-325` (`_forward`, `except
urllib.error.HTTPError` branch), contrasted with the success branch at
lines 335-342
*Evidence:* reproduction
*Proof:* `tests/test_c1_authproxy_characterization.py::test_ordinary_401_passthrough_drops_all_upstream_headers`
— a custom header (`X-Upstream-Marker`) set on a 401 upstream response is
confirmed absent from the client-visible response; only `content-type` and
`content-length`, both generated by the proxy itself, are present.
*What happens:* The success branch (`with upstream: ... for key, value in
upstream.headers.items(): ...`) forwards every upstream response header
except `transfer-encoding`/`connection`/`content-length`. The HTTPError
branch does none of this — it never touches `exc.headers` at all, so a
`Retry-After` on a plain (non-429/529) 4xx, a `WWW-Authenticate`, or any
other header the real upstream would have sent alongside an error is
silently discarded, replaced by nothing.
*Disposition:* fix
*Reasoning:* Low severity because no observed case turns this into a
security issue (headers dropped, not leaked), and the two response paths
already differ in purpose (streaming success vs. a small scrubbed error
body). Still an inconsistency worth naming: a client that relies on a header
the real Anthropic API sends alongside a particular error code gets it on
some status codes (whatever reaches the success branch) and never on others
(anything that raises `HTTPError`), for reasons invisible from outside this
file.

**F23** — Every HTTP verb reaching the proxy is forwarded upstream as `POST`, including a literal `GET`
*Class:* bug
*Severity:* low
*Where:* `src/multiagents/authproxy.py:299` (`do_GET = do_POST`) and
`authproxy.py:305-306` (`_forward` hardcodes `method="POST"` on the outbound
`urllib.request.Request`, ignoring `self.command`)
*Evidence:* reproduction
*Proof:* `tests/test_c1_authproxy_characterization.py::test_get_request_is_forwarded_upstream_as_post`
— a client `GET` to the proxy is confirmed, via an upstream handler that
echoes back `self.command`, to arrive at the upstream as `POST`.
*What happens:* `do_GET` is not a separate handler; it is the same function
object as `do_POST`, and that function always builds its upstream request
with `method="POST"` regardless of which verb the client actually used to
reach the proxy.
*Disposition:* fix (or drop `do_GET` entirely if it exists only to answer
GET with the same 401/429/whatever a POST would get)
*Reasoning:* Low severity in the current system because every real caller is
the Claude/Anthropic Messages API client, which only issues POSTs here — but
the alias is silent and total: nothing rejects a GET as the wrong verb, and
nothing about the response tells a caller their verb was ignored. If a
future caller (health check, a different SDK, a debugging `curl -X GET`)
ever hits this proxy, it will get a POST's worth of upstream behaviour
without any indication that happened.

**F24** — `_scrub` is a complete no-op on a non-JSON error body, bypassing even its shape-based patterns
*Class:* security
*Severity:* high
*Where:* `src/multiagents/authproxy.py:359-367` (`_scrub`)
*Evidence:* reproduction
*Proof:* `tests/test_c1_authproxy_characterization.py::test_scrub_falls_back_to_original_bytes_on_non_json_error_body`
— an upstream error body containing an `sk-live-...`-shaped string, wrapped
in HTML rather than JSON, passes through completely unscrubbed, byte for
byte, including the token-shaped substring `_PATTERNS` would otherwise catch.
*What happens:* `_scrub` is `json.dumps(scrub(json.loads(payload)))` inside a
bare `try/except Exception: return payload`. `redact.scrub` (the function
that applies both the key-based and the shape-based regex protections) is
only ever reached through `json.loads` succeeding first. Any error body that
is not valid JSON — an HTML error page from a gateway/CDN in front of the
real API, a plaintext message, a truncated/corrupted body — skips `scrub`
entirely and is relayed to the client (and to whatever it does with the
response) exactly as received.
*Disposition:* fix
*Reasoning:* This is more severe than F21: F21 is about PII that no
mechanism in `_scrub` was ever designed to catch. This is about the
mechanisms that DO exist — the JWT/`sk-`/`ghp_`/`bearer` shape patterns,
specifically built to catch credential-shaped text without needing a
recognised key name — being skipped outright whenever the body isn't valid
JSON. `UPSTREAM_TIMEOUT` and the rest of this file's design treat
`api.anthropic.com` as trustworthy but not infallible (see the unreachable-
upstream and redirect-handling behaviour already pinned); a non-JSON error
response from something in front of it (a proxy, a load balancer, a WAF
block page) is exactly the kind of response likely to NOT be the clean JSON
`{"type":"error",...}` shape this code otherwise assumes, and is the one
case where `_scrub`'s protection silently does not apply.
