# C1 — Sandbox allowlist characterization

**F10** — Bare or short generic suffix entries act as wildcards across unrelated hosts
*Class:* security
*Severity:* high
*Where:* `src/multiagents/executor/docker.py:write_proxy_config`
*Evidence:* reproduction
*Proof:* `tests/test_c1_allowlist_characterization.py::test_bare_public_suffix_entry_admits_every_host_under_that_suffix`
*What happens:* A generic suffix entry like `com` admits every `.com` host because the regex anchors to `(^|\.)` and allows any prefix.
*Disposition:* rewrite
*Reasoning:* A small typo (e.g., `com` instead of `example.com`) silently grants egress to entire TLDs or arbitrary IP ranges.

**F11** — Dead or malformed entries (leading/trailing dot, whitespace, port) silently match nothing
*Class:* maintainability
*Severity:* medium
*Where:* `src/multiagents/executor/docker.py:write_proxy_config`
*Evidence:* reproduction
*Proof:* `tests/test_c1_allowlist_characterization.py::test_leading_or_trailing_dot_on_an_entry_makes_it_match_no_ordinary_host`
*What happens:* Entries containing unescaped artifacts like trailing spaces, URL schemes, or ports silently fail to match anything.
*Disposition:* fix
*Reasoning:* Operators have no signal that their entry is inert due to a trailing space or port number, causing operational confusion.

**F12** — Unescaped quantifiers `*`, `+`, `?` or interval `{n}` change match semantics
*Class:* security
*Severity:* medium
*Where:* `src/multiagents/executor/docker.py:write_proxy_config`
*Evidence:* reproduction
*Proof:* `tests/test_c1_allowlist_characterization.py::test_unescaped_star_in_an_entry_makes_it_match_a_range_of_hosts` and `tests/test_char_c1_allowlist.py::test_interval_quantifier_changes_match_semantics`
*What happens:* Metacharacters like `*` are evaluated as ERE quantifiers rather than literal characters.
*Disposition:* fix
*Reasoning:* Glob-like configurations are a common intuition (e.g., `*.com`) and will evaluate incorrectly, matching unintended hosts.

**F13** — Unbalanced grouping/class metacharacters `(` or `[` crash the regex match
*Class:* bug
*Severity:* high
*Where:* `src/multiagents/executor/docker.py:write_proxy_config`
*Evidence:* reproduction
*Proof:* `tests/test_c1_allowlist_characterization.py::test_unbalanced_paren_in_an_entry_crashes_the_match_instead_of_deciding` and `tests/test_char_c1_allowlist.py::test_lone_close_paren_crashes_the_match_instead_of_deciding`
*What happens:* An unescaped grouping metacharacter creates a syntactically invalid ERE line, crashing tinyproxy's evaluation.
*Disposition:* rewrite
*Reasoning:* A single typo can completely break the egress proxy for the entire container on every request.

**F14** — Balanced `()`, `[]` create a silent regex match, ignoring literal meaning
*Class:* security
*Severity:* high
*Where:* `src/multiagents/executor/docker.py:write_proxy_config`
*Evidence:* reproduction
*Proof:* `tests/test_char_c1_allowlist.py::test_balanced_parens_silently_match_inner_string` and `tests/test_char_c1_allowlist.py::test_balanced_brackets_silently_match_character_class`
*What happens:* Valid regex groups silently evaluate, meaning `a(b)c` matches `abc` and `a[bc]d` matches `abd` instead of their literal representations.
*Disposition:* fix
*Reasoning:* Distinct from F13's crash; a user trying to filter exactly `a[bc]d` ends up allowing different hosts silently.
