"""Characterization of the allowlist decision and filter generation — C1.

Pins what `DockerExecutor.write_proxy_config` and the allow/refuse decision it
produces actually do today, through `h.filter_patterns` and
`h.allowlist_admits` only. Findings from this pass are filed as F10 onward in
`context/review/C1-sandbox-allowlist.md` (F1, F2 already exist from the
harness-building pass, in `context/review/C1-sandbox.md`).

Every test here was run against the real production method — nothing about
pattern generation or matching is reimplemented. `allowlist_admits` matches
with Python's `re`, documented (see the harness) as equivalent to tinyproxy's
`regexec` for the constructs these patterns use — no tinyproxy binary runs in
this sandbox.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import c1_harness as h  # noqa: E402


# ---------------------------------------------------------------------------
# The public surface: ordinary hosts
# ---------------------------------------------------------------------------

def test_exact_host_is_admitted(tmp_path):
    allowlist = ["example.com"]
    assert h.filter_patterns(tmp_path, allowlist) == [r"(^|\.)example\.com$"]
    assert h.allowlist_admits(tmp_path, allowlist, "example.com")


def test_subdomain_of_a_listed_host_is_admitted(tmp_path):
    allowlist = ["example.com"]
    assert h.allowlist_admits(tmp_path, allowlist, "api.example.com")
    assert h.allowlist_admits(tmp_path, allowlist, "deeply.nested.api.example.com")


def test_lookalike_host_sharing_a_suffix_but_not_a_label_boundary_is_refused(tmp_path):
    """`evil-example.com` is not a subdomain of `example.com` — no dot
    boundary between the shared suffix and the rest — and is correctly
    refused. Same for a substring match in the middle of the string."""
    allowlist = ["example.com"]
    assert not h.allowlist_admits(tmp_path, allowlist, "evil-example.com")
    assert not h.allowlist_admits(tmp_path, allowlist, "notexample.com")
    assert not h.allowlist_admits(tmp_path, allowlist, "example.com.evil.com")


def test_parent_domain_of_a_listed_subdomain_is_refused(tmp_path):
    """Listing `sub.example.com` does not also admit its parent `example.com`
    — the match is a suffix match, not a shared-label match in the other
    direction."""
    allowlist = ["sub.example.com"]
    assert not h.allowlist_admits(tmp_path, allowlist, "example.com")
    assert h.allowlist_admits(tmp_path, allowlist, "sub.example.com")
    assert h.allowlist_admits(tmp_path, allowlist, "x.sub.example.com")


def test_case_of_entry_and_host_does_not_affect_the_decision(tmp_path):
    """The raw generated pattern preserves the entry's original case, but
    `allowlist_admits` matches case-insensitively — consistent with tinyproxy
    being configured `FilterCaseSensitive Off`."""
    allowlist = ["Example.COM"]
    assert h.filter_patterns(tmp_path, allowlist) == [r"(^|\.)Example\.COM$"]
    assert h.allowlist_admits(tmp_path, allowlist, "example.com")
    assert h.allowlist_admits(tmp_path, allowlist, "EXAMPLE.COM")


def test_duplicate_entries_produce_duplicate_lines_but_no_behavior_change(tmp_path):
    allowlist = ["example.com", "example.com"]
    assert h.filter_patterns(tmp_path, allowlist) == [
        r"(^|\.)example\.com$", r"(^|\.)example\.com$"]
    assert h.allowlist_admits(tmp_path, allowlist, "example.com")


def test_very_long_entry_is_handled_with_no_truncation_or_error(tmp_path):
    long_host = "x" * 300
    allowlist = [long_host]
    assert h.allowlist_admits(tmp_path, allowlist, long_host)
    assert not h.allowlist_admits(tmp_path, allowlist, "short.com")


def test_unicode_host_is_matched_literally_including_its_subdomains(tmp_path):
    """A raw-unicode entry (not punycode) works the same way an ASCII entry
    does — matched as a literal string, admitting subdomains the same way."""
    allowlist = ["例え.jp"]
    assert h.allowlist_admits(tmp_path, allowlist, "例え.jp")
    assert h.allowlist_admits(tmp_path, allowlist, "sub.例え.jp")


def test_punycode_idn_form_is_matched_literally(tmp_path):
    allowlist = ["xn--nxasmq6b.xn--fiqs8s"]
    assert h.allowlist_admits(tmp_path, allowlist, "xn--nxasmq6b.xn--fiqs8s")


def test_ip_address_entry_matches_the_exact_address_and_dotted_suffixes(tmp_path):
    """An IP address is escaped and anchored exactly like a hostname — the
    dots in it get the same `\\.` escaping and the same subdomain-shaped
    prefix rule, since nothing distinguishes an IP entry from a hostname
    entry in this code path."""
    allowlist = ["192.168.1.1"]
    assert h.allowlist_admits(tmp_path, allowlist, "192.168.1.1")
    assert h.allowlist_admits(tmp_path, allowlist, "x.192.168.1.1")


# ---------------------------------------------------------------------------
# The boundaries: empty list, empty entry
# ---------------------------------------------------------------------------

def test_empty_allowlist_denies_everything(tmp_path):
    assert h.filter_patterns(tmp_path, []) == []
    assert not h.allowlist_admits(tmp_path, [], "example.com")
    assert not h.allowlist_admits(tmp_path, [], "")


def test_empty_string_entry_produces_a_pattern_matching_only_a_trailing_dot(tmp_path):
    """An empty-string entry generates the line `(^|\\.)$`, which does not
    match an ordinary host, but does match a host with a literal trailing dot
    (the FQDN root-dot form) and matches the empty string itself."""
    allowlist = [""]
    assert h.filter_patterns(tmp_path, allowlist) == [r"(^|\.)$"]
    assert not h.allowlist_admits(tmp_path, allowlist, "example.com")
    assert h.allowlist_admits(tmp_path, allowlist, "example.com.")
    assert h.allowlist_admits(tmp_path, allowlist, "")


# ---------------------------------------------------------------------------
# F10 — bare/short suffix entries act as a wildcard over every host sharing
# that suffix, including an entire public suffix like a TLD.
# ---------------------------------------------------------------------------

def test_bare_public_suffix_entry_admits_every_host_under_that_suffix(tmp_path):
    """F10: a bare TLD entry (a very plausible typo, or a misguided attempt
    to allow 'anything .com') is not rejected or special-cased — it produces
    `(^|\\.)com$`, which admits `com` itself, AND every domain ending in
    `.com`, because the anchoring rule that lets `example.com` correctly cover
    `api.example.com` has no floor on how short or generic the listed label
    sequence is. One entry `com` silently grants egress to the entire `.com`
    namespace."""
    allowlist = ["com"]
    assert h.filter_patterns(tmp_path, allowlist) == [r"(^|\.)com$"]
    assert h.allowlist_admits(tmp_path, allowlist, "com")
    assert h.allowlist_admits(tmp_path, allowlist, "example.com")
    assert h.allowlist_admits(tmp_path, allowlist, "evil.com")
    assert h.allowlist_admits(tmp_path, allowlist, "attacker-controlled.com")
    # It is a suffix match, not a substring match, so a name merely
    # containing "com" without a dot boundary is still refused.
    assert not h.allowlist_admits(tmp_path, allowlist, "com.evil.net")


def test_short_generic_entry_admits_unrelated_hosts_sharing_it_as_a_suffix(tmp_path):
    """F10 (same root cause): a short, IP-fragment-shaped entry like `1.1`
    (e.g. meant as shorthand, or a typo dropping the first two octets of an
    internal address) matches any host ending in `.1.1` — including an
    unrelated public IP address that merely happens to end that way."""
    allowlist = ["1.1"]
    assert h.allowlist_admits(tmp_path, allowlist, "1.1")
    assert h.allowlist_admits(tmp_path, allowlist, "192.168.1.1")


# ---------------------------------------------------------------------------
# F11 — malformed-looking entries silently match nothing: no trimming, no
# normalization, no validation error. The operator gets a dead entry with no
# signal that it is dead.
# ---------------------------------------------------------------------------

def test_leading_or_trailing_dot_on_an_entry_makes_it_match_no_ordinary_host(tmp_path):
    """F11: a leading dot is a common way to write 'this host and all its
    subdomains' in other allowlist syntaxes (e.g. cookie domains, nginx
    server_name). Here it is not special-cased — `.` is escaped like any
    other character in the entry, so `.example.com` requires a LITERAL double
    dot (`..example.com`) or a leading dot at the very start of the host to
    match, neither of which any real hostname has. The entry matches nothing
    a real request would ever send."""
    allowlist = [".example.com"]
    assert h.filter_patterns(tmp_path, allowlist) == [r"(^|\.)\.example\.com$"]
    assert not h.allowlist_admits(tmp_path, allowlist, "example.com")
    assert not h.allowlist_admits(tmp_path, allowlist, "sub.example.com")
    assert h.allowlist_admits(tmp_path, allowlist, "..example.com")


def test_trailing_dot_on_an_entry_makes_it_match_only_the_fqdn_root_dot_form(tmp_path):
    """F11: same mechanism as the leading-dot case, mirrored — an entry
    written with a trailing dot (a technically-valid FQDN form some tools
    emit) requires the destination host to ALSO carry that trailing dot to
    match, which ordinary proxied requests do not."""
    allowlist = ["example.com."]
    assert not h.allowlist_admits(tmp_path, allowlist, "example.com")
    assert h.allowlist_admits(tmp_path, allowlist, "example.com.")


def test_leading_or_trailing_whitespace_on_an_entry_makes_it_match_nothing(tmp_path):
    """F11: whitespace is not stripped from an entry before it is turned into
    a pattern. A `project.yaml` entry with accidental leading/trailing
    whitespace (very easy to introduce via YAML block scalars or a stray
    paste) silently becomes an entry that can never match any real host,
    with no warning that it is inert."""
    assert not h.allowlist_admits(tmp_path, [" example.com"], "example.com")
    assert not h.allowlist_admits(tmp_path, ["example.com "], "example.com")


def test_entry_with_a_port_never_matches_because_matching_is_against_the_bare_host(tmp_path):
    """F11: `allowlist_admits`/tinyproxy (`FilterURLs Off`) matches the bare
    destination host, with no port component. An entry written as
    `example.com:8080` (a plausible way to try to scope egress to one port)
    silently never matches the bare host `example.com` that is actually
    checked."""
    allowlist = ["example.com:8080"]
    assert not h.allowlist_admits(tmp_path, allowlist, "example.com")


def test_entry_with_a_scheme_and_path_never_matches_the_bare_host(tmp_path):
    """F11: same class — an entry copy-pasted as a full URL
    (`https://example.com/path`) never matches the bare host that is actually
    checked."""
    allowlist = ["https://example.com/path"]
    assert not h.allowlist_admits(tmp_path, allowlist, "example.com")


def test_embedded_caret_or_dollar_in_an_entry_makes_it_unmatchable(tmp_path):
    """F11 (different mechanism, same consequence): `^` and `$` are not
    escaped, so a literal caret or dollar sign inside an entry is read as a
    start/end-of-string assertion in the middle of the pattern — a position
    that can never be satisfied except at the true start/end of the whole
    string — making the entry match nothing at all, not even the literal
    string it was written to represent."""
    assert not h.allowlist_admits(tmp_path, ["a^b"], "a^b")
    assert not h.allowlist_admits(tmp_path, ["a$b"], "a$b")


def test_embedded_backslash_in_an_entry_is_read_as_a_regex_escape(tmp_path):
    """F11 (different mechanism, same consequence): a literal backslash in an
    entry is not escaped either, so `a\\b` is read as the regex escape `\\b`
    (a word-boundary assertion) rather than the two literal characters —
    again making the entry match neither the literal string nor the plain
    concatenation of its parts."""
    allowlist = ["a\\b"]
    assert not h.allowlist_admits(tmp_path, allowlist, "a\\b")
    assert not h.allowlist_admits(tmp_path, allowlist, "ab")


# ---------------------------------------------------------------------------
# F12 — unescaped ERE quantifiers change what an entry matches, rather than
# either failing closed or matching the literal string.
# ---------------------------------------------------------------------------

def test_unescaped_star_in_an_entry_makes_it_match_a_range_of_hosts(tmp_path):
    """F12: only `.` is escaped before the entry is embedded in the pattern.
    A `*` in an entry (e.g. from a glob-style config pasted in by mistake)
    is read as the ERE repetition operator on the character before it, so
    `a.b*c` matches `a.bc` (zero `b`s) and `a.bbbc` (three), not just the
    literal string `a.b*c` the author wrote — which itself is refused."""
    allowlist = ["a.b*c"]
    assert h.allowlist_admits(tmp_path, allowlist, "a.bc")
    assert h.allowlist_admits(tmp_path, allowlist, "a.bbbc")
    assert not h.allowlist_admits(tmp_path, allowlist, "a.xc")
    assert not h.allowlist_admits(tmp_path, allowlist, "a.b*c")


def test_unescaped_plus_in_an_entry_requires_one_or_more_of_the_preceding_char(tmp_path):
    """F12: same mechanism as `*` — `a.b+c` matches `a.bc`... """
    allowlist = ["a.b+c"]
    assert h.allowlist_admits(tmp_path, allowlist, "a.bc")
    assert h.allowlist_admits(tmp_path, allowlist, "a.bbc")


def test_unescaped_question_mark_in_an_entry_makes_the_preceding_char_optional(tmp_path):
    """F12: same mechanism — `a.b?c` matches both `a.c` and `a.bc`."""
    allowlist = ["a.b?c"]
    assert h.allowlist_admits(tmp_path, allowlist, "a.c")
    assert h.allowlist_admits(tmp_path, allowlist, "a.bc")


# ---------------------------------------------------------------------------
# F13 — unescaped grouping/class metacharacters with no matching close
# produce an invalid regex: the match itself raises, rather than admitting or
# refusing the host.
# ---------------------------------------------------------------------------

def test_unbalanced_paren_in_an_entry_crashes_the_match_instead_of_deciding(tmp_path):
    """F13: `(` is not escaped. An entry with an unmatched `(` (plausible in
    a pasted regex fragment, or just a typo) produces a syntactically invalid
    ERE line. Evaluating it — the exact thing `allowlist_admits` /
    tinyproxy's `regexec` must do for every request — raises instead of
    cleanly admitting or refusing the connection."""
    allowlist = ["a(b"]
    assert h.filter_patterns(tmp_path, allowlist) == [r"(^|\.)a(b$"]
    with pytest.raises(Exception):
        h.allowlist_admits(tmp_path, allowlist, "a(b")


def test_unbalanced_bracket_in_an_entry_crashes_the_match_instead_of_deciding(tmp_path):
    """F13: same mechanism as the unbalanced paren, with `[` (an unterminated
    character class) instead."""
    allowlist = ["a[b"]
    with pytest.raises(Exception):
        h.allowlist_admits(tmp_path, allowlist, "a[b")


# ---------------------------------------------------------------------------
# F2 (already filed) — non-string entry, confirmed again here as part of the
# ordinary boundary sweep, not re-filed.
# ---------------------------------------------------------------------------

def test_non_string_entry_raises_attributeerror_not_a_validation_error(tmp_path):
    with pytest.raises(AttributeError):
        h.filter_patterns(tmp_path, [{"sub": "example.com"}])


# ---------------------------------------------------------------------------
# F1 (already filed) — reconfirmed once here for completeness of this file's
# own sweep of ERE metacharacters; not re-filed, see C1-sandbox.md.
# ---------------------------------------------------------------------------

def test_unescaped_pipe_in_one_entry_admits_hosts_unrelated_to_any_entry(tmp_path):
    allowlist = ["good.example.com", "evil.com|.*"]
    assert h.allowlist_admits(tmp_path, allowlist, "totally-unrelated.example")
