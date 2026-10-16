"""GG-R10 (amendment 2026-10-16): reserved example domains are not email findings.

Same throwaway-repo helpers as the other test_gg_* files. Every address is
fictional. Secret-shaped values come from gg_world, assembled at run time.
Look-alike domains are decided by label boundaries, never by substring.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))
import gg_world as gw  # noqa: E402
from gg_world import World, finding_lines, mask, said  # noqa: E402

LOCAL = "pat.doe"


@pytest.fixture
def w(tmp_path):
    return World(tmp_path)


def addr(domain: str, local: str = LOCAL) -> str:
    return local + "@" + domain


def commit_mail(w: World, text: str) -> str:
    return w.commit_file("n.txt", "x\ny\nmail " + text + "\n")


# ------------------------------------------------------- exempt: not findings
EXEMPT = [
    # apex
    "example.com", "example.net", "example.org", "example.test", "example.invalid",
    # subdomains of the reserved second-level names
    "mail.example.com", "a.b.example.net", "corp.example.org", "mail.example.test",
    "deep.er.example.invalid", "example.example.com", "xexample.example.invalid",
    # the .example TLD
    "corp.example", "a.b.example", "example.example", "com.example",
]


@pytest.mark.parametrize("domain", EXEMPT)
def test_gg_r10_exempt_domain_in_file_content_is_not_a_finding(w, domain):
    commit_mail(w, addr(domain))
    p = w.scan()
    assert p.returncode == 0, said(p)


@pytest.mark.parametrize("domain", [
    "EXAMPLE.COM", "Example.Org", "Mail.Example.NET", "MAIL.EXAMPLE.TEST",
    "Corp.EXAMPLE.Invalid", "Corp.Example", "EXAMPLE.INVALID",
])
def test_gg_r10_exempt_domain_is_compared_without_case(w, domain):
    commit_mail(w, addr(domain, "Pat.Doe"))
    p = w.scan()
    assert p.returncode == 0, said(p)


@pytest.mark.parametrize("domain", [
    "example.com.", "mail.example.org.", "example.test.", "corp.example.invalid.",
    "corp.example.", "EXAMPLE.NET.",
])
@pytest.mark.parametrize("wrap", ["{} end", "{}", "<{}>", "({})"])
def test_gg_r10_trailing_dot_is_removed_before_comparing(w, domain, wrap):
    commit_mail(w, wrap.format(addr(domain)))
    p = w.scan()
    assert p.returncode == 0, said(p)


def test_gg_r10_a_bare_example_host_is_not_a_finding(w):
    commit_mail(w, addr("example"))
    commit_mail(w, addr("example."))
    p = w.scan()
    assert p.returncode == 0, said(p)


@pytest.mark.parametrize("domain", ["mail.example.com", "corp.example", "example.test",
                                    "Mail.EXAMPLE.org."])
def test_gg_r10_exemption_applies_to_the_commit_message(w, domain):
    w.commit("contact " + addr(domain) + " for details", add=False)
    p = w.scan()
    assert p.returncode == 0, said(p)


@pytest.mark.parametrize("domain", ["mail.example.com", "corp.example", "example.test",
                                    "sub.example.invalid"])
def test_gg_r10_exemption_applies_to_the_author_email(w, domain):
    w.commit("clean message", author="Pat Doe <" + addr(domain) + ">", add=False)
    p = w.scan()
    assert p.returncode == 0, said(p)


@pytest.mark.parametrize("domain", ["mail.example.com", "corp.example", "example.test"])
def test_gg_r10_exemption_applies_to_the_committer_email(w, domain):
    w.commit("clean message", committer_email=addr(domain), add=False)
    p = w.scan()
    assert p.returncode == 0, said(p)


def test_gg_r10_many_exempt_addresses_in_one_commit_are_all_silent(w):
    body = "".join("mail " + addr(d, "u%d" % i) + "\n" for i, d in enumerate(EXEMPT))
    w.commit_file("many.txt", "x\n" + body)
    p = w.scan()
    assert p.returncode == 0, said(p)


# ------------------------------------------------- look-alikes: still findings
LOOKALIKES = [
    # listed in the spec
    "example.com.evil.io", "corp-example.com", "myexample.org", "example.co",
    "example.com.au", "examp1e.com",
    # IDN / Unicode look-alikes of "example" (Cyrillic а, е, о; fullwidth e; Greek ο)
    "exаmple.com", "еxample.org", "examplе.net", "exampмe.com",
    "ｅxample.com", "example.cоm", "exаmple.test", "corp.exаmple",
    "xn--exmple-cua.com",
    # not a label boundary
    "notexample.net", "examples.com", "example1.com", "1example.com", "example_.com",
    "corp_example.com", "corp-example.net", "example-.org", "-example.org",
    "xexample.com", "example-corp.com", "example--.com",
    # right label, wrong suffix
    "example.comm", "example.nett", "example.orgs", "example.cm", "example.or",
    "example.io", "example.co.uk", "example.net.au", "example.org.uk",
    "example.com.org", "example.net.com",
    # reserved name buried or not the suffix
    "example.com.evil.example.io", "example.org.evil.io", "example.net.evil.io",
    "example.test.evil.io", "example.invalid.evil.io", "example.test.com",
    "example.invalid.com", "example.example.io", "corp.example.io",
    "corp.example.co", "corp.examples", "corp.exampl", "corp.myexample",
    "corp.example-x", "corp.example1", "x.examplee",
    "example.invalid.test", "example.test.invalid",
    # the .test / .invalid TLDs as a whole are not exempt
    "test.invalid", "invalid.test", "myexample.test", "testexample.invalid",
    "example.testing", "example.invalidx", "example.te", "example.inval",
    "corp-example.invalid", "example-corp.test", "mail.test", "mail.invalid",
]


@pytest.mark.parametrize("domain", LOOKALIKES)
def test_gg_r10_lookalike_domain_is_still_one_email_finding(w, domain):
    address = addr(domain)
    sha = commit_mail(w, address)
    p = w.scan()
    assert p.returncode == 1, said(p)
    lines = finding_lines(p, sha)
    assert len(lines) == 1 and "email" in lines[0], said(p)
    assert mask(address) in lines[0]
    assert address not in said(p)


@pytest.mark.parametrize("domain", [
    "EXAMPLE.CO", "Example.Com.Au", "Corp-Example.COM", "EXAMPLE.COM.EVIL.IO",
    "Corp.Example.IO", "MAIL.TEST", "Example.Invalid.Test",
])
def test_gg_r10_lookalike_findings_survive_mixed_case(w, domain):
    sha = commit_mail(w, addr(domain))
    p = w.scan()
    assert p.returncode == 1, said(p)
    assert len(finding_lines(p, sha)) == 1, said(p)


@pytest.mark.parametrize("domain", [
    "example.co.", "example.com.au.", "corp-example.com.", "example.com.evil.io.",
    "corp.example.io.", "acme-widgets.test.", "acme-widgets.invalid.",
])
def test_gg_r10_a_trailing_dot_does_not_make_a_lookalike_exempt(w, domain):
    sha = commit_mail(w, addr(domain) + " end")
    p = w.scan()
    assert p.returncode == 1, said(p)
    assert len(finding_lines(p, sha)) == 1, said(p)


@pytest.mark.parametrize("local, domain", [
    ("jane.smith", "acme-widgets.invalid"),
    ("john.rivera", "acme-widgets.test"),
    ("ana.kowalski", "globex-trading.invalid"),
    ("li.wei", "initech-labs.test"),
    ("sam.o-brien", "hooli.invalid"),
    ("a.b", "umbrella-corp.test"),
    ("first.last", "mail.acme-widgets.test"),
    ("first.last", "eu.globex-trading.invalid"),
    ("Jane.Smith", "ACME-WIDGETS.INVALID"),
])
def test_gg_r10_first_dot_last_at_a_company_invalid_or_test_is_a_finding(w, local, domain):
    address = addr(domain, local)
    sha = commit_mail(w, address)
    p = w.scan()
    assert p.returncode == 1, said(p)
    lines = finding_lines(p, sha)
    assert len(lines) == 1 and "email" in lines[0], said(p)
    assert address not in said(p)


@pytest.mark.parametrize("surface", ["message", "author", "committer"])
def test_gg_r10_a_lookalike_is_found_on_every_surface(w, surface):
    bad = addr("example.com.evil.io")
    if surface == "message":
        sha = w.commit("contact " + bad, add=False)
    elif surface == "author":
        sha = w.commit("clean message", author="Pat Doe <" + bad + ">", add=False)
    else:
        sha = w.commit("clean message", committer_email=bad, add=False)
    p = w.scan()
    assert p.returncode == 1, said(p)
    assert any("email" in ln for ln in finding_lines(p, sha)), said(p)


def test_gg_r10_exempt_and_lookalike_on_one_line_leave_exactly_one_finding(w):
    bad = addr("corp-example.com", "other.one")
    sha = commit_mail(w, addr("mail.example.com") + " and " + bad + " and " + addr("corp.example"))
    p = w.scan()
    assert p.returncode == 1, said(p)
    lines = finding_lines(p, sha)
    assert len(lines) == 1 and mask(bad) in lines[0], said(p)


def test_gg_r10_the_same_localpart_on_two_domains_is_judged_per_domain(w):
    sha = commit_mail(w, addr("example.org") + " " + addr("example.org.au"))
    p = w.scan()
    assert p.returncode == 1, said(p)
    assert len(finding_lines(p, sha)) == 1, said(p)


# ------------------------------------------------------------------ unchanged
def test_gg_r10_the_previously_exempt_forms_stay_exempt(w):
    for t in ["x@example.com", "x@example.org", "x@example.net", "x@example.invalid",
              "agent@multiagents.local", "agent@multiagents.invalid",
              "12345+someone@users.noreply.github.com", "noreply@anthropic.com"]:
        commit_mail(w, t)
    p = w.scan()
    assert p.returncode == 0, said(p)


def test_gg_r10_allowed_emails_still_exempt_and_only_that_exact_address(tmp_path):
    ok = addr("acme-widgets.test", "jane.smith")
    w = World(tmp_path, allowed_emails=[ok])
    commit_mail(w, ok)
    assert w.scan().returncode == 0
    sha = commit_mail(w, addr("acme-widgets.test", "john.rivera"))
    p = w.scan()
    assert p.returncode == 1 and len(finding_lines(p, sha)) == 1, said(p)


def test_gg_r10_the_repos_own_email_still_exempt(tmp_path):
    own = addr("acme-widgets.invalid", "owner.one")
    w = World(tmp_path, repo_email=own)
    w.commit_file("a.txt", "mail " + own + "\n", author="Owner <" + own + ">")
    assert w.scan().returncode == 0


def test_gg_r10_allow_literal_still_suppresses_a_lookalike(tmp_path):
    bad = addr("example.co")
    w = World(tmp_path, allow=[bad])
    commit_mail(w, bad)
    assert w.scan().returncode == 0


# ---------------------------------------- other categories beside exempt domains
@pytest.mark.parametrize("name", sorted(gw.tokens()))
@pytest.mark.parametrize("domain", ["example.com", "mail.example.org", "corp.example",
                                    "example.test", "sub.example.invalid"])
def test_gg_r10_a_token_beside_an_exempt_domain_is_still_a_finding(w, name, domain):
    token = gw.tokens()[name]
    sha = commit_mail(w, addr(domain) + " key=" + token)
    p = w.scan()
    assert p.returncode == 1, said(p)
    lines = finding_lines(p, sha)
    assert len(lines) == 1 and "token" in lines[0] and "email" not in lines[0], said(p)
    assert mask(token) in lines[0]
    assert token not in said(p)


@pytest.mark.parametrize("domain", ["example.com", "mail.example.org", "corp.example",
                                    "example.test", "sub.example.invalid"])
def test_gg_r10_a_token_as_the_local_part_of_an_exempt_address_is_a_finding(w, domain):
    token = gw.tokens()["ghp"]
    sha = commit_mail(w, token + "@" + domain)
    p = w.scan()
    assert p.returncode == 1, said(p)
    lines = finding_lines(p, sha)
    assert lines and any("token" in ln for ln in lines), said(p)
    assert token not in said(p)


def test_gg_r10_a_token_in_the_commit_message_beside_an_exempt_domain(w):
    token = gw.tokens()["anthropic"]
    sha = w.commit("see " + addr("mail.example.com") + " " + token, add=False)
    p = w.scan()
    assert p.returncode == 1, said(p)
    assert any("token" in ln for ln in finding_lines(p, sha)), said(p)


def test_gg_r10_tailnet_ip_and_private_key_beside_exempt_domain_still_found(w):
    sha = w.commit_file("n.txt", "x\ny\n" + addr("mail.example.com") + " " + gw.tailnet_ip()
                        + "\n" + gw.private_key_header() + "\n")
    p = w.scan()
    assert p.returncode == 1, said(p)
    cats = " ".join(finding_lines(p, sha))
    assert "tailnet-ip" in cats and "private-key" in cats and "email" not in cats, said(p)


def test_gg_r10_a_private_pattern_inside_an_exempt_address_is_still_a_finding(tmp_path):
    w = World(tmp_path, patterns="acme-confidential\n")
    sha = commit_mail(w, addr("mail.example.com", "acme-confidential"))
    p = w.scan()
    assert p.returncode == 1, said(p)
    lines = finding_lines(p, sha)
    assert len(lines) == 1 and "private" in lines[0], said(p)


def test_gg_r10_the_exemption_does_not_reach_the_tailnet_host_check(w):
    sha = w.commit_file("n.txt", "x\ny\nhost " + gw.tailnet_host() + " " + addr("example.com") + "\n")
    p = w.scan()
    assert p.returncode == 1, said(p)
    lines = finding_lines(p, sha)
    assert len(lines) == 1 and "tailnet-host" in lines[0], said(p)
