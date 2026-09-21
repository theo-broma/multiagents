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


# The two tests below were also filed under F11, by a different mechanism:
# `^`, `$` and `\` reached ERE unescaped and made the entry unsatisfiable.
# R12 (context/specs/phase2-ere-escaping.md) escapes all three as part of the
# same table, so that mechanism is gone and both are inverted below. The five
# F11 tests above are untouched and still pass — a leading dot, a trailing
# dot, stray whitespace, a `:port` and a full URL still produce an entry that
# matches no real host, with no signal that it is inert. F11 is narrowed by
# R12, not closed by it.


def test_embedded_caret_or_dollar_in_an_entry_is_matched_as_a_literal_character(tmp_path):
    """F11, inverted by R12 — deliberate, not a weakened test.

    This test was written during the review to record that `^` and `$` were
    not escaped: a literal caret or dollar inside an entry was read as a
    start/end-of-string assertion in the middle of the pattern, a position
    nothing could satisfy, so the entry matched nothing at all — not even the
    string it was written to represent.

    R12 escapes both, so the entry now means its own text. The assertion is
    inverted rather than deleted, and the inverted form is the stronger one:
    "matches nothing" was satisfied by any broken pattern, whereas "matches
    exactly its own literal text and not the concatenation without the
    metacharacter" rules out both the old behaviour and a fix that merely
    dropped the offending character.
    """
    assert h.allowlist_admits(tmp_path, ["a^b"], "a^b")
    assert not h.allowlist_admits(tmp_path, ["a^b"], "ab")
    assert h.allowlist_admits(tmp_path, ["a$b"], "a$b")
    assert not h.allowlist_admits(tmp_path, ["a$b"], "ab")


def test_embedded_backslash_in_an_entry_is_matched_as_a_literal_backslash(tmp_path):
    """F11, inverted by R12 — deliberate, not a weakened test.

    Originally: a literal backslash in an entry was not escaped either, so
    `a\\b` was read as the regex escape `\\b` (a word-boundary assertion) rather
    than two literal characters, and the entry matched neither the literal
    string nor the plain concatenation of its parts.

    R12 escapes the backslash, so the entry matches the two literal
    characters. Note the second assertion is carried over unchanged from the
    original: `ab` must still be refused. That was true when the entry was
    dead and it is true now that it is literal, and keeping it is what stops
    this inversion from admitting a superset of what it used to.
    """
    allowlist = ["a\\b"]
    assert h.allowlist_admits(tmp_path, allowlist, "a\\b")
    assert not h.allowlist_admits(tmp_path, allowlist, "ab")


# ---------------------------------------------------------------------------
# F12 — ERE quantifiers in an entry. These tests recorded quantifiers changing
# what an entry matched instead of it matching the literal string; R12
# (context/specs/phase2-ere-escaping.md) escapes `*`, `+`, `?`, `{` and `}`,
# so all three are inverted below. Deliberate inversions, not weakened tests.
# ---------------------------------------------------------------------------

def test_a_star_in_an_entry_is_a_literal_star_not_a_repetition_operator(tmp_path):
    """F12, inverted by R12 — deliberate, not a weakened test.

    Originally: only `.` was escaped, so a `*` in an entry (e.g. from a
    glob-style config pasted in by mistake) was the ERE repetition operator
    on the character before it. `a.b*c` matched `a.bc` (zero `b`s) and
    `a.bbbc` (three) and refused the literal string its author wrote.

    Every host from the original is still named here, with the two admits
    flipped to refusals and the one refusal flipped to an admit. `a.xc` keeps
    its original polarity: it was refused under the quantifier reading and is
    refused under the literal one, and asserting it still rules out a fix
    that turned the entry into something broader.
    """
    allowlist = ["a.b*c"]
    assert h.allowlist_admits(tmp_path, allowlist, "a.b*c")
    assert not h.allowlist_admits(tmp_path, allowlist, "a.bc")
    assert not h.allowlist_admits(tmp_path, allowlist, "a.bbbc")
    assert not h.allowlist_admits(tmp_path, allowlist, "a.xc")


def test_a_plus_in_an_entry_is_a_literal_plus_not_a_one_or_more_operator(tmp_path):
    """F12, inverted by R12 — deliberate, not a weakened test.

    Originally, same mechanism as `*`: `a.b+c` matched `a.bc` and `a.bbc`,
    one-or-more of the preceding character. Both are now refused and the
    entry admits its own text instead.
    """
    allowlist = ["a.b+c"]
    assert h.allowlist_admits(tmp_path, allowlist, "a.b+c")
    assert not h.allowlist_admits(tmp_path, allowlist, "a.bc")
    assert not h.allowlist_admits(tmp_path, allowlist, "a.bbc")


