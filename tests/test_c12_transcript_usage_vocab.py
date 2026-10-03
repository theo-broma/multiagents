"""Phase 6 closing round, C12 — a transcript's usage vocabulary comes from the
provider's config, not from `transcripts.py`.

Contract: `context/specs/phase6-closing-fixes.md` § C12 and its revision
("Revision of C11/C12 after the advisor's check"), ids C12-R1, R1a, R1b, R1c,
R2, R3.

Black box: everything is read through `session_context(provider, cwd,
session_id)`, given a provider built from config (`Provider.from_dict`) and a
transcript file on disk. Nothing private of `transcripts.py` is touched.

The contract leaves the config key names to the implementer. They are the TWO
constants below and nowhere else in this file; align them when the names are
chosen. They are assumed to live inside the provider's `transcript:` block
(next to `dir` / `glob`) — if the implementer puts them elsewhere, change
`_provider` only.
"""

from __future__ import annotations

import dataclasses
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import p0_context_harness as ch  # noqa: E402

from multiagents import config as config_mod  # noqa: E402
from multiagents.paths import ProjectPaths  # noqa: E402
from multiagents.providers import Provider  # noqa: E402
from multiagents.transcripts import session_context  # noqa: E402

# ===== CONFIG KEY NAMES — the implementer's choice; align here (see docstring) ==
USAGE_PATH_KEY = "usage_path"        # dot-separated path to the usage object
USAGE_FIELDS_KEY = "context_fields"  # token fields summed for the context size
# ================================================================================

SID = "c12c12c1-0000-4000-8000-000000000001"
CLAUDE_PATH = "message.usage"
CLAUDE_FIELDS = ["input_tokens", "cache_read_input_tokens",
                 "cache_creation_input_tokens"]


def _provider(root: Path, path: str | None = None, fields: list[str] | None = None,
              name: str = "c12p") -> Provider:
    block = ch.transcript_block(root)
    if path is not None:
        block[USAGE_PATH_KEY] = path
    if fields is not None:
        block[USAGE_FIELDS_KEY] = fields
    return Provider.from_dict(name, {"bin": name, "transcript": block})


@pytest.fixture
def env(tmp_path):
    root = tmp_path / "transcripts"
    cwd = (tmp_path / "proj").resolve()
    cwd.mkdir()
    return root, cwd, root / ch.slug(cwd) / f"{SID}.jsonl"


def _shipped(paths: ProjectPaths, name: str) -> Provider:
    raw = config_mod.load(paths).providers[name]
    return raw if isinstance(raw, Provider) else Provider.from_dict(name, raw)


def _lines(*records) -> str:
    return "".join(json.dumps(r) + "\n" for r in records)


def _write(path: Path, *records) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_lines(*records))


def _append(path: Path, text: str) -> None:
    with path.open("a") as f:
        f.write(text)


# A provider that is not Claude: usage under payload.token_usage, its own names.
ALT_PATH = "payload.token_usage"
ALT_FIELDS = ["prompt_tokens", "cached_tokens"]


def alt(prompt: int, cached: int, completion: int = 7) -> dict:
    return {"type": "event", "payload": {"token_usage": {
        "prompt_tokens": prompt, "cached_tokens": cached,
        "completion_tokens": completion}}}


# ------------------------------------------------------------------ R1 --

def test_c12_r1_reads_usage_at_the_declared_path(env):
    root, cwd, path = env
    _write(path, alt(100, 20), alt(1_000, 234))
    prov = _provider(root, ALT_PATH, ALT_FIELDS)
    assert session_context(prov, cwd, SID) == 1_234


