"""Quoted paths (review ag-1b916e): git C-quotes paths with spaces/non-ASCII.

`_split_git_path` decoded the octal escapes with `unicode_escape` (mojibake)
but kept the surrounding double quotes, so the `b/` prefix survived, the path
never matched the unquoted `-z` path in `diff.added`, and a binary file with
such a name was not scanned. Text findings in such files were reported with
garbled paths.

Every secret-shaped value is assembled at run time, so none sits in this file
as a literal. The non-ASCII name is built from an escape for the same reason
the suite avoids literals: the file stays ASCII-only.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from multiagents import git_guard as gg

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))
import gg_world as gw  # noqa: E402
from gg_world import World, finding_lines  # noqa: E402
from gg_world import said  # noqa: E402

EMAIL = gw.email()

# "Caf\u00e9" without a non-ASCII literal in this file.
CAFE = "Caf" + "\u00e9"


@pytest.fixture
def w(tmp_path):
    return World(tmp_path)


def assert_no_leak(w: World, p, *unmasked: str):
    text = said(p)
    for secret in unmasked:
        assert secret not in text, "unmasked match in output"
        assert secret[2:] not in text, "unmasked tail of the match in output"
    for f in w.all_output_files():
        data = f.read_bytes()
        for secret in unmasked:
            assert secret.encode() not in data, f"unmasked match logged in {f}"


def test_gg_quoted_binary_file_with_space_and_non_ascii_is_scanned(w):
    name = "b/" + CAFE + " binary.bin"
    payload = b"\x00\x01\x02\xff" + EMAIL.encode() + b"\x00\xfe\x00"
    sha = w.commit_file(name, payload)
    p = w.scan()
    assert p.returncode == 1, said(p)
    lines = finding_lines(p, sha)
    assert len(lines) == 1, said(p)
    assert "email" in lines[0], said(p)
    assert name + ":bin" in lines[0], said(p)
    assert_no_leak(w, p, EMAIL)


def test_gg_quoted_text_file_with_space_and_non_ascii_reports_readable_path(w):
    name = "b/" + CAFE + " notes.txt"
    body = "filler 0\nfiller 1\ncontact " + EMAIL + " today\n"
    sha = w.commit_file(name, body)
    p = w.scan()
    assert p.returncode == 1, said(p)
    lines = finding_lines(p, sha)
    assert len(lines) == 1, said(p)
    assert "email" in lines[0], said(p)
    assert name + ":3" in lines[0], said(p)
    assert_no_leak(w, p, EMAIL)


def test_gg_quoted_text_and_binary_names_are_not_garbled(w):
    text_name = "b/" + CAFE + " notes.txt"
    bin_name = "b/" + CAFE + " binary.bin"
    w.commit_file(text_name, "contact " + EMAIL + " today\n")
    payload = b"\x00" + EMAIL.encode() + b"\x00"
    sha = w.commit_file(bin_name, payload)
    p = w.scan()
    assert p.returncode == 1, said(p)
    lines = finding_lines(p, sha)
    assert lines, said(p)
    for ln in lines:
        assert '"b/' not in ln, said(p)
        assert "\\303" not in ln and "\\251" not in ln, said(p)
        assert bin_name in ln, said(p)
    assert_no_leak(w, p, EMAIL)


# ------------------------------------------------- the decoder, unit level ---
def test_gg_split_git_path_decodes_octal_escapes_as_utf8_bytes():
    # Exactly what git prints for the b-side of `Caf\u00e9 binary.bin`.
    assert (gg._split_git_path('"b/Caf\\303\\251 binary.bin"')
            == "Caf\u00e9 binary.bin")
    # The reviewer's case: a quoted b-side under a sub-directory.
    assert (gg._split_git_path('"b/b/Caf\\303\\251 binary.bin"')
            == "b/Caf\u00e9 binary.bin")


def test_gg_split_git_path_strips_prefixes_without_quotes():
    assert gg._split_git_path("b/plain.txt") == "plain.txt"
    assert gg._split_git_path("a/plain.txt") == "plain.txt"
    assert gg._split_git_path('"a/with space.txt"') == "with space.txt"
    assert gg._split_git_path("/dev/null") == "/dev/null"
    assert gg._split_git_path("plain.txt") == "plain.txt"
