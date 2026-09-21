"""Characterization of ERE metacharacters in an allowlist entry — C1.

Four of the five tests in this file recorded F13 and F14: `(`, `)`, `[`, `]`,
`{` and `}` reached POSIX ERE unescaped, so an entry either crashed the match
(unbalanced) or silently matched something other than its own text (balanced).

R12 (`context/specs/phase2-ere-escaping.md`) escapes all of them, so those
four facts have changed and the assertions below are inverted — renamed to say
what is now true, each with its finding named in the docstring. These are
deliberate inversions, not weakened tests: where a test asserted a crash, what
replaces it is the decision the entry now makes, and where a test asserted
that some *other* host was admitted, the inverted form additionally pins that
the listed entry admits itself, which the original never checked.

`test_lone_close_bracket_matches_literal` is untouched: a lone `]` was already
an ordinary character to ERE, so R12 did not change what that entry does.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))
import c1_harness as h

def test_lone_close_paren_decides_and_admits_only_that_literal(tmp_path):
    """F13, inverted by R12 — deliberate, not a weakened test.

    A lone `)` used to close the `(^|` group the pattern opens, leaving a
    trailing `)` that made the line an unbalanced parenthesis, so evaluating
    it raised rather than deciding. It now decides.

    The crash assertion is replaced by the decision, not removed: the entry
    admits the literal `)` and nothing else. `a)b` is asserted refused so
    that "decides" cannot be satisfied by a line matching more than it was
    given, which is the failure mode a bare no-raise check would miss.
    """
    allowlist = [")"]
    assert h.allowlist_admits(tmp_path, allowlist, ")")
    assert not h.allowlist_admits(tmp_path, allowlist, "a)b")
    assert not h.allowlist_admits(tmp_path, allowlist, "")

def test_lone_close_bracket_matches_literal(tmp_path):
    allowlist = ["]"]
    assert h.allowlist_admits(tmp_path, allowlist, "]")
    assert not h.allowlist_admits(tmp_path, allowlist, "a]b")

def test_balanced_parens_match_the_literal_entry_not_the_inner_string(tmp_path):
    """F14, inverted by R12 — deliberate, not a weakened test.

    `a(b)c` used to be a group around `b`: the line admitted `abc`, a host
    nobody listed, and refused `a(b)c`, the one that was. Both assertions are
    flipped; the entry now means its own text.
    """
    allowlist = ["a(b)c"]
    assert h.allowlist_admits(tmp_path, allowlist, "a(b)c")
    assert not h.allowlist_admits(tmp_path, allowlist, "abc")

def test_balanced_brackets_match_the_literal_entry_not_the_character_class(tmp_path):
    """F14, inverted by R12 — deliberate, not a weakened test.

    `a[bc]d` used to be a character class: one short entry stood in for two
    hosts nobody listed (`abd`, `acd`) while refusing the one that was
    listed. Both admits are flipped to refusals and the refusal of `a[bc]d`
    to an admit. `axd` keeps its original polarity — it was outside the class
    then and is not the literal now — because asserting it still rules out a
    fix that widened the entry rather than making it literal.
    """
    allowlist = ["a[bc]d"]
    assert h.allowlist_admits(tmp_path, allowlist, "a[bc]d")
    assert not h.allowlist_admits(tmp_path, allowlist, "abd")
    assert not h.allowlist_admits(tmp_path, allowlist, "acd")
    assert not h.allowlist_admits(tmp_path, allowlist, "axd")

def test_interval_quantifier_braces_are_matched_as_literal_braces(tmp_path):
    """F12, inverted by R12 — deliberate, not a weakened test.

    `a{2}b` used to be the interval quantifier "two a's", so the line
    admitted `aab` and refused `a{2}b`. Both are flipped. `ab` is added: it
    was refused under the quantifier reading (one `a`, not two) and is
    refused now, and pinning it distinguishes a literal `{2}` from a fix that
    merely deleted the braces and their contents.
    """
    allowlist = ["a{2}b"]
    assert h.allowlist_admits(tmp_path, allowlist, "a{2}b")
    assert not h.allowlist_admits(tmp_path, allowlist, "aab")
    assert not h.allowlist_admits(tmp_path, allowlist, "ab")