def test_c12_r1_claude_shipped_config_reads_the_same_number_as_today(tmp_path):
    """Claude's shipped declaration is today's `message.usage` and the three
    input fields: the number is input + cache read + cache creation of the last
    request, output tokens excluded."""
    paths = ProjectPaths(tmp_path / "p")
    paths.ensure()
    claude = _shipped(paths, "claude")
    root = tmp_path / "tx"
    cwd = (tmp_path / "proj").resolve()
    cwd.mkdir()
    prov = dataclasses.replace(
        claude, transcript={**claude.transcript, "dir": str(root) + "/{slug}"})
    rec = ch.request(0)
    rec["message"]["usage"] = {"input_tokens": 3, "cache_read_input_tokens": 70_000,
                               "cache_creation_input_tokens": 4_000,
                               "output_tokens": 999}
    ch.write_transcript(root / ch.slug(cwd) / f"{SID}.jsonl",
                        [ch.user("hi"), ch.request(10_000), rec, ch.user("more")])
    assert session_context(prov, cwd, SID) == 74_003


def test_c12_r1_claude_shipped_config_does_not_read_the_alternative_shape(tmp_path):
    paths = ProjectPaths(tmp_path / "p")
    paths.ensure()
    claude = _shipped(paths, "claude")
    root = tmp_path / "tx"
    cwd = (tmp_path / "proj").resolve()
    cwd.mkdir()
    prov = dataclasses.replace(
        claude, transcript={**claude.transcript, "dir": str(root) + "/{slug}"})
    ch.write_transcript(root / ch.slug(cwd) / f"{SID}.jsonl", [alt(5_000, 500)])
    assert session_context(prov, cwd, SID) is None


def test_c12_r1_a_record_in_the_old_vocabulary_is_not_read_under_a_new_path(env):
    """Declaring `payload.token_usage` means `message.usage` is not consulted."""
    root, cwd, path = env
    _write(path, ch.request(90_000))
    prov = _provider(root, ALT_PATH, ALT_FIELDS)
    assert session_context(prov, cwd, SID) is None


def test_c12_r1_the_last_record_with_usage_wins(env):
    root, cwd, path = env
    _write(path, alt(1, 1), alt(900, 100), {"type": "event", "payload": {"x": 1}})
    prov = _provider(root, ALT_PATH, ALT_FIELDS)
    assert session_context(prov, cwd, SID) == 1_000


def test_c12_r1_an_unreadable_value_at_the_path_is_no_reading_not_a_crash(env):
    root, cwd, path = env
    # usage is a string, then a list, then a figure that is not a number
    _write(path, {"payload": {"token_usage": "lots"}},
           {"payload": {"token_usage": [1, 2]}},
           {"payload": {"token_usage": {"prompt_tokens": "many", "cached_tokens": 1}}})
    prov = _provider(root, ALT_PATH, ALT_FIELDS)
    assert session_context(prov, cwd, SID) is None


def test_c12_r1_an_intermediate_that_is_not_an_object_is_no_reading(env):
    root, cwd, path = env
    _write(path, {"payload": 7}, {"payload": ["token_usage"]})
    prov = _provider(root, ALT_PATH, ALT_FIELDS)
    assert session_context(prov, cwd, SID) is None


# ----------------------------------------------------------------- R1a --

def test_c12_r1a_a_declared_path_without_the_word_usage_is_still_read(env):
    """The byte prefilter on `"usage"` must go or follow the declaration."""
    root, cwd, path = env
    _write(path, {"meta": {"tally": {"in": 40, "cached": 2, "out": 9}}},
           {"meta": {"tally": {"in": 600, "cached": 50, "out": 9}}})
    prov = _provider(root, "meta.tally", ["in", "cached"])
    assert session_context(prov, cwd, SID) == 650


def test_c12_r1a_a_single_segment_path_works(env):
    root, cwd, path = env
    _write(path, {"tokens": {"a": 5, "b": 6}})
    prov = _provider(root, "tokens", ["a", "b"])
    assert session_context(prov, cwd, SID) == 11


