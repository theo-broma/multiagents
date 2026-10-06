"""GG-R2: the orchestrator's composed instructions state the co-author rule.

Composed through `multiagents prompt orchestrator`, which is the launcher's
route for the orchestrator brief, in a real initialised throwaway project.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))
import gg_world as gw  # noqa: E402

TEAMS = ("implement", "review")
TRAILER = "Co-Authored-By"


def _cli(base: Path, root: Path, env: dict, *args: str):
    return subprocess.run([sys.executable, "-m", "multiagents.cli", *args], cwd=str(root),
                          env=env, capture_output=True, text=True, timeout=120,
                          stdin=subprocess.DEVNULL)


@pytest.fixture(scope="module")
def briefs(tmp_path_factory):
    base = tmp_path_factory.mktemp("ggbrief")
    env = gw.base_env(base)
    # init probes providers; with nothing on PATH but git/sh/env it fails fast.
    bin_ = base / "bin"
    bin_.mkdir()
    import shutil
    for tool in ("git", "sh", "env"):
        (bin_ / tool).symlink_to(shutil.which(tool))
    env["PATH"] = str(bin_)
    root = base / "proj"
    root.mkdir()
    gw.git(root, "init", "-q", "-b", "main", env=env)
    (root / "seed.txt").write_text("seed\n")
    gw.git(root, "add", "-A", env=env)
    gw.git(root, "commit", "-q", "-m", "seed", env=env)
    r = _cli(base, root, env, "init")
    assert r.returncode == 0, r.stdout + r.stderr
    cfg = root / ".multiagents" / "config" / "project.yaml"
    original = yaml.safe_load(cfg.read_text()) or {}

    def set_value(value):
        data = yaml.safe_load(yaml.safe_dump(original))
        git = data.setdefault("git", {})
        git.pop("coauthor_orchestrator", None)
        if value is not None:
            git["coauthor_orchestrator"] = value
        cfg.write_text(yaml.safe_dump(data))

    out = {}
    for value in (True, False, None):
        set_value(value)
        for team in TEAMS:
            r = _cli(base, root, env, "prompt", "orchestrator", "--team", team)
            assert r.returncode == 0, r.stdout + r.stderr
            out[(team, value)] = r.stdout
    return out


def _paragraphs(text: str) -> list[str]:
    return [" ".join(p.split()) for p in re.split(r"\n\s*\n", text) if TRAILER in p]


def _states_the_trailer_is_required(text: str) -> bool:
    for p in _paragraphs(text):
        if (re.search(r"commit", p, re.I) and re.search(r"\b(every|each|all|always|end)\b", p, re.I)
                and not re.search(r"\b(never|do not|don't|must not)\b.{0,40}\b(add|include|write|append)\b",
                                  p, re.I)):
            return True
    return False


def _states_the_trailer_is_forbidden(text: str) -> bool:
    return any(re.search(r"\b(never|do not|don't|must not)\b", p, re.I) for p in _paragraphs(text))


@pytest.mark.parametrize("team", TEAMS)
def test_gg_r2_true_tells_the_orchestrator_to_end_every_commit_with_the_trailer(briefs, team):
    assert _states_the_trailer_is_required(briefs[(team, True)]), _paragraphs(briefs[(team, True)])


@pytest.mark.parametrize("team", TEAMS)
def test_gg_r2_false_tells_the_orchestrator_never_to_add_one(briefs, team):
    text = briefs[(team, False)]
    assert _states_the_trailer_is_forbidden(text), _paragraphs(text)
    assert not _states_the_trailer_is_required(text)


@pytest.mark.parametrize("team", TEAMS)
def test_gg_r2_changing_the_value_changes_the_composed_text(briefs, team):
    assert briefs[(team, True)] != briefs[(team, False)]


@pytest.mark.parametrize("team", TEAMS)
def test_gg_r2_absent_key_defaults_to_true(briefs, team):
    assert _states_the_trailer_is_required(briefs[(team, None)])
    assert briefs[(team, None)] == briefs[(team, True)]
