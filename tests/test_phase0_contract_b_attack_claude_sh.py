"""Attack on P0-R8d.2 — the `compact)` arm of the shipped `claude.sh`.

Contract: `context/specs/phase0-context-and-team.md` § P0-R8d.2: succeed
**only** if a manual compaction record was appended by this call; a record that
existed before the call does not count.

The script is run as in `test_phase0_provider_compact.py` (its `Scratch` is
reused), with a fake CLI of our own: it finds the transcript by Claude Code's
real folder rule, and appends — or rewrites — whatever record the test hands it.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import p0_context_harness as ch  # noqa: E402
from test_phase0_contract_b_attack_reading import claude_slug  # noqa: E402
from test_phase0_provider_compact import SID, Scratch  # noqa: E402

FAKE = r'''#!{python}
import json, os, pathlib, re, sys
argv = sys.argv[1:]
sid = argv[argv.index("--resume") + 1] if "--resume" in argv else ""
slug = re.sub(r"[^a-zA-Z0-9]", "-", os.getcwd())
path = pathlib.Path(os.environ["HOME"]) / ".claude" / "projects" / slug / f"{sid}.jsonl"
record = os.environ.get("FAKE_RECORD")
if record and path.is_file():
    if os.environ.get("FAKE_HOW") == "rewrite":
        # A CLI that writes the compacted session back as a new, shorter file.
        path.write_text(json.dumps({"type": "user", "message": {"content": "summary"}})
                        + "\n" + record + "\n")
    else:
        with path.open("a") as fh:
            fh.write(record + "\n")
print(json.dumps({"type": "result", "subtype": "success", "session_id": sid}))
'''


def manual(pre=27729, post=1607) -> str:
    rec = ch.compaction(27729, 1607, trigger="manual")
    rec["compactMetadata"]["preTokens"] = pre
    rec["compactMetadata"]["postTokens"] = post
    return json.dumps(rec)


@pytest.fixture
def scratch(tmp_path):
    s = Scratch(tmp_path)
    s.fake.write_text(FAKE.replace("{python}", sys.executable))
    s.transcript = (s.home / ".claude" / "projects" / claude_slug(s.cwd)
                    / f"{SID}.jsonl")
    return s


def test_attack_r8d_2_an_old_record_on_an_unterminated_last_line_is_not_success(scratch):
    """`before=$(wc -l < transcript)` counts newlines, not lines. When the
    last line has no trailing newline (a writer killed mid-flush, or any
    writer that terminates lines on the next write), `lines[before:]` starts
    at that pre-existing last line. If it is an earlier manual compaction
    record, a CLI that did nothing is reported as a verified compaction —
    exactly R8d.2's case (c), shifted by one newline."""
    scratch.session()
    with scratch.transcript.open("a") as fh:
        fh.write(manual(90_000, 3_000))           # no "\n": the old record ends the file
    got = scratch.run("claude", "compact")        # FAKE_RECORD unset: appends nothing
    assert got.returncode == 1, (
        f"exit {got.returncode}, stdout {got.stdout!r}: a pre-existing record "
        f"was taken as this call's compaction")


def test_attack_r8d_2_a_non_utf8_byte_anywhere_in_the_session_is_not_a_failure(scratch):
    """The verifier reads the WHOLE transcript with `open(path).readlines()`
    in text mode, so one byte that is not UTF-8 in any line — even one written
    long before this call — raises, prints nothing, and a compaction that did
    happen is reported as "no compaction was recorded". The driver then
    re-compacts on every qualifying turn, paying for a summary each time."""
    scratch.session()
    with scratch.transcript.open("ab") as fh:
        fh.write(b'{"type":"user","message":{"content":"caf\xe9"}}\n')
    got = scratch.run("claude", "compact", FAKE_RECORD=manual())
    assert got.returncode == 0, got.stderr[-400:]
    assert got.stdout.splitlines()[0] == "27729 -> 1607 tokens"


def test_attack_r8d_2_a_project_path_with_a_space_finds_its_transcript(scratch, tmp_path):
    """`slug=$(pwd | sed 's|[/._]|-|g')` — Claude Code replaces every
    non-alphanumeric character. From `/…/my project` the transcript is under
    `-…-my-project`; the script looks in `-…-my project`, exits 1 "no
    transcript", and the session is never compacted."""
    scratch.cwd = (tmp_path / "my project").resolve()
    scratch.cwd.mkdir()
    scratch.transcript = (scratch.home / ".claude" / "projects"
                          / claude_slug(scratch.cwd) / f"{SID}.jsonl")
    scratch.session()
    got = scratch.run("claude", "compact", FAKE_RECORD=manual())
    assert got.returncode == 0, got.stderr[-400:]


@pytest.mark.xfail(strict=True, reason=(
    "Finding judged acceptable: figures are passed through unchecked, so a "
    "record with preTokens 'n/a' yields 'n/a -> 1607 tokens' and exit 0. The "
    "compaction record itself is genuine; the line is informational."))
def test_attack_r8d_2_non_numeric_figures_are_not_printed_as_figures(scratch):
    scratch.session()
    got = scratch.run("claude", "compact", FAKE_RECORD=manual(pre="n/a"))
    assert got.returncode != 0 or got.stdout.split()[0].isdigit()


@pytest.mark.xfail(strict=True, reason=(
    "Finding judged acceptable: a CLI that rewrites the session shorter "
    "instead of appending is reported as failed although it compacted. It "
    "errs on the safe side (the driver continues), and Claude Code appends."))
def test_attack_r8d_2_a_cli_that_rewrites_the_file_shorter_is_still_success(scratch):
    scratch.session([ch.user("a"), ch.request(9_000), ch.user("b"), ch.request(9_500)])
    got = scratch.run("claude", "compact", FAKE_RECORD=manual(), FAKE_HOW="rewrite")
    assert got.returncode == 0
