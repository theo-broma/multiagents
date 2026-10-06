"""GG-R3 path edge cases (review ag-c94684): paths come from `-z` output only.

The scan used to read a file's path from the `diff --git a/X b/Y` header by
splitting on the first ` b/`. A name holding ` b/` came out garbled; for a
binary file no `+++` line corrected it, `git show <sha>:<garbled>` failed and
the scan crashed. Every name below holds one of the characters that header
mangles or quotes, and each commit carries a text and a binary file with a
finding: both must be reported under their exact path, and the scan must not
crash.

Exact paths are checked on the API (`Finding.where`). On the command line a
finding is one line, so a newline in a path shows as `\\n` there.

Every secret-shaped value is assembled at run time by `gg_world`, and the
non-ASCII names are built from escapes, so this file stays ASCII-only.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from multiagents import git_guard as gg

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))
import gg_world as gw  # noqa: E402
from gg_world import World, finding_lines, git, said  # noqa: E402

EMAIL = gw.email()
TOKEN = gw.tokens()["ghp"]

NAMES = {
    "space-b-slash": "foo b/c",
    "space-a-slash": "x a/y b/z",
    "tab": "tab\there",
    "newline": "new\nline",
    "double-quote": 'say "hi"',
    "backslash": "back\\slash",
    "non-ascii": "Café 日本",
    "all-at-once": 'd b/" a/\t\\\né',
}


@pytest.fixture
def w(tmp_path):
    return World(tmp_path)


def where(w: World, sha: str) -> list[tuple[str, str]]:
    settings = gg.GuardSettings(patterns_file=str(w.patterns))
    result = gg.scan_commits(w.root, [sha], settings)
    return sorted((f.category, f.where) for f in result.findings)


def shown(path: str) -> str:
    return path.replace("\n", "\\n")


def assert_no_leak(p, *unmasked: str):
    text = said(p)
    for secret in unmasked:
        assert secret not in text, "unmasked match in output"
        assert secret[2:] not in text, "unmasked tail of the match in output"


@pytest.mark.parametrize("key", sorted(NAMES))
def test_gg_edge_new_text_and_binary_files_report_exact_paths(w, key):
    stem = NAMES[key]
    text_name, bin_name = stem + ".txt", stem + ".bin"
    w.write(text_name, "filler\ncontact " + EMAIL + " today\n")
    w.write(bin_name, b"\x00\x01\xff" + EMAIL.encode() + b"\x00")
    sha = w.commit("add")
    assert where(w, sha) == sorted([("email", text_name + ":2"),
                                    ("email", bin_name + ":bin")])
    p = w.scan()
    assert p.returncode == 1, said(p)
    lines = finding_lines(p, sha)
    assert len(lines) == 2, said(p)
    assert any(shown(text_name) + ":2 " in ln for ln in lines), said(p)
    assert any(shown(bin_name) + ":bin " in ln for ln in lines), said(p)
    assert_no_leak(p, EMAIL)


@pytest.mark.parametrize("key", sorted(NAMES))
def test_gg_edge_modified_text_and_binary_files_report_exact_paths(w, key):
    stem = NAMES[key]
    text_name, bin_name = stem + ".txt", stem + ".bin"
    w.write(text_name, "one\ntwo\n")
    w.write(bin_name, b"\x00\x01")
    w.commit("seed files")
    w.write(text_name, "one\ncontact " + EMAIL + "\ntwo\n")
    w.write(bin_name, b"\x00\x01" + EMAIL.encode() + b"\x00")
    sha = w.commit("modify")
    assert where(w, sha) == sorted([("email", text_name + ":2"),
                                    ("email", bin_name + ":bin")])


def test_gg_edge_binary_name_cannot_hide_behind_a_text_name(w):
    # The header of this binary reads `a/safe.txt b/evil.bin b/safe.txt
    # b/evil.bin`; splitting on ` b/` named the wrong file.
    name = "safe.txt b/evil.bin"
    w.write(name, b"\x00" + EMAIL.encode() + b"\x00")
    w.write("safe.txt", "nothing here\n")
    sha = w.commit("add")
    assert where(w, sha) == [("email", name + ":bin")]


@pytest.mark.parametrize("key", sorted(NAMES))
def test_gg_edge_path_names_with_odd_characters_are_scanned(w, key):
    name = NAMES[key] + " " + TOKEN + ".txt"
    sha = w.commit_file(name, "plain\n")
    assert where(w, sha) == [("token", "path-name")]
    p = w.scan()
    assert p.returncode == 1, said(p)
    assert_no_leak(p, TOKEN)


def test_gg_edge_renamed_file_with_odd_name_reports_new_path(w):
    old = NAMES["space-b-slash"] + ".txt"
    w.commit_file(old, "".join(f"line {i}\n" for i in range(20)))
    new = NAMES["all-at-once"] + " " + TOKEN + ".txt"
    (w.root / old).unlink()
    w.write(new, "".join(f"line {i}\n" for i in range(20))
            + "contact " + EMAIL + "\n")
    sha = w.commit("rename")
    status = git(w.root, "diff-tree", "--no-commit-id", "-r", "-M",
                 "--name-status", "-z", sha, env=w.env).stdout
    assert status.startswith(b"R"), "the commit must be a rename"
    assert where(w, sha) == sorted([("email", new + ":21"),
                                    ("token", "path-name")])


def test_gg_edge_deleted_binary_with_odd_name_does_not_crash(w):
    name = NAMES["all-at-once"] + ".bin"
    w.commit_file(name, b"\x00\x01\x02")
    git(w.root, "rm", "-q", name, env=w.env)
    sha = w.commit("remove")
    assert where(w, sha) == []
    assert w.scan().returncode == 0


def test_gg_edge_control_characters_never_reach_the_output_raw():
    finding = gg.Finding("email", "0" * 40, "a\nb\x1b[2Jc\td:1", EMAIL)
    line = finding.line()
    assert "\n" not in line and "\x1b" not in line
    assert "a\\nb\\x1b[2Jc\td:1" in line


# --------------------------------------------------------------------------
# the -z stream is parsed fail-closed (review ag-4bdd60)
# --------------------------------------------------------------------------
# `_changes` pairs the `--raw` records with the `--numstat` records of one
# diff. A record left over on either side, or one cut short, must fail the
# scan: a partial list is a file the scan never looks at.

_BLOB_A = b"1" * 40
_BLOB_B = b"2" * 40


def _raw(status: bytes, *paths: bytes) -> bytes:
    meta = b":100644 100644 " + _BLOB_A + b" " + _BLOB_B + b" " + status
    return b"\0".join([meta, *paths]) + b"\0"


_GOOD = (_raw(b"M", b"a b/c") + _raw(b"R090", b"old", b"new\tname")
         + b"1\t0\ta b/c\0" + b"-\t-\t\0old\0new\tname\0")


def changes_of(monkeypatch, stream: bytes) -> list:
    monkeypatch.setattr(gg, "_git_bytes", lambda repo, *args: stream)
    return gg._changes(Path("."), "0" * 40, "f" * 40)


def test_gg_edge_well_formed_stream_pairs_every_record(monkeypatch):
    got = changes_of(monkeypatch, _GOOD)
    assert [(c.status, c.path, c.binary) for c in got] == [
        ("M", "a b/c", False), ("R", "new\tname", True)]


def test_gg_edge_empty_diff_has_no_changes(monkeypatch):
    assert changes_of(monkeypatch, b"") == []


@pytest.mark.parametrize("stream", [
    pytest.param(_GOOD + b"3\t1\textra\0", id="extra-numstat"),
    pytest.param(_GOOD + b"3\t1\textra", id="extra-numstat-unterminated"),
    pytest.param(_GOOD + b"\0", id="extra-empty-token"),
    pytest.param(_raw(b"M", b"a b/c") + _raw(b"R090", b"old", b"new\tname")
                 + b"1\t0\ta b/c\0", id="missing-numstat"),
    pytest.param(_raw(b"M", b"a b/c") + _raw(b"M", b"other"),
                 id="no-numstat-at-all"),
    pytest.param(b"1\t0\ta b/c\0", id="numstat-without-raw"),
    pytest.param(_GOOD[:-1], id="truncated-final-nul"),
    pytest.param(_GOOD[:-6], id="truncated-rename-new-path"),
    pytest.param(_GOOD[:-10], id="truncated-rename-old-path"),
    pytest.param(_raw(b"M", b"a b/c")[:-1], id="truncated-raw-path"),
    pytest.param(_raw(b"R090", b"old"), id="rename-missing-new-path"),
    pytest.param(_raw(b"M", b"a") + b"1\t0\0", id="numstat-without-path"),
    pytest.param(_raw(b"M", b"a") + b"1\t0\tb\0", id="numstat-other-path"),
    pytest.param(_raw(b"M", b"a") + b"x\t0\ta\0", id="numstat-bad-count"),
    pytest.param(_raw(b"M", b"a") + b"-\t0\ta\0", id="numstat-half-binary"),
    pytest.param(_raw(b"M", b"a") + b"1\t0\t\0x\0a\0",
                 id="numstat-rename-for-raw-modify"),
    pytest.param(_raw(b"R090", b"old", b"new") + b"1\t0\tnew\0",
                 id="numstat-modify-for-raw-rename"),
    pytest.param(_raw(b"R090", b"old", b"new") + b"1\t0\t\0gone\0new\0",
                 id="numstat-rename-other-old-path"),
    pytest.param(_raw(b"R", b"old", b"new") + b"1\t0\t\0old\0new\0",
                 id="rename-without-score"),
    pytest.param(_raw(b"M", b"") + b"1\t0\t\0", id="empty-path"),
    pytest.param(b":100644 100644 " + _BLOB_A + b" M\0a\0" + b"1\t0\ta\0",
                 id="raw-missing-field"),
    pytest.param(_raw(b"M", b"a").replace(b"100644 1", b"10064 1", 1)
                 + b"1\t0\ta\0", id="raw-bad-mode"),
])
def test_gg_edge_malformed_diff_stream_raises(monkeypatch, stream):
    with pytest.raises(gg.GitError):
        changes_of(monkeypatch, stream)
