# C1 — Sandbox and egress boundary

Findings from building the test harness for this context. Both were found by
reading the code and confirmed by calling the real production method
(`DockerExecutor.write_proxy_config`) against a temp directory, not by
reasoning about it in the abstract — see `tests/support/c1_harness.py` and
`tests/test_c1_sandbox_harness.py::test_an_unescaped_pipe_in_an_allowlist_entry_admits_every_host`
for a reproduction.

**F1** — An `egress_allowlist` entry containing `|` turns the allowlist into an allow-all
*Class:* security
*Severity:* critical
*File:* `src/multiagents/executor/docker.py`
*Trace:* `docker.py:write_proxy_config` builds one ERE line per allowlist entry as
`escaped = host.replace(".", r"\.")` then `f"(^|\\.){escaped}$"` — escaping only
the literal dot. `Dockerfile.proxy` configures tinyproxy with `FilterType ere`
and `FilterDefaultDeny Yes`, and `write_proxy_config` writes one such line per
`filter` file with `FilterCaseSensitive Off`. POSIX ERE alternation (`|`) has
the lowest precedence of any operator, binding across the whole pattern rather
than to the token adjacent to it. An allowlist entry such as `evil.com|.*`
therefore produces the filter line `(^|\.)evil\.com|\.*$` — read by the regex
engine as `(^|\.)evil\.com` **OR** `\.*$`, and the second alternative matches
any string at all (zero or more literal dots at the end, which every host
satisfies vacuously). Because tinyproxy allows a request if ANY filter line
matches, one crafted-looking entry silently defeats every other entry in the
same allowlist, converting a supposedly narrow allowlist into open egress for
the whole container network.
*Evidence:* Confirmed twice: (1) a standalone `re` simulation of the generated
pattern; (2) instantiating a real `DockerExecutor` and calling its actual
`write_proxy_config()` against a temp directory, confirming the exact
vulnerable output `(^|\.)evil\.com|\.*$`, and confirming with `re.search`
(case-insensitive, matching tinyproxy's `FilterCaseSensitive Off`) that
unrelated hosts — `attacker.example`, `steal-creds.io`, `pastebin.com` — all
match it. Reproduced as a harness proof test:
`tests/test_c1_sandbox_harness.py::test_an_unescaped_pipe_in_an_allowlist_entry_admits_every_host`.
*Reasoning:* `docker.py`'s `mounts()` method (~lines 260-269) already documents
that `project.yaml` — which holds `egress_allowlist` — is writable from inside
the container, and accepts as a known tradeoff that "an agent could widen its
own sandbox" by adding its own host to the list. This finding is materially
worse than that documented tradeoff: it is not widening the list by one host,
it is a single string that defeats the *entire* allowlist mechanism outright,
and it does not require the agent to understand tinyproxy or ERE — `|` is a
character that shows up in legitimate-looking hostnames pasted from
elsewhere (query strings, glob-like configs) as much as in a deliberate
attack. Given C1 is "the only context where being wrong costs something that
cannot be taken back" (per `context/review/MAP.md`), and this is the exact
mechanism that stands between an agent and the open internet, this should be
treated as the top-priority defect in the context.
*Recommendation:* Escape the whole host with `re.escape`-equivalent behaviour
(escape every ERE metacharacter tinyproxy's regex engine recognizes:
`. ^ $ * + ? ( ) [ ] { } | \`), not just `.`. Consider also validating that
each allowlist entry is a plausible hostname (see F2) before it ever reaches
pattern generation, as defense in depth against a future regression in the
escaping logic itself.

**F2** — A non-string `egress_allowlist` entry crashes container config generation with an uncaught `AttributeError`
*Class:* correctness
*Severity:* medium
*File:* `src/multiagents/executor/docker.py`, `src/multiagents/config.py`
*Trace:* `write_proxy_config` calls `host.replace(".", r"\.")` on every entry of
`self.config.get("egress_allowlist", [])` with no type check. `config.py` has
no schema or validation for `egress_allowlist` (confirmed by grep: the only
reference outside `docker.py` is the default value set in
`defaults/project.yaml:65`). A `project.yaml` with e.g.
`egress_allowlist: [{sub: example.com}]` (a plausible typo reaching for
per-host options that do not exist) — or any other non-string YAML scalar
type — raises `AttributeError: 'dict' object has no attribute 'replace'` from
inside `write_proxy_config`, with no validation error pointing at the actual
mistake, at whatever point in the container-startup path calls it.
*Evidence:* `tests/test_c1_sandbox_harness.py::test_allowlist_admits_exact_and_subdomain_hosts_and_refuses_others`
asserts this via `pytest.raises(AttributeError)` calling the real
`write_proxy_config` through the harness's `filter_patterns()`.
*Reasoning:* Lower severity than F1 because it fails closed (the container
never starts with a working proxy) rather than silently open, but it is an
uncaught exception surfacing from config parsing deep inside executor
plumbing, with a message that gives no hint the fault is in `egress_allowlist`
specifically. A user or agent editing `project.yaml` gets a stack trace instead
of a validation error.
*Recommendation:* Validate that each `egress_allowlist` entry is a string
(and, ideally, looks like a hostname) at config-load time in `config.py`,
raising a clear error there instead of letting a malformed entry surface as an
unrelated `AttributeError` from `write_proxy_config`.
