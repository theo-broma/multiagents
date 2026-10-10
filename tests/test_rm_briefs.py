"""RM-R4 content checks: these prove guidance ships, not compliance."""
import re

import pytest
import yaml

from multiagents.paths import shipped_defaults_dir


def brief(agent):
    root = shipped_defaults_dir()
    roster = yaml.safe_load((root / "agents.yaml").read_text())["agents"]
    paths = roster[agent]["instructions"]
    if isinstance(paths, str):
        paths = [paths]
    return "\n".join((root / "agents" / path).read_text() for path in paths).lower()


def includes(text, pattern):
    assert re.search(pattern, text, re.S), f"missing shipped guidance: {pattern}"


@pytest.mark.parametrize("pattern", [
    r"run.{0,120}(suite|tests).{0,160}wait.{0,120}(finish|exit|complet)",
    r"background.{0,180}poll.{0,180}(exit|finish|complet)",
    r"(never|do not|don't).{0,80}end.{0,100}turn.{0,160}(running|exited|finish)",
    r"(report|state).{0,160}(observed|counts).{0,200}(not run|was not run|did not run).{0,120}why",
    r"approval.{0,180}(no observed counts|without observed counts).{0,160}first line",
], ids=["wait-for-suite", "poll-background", "do-not-end-running", "counts-or-reason", "approval-first-line"])
def test_rm_r4_reviewer_brief_ships_each_validation_rule(pattern):
    includes(brief("reviewer"), pattern)


@pytest.mark.parametrize("agent", ["implementer-quick", "implementer", "implementer-deep", "tester"])
def test_rm_r4_every_writing_tier_is_told_not_to_commit_validation_artifacts(agent):
    text = brief(agent)
    includes(text, r"(never|do not|don't).{0,100}commit.{0,100}logs.{0,100}validation.{0,100}scratch")
    includes(text, r"outside.{0,100}worktree.{0,160}gitignored")
