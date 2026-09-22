"""R13 — the directives that decide what the filter patterns *mean*.

The contract is `context/specs/phase2-proxy-directives.md`. `write_proxy_config`
generates two things: a set of ERE filter patterns, and the tinyproxy
directives that decide how those patterns are read and whether a host matching
none of them is refused. The 48-test characterization suite pins the patterns
between 9 and 43 times over; F50–F54 are what happens to the directives.

**What this file adds that `test_adversary_allowlist_mutation.py` does not.**
That file — the adversary run that *found* these defects — already carries five
guards, and a plain deletion of a directive goes red there today. They are
substring searches over the whole config, and a substring search is defeated by
three mutations that each leave the directive with no effect at all:

    'FilterDefaultDeny Yes' in '# FilterDefaultDeny Yes'        -> True
    'FilterDefaultDeny Yes' in 'XFilterDefaultDeny Yes'         -> True
    'FilterDefaultDeny Yes' in 'FilterDefaultDeny Yes\\nFilterDefaultDeny No'
                                                                -> True

Commented out, keyword misspelled so tinyproxy ignores the line, contradicted
by a later line that wins. So every assertion here goes through
`h.proxy_config(...).directive(name)`, which reads the file the way tinyproxy's
grammar reads it — `#` stripped, keywords case-folded, every value for a
keyword kept — and fails unless the keyword appears exactly once. That is what
closes all three: the comment and the misspelling make the keyword absent, the
contradiction makes it appear twice. The adversary's file is left alone; it is
the record of the finding.

**On the exact literals.** tinyproxy accepts `Yes`/`On`/`1` and `No`/`Off`/`0`
interchangeably, so `FilterDefaultDeny On` would mean exactly what
`FilterDefaultDeny Yes` means and these tests would still go red. That is
deliberate, and the contract asks for it: the value of a guard on a
security-critical directive is that nothing changes the line without a human
looking at it. A legitimate respelling updates the test in the same commit.

**What is not asserted here.** What tinyproxy *does* with the configuration.
tinyproxy is not installed in this sandbox (see the harness docstring), so
these tests assert what the generated file says; `h.allowlist_admits` and the
characterization suite cover what the patterns mean under those directives.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import c1_harness as h  # noqa: E402


# ---------------------------------------------------------------------------
# F50 — critical. The one directive separating a sandbox from an open relay.
# ---------------------------------------------------------------------------

def test_filter_default_deny_is_yes_so_the_filter_is_an_allow_list(tmp_path):
    """Without `FilterDefaultDeny Yes` tinyproxy allows by default and the
    generated filter file becomes a deny-list: every host not named in the
    `egress_allowlist` is admitted, which is an open relay with extra steps.

    Deleting the line, commenting it out, misspelling the keyword or appending
    `FilterDefaultDeny No` all produce that proxy, and all four fail here."""
    conf = h.proxy_config(tmp_path, ["example.com"])
    assert conf.directive("FilterDefaultDeny") == "Yes"


# ---------------------------------------------------------------------------
# F51 — the patterns and the dialect that reads them are generated in one
# function and, until now, checked in two places that never met.
# ---------------------------------------------------------------------------

def test_filter_type_is_ere_so_the_generated_patterns_mean_what_they_say(tmp_path):
    """The generated lines are POSIX *extended* regexes — `(^|\\.)host$` uses
    grouping and alternation, and `_ere_literal` escapes exactly the set ERE
    treats as special. `FilterType regex` selects BRE, where `(`, `)` and `|`
    are ordinary characters, so the anchored subdomain group stops being a
    group and every pattern silently changes meaning."""
    conf = h.proxy_config(tmp_path, ["example.com"])
    assert conf.directive("FilterType") == "ere"


# ---------------------------------------------------------------------------
# F52 — DNS is case-insensitive; the patterns are not.
# ---------------------------------------------------------------------------

def test_filter_case_sensitive_is_off_so_a_capitalised_host_still_matches(tmp_path):
    """`API.Example.com` and `api.example.com` name the same host, and the
    generated pattern carries whatever case the allowlist entry was written
    in. `FilterCaseSensitive On` refuses the request rather than admitting a
    host it should not, so it fails closed — but it fails with no explanation
    and against an allowlist that names the host."""
    conf = h.proxy_config(tmp_path, ["example.com"])
    assert conf.directive("FilterCaseSensitive") == "Off"


# ---------------------------------------------------------------------------
# F53 — what the patterns are matched *against*.
# ---------------------------------------------------------------------------

def test_filter_urls_is_off_so_the_patterns_match_the_host_not_the_url(tmp_path):
    """`(^|\\.)example\\.com$` is anchored at both ends against a bare
    destination host. `FilterURLs On` matches it against the full URL instead,
    where the `$` anchor lands after the path — so the allowlist stops
    admitting the hosts it names."""
    conf = h.proxy_config(tmp_path, ["example.com"])
    assert conf.directive("FilterURLs") == "Off"