def test_a_question_mark_in_an_entry_is_a_literal_not_an_optional_marker(tmp_path):
    """F12, inverted by R12 — deliberate, not a weakened test.

    Originally, same mechanism: `a.b?c` made the `b` optional and so matched
    both `a.c` and `a.bc`, neither of which anybody listed. Both are now
    refused and the entry admits its own text instead.
    """
    allowlist = ["a.b?c"]
    assert h.allowlist_admits(tmp_path, allowlist, "a.b?c")
    assert not h.allowlist_admits(tmp_path, allowlist, "a.c")
    assert not h.allowlist_admits(tmp_path, allowlist, "a.bc")


# ---------------------------------------------------------------------------
# F13 — grouping/class metacharacters with no matching close. These tests
# recorded an entry like `a(b` producing a syntactically invalid ERE line, so
# that evaluating it raised instead of deciding. R12
# (context/specs/phase2-ere-escaping.md) escapes `(`, `)`, `[` and `]`, so
# both are inverted below. Deliberate inversions, not weakened tests.
#
# What replaces "it raises" is not "it does not raise" — that on its own
# would assert almost nothing, since a filter that admitted every host would
# also not raise. Each test below names the decision the entry now makes.
# ---------------------------------------------------------------------------

def test_unbalanced_paren_in_an_entry_admits_its_literal_text_without_raising(tmp_path):
    """F13, inverted by R12 — deliberate, not a weakened test.

    Originally: `(` was not escaped, so an entry with an unmatched `(`
    (plausible in a pasted regex fragment, or just a typo) produced an
    invalid ERE line, and evaluating it — the exact thing `allowlist_admits`
    and tinyproxy's `regexec` must do for every request — raised instead of
    admitting or refusing the connection.

    The entry now decides, and the decision asserted here is the one the
    contract requires: it admits its own literal text and nothing else.

    The original also pinned the generated line as the exact string
    `(^|\\.)a(b$`. That is replaced rather than dropped: the line count is
    still checked (generation still produces one line per entry, and still
    does not raise), and what the line *means* is checked by the two
    decisions below. Pinning the exact escape spelling would fail a correct
    reimplementation that wrote, say, `[(]` instead of `\\(`, and the contract
    is about what the entry matches, not how the escaping is spelled.
    """
    allowlist = ["a(b"]
    assert len(h.filter_patterns(tmp_path, allowlist)) == 1
    assert h.allowlist_admits(tmp_path, allowlist, "a(b")
    assert not h.allowlist_admits(tmp_path, allowlist, "ab")


def test_unbalanced_bracket_in_an_entry_admits_its_literal_text_without_raising(tmp_path):
    """F13, inverted by R12 — deliberate, not a weakened test.

    Same mechanism as the unbalanced paren, with `[` (an unterminated
    character class) instead: the match used to raise, and now decides. The
    decision is the literal one.
    """
    allowlist = ["a[b"]
    assert len(h.filter_patterns(tmp_path, allowlist)) == 1
    assert h.allowlist_admits(tmp_path, allowlist, "a[b")
    assert not h.allowlist_admits(tmp_path, allowlist, "ab")


# ---------------------------------------------------------------------------
# F2 (already filed) — non-string entry, confirmed again here as part of the
# ordinary boundary sweep, not re-filed.
# ---------------------------------------------------------------------------

def test_non_string_entry_raises_attributeerror_not_a_validation_error(tmp_path):
    with pytest.raises(AttributeError):
        h.filter_patterns(tmp_path, [{"sub": "example.com"}])


# ---------------------------------------------------------------------------
# F1 (already filed, see C1-sandbox.md) — reconfirmed here for completeness of
# this file's own sweep of ERE metacharacters. Fixed by R12
# (context/specs/phase2-ere-escaping.md) and inverted below. Deliberate
# inversion, not a weakened test.
# ---------------------------------------------------------------------------

def test_a_pipe_in_one_entry_admits_only_that_entrys_own_literal_text(tmp_path):
    """F1, inverted by R12 — deliberate, not a weakened test.

    Originally one assertion: with `evil.com|.*` in the list,
    `totally-unrelated.example` was admitted, because ERE alternation has the
    lowest precedence of any operator and so split the whole anchored line
    into `(^|\\.)evil\\.com` or `.*` — an allow-all.

    That host is still named, with its polarity flipped, and the inversion is
    widened in the two directions that make the flip mean something. `|` is
    now a literal, so the entry admits the (absurd, but literal) host
    `evil.com|.*` and does NOT admit `evil.com` — the second matters because
    a fix that merely dropped everything from the `|` onwards would leave
    `evil.com` admitted and still pass an "unrelated host is refused" check.
    The honest neighbouring entry is asserted to still work, exact and by
    subdomain, since a two-entry list is the shape in which alternation did
    its damage.
    """
    allowlist = ["good.example.com", "evil.com|.*"]
    assert not h.allowlist_admits(tmp_path, allowlist, "totally-unrelated.example")
    assert h.allowlist_admits(tmp_path, allowlist, "evil.com|.*")
    assert not h.allowlist_admits(tmp_path, allowlist, "evil.com")
    assert h.allowlist_admits(tmp_path, allowlist, "good.example.com")
    assert h.allowlist_admits(tmp_path, allowlist, "api.good.example.com")
