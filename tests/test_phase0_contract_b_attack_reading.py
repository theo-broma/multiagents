"""Attack on P0-R8a.2 / R8a.7 — `transcripts.session_context` and its cache.

Contract: `context/specs/phase0-context-and-team.md` § P0-R8a.2, R8a.7.
Each test here is red for a defect found by attacking the implementation on
`refactor/split-consume`; an `xfail(strict=True)` documents a finding judged
acceptable. Black box: only `session_context(provider, cwd, session_id)` and
files on disk.
"""

from __future__ import annotations

import json
import re
import sys
import time
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import p0_context_harness as ch  # noqa: E402

from multiagents.transcripts import session_context  # noqa: E402

SID = "a77ac000-0000-4000-8000-00000000beef"


def claude_slug(cwd: Path) -> str:
    """The folder name Claude Code itself uses for a project directory.

    Read out of the installed CLI (2026-09-23): `e.replace(/[^a-zA-Z0-9]/g,"-")`
    — every non-alphanumeric character becomes `-`, not only `/`, `.`, `_`.
    """
    return re.sub(r"[^a-zA-Z0-9]", "-", str(cwd))


@pytest.fixture
def tx(tmp_path):
    root = tmp_path / "tx"
    provider = types.SimpleNamespace(transcript=ch.transcript_block(root))
    cwd = (tmp_path / "proj").resolve()
    path = root / ch.slug(cwd) / f"{SID}.jsonl"
    return types.SimpleNamespace(provider=provider, cwd=cwd, path=path,
                                 read=lambda: session_context(provider, cwd, SID))


def _usage_line(usage_json: str) -> str:
    return ('{"type":"assistant","message":{"role":"assistant","usage":'
            + usage_json + '}}\n')


# ------------------------------------------------ wrong-typed usage fields --

def test_attack_r8a_2_a_non_finite_usage_figure_does_not_raise(tx):
    """`Infinity` is valid to Python's json. `int(inf)` raises OverflowError,
    which the reader does not catch — and since a raising read is never
    cached, every later tool call re-reads and raises again: the launched
    role's MCP server fails every tool call until the transcript grows past a
    newer request. The reading must be None (or the earlier figure), never an
    exception."""
    tx.path.parent.mkdir(parents=True)
    tx.path.write_text(json.dumps(ch.request(5_000)) + "\n"
                       + _usage_line('{"input_tokens": Infinity}'))
    try:
        got = tx.read()
    except Exception as exc:  # noqa: BLE001 — the defect is that it raises at all
        pytest.fail(f"session_context raised {type(exc).__name__}: {exc}")
    assert got in (None, 5_000)


def test_attack_r8a_2_string_usage_figures_are_not_concatenated(tx):
    """Three string figures are summed with `+`, i.e. concatenated:
    "5" + "1000" + "00" -> "5100000" -> 5,100,000 tokens. A garbage reading
    over every threshold fires the wind-down and the compaction. The right
    answer is 1,005 (coerced) or None (rejected) — never five million."""
    tx.path.parent.mkdir(parents=True)
    tx.path.write_text(_usage_line(
        '{"input_tokens": "5", "cache_read_input_tokens": "1000",'
        ' "cache_creation_input_tokens": "00"}'))
    assert tx.read() in (None, 1_005)


@pytest.mark.xfail(strict=True, reason=(
    "Finding judged acceptable: negative figures pass through as a negative "
    "reading (budget_status shows known:true, tokens:-7). Harmless — it "
    "is below every threshold, so nothing fires — and no CLI writes it."))
def test_attack_r8a_2_negative_usage_is_not_a_reading(tx):
    tx.path.parent.mkdir(parents=True)
    tx.path.write_text(_usage_line('{"input_tokens": -7}'))
    got = tx.read()
    assert got is None or got >= 0


# --------------------------------------------------------------- huge lines --

def test_attack_r8a_7_a_huge_last_line_is_read_in_linear_time(tx):
    """The backwards scan re-joins the carried fragment onto every 64 KiB
    block it reads, so one long line costs O(n^2 / 64 KiB): 4 MiB takes
    ~0.1 s, 16 MiB ~0.9 s, 32 MiB several seconds — spent inside a tool call.
    A tool result of that size (a big file read, a base64 image) is one line
    in the transcript. Reading 32 MiB once must not take seconds."""
    tx.path.parent.mkdir(parents=True)
    with tx.path.open("w") as fh:
        fh.write(json.dumps(ch.request(5_000)) + "\n")
        fh.write(json.dumps(ch.tool_result() | {"blob": "a" * (32 << 20)}) + "\n")
    start = time.perf_counter()
    got = tx.read()
    took = time.perf_counter() - start
    assert got == 5_000
    assert took < 1.5, f"one read of a 32 MiB line took {took:.1f}s"


# ------------------------------------------------------- where the file is --

def test_attack_r8a_2_a_project_path_with_a_space_is_found(tmp_path, monkeypatch):
    """The shipped claude `transcript.dir` is `~/.claude/projects/{slug}`, and
    the slug is built by replacing only `/`, `.` and `_`. Claude Code replaces
    every non-alphanumeric character, so for `/…/my project` the transcript is
    in `-…-my-project` and the reading looks in `-…-my project`: None for
    ever — no wind-down notice and no compaction, silently. Same for `+`, `@`,
    `~`, non-ASCII and anything else outside [A-Za-z0-9/._]."""
    from multiagents.providers import load_providers
    from multiagents.paths import ProjectPaths

    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    cwd = (tmp_path / "my project").resolve()
    cwd.mkdir()
    paths = ProjectPaths(cwd)
    import yaml

    raw = yaml.safe_load((ch.SHIPPED / "providers.yaml").read_text())
    providers = load_providers(raw.get("providers", raw))
    claude = providers["claude"]
    path = home / ".claude" / "projects" / claude_slug(paths.root) / f"{SID}.jsonl"
    ch.write_transcript(path, [ch.user("hi"), ch.request(42_000)])
    assert session_context(claude, paths.root, SID) == 42_000
