"""GG-R10 adversary: addresses the reserved-domain exemption hides but should not.

The email regex stops a domain at the first character outside [\\w.-]. When a
real domain continues past that character, the exemption judges only the cut
prefix, and a cut prefix that lands on a reserved name is waved through. Each
case below was reported before GG-R10 (or names a domain the spec says stays a
finding) and is silent now. Every address is fictional.
"""
from __future__ import annotations

import sys
import unicodedata
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))
from gg_world import World, finding_lines, said  # noqa: E402

LOCAL = "pat.doe"


@pytest.fixture
def w(tmp_path):
    return World(tmp_path)


def addr(domain: str, local: str = LOCAL) -> str:
    return local + "@" + domain


def commit_mail(w: World, text: str) -> str:
    return w.commit_file("n.txt", "x\ny\nmail " + text + "\n")


def assert_one_email_finding(w: World, sha: str) -> None:
    p = w.scan()
    assert p.returncode == 1, said(p)
    lines = finding_lines(p, sha)
    assert any("email" in ln for ln in lines), said(p)


# IDNA (UTS #46) maps these three to "." -- they are label separators in a
# domain name. "mail.example.com<sep>acme.io" is a host under acme.io: the
# reserved name is not the suffix, so the spec keeps it a finding.
IDNA_DOTS = {"ideographic": "\u3002", "fullwidth": "\uff0e", "halfwidth": "\uff61"}


@pytest.mark.parametrize("sep", sorted(IDNA_DOTS))
@pytest.mark.parametrize("left, right", [
    ("mail.example.com", "acme.io"),
    ("corp.example", "acme.io"),
    ("eu.example.test", "acme.de"),
])
def test_hole_idna_dot_separator_cuts_domain_onto_reserved_prefix(w, sep, left, right):
    sha = commit_mail(w, addr(left + IDNA_DOTS[sep] + right))
    assert_one_email_finding(w, sha)


def test_hole_fullwidth_dot_hides_example_com_au(w):
    # Pre-dates GG-R10, but the spec lists example.com.au as a finding.
    sha = commit_mail(w, addr("example.com\uff0eau"))
    assert_one_email_finding(w, sha)


# A decomposed (NFD) accent is a combining mark, which \w does not match.
# "examplé" is a real IDN label and a look-alike of "example"; NFC is reported,
# NFD is not.
@pytest.mark.parametrize("domain", ["mail.exampl\u00e9.com", "corp.exampl\u00e9.fr",
                                    "shop.exampl\u00e9.de"])
def test_hole_nfd_combining_mark_cuts_idn_label_to_example(w, domain):
    nfd = unicodedata.normalize("NFD", domain)
    assert nfd != domain
    sha = commit_mail(w, addr(nfd))
    assert_one_email_finding(w, sha)


def test_hole_nfd_domain_in_author_field(w):
    nfd = unicodedata.normalize("NFD", addr("mail.exampl\u00e9.com"))
    sha = w.commit("clean message", author="Pat Doe <" + nfd + ">", add=False)
    assert_one_email_finding(w, sha)


def test_hole_nfd_domain_in_commit_message(w):
    nfd = unicodedata.normalize("NFD", addr("corp.exampl\u00e9.fr"))
    sha = w.commit("contact " + nfd + " for details", add=False)
    assert_one_email_finding(w, sha)


# Other in-label characters outside [\w.-]: ZWJ (valid in some IDN contexts),
# soft hyphen (IDNA maps it away, so the label continues past it).
@pytest.mark.parametrize("domain", [
    "corp.example\u200d.io",
    "corp.example\u00ad.io",
])
def test_hole_invisible_in_label_char_cuts_domain(w, domain):
    sha = commit_mail(w, addr(domain))
    assert_one_email_finding(w, sha)
