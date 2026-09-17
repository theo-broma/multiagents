# C1 — Adversarial attack on the egress allowlist

Mutation and fuzzing against `write_proxy_config` in
`src/multiagents/executor/docker.py`, run against the 48-test allowlist
characterization suite. Reproductions are in
`tests/test_adversary_allowlist_mutation.py` (13 tests) and
`tests/test_adversary_allowlist_fuzz.py` (23 tests), both green.

**Verdict: the suite is not load-bearing.** It validates the generated filter
*patterns* thoroughly — every mutation to the regex construction is caught by
between 9 and 43 tests — and never validates the tinyproxy directives that
decide what those patterns mean. A mutation that turns the proxy into an open
relay passes all 48.

IDs continue from the other C1 findings files; F50 onward is this run's range.


**F50** — Removing `FilterDefaultDeny Yes` inverts the proxy from allow-list to deny-list, and every test still passes
*Class:* security
*Severity:* critical
*Where:* `src/multiagents/executor/docker.py:719`
*Evidence:* reproduction
*Proof:* `tests/test_adversary_allowlist_mutation.py`
*What happens:* Deleting the line `"FilterDefaultDeny Yes\n"` leaves all 48 characterization tests passing. That directive is the one that makes tinyproxy deny by default; without it tinyproxy allows by default and the generated filter file becomes a deny-list instead of an allow-list. The proxy stops being a security boundary and becomes an open relay, and nothing in the suite notices.
*Disposition:* rewrite
*Reasoning:* This is the single directive separating a sandbox from an open relay, and it is the one thing the suite does not check. Every other finding in this context concerns which hosts a correct allow-list admits; this one concerns whether there is an allow-list at all.

**F51** — `FilterType ere` can be changed to `regex` with no test failing
*Class:* correctness
*Severity:* medium
*Where:* `src/multiagents/executor/docker.py`
*Evidence:* reproduction
*Proof:* `tests/test_adversary_allowlist_mutation.py`
*What happens:* Switching the directive from `ere` to `regex` selects BRE rather than ERE. The generated patterns are written in ERE syntax, so their meaning changes silently. All 48 tests pass.
*Disposition:* fix
*Reasoning:* The patterns and the dialect that interprets them are generated in the same function and validated separately, so a mismatch between them is invisible.

**F52** — `FilterCaseSensitive Off` can be changed to `On` with no test failing
*Class:* correctness
*Severity:* medium
*Where:* `src/multiagents/executor/docker.py`
*Evidence:* reproduction
*Proof:* `tests/test_adversary_allowlist_mutation.py`
*What happens:* Flipping the directive to `On` makes matching case-sensitive. DNS is case-insensitive, so a host differing only in case would be refused. All 48 tests pass.
*Disposition:* fix
*Reasoning:* An allow-list that silently stops matching a host because of letter case fails closed rather than open, but it fails without explanation.

**F53** — `FilterURLs Off` can be changed to `On` with no test failing
*Class:* correctness
*Severity:* medium
*Where:* `src/multiagents/executor/docker.py`
*Evidence:* reproduction
*Proof:* `tests/test_adversary_allowlist_mutation.py`
*What happens:* The generated patterns match a host, not a full URL. Turning `FilterURLs` on changes what tinyproxy matches them against. All 48 tests pass.
*Disposition:* fix
*Reasoning:* Same shape as F51: the patterns and the thing they are matched against are decided in one place and checked in another.

**F54** — The filter file path in `tinyproxy.conf` can be changed with no test failing
*Class:* correctness
*Severity:* medium
*Where:* `src/multiagents/executor/docker.py`
*Evidence:* reproduction
*Proof:* `tests/test_adversary_allowlist_mutation.py`
*What happens:* Nothing verifies that the `FilterFile` directive names the file the patterns are actually written to. Pointing it elsewhere leaves tinyproxy with no filter file at all. All 48 tests pass.
*Disposition:* fix
*Reasoning:* A filter file that is written correctly and never read is indistinguishable, from the suite's point of view, from one that is enforced.

**F55** — `egress_allowlist: null` raises an uncaught `TypeError`
*Class:* correctness
*Severity:* low
*Where:* `src/multiagents/executor/docker.py`
*Evidence:* reproduction
*Proof:* `tests/test_adversary_allowlist_fuzz.py`
*What happens:* A `null` allowlist in the config reaches `write_proxy_config` and raises `TypeError`. The code handles the case in the sense that it fails rather than misbehaves, but no test covers it.
*Disposition:* fix
*Reasoning:* Adjacent to F2, which covers a non-string *entry*; this one is the whole list being null.

**F56** — The filter file's trailing newline is untested
*Class:* maintainability
*Severity:* low
*Where:* `src/multiagents/executor/docker.py`
*Evidence:* reproduction
*Proof:* `tests/test_adversary_allowlist_fuzz.py`
*What happens:* Removing the trailing newline from the generated filter file breaks no test. Whether tinyproxy tolerates a file with no final newline is not established here.
*Disposition:* accept
*Reasoning:* Filed for completeness. Worth knowing only if the others are fixed and someone is tightening the file format.

---

## The original run's own write-up

Kept verbatim below, because its framing of the gap is clearer than the
reformatting above and because the reproduction steps are stated in the
adversary's own words.


# Adversary Findings — write_proxy_config Allowlist Enforcement

## Verdict

**REJECTED (1): The suite is not load-bearing for the security-critical configuration it generates.**

The 48-test suite thoroughly validates the filter pattern generation logic and correctly catches mutations to the regex construction. However, it completely fails to validate the tinyproxy.conf directives that determine whether the proxy operates as an allow-list or deny-all-by-default. A mutation that removes `FilterDefaultDeny Yes` — which inverts the entire security model from "deny everything except what's listed" to "allow everything except what's listed" — passes all 48 tests.

This is a critical gap: the suite would not notice if the proxy became an open relay.

---

