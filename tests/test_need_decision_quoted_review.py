"""List decisions and missing-commit notes use the same marker grammar."""

import pytest

from test_need_decision_quoted import run_text


@pytest.mark.parametrize("prefix", ["- ", "* ", "+ ", "1. ", "1) ", "  - \t", "\t12) "])
def test_list_marker_parks_with_default(tmp_path, monkeypatch, prefix):
    runner, agent = run_text(
        tmp_path, monkeypatch,
        f"{prefix}NEED_DECISION(store): Postgres or SQLite?\nDEFAULT: SQLite",
    )
    assert runner.tree.get(agent).status == "awaiting_user"
    questions = runner.tree.open_questions(agent)
    assert len(questions) == 1
    assert questions[0]["topic"] == "store"
    assert questions[0]["question"] == "Postgres or SQLite?"
    assert questions[0]["proposed_default"] == "SQLite"


@pytest.mark.parametrize("text", [
    "- The `NEED_DECISION(topic): question` marker is an example.",
    "- `NEED_DECISION(topic): question`",
    "1. `NEED_DECISION(topic): question`",
    "The marker NEED_DECISION(topic): question is an example.",
    "-NEED_DECISION(topic): question",
])
def test_quoted_marker_without_commit_still_warns(tmp_path, monkeypatch, text):
    runner, agent = run_text(tmp_path, monkeypatch, text)
    node = runner.tree.get(agent)
    assert node.status == "done"
    assert runner.tree.open_questions(agent) == []
    note = runner._no_commits_note(node, text)
    assert note["no_commits"] is True
    assert "without a commit" in note["no_commits_note"]
