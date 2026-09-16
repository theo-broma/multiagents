import sys
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))
import c1_harness as h

def test_lone_close_paren_crashes_the_match_instead_of_deciding(tmp_path):
    allowlist = [")"]
    with pytest.raises(Exception, match="unbalanced parenthesis"):
        h.allowlist_admits(tmp_path, allowlist, ")")

def test_lone_close_bracket_matches_literal(tmp_path):
    allowlist = ["]"]
    assert h.allowlist_admits(tmp_path, allowlist, "]")
    assert not h.allowlist_admits(tmp_path, allowlist, "a]b")

def test_balanced_parens_silently_match_inner_string(tmp_path):
    allowlist = ["a(b)c"]
    assert not h.allowlist_admits(tmp_path, allowlist, "a(b)c")
    assert h.allowlist_admits(tmp_path, allowlist, "abc")

def test_balanced_brackets_silently_match_character_class(tmp_path):
    allowlist = ["a[bc]d"]
    assert not h.allowlist_admits(tmp_path, allowlist, "a[bc]d")
    assert h.allowlist_admits(tmp_path, allowlist, "abd")
    assert h.allowlist_admits(tmp_path, allowlist, "acd")
    assert not h.allowlist_admits(tmp_path, allowlist, "axd")

def test_interval_quantifier_changes_match_semantics(tmp_path):
    allowlist = ["a{2}b"]
    assert not h.allowlist_admits(tmp_path, allowlist, "a{2}b")
    assert h.allowlist_admits(tmp_path, allowlist, "aab")
