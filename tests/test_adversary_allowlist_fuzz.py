"""Adversary fuzzing and hardcoding tests for write_proxy_config.

Tests inputs the existing suite never uses: boundary values, malformed hosts,
Unicode edge cases, and property-based checks.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import c1_harness as h  # noqa: E402


# ---------------------------------------------------------------------------
# Boundary values — hosts the suite never tests
# ---------------------------------------------------------------------------

def test_host_with_consecutive_dots(tmp_path):
    """A host with consecutive dots is not a valid DNS name, but what does
    the allowlist do with it?"""
    allowlist = ["example..com"]
    patterns = h.filter_patterns(tmp_path, allowlist)
    assert patterns == [r"(^|\.)example\.\.com$"]
    assert h.allowlist_admits(tmp_path, allowlist, "example..com")
    assert not h.allowlist_admits(tmp_path, allowlist, "example.com")


def test_host_with_only_dots(tmp_path):
    """A host of only dots."""
    allowlist = ["..."]
    patterns = h.filter_patterns(tmp_path, allowlist)
    assert patterns == [r"(^|\.)\.\.\.$"]
    assert h.allowlist_admits(tmp_path, allowlist, "...")
    assert not h.allowlist_admits(tmp_path, allowlist, "..")


def test_host_with_underscore(tmp_path):
    """Underscores are not valid in DNS hostnames but are sometimes used."""
    allowlist = ["my_host.example.com"]
    assert h.allowlist_admits(tmp_path, allowlist, "my_host.example.com")
    assert h.allowlist_admits(tmp_path, allowlist, "sub.my_host.example.com")


def test_ipv6_loopback(tmp_path):
    """IPv6 addresses use colons, not dots. How does the allowlist handle them?"""
    allowlist = ["::1"]
    patterns = h.filter_patterns(tmp_path, allowlist)
    # Colons are not escaped, so the pattern is literal
    assert patterns == [r"(^|\.)::1$"]
    assert h.allowlist_admits(tmp_path, allowlist, "::1")


def test_ipv6_full_address(tmp_path):
    """A full IPv6 address."""
    allowlist = ["2001:db8::1"]
    assert h.allowlist_admits(tmp_path, allowlist, "2001:db8::1")
    assert not h.allowlist_admits(tmp_path, allowlist, "2001:db8::2")


def test_very_long_hostname(tmp_path):
    """DNS hostnames are limited to 253 characters. What about longer?"""
    long_host = "a" * 300 + ".com"
    allowlist = [long_host]
    assert h.allowlist_admits(tmp_path, allowlist, long_host)
    assert not h.allowlist_admits(tmp_path, allowlist, "a" * 299 + ".com")


def test_hostname_at_dns_limit(tmp_path):
    """A hostname at the DNS limit (253 chars)."""
    host = "a" * 249 + ".com"  # 253 chars total
    assert len(host) == 253
    allowlist = [host]
    assert h.allowlist_admits(tmp_path, allowlist, host)


def test_empty_host_string(tmp_path):
    """An empty host string."""
    allowlist = ["example.com"]
    assert not h.allowlist_admits(tmp_path, allowlist, "")


def test_host_with_whitespace_only(tmp_path):
    """A host with only whitespace."""
    allowlist = ["example.com"]
    assert not h.allowlist_admits(tmp_path, allowlist, " ")
    assert not h.allowlist_admits(tmp_path, allowlist, "\t")
    assert not h.allowlist_admits(tmp_path, allowlist, "\n")


def test_host_with_newline_in_middle(tmp_path):
    """A host with a newline in the middle — should not match."""
    allowlist = ["example.com"]
    assert not h.allowlist_admits(tmp_path, allowlist, "example\n.com")


def test_host_with_percent_encoding(tmp_path):
    """Percent-encoded characters in a host."""
    allowlist = ["example%20.com"]
    patterns = h.filter_patterns(tmp_path, allowlist)
    # Percent is not a regex metacharacter, so it's literal
    assert h.allowlist_admits(tmp_path, allowlist, "example%20.com")


def test_host_with_at_sign(tmp_path):
    """An at-sign in a host (user@host format)."""
    allowlist = ["user@example.com"]
    patterns = h.filter_patterns(tmp_path, allowlist)
    # At-sign is not a regex metacharacter
    assert h.allowlist_admits(tmp_path, allowlist, "user@example.com")


# ---------------------------------------------------------------------------
# Unicode edge cases
# ---------------------------------------------------------------------------

def test_unicode_nfc_vs_nfd(tmp_path):
    """Unicode can be normalized in different forms (NFC vs NFD). Are they
    treated as equivalent?"""
    import unicodedata
    # é can be represented as a single character (NFC) or e + combining accent (NFD)
    nfc = "café.com"  # single character é
    nfd = unicodedata.normalize("NFD", nfc)  # e + combining accent
    assert nfc != nfd  # They're different strings

    allowlist = [nfc]
    # The allowlist should match the exact string, not the normalized form
    assert h.allowlist_admits(tmp_path, allowlist, nfc)
    # NFD form is a different string, so it should not match
    assert not h.allowlist_admits(tmp_path, allowlist, nfd)


def test_unicode_with_zero_width_space(tmp_path):
    """A zero-width space in a hostname."""
    host = "example\u200b.com"  # zero-width space
    allowlist = [host]
    assert h.allowlist_admits(tmp_path, allowlist, host)
    assert not h.allowlist_admits(tmp_path, allowlist, "example.com")


def test_mixed_script_hostname(tmp_path):
    """A hostname with mixed scripts (Latin + Cyrillic)."""
    host = "exampleпример.com"
    allowlist = [host]
    assert h.allowlist_admits(tmp_path, allowlist, host)


# ---------------------------------------------------------------------------
# Property-based checks
# ---------------------------------------------------------------------------

def test_roundtrip_property_exact_match(tmp_path):
    """Property: if a host is in the allowlist, it should always be admitted."""
    hosts = ["example.com", "test.org", "api.service.io", "192.168.1.1"]
    for host in hosts:
        allowlist = [host]
        assert h.allowlist_admits(tmp_path, allowlist, host), f"Failed for {host}"


def test_roundtrip_property_subdomain_always_admitted(tmp_path):
    """Property: if a host is in the allowlist, any subdomain should be admitted."""
    hosts = ["example.com", "test.org"]
    for host in hosts:
        allowlist = [host]
        for subprefix in ["api", "deeply.nested.api", "sub"]:
            subdomain = f"{subprefix}.{host}"
            assert h.allowlist_admits(tmp_path, allowlist, subdomain), \
                f"Failed for {subdomain}"


def test_roundtrip_property_parent_domain_refused(tmp_path):
    """Property: if a subdomain is in the allowlist, its parent should be refused."""
    allowlist = ["sub.example.com"]
    assert not h.allowlist_admits(tmp_path, allowlist, "example.com")


def test_allowlist_is_monotonic(tmp_path):
    """Property: adding more entries to an allowlist should never cause a
    previously-admitted host to be refused."""
    allowlist1 = ["example.com"]
    allowlist2 = ["example.com", "test.org"]

    assert h.allowlist_admits(tmp_path, allowlist1, "example.com")
    assert h.allowlist_admits(tmp_path, allowlist2, "example.com")


def test_empty_allowlist_denies_all(tmp_path):
    """Property: an empty allowlist should deny every host."""
    hosts = ["example.com", "test.org", "localhost", "127.0.0.1", ""]
    for host in hosts:
        assert not h.allowlist_admits(tmp_path, [], host), f"Failed for {host}"


# ---------------------------------------------------------------------------
# Entries that are valid regex but not valid hostnames
#
# Both tests below came from the mutation run, and both are inverted by R12
# (`context/specs/phase2-ere-escaping.md`, findings F14). Deliberate
# inversions, not weakened tests — and the inversion was checked against what
# the adversary was actually proving, because a fuzz finding and a
# characterization finding do not always survive a fix the same way.
#
# The mutation was making two claims at once:
#
#   1. Nothing validates that an allowlist entry is a hostname. A string that
#      is not one is accepted in silence, produces a filter line, and becomes
#      part of the egress boundary.
#   2. That string is then read as a regex, so the line admits hosts its
#      author never wrote.
#
# R12 removes (2) and leaves (1) exactly as it was. So the point survives, in
# a different form, and each test below now asserts it in that form: the
# non-hostname entry is still accepted without complaint and still produces a
# line, but the line now admits only the entry's own literal text — a string
# no real request can carry, which makes the entry inert rather than
# dangerous. Inert with no signal is F11 and F10's territory and is not in
# R12's scope; the assertions here are written so that a later fix which
# starts REJECTING non-hostname entries will fail them loudly rather than
# pass by accident, which is what an adversarial test is for.
# ---------------------------------------------------------------------------

def test_entry_that_is_valid_regex_but_not_hostname_is_accepted_and_matches_only_itself(tmp_path):
    """An entry like `a(b)c` is valid regex but not a valid hostname.

    Originally this asserted the parens were read as a group: `abc` admitted,
    `a(b)c` refused. R12 escapes them, so both flip.
    """
    allowlist = ["a(b)c"]
    # Claim 1, unchanged by R12: the entry is not validated. No error, and it
    # still becomes one line of the egress boundary.
    assert len(h.filter_patterns(tmp_path, allowlist)) == 1
    # Claim 2, inverted by R12: the parens are literal, so the line admits the
    # entry's own text and not the group it used to denote.
    assert h.allowlist_admits(tmp_path, allowlist, "a(b)c")
    assert not h.allowlist_admits(tmp_path, allowlist, "abc")


def test_entry_with_character_class_is_accepted_and_matches_only_itself(tmp_path):
    """An entry like `a[bc]d` is valid regex but not a valid hostname.

    The sharper of the two: a character class let one short entry stand in
    for a whole set of hosts (`abd`, `acd`) rather than for one wrong host.
    R12 escapes the brackets, so the set collapses to the literal entry.
    """
    allowlist = ["a[bc]d"]
    assert len(h.filter_patterns(tmp_path, allowlist)) == 1
    assert h.allowlist_admits(tmp_path, allowlist, "a[bc]d")
    # Every member of the class the entry used to denote is now refused...
    assert not h.allowlist_admits(tmp_path, allowlist, "abd")
    assert not h.allowlist_admits(tmp_path, allowlist, "acd")
    # ...and a non-member stays refused, as it was before the fix.
    assert not h.allowlist_admits(tmp_path, allowlist, "axd")


# ---------------------------------------------------------------------------
# Fuzzing with random inputs
# ---------------------------------------------------------------------------

def test_fuzz_random_hosts_against_random_allowlists(tmp_path):
    """Fuzzing: generate random hosts and allowlists, check properties hold."""
    import random
    random.seed(42)  # Reproducible

    for _ in range(100):
        # Generate a random allowlist
        allowlist_size = random.randint(0, 5)
        allowlist = []
        for _ in range(allowlist_size):
            # Generate a random hostname-like string
            parts = random.randint(1, 4)
            host = ".".join(
                "".join(random.choices("abcdefghijklmnopqrstuvwxyz0123456789-", k=random.randint(1, 10)))
                for _ in range(parts)
            )
            allowlist.append(host)

        # Property: every host in the allowlist should be admitted
        for host in allowlist:
            assert h.allowlist_admits(tmp_path, allowlist, host), \
                f"Failed: allowlist={allowlist}, host={host}"

        # Property: an empty allowlist denies everything
        if allowlist:
            random_host = "test.example.com"
            assert not h.allowlist_admits(tmp_path, [], random_host)
