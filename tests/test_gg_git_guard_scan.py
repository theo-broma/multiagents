"""GG-R3: `multiagents git-guard scan [RANGE]` on throwaway repositories.

Every secret-shaped value is assembled at run time by `gg_world`, so none sits
in this file as a literal. Exit codes, masked output lines and the absence of
the unmasked match are the observable contract.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))
import gg_world as gw  # noqa: E402
from gg_world import World, finding_lines, git, mask, out, said  # noqa: E402

EMAIL = gw.email()
TOKENS = gw.tokens()


@pytest.fixture
def w(tmp_path):
    return World(tmp_path)


def new_commit(w: World, name: str, line_text: str, *, lead: int = 2, **kw):
    """Commit a file whose line `lead + 1` holds `line_text`."""
    body = "".join(f"filler {i}\n" for i in range(lead)) + line_text + "\n"
    return w.commit_file(name, body, **kw)


def assert_no_leak(w: World, p, *unmasked: str):
    text = said(p)
    for secret in unmasked:
        assert secret not in text, "unmasked match in output"
        assert secret[2:] not in text, "unmasked tail of the match in output"
    for f in w.all_output_files():
        data = f.read_bytes()
        for secret in unmasked:
            assert secret.encode() not in data, f"unmasked match logged in {f}"


# ------------------------------------------------------------ exit codes -----
def test_gg_r3_clean_range_exits_0(w):
    w.commit_file("ok.txt", "nothing to see\n")
    p = w.scan()
    assert p.returncode == 0, said(p)


def test_gg_r3_empty_default_range_exits_0(w):
    p = w.scan()
    assert p.returncode == 0, said(p)


def test_gg_r3_scan_changes_nothing(w):
    sha = new_commit(w, "a.txt", "mail " + EMAIL)
    before = (w.head(), out(git(w.root, "status", "--porcelain", env=w.env)), w.remote_refs())
    assert w.scan().returncode == 1
    assert (w.head(), out(git(w.root, "status", "--porcelain", env=w.env)), w.remote_refs()) == before
    assert sha == w.head()


def test_gg_r3_unknown_range_is_a_usage_error_not_findings(w):
    assert w.scan().returncode == 0              # the guard itself works here
    p = w.scan("no-such-ref..main")
    assert p.returncode == 2, said(p)
    assert "invalid choice" not in said(p)


def test_gg_r3_outside_a_repository_exits_2(w, tmp_path):
    assert w.scan().returncode == 0
    plain = tmp_path / "plain"
    plain.mkdir()
    import subprocess
    p = subprocess.run([sys.executable, "-m", "multiagents.cli", "git-guard", "scan"],
                       cwd=str(plain), env=w.env, capture_output=True, text=True, timeout=60)
    assert p.returncode == 2, said(p)
    assert "invalid choice" not in said(p)


# ------------------------------------------------------ one per category -----
CASES = {
    "email": (EMAIL, lambda m: "contact " + m + " today"),
    "tailnet-ip": (gw.tailnet_ip(), lambda m: "ssh to " + m),
    "tailnet-host": (gw.tailnet_host(), lambda m: "host: " + m),
    **{f"token-{k}": (v, lambda m: "key = " + m) for k, v in TOKENS.items()},
}


def _category(name: str) -> str:
    return name.split("-")[0] if name.startswith("token-") else name


@pytest.mark.parametrize("name", sorted(CASES))
def test_gg_r3_each_category_is_found_masked_and_located(w, name):
    match, wrap = CASES[name]
    sha = new_commit(w, "src/conf.txt", wrap(match), lead=2)
    p = w.scan()
    assert p.returncode == 1, said(p)
    lines = finding_lines(p, sha)
    assert len(lines) == 1, said(p)
    line = lines[0]
    assert _category(name) in line
    assert "src/conf.txt:3" in line
    assert mask(match) in line
    assert_no_leak(w, p, match)


def test_gg_r3_private_key_block_is_found_at_its_first_line(w):
    block = gw.private_key_block()
    sha = new_commit(w, "id.pem", block, lead=1)
    p = w.scan()
    assert p.returncode == 1, said(p)
    lines = finding_lines(p, sha)
    assert len(lines) == 1, said(p)
    assert "private-key" in lines[0] and "id.pem:2" in lines[0]
    assert_no_leak(w, p, gw.private_key_header().replace("OPENSSH", "RSA"), "MIIBplaceholder")


def test_gg_r3_private_key_header_alone_is_found(w):
    sha = new_commit(w, "k.txt", gw.private_key_header())
    p = w.scan()
    assert p.returncode == 1, said(p)
    assert any("private-key" in ln for ln in finding_lines(p, sha))


def test_gg_r3_a_finding_names_the_commit_not_its_neighbours(w):
    clean = w.commit_file("a.txt", "fine\n")
    dirty = new_commit(w, "b.txt", "m " + EMAIL)
    after = w.commit_file("c.txt", "fine too\n")
    p = w.scan()
    assert p.returncode == 1
    assert finding_lines(p, dirty)
    assert not finding_lines(p, clean)
    assert not finding_lines(p, after)


def test_gg_r3_one_line_per_finding_across_files_and_commits(w):
    other = gw.email("another", "corp-mail.test")
    first = w.commit_file("one.txt", "a " + EMAIL + "\n")
    second = w.commit_file("two.txt", "b " + other + "\n")
    p = w.scan()
    assert p.returncode == 1
    assert len(finding_lines(p, first)) == 1
    assert len(finding_lines(p, second)) == 1
    assert_no_leak(w, p, EMAIL, other)


def test_gg_r3_two_matches_on_distinct_lines_are_two_findings(w):
    other = gw.email("another", "corp-mail.test")
    sha = w.commit_file("two.txt", f"x\n{EMAIL}\ny\n{other}\n")
    p = w.scan()
    lines = finding_lines(p, sha)
    assert len(lines) == 2, said(p)
    assert any("two.txt:2" in ln for ln in lines) and any("two.txt:4" in ln for ln in lines)


# ------------------------------------------------- boundaries: not findings ---
@pytest.mark.parametrize("text", [
    "x@example.com", "x@example.org", "x@example.net", "x@example.invalid",
    "agent@multiagents.local", "agent@multiagents.invalid",
    "12345+someone@users.noreply.github.com", "noreply@anthropic.com",
])
def test_gg_r3_allowed_email_shapes_are_not_findings(w, text):
    w.commit_file("a.txt", "mail " + text + "\n")
    p = w.scan()
    assert p.returncode == 0, said(p)


def test_gg_r3_the_repos_own_user_email_is_not_a_finding(tmp_path):
    own = gw.email("owner", "work-mail.test")
    w = World(tmp_path, repo_email=own)
    sha = w.commit_file("a.txt", "mail " + own + "\n", author="Owner <" + own + ">")
    p = w.scan()
    assert p.returncode == 0, said(p)
    # while a different address of the same domain is one
    w.commit_file("b.txt", "mail " + gw.email("stranger", "work-mail.test") + "\n")
    assert w.scan().returncode == 1


@pytest.mark.parametrize("ip", ["100.63.255.255", "100.128.0.1", "10.64.0.1", "99.64.0.1",
                                "192.168.1.10", "127.0.0.1"])
def test_gg_r3_addresses_outside_100_64_10_are_not_findings(w, ip):
    w.commit_file("a.txt", "host " + ip + "\n")
    p = w.scan()
    assert p.returncode == 0, said(p)


@pytest.mark.parametrize("ip", [gw.tailnet_ip("64", "0", "0"), gw.tailnet_ip("127", "255", "255"),
                                gw.tailnet_ip("100", "100", "100")])
def test_gg_r3_the_edges_of_100_64_10_are_findings(w, ip):
    sha = w.commit_file("a.txt", "host " + ip + "\n")
    p = w.scan()
    assert p.returncode == 1, said(p)
    assert any("tailnet-ip" in ln for ln in finding_lines(p, sha))


def test_gg_r3_a_placeholder_tailnet_host_is_not_a_finding(w):
    w.commit_file("a.txt", "host <name>.<tailnet>" + ".ts" + ".net\n")
    p = w.scan()
    assert p.returncode == 0, said(p)


def test_gg_r3_credential_shapes_have_a_minimum_length(w):
    short_openai = "sk" + "-" + "Ab1Cd2Ef3Gh4Ij5Kl6M"          # 19 after the prefix
    short_aws = "AK" + "IA" + "ABCDEFGH2345678"                   # 15 after AKIA
    w.commit_file("a.txt", f"{short_openai}\n{short_aws}\n")
    p = w.scan()
    assert p.returncode == 0, said(p)


def test_gg_r3_boundary_lengths_are_findings(w):
    openai = "sk" + "-" + "Ab1Cd2Ef3Gh4Ij5Kl6Mn"                # exactly 20
    aws = "AK" + "IA" + "ABCDEFGH23456789"                        # exactly 16
    sha = w.commit_file("a.txt", f"{openai}\n{aws}\n")
    p = w.scan()
    assert p.returncode == 1
    assert len(finding_lines(p, sha)) == 2, said(p)


# ------------------------------------------------------ what is scanned ------
def test_gg_r3_only_added_lines_count_not_context_or_removed_lines(w):
    sha = w.commit_file("a.txt", "head\n" + EMAIL + "\ntail\n")
    git(w.root, "push", "-q", "origin", "main", env=w.env)         # now history
    w.commit_file("a.txt", "head\n" + EMAIL + "\ntail\nadded fine line\n")   # context only
    p = w.scan()
    assert p.returncode == 0, said(p)
    w.commit_file("a.txt", "head\ntail\nadded fine line\n")         # removal
    assert w.scan().returncode == 0
    # the full history still shows it
    p = w.scan(sha + "~1.." + sha)
    assert p.returncode == 1


def test_gg_r3_a_secret_added_then_removed_inside_the_range_is_still_found(w):
    first = w.commit_file("a.txt", "m " + EMAIL + "\n")
    w.commit_file("a.txt", "clean now\n")
    p = w.scan()
    assert p.returncode == 1, said(p)
    assert finding_lines(p, first)


def test_gg_r3_default_range_is_remote_base_to_base_not_head(w):
    w.branch("feature")
    w.commit_file("a.txt", "m " + EMAIL + "\n")
    p = w.scan()                       # origin/main..main: the feature commit is not in it
    assert p.returncode == 0, said(p)
    w.checkout("main")


def test_gg_r3_already_pushed_history_is_not_rescanned_by_default(w):
    w.commit_file("a.txt", "m " + EMAIL + "\n")
    git(w.root, "push", "-q", "origin", "main", env=w.env)
    assert w.scan().returncode == 0


def test_gg_r3_an_explicit_range_overrides_the_default(w):
    dirty = w.commit_file("a.txt", "m " + EMAIL + "\n")
    clean = w.commit_file("b.txt", "fine\n")
    assert w.scan(dirty + ".." + clean).returncode == 0
    p = w.scan(dirty + "~1.." + clean)
    assert p.returncode == 1 and finding_lines(p, dirty)


def test_gg_r3_binary_files_are_scanned_as_raw_bytes(w):
    payload = b"\x00\x01\x02\xff" + EMAIL.encode() + b"\x00\xfe\x00"
    sha = w.commit_file("blob.bin", payload)
    p = w.scan()
    assert p.returncode == 1, said(p)
    lines = finding_lines(p, sha)
    assert len(lines) == 1 and "email" in lines[0] and "blob.bin" in lines[0], said(p)
    assert_no_leak(w, p, EMAIL)


# ----------------------------------- message, author, committer, path names ---
def test_gg_r3_commit_message_is_scanned(w):
    sha = w.commit("fix by " + EMAIL, add=False)
    p = w.scan()
    assert p.returncode == 1, said(p)
    lines = finding_lines(p, sha)
    assert len(lines) == 1 and "email" in lines[0] and "message" in lines[0]
    assert_no_leak(w, p, EMAIL)


def test_gg_r3_author_email_is_scanned(w):
    sha = w.commit("clean message", author="Someone <" + EMAIL + ">", add=False)
    p = w.scan()
    assert p.returncode == 1, said(p)
    lines = finding_lines(p, sha)
    assert len(lines) == 1 and "email" in lines[0] and "author" in lines[0]
    assert_no_leak(w, p, EMAIL)


def test_gg_r3_committer_email_is_scanned(w):
    sha = w.commit("clean message", committer_email=EMAIL, add=False)
    p = w.scan()
    assert p.returncode == 1, said(p)
    assert any("email" in ln for ln in finding_lines(p, sha))
    assert_no_leak(w, p, EMAIL)


def test_gg_r3_author_name_is_scanned_against_private_patterns(tmp_path):
    name = "Zebra" + " Quux"
    w = World(tmp_path, patterns=name + "\n")
    sha = w.commit("clean message", author=name + " <t@example.invalid>", add=False)
    p = w.scan()
    assert p.returncode == 1, said(p)
    lines = finding_lines(p, sha)
    assert len(lines) == 1 and "private" in lines[0] and "author" in lines[0]
    assert_no_leak(w, p, name)


def test_gg_r3_added_path_names_are_scanned(w):
    token = TOKENS["ghp"]
    sha = w.commit_file("notes-" + token + ".txt", "harmless\n")
    p = w.scan()
    assert p.returncode == 1, said(p)
    lines = finding_lines(p, sha)
    assert len(lines) == 1 and "token" in lines[0] and "path-name" in lines[0]
    assert_no_leak(w, p, token)


def test_gg_r3_renamed_path_names_are_scanned(w):
    token = TOKENS["ghp"]
    git(w.root, "mv", "seed.txt", "moved-" + token + ".txt", env=w.env)
    sha = w.commit("rename")
    p = w.scan()
    assert p.returncode == 1, said(p)
    assert any("path-name" in ln for ln in finding_lines(p, sha)), said(p)
    assert_no_leak(w, p, token)


def test_gg_r3_a_deleted_path_is_not_scanned(w):
    name = "gone-" + TOKENS["ghp"] + ".txt"
    w.commit_file(name, "x\n")
    sha = w.commit("delete it", add=False)
    git(w.root, "rm", "-q", name, env=w.env)
    sha = w.commit("remove", add=False)
    p = w.scan(sha + "~1.." + sha)
    assert p.returncode == 0, said(p)


# -------------------------------------------------------------- allow --------
def test_gg_r3_allowed_emails_entries_are_not_findings(tmp_path):
    w = World(tmp_path, allowed_emails=[EMAIL])
    w.commit_file("a.txt", "m " + EMAIL + "\n")
    p = w.scan()
    assert p.returncode == 0, said(p)


def test_gg_r3_allowed_emails_only_cover_that_address(tmp_path):
    other = gw.email("another", "corp-mail.test")
    w = World(tmp_path, allowed_emails=[EMAIL])
    sha = w.commit_file("a.txt", "m " + EMAIL + "\nn " + other + "\n")
    p = w.scan()
    assert p.returncode == 1
    assert len(finding_lines(p, sha)) == 1


def test_gg_r3_allowed_emails_also_cover_the_author(tmp_path):
    w = World(tmp_path, allowed_emails=[EMAIL])
    w.commit("m", author="Someone <" + EMAIL + ">", add=False)
    assert w.scan().returncode == 0


@pytest.mark.parametrize("name", ["email", "tailnet-ip", "tailnet-host", "token-ghp"])
def test_gg_r3_allow_entries_equal_to_the_match_are_not_findings(tmp_path, name):
    match, wrap = CASES[name]
    w = World(tmp_path, allow=[match])
    w.commit_file("a.txt", wrap(match) + "\n")
    p = w.scan()
    assert p.returncode == 0, said(p)


def test_gg_r3_allow_is_an_exact_match_not_a_substring(tmp_path):
    w = World(tmp_path, allow=[EMAIL[:6], EMAIL[3:]])
    sha = w.commit_file("a.txt", "m " + EMAIL + "\n")
    p = w.scan()
    assert p.returncode == 1, said(p)
    assert finding_lines(p, sha)


def test_gg_r3_allow_does_not_cover_a_different_match(tmp_path):
    other = gw.email("another", "corp-mail.test")
    w = World(tmp_path, allow=[EMAIL])
    sha = w.commit_file("a.txt", "m " + other + "\n")
    assert w.scan().returncode == 1


# ----------------------------------------------------- private patterns ------
def pw(tmp_path, patterns, **kw):
    return World(tmp_path, patterns=patterns, **kw)


def test_gg_r3_private_literal_matches_case_insensitively_and_is_masked(tmp_path):
    secret = "Zebra" + " Quux"
    w = pw(tmp_path, "# header\n\n" + secret.lower() + "\n")
    text = secret.upper()
    sha = new_commit(w, "n.txt", "about " + text + " here")
    p = w.scan()
    assert p.returncode == 1, said(p)
    lines = finding_lines(p, sha)
    assert len(lines) == 1 and "private" in lines[0] and "n.txt:3" in lines[0]
    assert text[:2] + "…" in lines[0]
    assert_no_leak(w, p, text, secret.lower())


def test_gg_r3_private_literal_is_not_a_regex(tmp_path):
    w = pw(tmp_path, "a" + "." + "c" + "\n(unbalanced" + "\n")
    w.commit_file("a.txt", "abc\naxc\n")
    p = w.scan()
    assert p.returncode == 0, said(p)
    sha = w.commit_file("b.txt", "has a" + "." + "c here and (unbalanced" + " too\n")
    p = w.scan()
    assert p.returncode == 1
    assert len(finding_lines(p, sha)) == 2, said(p)


def test_gg_r3_private_regex_lines_start_with_re(tmp_path):
    w = pw(tmp_path, "re:" + "proj" + "-[0-9]{3}\n")
    w.commit_file("a.txt", "proj-12 and proj-x\n")
    assert w.scan().returncode == 0
    sha = w.commit_file("b.txt", "see PROJ-123 now\n")           # also case-insensitive
    p = w.scan()
    assert p.returncode == 1 and finding_lines(p, sha), said(p)
    assert_no_leak(w, p, "PROJ-123")


def test_gg_r3_blank_and_comment_lines_of_the_patterns_file_are_ignored(tmp_path):
    word = "commentword"
    w = pw(tmp_path, "\n\n# " + word + "\n#" + word + "\n\n")
    w.commit_file("a.txt", "# " + word + "\n" + word + "\nanything at all\n")
    p = w.scan()
    assert p.returncode == 0, said(p)


def test_gg_r3_private_patterns_apply_to_the_commit_message_and_path_names(tmp_path):
    word = "quuxword"
    w = pw(tmp_path, word + "\n")
    msg = w.commit("talks about " + word, add=False)
    named = w.commit_file("dir-" + word + ".txt", "fine\n")
    p = w.scan()
    assert p.returncode == 1
    assert any("message" in ln for ln in finding_lines(p, msg)), said(p)
    assert any("path-name" in ln for ln in finding_lines(p, named)), said(p)


def test_gg_r3_allow_covers_private_matches_too(tmp_path):
    word = "quuxword"
    w = pw(tmp_path, word + "\n", allow=[word])
    w.commit_file("a.txt", word + "\n")
    assert w.scan().returncode == 0


def test_gg_r3_missing_patterns_file_is_not_an_error_and_says_so(tmp_path):
    w = World(tmp_path)                       # config names a file that does not exist
    w.commit_file("a.txt", "fine\n")
    p = w.scan()
    assert p.returncode == 0, said(p)
    low = said(p).lower()
    assert "private" in low and "not checked" in low, said(p)


def test_gg_r3_missing_patterns_file_still_runs_the_builtin_checks(tmp_path):
    w = World(tmp_path)
    sha = w.commit_file("a.txt", "m " + EMAIL + "\n")
    p = w.scan()
    assert p.returncode == 1, said(p)
    assert finding_lines(p, sha)
    assert "not checked" in said(p).lower()


def test_gg_r3_a_present_patterns_file_prints_no_not_checked_notice(tmp_path):
    w = pw(tmp_path, "somewordnobodyuses\n")
    w.commit_file("a.txt", "fine\n")
    p = w.scan()
    assert p.returncode == 0, said(p)
    assert "not checked" not in said(p).lower()


def test_gg_r3_default_patterns_file_location_is_under_home_config(tmp_path):
    w = World(tmp_path, patterns_file=False)            # key absent from the config
    default = tmp_path / "home" / ".config" / "multiagents" / "sensitive-patterns"
    default.parent.mkdir(parents=True)
    default.write_text("defaultword\n")
    os.chmod(default, 0o600)
    sha = w.commit_file("a.txt", "has defaultword inside\n")
    p = w.scan()
    assert p.returncode == 1, said(p)
    assert any("private" in ln for ln in finding_lines(p, sha))


def test_gg_r3_patterns_file_path_may_use_a_tilde(tmp_path):
    w = World(tmp_path, patterns_file=False)
    mine = tmp_path / "home" / "mine"
    mine.write_text("tildeword\n")
    os.chmod(mine, 0o600)
    w.write_config({"git": {"remote": "origin", "base_branch": "main",
                            "guard": {"patterns_file": "~/mine"}}})
    sha = w.commit_file("a.txt", "tildeword\n")
    p = w.scan()
    assert p.returncode == 1 and finding_lines(p, sha), said(p)


@pytest.mark.parametrize("mode", [0o400, 0o600, 0o700])
def test_gg_r3_owner_only_patterns_files_are_accepted(tmp_path, mode):
    w = pw(tmp_path, "somewordnobodyuses\n", patterns_mode=mode)
    w.commit_file("a.txt", "fine\n")
    assert w.scan().returncode == 0


@pytest.mark.parametrize("mode", [0o640, 0o604, 0o644, 0o660, 0o620, 0o666, 0o602])
def test_gg_r3_patterns_file_readable_by_group_or_others_is_refused(tmp_path, mode):
    w = pw(tmp_path, "secretwordhere\n", patterns_mode=0o600)
    w.commit_file("a.txt", "fine\n")
    assert w.scan().returncode == 0
    os.chmod(w.patterns, mode)
    p = w.scan()
    assert p.returncode == 2, said(p)
    assert "invalid choice" not in said(p)
    assert said(p).strip(), "refusal carries a message"
    assert "secretwordhere" not in said(p)


def test_gg_r3_refused_patterns_file_hides_builtin_findings_behind_exit_2(tmp_path):
    w = pw(tmp_path, "x\n", patterns_mode=0o600)
    w.commit_file("a.txt", "m " + EMAIL + "\n")
    assert w.scan().returncode == 1
    os.chmod(w.patterns, 0o644)
    p = w.scan()
    assert p.returncode == 2, said(p)
    assert "invalid choice" not in said(p)


# ------------------------------------------------------------ coauthor -------
MODELS = ["Claude", "Opus", "Sonnet", "Haiku", "Fable", "GPT", "Codex", "Gemini"]


@pytest.mark.parametrize("model", MODELS)
def test_gg_r3_coauthor_trailer_naming_a_model_is_a_finding_when_disabled(tmp_path, model):
    w = World(tmp_path, coauthor=False)
    sha = w.commit("work\n\nCo-Authored-By: " + model + " 5 <noreply@example.com>", add=False)
    p = w.scan()
    assert p.returncode == 1, said(p)
    lines = finding_lines(p, sha)
    assert len(lines) == 1, said(p)
    assert "coauthor" in lines[0] and "message" in lines[0]
    assert "Co…" in lines[0]


@pytest.mark.parametrize("coauthor", [True, None])
def test_gg_r3_the_same_trailer_is_fine_when_enabled_or_unset(tmp_path, coauthor):
    w = World(tmp_path, coauthor=coauthor)
    w.commit("work\n\nCo-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>", add=False)
    p = w.scan()
    assert p.returncode == 0, said(p)


def test_gg_r3_human_coauthors_are_not_findings_when_disabled(tmp_path):
    w = World(tmp_path, coauthor=False)
    w.commit("work\n\nCo-Authored-By: Jane Doe <jane@example.com>", add=False)
    p = w.scan()
    assert p.returncode == 0, said(p)


def test_gg_r3_naming_a_model_outside_a_trailer_is_not_a_coauthor_finding(tmp_path):
    w = World(tmp_path, coauthor=False)
    w.commit("ask Claude about Opus and GPT later", add=False)
    p = w.scan()
    assert p.returncode == 0, said(p)
