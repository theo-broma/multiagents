"""R12 — every ERE metacharacter in an `egress_allowlist` entry is a literal.

The contract is `context/specs/phase2-ere-escaping.md`. It gathers four
findings that share one cause — `write_proxy_config` escapes the literal dot
and nothing else — but **not** one test: a change that stops the crash while
leaving alternation working would look finished. So there is one test per
finding here, each against that finding's own reproduction, plus a guard
pinning the ordinary matching the contract explicitly does not touch.

Everything below goes through the public surface only: `h.allowlist_admits`
(the allow/refuse decision) and, where the contract is about the generated
line rather than the decision, `h.filter_patterns`. Nothing asserts the text
of a generated pattern — how an entry gets escaped is the implementer's
choice, and an entry that admits its literal text and nothing else has
honoured R12 whichever way it spells the escape.

**Where Python `re` and POSIX ERE could diverge, and why these tests do not
rely on it.** The harness evaluates tinyproxy's lines with Python's `re`
(tinyproxy is not installed here). For the constructs these entries produce
today — anchors, alternation, `()`, `[]`, `*`, `+`, `?`, `{n}` — the two
agree, which is the whole reason the findings are real for the live proxy and
not just for the stand-in. They diverge on one thing that a *fix* may
introduce: POSIX ERE leaves a backslash before an ordinary character
undefined, so an escape of `-`, `&`, `~`, `#` or a space (which Python's
`re.escape` does emit) is well-defined for `re` and not for ERE, even though
glibc's `regcomp` reads it as the literal. No entry in this file contains such
a character, so no assertion here passes or fails on that difference.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import c1_harness as h  # noqa: E402


# ---------------------------------------------------------------------------
# F1 — critical. `|` has ERE's lowest precedence, so it escapes the anchors
# that surround the entry and the line matches every host.
# ---------------------------------------------------------------------------

def test_r12_f1_entry_containing_a_pipe_admits_only_its_own_literal_text(tmp_path):
    """The entry `evil.com|.*` must admit its literal text and nothing else.

    Today the generated line is `(^|\\.)evil\\.com|.*$`, whose second branch
    is `.*` unanchored on the left — so every host is admitted and the
    allowlist is an allow-all. The load-bearing assertion is the refusal of
    `anything.example`, which is related to no entry in the list.
    """
    allowlist = ["evil.com|.*"]
    assert h.allowlist_admits(tmp_path, allowlist, "evil.com|.*")
    assert not h.allowlist_admits(tmp_path, allowlist, "anything.example")
    assert not h.allowlist_admits(tmp_path, allowlist, "evil.com")
    assert not h.allowlist_admits(tmp_path, allowlist, "")

    # The reproduction as it is actually written by hand: one honest entry
    # alongside the crafted one. The honest entry must keep working, and the
    # crafted one must not widen it.
    mixed = ["good.example.com", "evil.com|.*"]
    assert h.allowlist_admits(tmp_path, mixed, "good.example.com")
    assert h.allowlist_admits(tmp_path, mixed, "api.good.example.com")
    assert not h.allowlist_admits(tmp_path, mixed, "totally-unrelated.example")
    assert not h.allowlist_admits(tmp_path, mixed, "evil.com")


# ---------------------------------------------------------------------------
# F13 — high. An unbalanced `(` or `[` produces a syntactically invalid line,
# and evaluating it raises rather than admitting or refusing.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("entry", ["a(b", "a[b"])
def test_r12_f13_entry_with_an_unbalanced_group_or_class_decides_without_raising(
        tmp_path, entry):
    """The contract asks for one thing here and it is not a match result:
    whatever the line then admits, evaluating it must not raise.

    Today `a(b` becomes `(^|\\.)a(b$` and `a[b` becomes `(^|\\.)a[b$`; both
    are invalid ERE, so tinyproxy's `regexec` — which runs on *every* request
    — fails on the whole filter rather than on that one entry. A typo in one
    line of `project.yaml` takes the egress proxy down for the container.

    So: the decision must come back. What it decides is secondary, which is
    why this asserts a boolean came back and not which boolean it was.
    """
    for host in (entry, "unrelated.example"):
        try:
            decision = h.allowlist_admits(tmp_path, [entry], host)
        except Exception as exc:  # noqa: BLE001 — the point is that none escapes
            pytest.fail(
                f"entry {entry!r} crashed the allow/refuse decision for host "
                f"{host!r}: {type(exc).__name__}: {exc}")
        assert isinstance(decision, bool)

    # And the same entry must not poison the decision for its neighbours: a
    # filter that cannot be evaluated is a filter that admits or refuses
    # nothing at all, so the honest entry has to survive beside it.
    mixed = ["example.com", entry]
    try:
        assert h.allowlist_admits(tmp_path, mixed, "example.com")
    except Exception as exc:  # noqa: BLE001
        pytest.fail(f"entry {entry!r} crashed the decision for a neighbouring "
                    f"entry: {type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# F14 — high, and the silent one. Balanced `(...)` and `[...]` are *valid*
# regex, so nothing crashes and nothing warns; the entry simply matches a set
# of hosts its author never wrote.
# ---------------------------------------------------------------------------

def test_r12_f14_entry_with_balanced_parens_admits_its_literal_text_not_the_group(
        tmp_path):
    """`a(b)c` is the host `a(b)c`, not a group around `b`.

    Today `(^|\\.)a(b)c$` admits `abc` and refuses `a(b)c` — the exact
    inversion of what the entry says. Both halves are asserted because a fix
    that only stopped admitting `abc` (by, say, refusing entries it cannot
    parse) would not have made the entry mean its own text.
    """
    allowlist = ["a(b)c"]
    assert h.allowlist_admits(tmp_path, allowlist, "a(b)c")
    assert not h.allowlist_admits(tmp_path, allowlist, "abc")


def test_r12_f14_entry_with_balanced_brackets_admits_its_literal_text_not_the_class(
        tmp_path):
    """`a[bc]d` is the host `a[bc]d`, not the class `[bc]`.

    Today the line admits `abd` and `acd` — two hosts nobody listed — and
    refuses the one that was listed. A character class is the dangerous shape
    of this finding, because one short entry silently stands in for many
    hosts rather than for one other host.
    """
    allowlist = ["a[bc]d"]
    assert h.allowlist_admits(tmp_path, allowlist, "a[bc]d")
    assert not h.allowlist_admits(tmp_path, allowlist, "abd")
    assert not h.allowlist_admits(tmp_path, allowlist, "acd")


# ---------------------------------------------------------------------------
# F12 — medium. `*`, `+`, `?` and `{n}` are quantifiers on the character
# before them rather than characters in their own right. Glob intuition
# (`*.example.com`) is the common way to hit this by accident.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("entry,must_not_admit", [
    # `a*b`: zero-or-more `a`, so `b` and `aab` today; the literal is refused.
    ("a*b", ["b", "ab", "aab", "aaab"]),
    # `a+b`: one-or-more `a`.
    ("a+b", ["ab", "aab", "aaab"]),
    # `a?b`: optional `a` — the widest of the four, admitting a bare `b`.
    ("a?b", ["b", "ab"]),
    # `a{2}b`: exactly two `a`. The interval form, easiest to overlook.
    ("a{2}b", ["aab", "ab", "aaab"]),
])
def test_r12_f12_entry_with_a_quantifier_admits_its_literal_self_and_nothing_else(
        tmp_path, entry, must_not_admit):
    """Each of `a*b`, `a+b`, `a?b`, `a{2}b` must admit exactly its own text.

    The positive and the negatives are both load-bearing. Today every one of
    these entries refuses the host its author typed and admits a family of
    hosts they did not, so a fix is only a fix if it moves both.
    """
    allowlist = [entry]
    assert h.allowlist_admits(tmp_path, allowlist, entry)
    for host in must_not_admit:
        assert not h.allowlist_admits(tmp_path, allowlist, host), (
            f"entry {entry!r} must not admit {host!r}")


def test_r12_f12_glob_style_entry_admits_only_itself_not_the_hosts_it_looks_like(
        tmp_path):
    """The accident this finding is actually about: someone writes
    `*.example.com` expecting a wildcard.

    R12 makes that entry mean its own eight-plus characters — a host no
    resolver will ever ask for — so it admits nothing real. That is the
    correct outcome for R12: the entry is useless rather than wrong, and
    `example.com` on its own already covers subdomains. What must not happen
    is that it keeps being read as regex.
    """
    allowlist = ["*.example.com"]
    assert not h.allowlist_admits(tmp_path, allowlist, "example.com")
    assert not h.allowlist_admits(tmp_path, allowlist, "api.example.com")
    assert not h.allowlist_admits(tmp_path, allowlist, "unrelated.example")
    assert h.allowlist_admits(tmp_path, allowlist, "*.example.com")


# ---------------------------------------------------------------------------
# The guard. R12 says explicitly that the anchoring is correct and is not what
# this contract touches, so these must be green BEFORE the fix and green
# AFTER it. They are the counterweight to a fix that escapes too much: escape
# the `.` in `example.com` along with everything else and the subdomain case
# quietly stops working, which no test above would notice.
# ---------------------------------------------------------------------------

def test_r12_guard_an_exact_host_still_matches_itself(tmp_path):
    allowlist = ["example.com"]
    assert h.allowlist_admits(tmp_path, allowlist, "example.com")
    assert h.allowlist_admits(tmp_path, allowlist, "EXAMPLE.COM")


def test_r12_guard_a_subdomain_is_still_admitted_through_the_dot_prefix(tmp_path):
    """The `(^|\\.)` prefix is what makes `example.com` cover its subdomains.
    It survives R12 untouched — only the entry between the anchors changes."""
    allowlist = ["example.com"]
    assert h.allowlist_admits(tmp_path, allowlist, "api.example.com")
    assert h.allowlist_admits(tmp_path, allowlist, "deeply.nested.api.example.com")


def test_r12_guard_an_unrelated_host_is_still_refused(tmp_path):
    """Including the two near misses the anchoring exists to refuse: a shared
    suffix with no label boundary, and the listed host as a prefix of a
    longer one."""
    allowlist = ["example.com"]
    assert not h.allowlist_admits(tmp_path, allowlist, "unrelated.test")
    assert not h.allowlist_admits(tmp_path, allowlist, "notexample.com")
    assert not h.allowlist_admits(tmp_path, allowlist, "example.com.evil.test")


def test_r12_guard_the_dot_in_an_entry_is_still_not_a_wildcard(tmp_path):
    """The one metacharacter the current code does handle. R12 must not lose
    it while adding the others: `example.com` must not admit `exampleXcom`."""
    allowlist = ["example.com"]
    assert not h.allowlist_admits(tmp_path, allowlist, "exampleXcom")
    assert not h.allowlist_admits(tmp_path, allowlist, "api.exampleXcom")


def test_r12_guard_an_empty_allowlist_still_refuses_everything(tmp_path):
    """`FilterDefaultDeny Yes` with no lines: the boundary at zero entries."""
    assert not h.allowlist_admits(tmp_path, [], "example.com")
    assert not h.allowlist_admits(tmp_path, [], "")


def test_r12_guard_several_ordinary_entries_are_independent(tmp_path):
    """Each entry decides for itself; adding one must not widen another, and
    the same entry twice must decide the same way as once (idempotence of the
    list, which a de-duplicating or accumulating fix could break)."""
    allowlist = ["example.com", "example.net"]
    assert h.allowlist_admits(tmp_path, allowlist, "api.example.com")
    assert h.allowlist_admits(tmp_path, allowlist, "api.example.net")
    assert not h.allowlist_admits(tmp_path, allowlist, "example.org")
    assert not h.allowlist_admits(tmp_path, allowlist, "example.com.example.org")

    doubled = ["example.com", "example.com"]
    assert h.allowlist_admits(tmp_path, doubled, "api.example.com")
    assert not h.allowlist_admits(tmp_path, doubled, "example.org")
