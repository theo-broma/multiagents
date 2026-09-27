"""Commit identity — context/specs/commit-identity.md, CI-R4.

CI-R4: when the runner's end-of-run commit fails (CI-R2) and the failure is
appended to the result text, the git output it carries is truncated to at most
500 characters, with a marker saying it was truncated. The agent's own answer
stays intact.

Black-box, through a real Runner run exactly as the CI-R2 runner test does:
a failing `pre-commit` hook makes the end-of-run commit fail, and the hook
prints a thousand lines (5000 characters) to stderr. The oracle is the run's
result.json text.

Measuring "the git-output portion" without pinning the format: every
character the hook prints is the filler character `Z`, which appears nowhere
in the agent's answer. The hook prints "ZZZZ\n" per line, so any 500
consecutive characters of its output hold exactly 400 `Z`: more than 400 `Z` in
the result text means more than 500 characters of git output were carried. Which end is kept (head or tail) is
the implementer's choice.

The truncation marker's wording is not pinned. The test accepts any marker
containing "truncat" (case-insensitive) — "truncated", "[… truncated]",
"(output truncated, 4500 more chars)" all qualify.
"""

from __future__ import annotations

import json
import re

from test_commit_identity import _run_to_end, _runner  # noqa: E402


FILLER = "Z"
HOOK_LINES = 1000
HOOK_OUTPUT_CHARS = HOOK_LINES * 5            # "ZZZZ\n" per line: 5000 chars
LIMIT = 500
MAX_FILLER = LIMIT * 4 // 5                   # Z's in any LIMIT-char window

# The agent's answer: longer than the git-output limit on purpose, so an
# implementation that truncates the whole result text (rather than just the
# git output) loses part of it. Lower-case only, so it contains no FILLER.
ANSWER = "answer-begin " + " ".join(f"word{i:03d}" for i in range(100)) + " answer-end"
assert FILLER not in ANSWER and len(ANSWER) > LIMIT


def _noisy_failing_hook(repo) -> None:
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text(
        "#!/bin/sh\n"
        f"i=0; while [ $i -lt {HOOK_LINES} ]; do echo ZZZZ >&2; i=$((i+1)); done\n"
        "exit 1\n")
    hook.chmod(0o755)


def test_ci_r4_a_failed_commit_report_is_bounded_and_keeps_the_answer(tmp_path, monkeypatch):
    for var in ("GIT_AUTHOR", "GIT_COMMITTER"):
        monkeypatch.setenv(f"{var}_NAME", "t")
        monkeypatch.setenv(f"{var}_EMAIL", "t@example.invalid")
    project = tmp_path / "project"
    project.mkdir()
    runner = _runner(project, f"echo done > work.txt; echo '{ANSWER}'")
    _noisy_failing_hook(project)      # linked worktrees share the repository's hooks

    agent_id = _run_to_end(runner)

    result = json.loads((runner.paths.run_dir(agent_id) / "result.json").read_text())
    text = result.get("text", "")

    assert ANSWER in text, f"the agent's answer must survive intact; got {text[:300]!r}…"

    carried = text.count(FILLER)
    assert carried > 0, (
        "the commit failure must still carry git's output (CI-R2); "
        f"none of the hook's output is in {text[-300:]!r}")
    assert carried <= MAX_FILLER, (
        f"CI-R4: at most {LIMIT} characters of git output may be appended; "
        f"{carried} of the hook's {HOOK_LINES * 4} 'Z's were carried, i.e. "
        f"~{carried * 5 // 4} of its {HOOK_OUTPUT_CHARS} characters "
        f"(result text is {len(text)} chars)")

    assert re.search(r"truncat", text, re.IGNORECASE), (
        "CI-R4: the truncated git output must carry a marker saying it was "
        f"truncated; tail of the result text: {text[-300:]!r}")

    # Overall bound: the answer, at most LIMIT chars of git output, and a
    # modest allowance for the failure heading, marker and newlines.
    assert len(text) <= len(ANSWER) + LIMIT + 300, (
        f"result text is {len(text)} chars; the appended failure is not bounded")