def test_c12_r1a_only_the_declared_fields_are_summed(env):
    """Not Claude's three fields, and not every number in the object."""
    root, cwd, path = env
    _write(path, {"payload": {"token_usage": {
        "prompt_tokens": 300, "cached_tokens": 40, "completion_tokens": 5_000,
        "input_tokens": 70_000, "cache_read_input_tokens": 80_000}}})
    prov = _provider(root, ALT_PATH, ALT_FIELDS)
    assert session_context(prov, cwd, SID) == 340


def test_c12_r1a_a_declared_field_missing_from_the_record_counts_as_zero(env):
    root, cwd, path = env
    _write(path, {"payload": {"token_usage": {"prompt_tokens": 250}}})
    prov = _provider(root, ALT_PATH, ALT_FIELDS)
    assert session_context(prov, cwd, SID) == 250


def test_c12_r1a_the_field_list_is_honoured_for_a_claude_shaped_path(env):
    """Same path as Claude, a narrower field list: the list decides, not a
    built-in set."""
    root, cwd, path = env
    ch.write_transcript(path, [ch.request(0)])
    rec = ch.request(0)
    rec["message"]["usage"] = {"input_tokens": 3, "cache_read_input_tokens": 70_000,
                               "cache_creation_input_tokens": 4_000}
    ch.write_transcript(path, [rec])
    prov = _provider(root, CLAUDE_PATH, ["input_tokens", "cache_creation_input_tokens"])
    assert session_context(prov, cwd, SID) == 4_003


def test_c12_r1a_a_long_line_at_the_declared_path_is_read(env):
    """The reading is not limited to short records (tool results are huge)."""
    root, cwd, path = env
    big = {"blob": "x" * 300_000, "payload": {"token_usage": {
        "prompt_tokens": 12, "cached_tokens": 30}}}
    _write(path, alt(1, 1), big)
    prov = _provider(root, ALT_PATH, ALT_FIELDS)
    assert session_context(prov, cwd, SID) == 42


# ------------------------------------------------------------------ R2 --

def test_c12_r2_no_declaration_gives_no_reading_for_a_claude_shaped_file(env):
    root, cwd, path = env
    ch.write_transcript(path, [ch.user("hi"), ch.request(50_000)])
    assert session_context(_provider(root), cwd, SID) is None


def test_c12_r2_no_declaration_gives_no_reading_for_any_shape(env):
    root, cwd, path = env
    _write(path, alt(100, 200))
    assert session_context(_provider(root), cwd, SID) is None


def test_c12_r2_a_path_without_a_field_list_is_not_a_reading_from_a_borrowed_list(env):
    """Half a declaration is no declaration for the missing half: the Claude
    field names are not silently assumed."""
    root, cwd, path = env
    ch.write_transcript(path, [ch.request(50_000)])
    assert session_context(_provider(root, CLAUDE_PATH), cwd, SID) is None


def test_c12_r2_a_field_list_without_a_path_gives_no_reading(env):
    root, cwd, path = env
    ch.write_transcript(path, [ch.request(50_000)])
    assert session_context(_provider(root, None, CLAUDE_FIELDS), cwd, SID) is None


# ----------------------------------------------------------------- R1b --

def _both(claude_ctx: int, alt_prompt: int, alt_cached: int) -> dict:
    rec = ch.request(claude_ctx)
    rec["payload"] = {"token_usage": {"prompt_tokens": alt_prompt,
                                      "cached_tokens": alt_cached}}
    return rec


def test_c12_r1b_one_unchanged_file_gives_each_declaration_its_own_answer(env):
    root, cwd, path = env
    ch.write_transcript(path, [_both(80_000, 10, 5)])
    a = _provider(root, CLAUDE_PATH, CLAUDE_FIELDS)
    b = _provider(root, ALT_PATH, ALT_FIELDS)
    none = _provider(root)
    assert session_context(a, cwd, SID) == 80_000
    assert session_context(b, cwd, SID) == 15          # not A's cached 80_000
    assert session_context(none, cwd, SID) is None     # not A's or B's
    assert session_context(a, cwd, SID) == 80_000      # and back again
    assert session_context(none, cwd, SID) is None


def test_c12_r1b_none_first_then_a_declaration_on_the_same_file(env):
    root, cwd, path = env
    ch.write_transcript(path, [_both(80_000, 10, 5)])
    assert session_context(_provider(root), cwd, SID) is None
    assert session_context(_provider(root, ALT_PATH, ALT_FIELDS), cwd, SID) == 15


def test_c12_r1b_two_field_lists_on_one_path_do_not_share_an_answer(env):
    root, cwd, path = env
    _write(path, {"payload": {"token_usage": {"prompt_tokens": 100, "cached_tokens": 20}}})
    p1 = _provider(root, ALT_PATH, ["prompt_tokens"])
    p2 = _provider(root, ALT_PATH, ["prompt_tokens", "cached_tokens"])
    assert session_context(p1, cwd, SID) == 100
    assert session_context(p2, cwd, SID) == 120
    assert session_context(p1, cwd, SID) == 100


def test_c12_r1b_records_appended_after_a_first_reading_are_seen(env):
    root, cwd, path = env
    _write(path, alt(100, 0))
    prov = _provider(root, ALT_PATH, ALT_FIELDS)
    assert session_context(prov, cwd, SID) == 100
    _append(path, _lines(alt(700, 70)))
    assert session_context(prov, cwd, SID) == 770
    _append(path, _lines({"type": "event"}))           # no usage: reading stands
    assert session_context(prov, cwd, SID) == 770


def test_c12_r1b_an_append_read_under_another_declaration_is_not_a_tail_only_read(env):
    """Read under A, grow the file with a record only A can read, then read
    under B: B's answer is the earlier record's, found by reading B's way from
    the start — not None because B's cache was borrowed from A's offset, and not
    A's number."""
    root, cwd, path = env
    ch.write_transcript(path, [_both(80_000, 10, 5)])
    a = _provider(root, CLAUDE_PATH, CLAUDE_FIELDS)
    b = _provider(root, ALT_PATH, ALT_FIELDS)
    assert session_context(a, cwd, SID) == 80_000
    _append(path, _lines(ch.request(90_000)))           # Claude-shaped only
    assert session_context(a, cwd, SID) == 90_000
    assert session_context(b, cwd, SID) == 15


def test_c12_r1b_a_trailing_incomplete_line_is_ignored_until_it_is_finished(env):
    root, cwd, path = env
    _write(path, alt(100, 0))
    prov = _provider(root, ALT_PATH, ALT_FIELDS)
    assert session_context(prov, cwd, SID) == 100
    whole = json.dumps(alt(5_000, 500))
    _append(path, whole[: len(whole) // 2])             # mid-write, no newline
    assert session_context(prov, cwd, SID) == 100
    _append(path, whole[len(whole) // 2:])              # complete JSON, still no newline
    assert session_context(prov, cwd, SID) == 100
    _append(path, "\n")
    assert session_context(prov, cwd, SID) == 5_500


def test_c12_r1b_a_trailing_incomplete_line_under_a_second_declaration(env):
    root, cwd, path = env
    ch.write_transcript(path, [_both(80_000, 10, 5)])
    _append(path, json.dumps(ch.request(99_000))[:40])
    a = _provider(root, CLAUDE_PATH, CLAUDE_FIELDS)
    b = _provider(root, ALT_PATH, ALT_FIELDS)
    assert session_context(a, cwd, SID) == 80_000
    assert session_context(b, cwd, SID) == 15
    assert session_context(_provider(root), cwd, SID) is None


# ----------------------------------------------------------------- R1c --

def test_c12_r1c_shipped_agy_opencode_codex_still_declare_no_transcript(tmp_path):
    paths = ProjectPaths(tmp_path / "p")
    paths.ensure()
    cwd = (tmp_path / "proj").resolve()
    cwd.mkdir()
    for name in ("agy", "opencode", "codex"):
        prov = _shipped(paths, name)
        assert not prov.transcript, name
        assert session_context(prov, cwd, SID) is None, name
