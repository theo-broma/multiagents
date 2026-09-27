"""Contract CF: a conversation reads the current code (ticket bug-7f6ba7).

The contract is `context/specs/consult-fresh-worktree.md`, CF-R1 to CF-R7 plus
"Decisions from the advisor's read". Every test here drives `Runner.consult()`
through a real subprocess and observes only what is outside the runner:

- the worktree as the agent found it when its turn STARTED (and when it
  ended), recorded by the fake CLI itself: a file's contents, `HEAD`'s sha,
  the symbolic ref, and `git status`;
- the prompt the provider was handed (its argv);
- the branch tip and symbolic HEAD, read with `git` after the turn;
- the fields of `consult`'s result;
- the project's event log, for CF-R5.

Nothing here knows how the refresh is done. A reset, a checkout, a fresh
worktree on the same branch name — any of them passes if the observable
result is the one the contract names.
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import c3_harness as h  # noqa: E402

GIT = shutil.which("git")
SESSION = "s-cf-1"

# The fake agent CLI. Called as: <script> <prompt> <workdir> [--resume <sid>].
# It records what the turn saw, answers, and exits. A prompt containing SLOW
# holds the turn open for a few seconds so a second consult can land inside it;
# HOLD holds it until the file `release` appears in the probe directory.
_CLI = r'''#!{python}
import json, os, subprocess, sys, time
from pathlib import Path

GIT = {git!r}
PROBE = Path({probe!r})
args = sys.argv[1:]
prompt, workdir = args[0], args[1]
resumed = args[args.index("--resume") + 1] if "--resume" in args else None

def git(*a):
    p = subprocess.run([GIT, "-C", workdir, *a], capture_output=True, text=True)
    return p.stdout.strip() if p.returncode == 0 else None

def snap():
    f = Path(workdir) / "notes.txt"
    return {{"notes": f.read_text() if f.exists() else None,
             "head": git("rev-parse", "HEAD"),
             "symref": git("symbolic-ref", "-q", "HEAD"),
             "status": git("status", "--porcelain")}}

busy = PROBE / "busy"
overlap = busy.exists()
busy.write_text(str(os.getpid()))
started = time.time_ns()
start = snap()
if "SLOW" in prompt:
    time.sleep(4)
if "HOLD" in prompt:
    deadline = time.time() + 150
    while not (PROBE / "release").exists() and time.time() < deadline:
        time.sleep(0.05)
end = snap()
(PROBE / f"turn-{{started}}-{{os.getpid()}}.json").write_text(json.dumps({{
    "started": started, "prompt": prompt, "workdir": workdir,
    "resumed": resumed, "overlap": overlap, "start": start, "end": end}}))
try:
    busy.unlink()
except FileNotFoundError:
    pass
print(json.dumps({{"type": "text", "text": "answered", "session": {session!r}}}))
sys.stdout.flush()
'''


# --------------------------------------------------------------------------
# Scaffolding
# --------------------------------------------------------------------------

# An explicit identity for every git call the test itself makes, so a host
# without a global git identity can still commit.
_IDENTITY = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e.invalid",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e.invalid"}


def git(cwd, *args, check=True):
    import os
    p = subprocess.run([GIT, "-C", str(cwd), *args], capture_output=True,
                       text=True, env={**os.environ, **_IDENTITY})
    if check and p.returncode != 0:
        raise AssertionError(f"git {args} failed: {p.stderr}")
    return p.stdout.strip()


class Project:
    """A project root with a base branch, a conversational `advisor`, and a
    fake CLI whose every turn is recorded under `probe`."""

    def __init__(self, tmp_path, monkeypatch, *, base_branch=None):
        self.root = tmp_path / "proj"
        self.probe = tmp_path / "probe"
        self.probe.mkdir()
        script = tmp_path / "fake-agent"
        script.write_text(_CLI.format(python=sys.executable, git=GIT,
                                      probe=str(self.probe), session=SESSION))
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        provider = {
            "bin": str(script),
            "spawn": {"args": ["{prompt}", "{workdir}"],
                      "resume": ["--resume", "{session_id}"]},
            "stream": {"format": "ndjson", "session_id_paths": ["session"],
                       "rules": [{"match": {"type": "text"}, "as": "text",
                                  "fields": {"text": "text"}}]},
        }
        spec = h.AgentSpec("advisor", "fake", "m", conversational=True)
        # The root checkout needs a base with content before the runner exists.
        self.root.mkdir()
        h.make_git_repo(self.root)
        (self.root / "notes.txt").write_text("v1\n")
        (self.root / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n")
        git(self.root, "add", "notes.txt", ".gitignore")
        git(self.root, "commit", "-m", "base v1")
        self.root_branch = git(self.root, "symbolic-ref", "--short", "HEAD")
        if base_branch:
            git(self.root, "branch", base_branch)
        self.base = base_branch or self.root_branch
        project = {"git": {"base_branch": base_branch}} if base_branch else None
        self.runner = h.make_runner(self.root, monkeypatch,
                                    agents={"advisor": spec},
                                    providers={"fake": provider},
                                    git=False, project=project)

    # -- driving --

    def consult(self, message, timeout=60):
        return asyncio.run(self.runner.consult("advisor", message, timeout=timeout))

    def turns(self):
        records = [json.loads(p.read_text()) for p in self.probe.glob("turn-*.json")]
        return sorted(records, key=lambda r: r["started"])

    def last(self):
        turns = self.turns()
        assert turns, "the agent never ran"
        return turns[-1]

    @property
    def worktree(self):
        return Path(self.turns()[0]["workdir"])

    # -- moving base (never through the worktree) --

    def advance_base(self, content, path="notes.txt", message="base moves"):
        """Commit on the base branch without touching the root's checkout
        when base is not the branch checked out there."""
        if self.base == self.root_branch:
            (self.root / path).write_text(content)
            git(self.root, "add", path)
            git(self.root, "commit", "-m", message)
        else:
            blob = subprocess.run([GIT, "-C", str(self.root), "hash-object", "-w",
                                   "--stdin"], input=content, text=True,
                                  capture_output=True, check=True).stdout.strip()
            tmp_index = self.root / ".git" / "cf-index"
            import os
            env = {**os.environ, **_IDENTITY, "GIT_INDEX_FILE": str(tmp_index)}
            run = lambda *a: subprocess.run([GIT, "-C", str(self.root), *a],
                                            env=env, text=True, capture_output=True,
                                            check=True).stdout.strip()
            run("read-tree", self.base)
            run("update-index", "--add", "--cacheinfo", f"100644,{blob},{path}")
            tree = run("write-tree")
            commit = run("commit-tree", tree, "-p", self.base, "-m", message)
            run("update-ref", f"refs/heads/{self.base}", commit)
            tmp_index.unlink(missing_ok=True)
        return self.base_sha()

    def base_sha(self):
        return git(self.root, "rev-parse", f"refs/heads/{self.base}")

    def events_for(self, node_id):
        path = self.runner.paths.events_file
        if not path.exists():
            return []
        out = []
        for line in path.read_text().splitlines():
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if entry.get("agent") == node_id:
                out.append(entry)
        return out


@pytest.fixture
def proj(tmp_path, monkeypatch):
    return Project(tmp_path, monkeypatch)


def is_short_of(short, full):
    """`short` is an abbreviated form of the full sha `full`."""
    return (isinstance(short, str) and 7 <= len(short) <= 40
            and re.fullmatch(r"[0-9a-f]+", short) is not None
            and full.startswith(short))


def hex_tokens(line):
    return re.findall(r"\b[0-9a-f]{7,40}\b", line)


def first_line_and_rest(prompt, message):
    """The notice line, and what follows it. The message must follow intact."""
    assert prompt.endswith(message), (
        f"the caller's message must reach the agent unchanged after any "
        f"notice: {prompt!r}")
    head = prompt[: len(prompt) - len(message)].strip("\n")
    assert head, f"expected a notice line before the message, got {prompt!r}"
    assert "\n" not in head.strip(), f"the notice must be ONE line: {head!r}"
    return head.strip()


def is_ancestor(repo, a, b):
    return subprocess.run([GIT, "-C", str(repo), "merge-base", "--is-ancestor",
                           a, b]).returncode == 0


def assert_branch_kept(proj, ref, own_head, new_base):
    """The node's branch still contains its own work and was not moved onto
    base. (After a turn the runner may add its own work-in-progress commit on
    top — that is pre-existing behaviour, not a move.)"""
    tip = git(proj.root, "rev-parse", ref)
    assert is_ancestor(proj.root, own_head, tip), (
        f"the node's branch was reset or rewritten: {own_head} is no longer on it")
    assert not is_ancestor(proj.root, new_base, tip), (
        "base was moved or merged into a branch that holds own work")


def start_conversation(proj):
    first = proj.consult("turn one")
    assert "error" not in first, first
    assert first.get("turn") == 1, first
    t1 = proj.last()
    assert t1["start"]["notes"] == "v1\n", "fixture: turn 1 sees base v1"
    return first, t1


# --------------------------------------------------------------------------
# CF-R1 — a turn starts on the current base when nothing would be lost
# --------------------------------------------------------------------------

def test_cf_r1_turn_two_sees_what_was_merged_into_base_after_turn_one(proj):
    first, t1 = start_conversation(proj)
    new_base = proj.advance_base("v2\n")

    second = proj.consult("turn two")

    t2 = proj.last()
    assert second.get("turn") == 2, second
    assert t2["start"]["notes"] == "v2\n", (
        "the agent was handed a worktree still pinned to turn 1's commit")
    assert t2["start"]["head"] == new_base
    assert t2["resumed"] == SESSION, "the conversation's session must be kept"
    assert t2["workdir"] == t1["workdir"] or Path(t2["workdir"]).is_dir()


def test_cf_r1_the_move_follows_base_across_several_commits(proj):
    start_conversation(proj)
    proj.advance_base("v2\n")
    proj.advance_base("added\n", path="other.txt")
    new_base = proj.advance_base("v3\n")

    proj.consult("turn two")

    t2 = proj.last()
    assert t2["start"]["head"] == new_base
    assert t2["start"]["notes"] == "v3\n"
    assert (Path(t2["workdir"]) / "other.txt").read_text() == "added\n"


def test_cf_r1_every_later_turn_is_refreshed_not_just_the_second(proj):
    start_conversation(proj)
    proj.advance_base("v2\n")
    proj.consult("turn two")
    new_base = proj.advance_base("v3\n")

    third = proj.consult("turn three")

    assert third.get("turn") == 3, third
    t3 = proj.last()
    assert t3["start"]["notes"] == "v3\n"
    assert t3["start"]["head"] == new_base
    assert t3["resumed"] == SESSION


def test_cf_r1_the_worktree_stays_on_the_nodes_own_branch_which_points_at_base(proj):
    """Decision: the mechanism is observable. Not a detached HEAD, and the
    branch name does not change."""
    _, t1 = start_conversation(proj)
    branch_ref = t1["start"]["symref"]
    assert branch_ref and branch_ref.startswith("refs/heads/"), t1
    new_base = proj.advance_base("v2\n")

    proj.consult("turn two")

    t2 = proj.last()
    assert t2["start"]["symref"] == branch_ref, (
        f"turn 2 ran on {t2['start']['symref']!r} (None means a detached "
        f"HEAD); the node's branch is {branch_ref!r}")
    wt = Path(t2["workdir"])
    assert git(wt, "symbolic-ref", "-q", "HEAD") == branch_ref
    assert git(proj.root, "rev-parse", branch_ref) == new_base, (
        "the node's branch itself must point at base's HEAD after the move")


def test_cf_r1_a_worktree_holding_only_ignored_files_is_refreshed(proj):
    """Decision: git-ignored files never count as own work — an agent that
    has run Python would otherwise be frozen forever."""
    start_conversation(proj)
    cache = proj.worktree / "__pycache__"
    cache.mkdir()
    (cache / "mod.cpython-312.pyc").write_bytes(b"\x00\x01")
    (proj.worktree / ".pytest_cache").mkdir()
    (proj.worktree / ".pytest_cache" / "v").write_text("x")
    new_base = proj.advance_base("v2\n")

    proj.consult("turn two")

    t2 = proj.last()
    assert t2["start"]["notes"] == "v2\n", "ignored files must not block the refresh"
    assert t2["start"]["head"] == new_base


def test_cf_r1_a_node_commit_squash_merged_into_base_does_not_block(proj):
    """Decision: a commit absorbed into base (merging the branch would change
    no file) is not own work, even though its sha never reaches base."""
    _, t1 = start_conversation(proj)
    wt = proj.worktree
    (wt / "feature.txt").write_text("from the advisor\n")
    git(wt, "add", "feature.txt")
    git(wt, "commit", "-m", "advisor's own commit")
    branch = t1["start"]["symref"].removeprefix("refs/heads/")
    # Squash-merge it into base in the root checkout, then move base on.
    git(proj.root, "merge", "--squash", branch)
    git(proj.root, "commit", "-m", "squashed advisor work")
    new_base = proj.advance_base("v2\n")

    proj.consult("turn two")

    t2 = proj.last()
    assert t2["start"]["head"] == new_base, (
        "a squash-merged commit is already in base and must not freeze the node")
    assert t2["start"]["notes"] == "v2\n"
    assert (Path(t2["workdir"]) / "feature.txt").read_text() == "from the advisor\n"
    assert t2["start"]["symref"] == t1["start"]["symref"]


def test_cf_r1_a_node_commit_merged_normally_into_base_does_not_block(proj):
    _, t1 = start_conversation(proj)
    wt = proj.worktree
    (wt / "feature.txt").write_text("from the advisor\n")
    git(wt, "add", "feature.txt")
    git(wt, "commit", "-m", "advisor's own commit")
    branch = t1["start"]["symref"].removeprefix("refs/heads/")
    git(proj.root, "merge", "--no-ff", "--no-edit", branch)
    new_base = proj.advance_base("v2\n")

    proj.consult("turn two")

    assert proj.last()["start"]["head"] == new_base


def test_cf_r1_base_is_resolved_at_each_turn_not_remembered(proj):
    """Terms: base is the branch checked out in the project root *at the time
    of each turn*. Switching the root to another branch between turns moves
    the next turn onto that branch."""
    start_conversation(proj)
    git(proj.root, "checkout", "-b", "release")
    (proj.root / "notes.txt").write_text("release\n")
    git(proj.root, "commit", "-am", "release line")
    release = git(proj.root, "rev-parse", "HEAD")

    second = proj.consult("turn two")

    t2 = proj.last()
    assert t2["start"]["head"] == release
    assert t2["start"]["notes"] == "release\n"
    assert is_short_of(second.get("base_commit"), release), second


def test_cf_r1_configured_base_branch_wins_over_the_root_checkout(tmp_path, monkeypatch):
    proj = Project(tmp_path, monkeypatch, base_branch="trunk")
    start_conversation(proj)
    # The root checkout moves, but it is not base.
    (proj.root / "notes.txt").write_text("root only\n")
    git(proj.root, "commit", "-am", "root only")
    new_base = proj.advance_base("trunk v2\n")

    proj.consult("turn two")

    t2 = proj.last()
    assert t2["start"]["head"] == new_base
    assert t2["start"]["notes"] == "trunk v2\n"


# --------------------------------------------------------------------------
# CF-R2 — own work is never destroyed
# --------------------------------------------------------------------------

@pytest.mark.parametrize("kind", ["tracked-edit", "untracked-file", "staged-file"])
def test_cf_r2_uncommitted_work_is_kept_and_the_branch_is_not_moved(proj, kind):
    _, t1 = start_conversation(proj)
    wt = proj.worktree
    if kind == "tracked-edit":
        (wt / "notes.txt").write_text("my edit\n")
    elif kind == "untracked-file":
        (wt / "draft.md").write_text("draft\n")
    else:
        (wt / "draft.md").write_text("draft\n")
        git(wt, "add", "draft.md")
    status_before = git(wt, "status", "--porcelain")
    new_base = proj.advance_base("v2\n")

    second = proj.consult("turn two")

    assert "error" not in second, second
    t2 = proj.last()
    assert t2["start"]["head"] == t1["start"]["head"], "HEAD must not move"
    assert t2["start"]["status"] == status_before, "own work was touched"
    if kind == "tracked-edit":
        assert (wt / "notes.txt").read_text() == "my edit\n"
    else:
        assert (wt / "draft.md").read_text() == "draft\n"
        assert t2["start"]["notes"] == "v1\n"
    assert t2["start"]["symref"] == t1["start"]["symref"]
    assert_branch_kept(proj, t1["start"]["symref"], t1["start"]["head"], new_base)
    assert t2["resumed"] == SESSION


def test_cf_r2_a_commit_of_the_nodes_own_is_kept_and_the_branch_is_not_moved(proj):
    _, t1 = start_conversation(proj)
    wt = proj.worktree
    (wt / "feature.txt").write_text("mine\n")
    git(wt, "add", "feature.txt")
    git(wt, "commit", "-m", "advisor's own commit")
    own = git(wt, "rev-parse", "HEAD")
    new_base = proj.advance_base("v2\n")

    proj.consult("turn two")

    t2 = proj.last()
    assert t2["start"]["head"] == own
    assert t2["start"]["notes"] == "v1\n", "base must not have been merged in"
    assert (wt / "feature.txt").read_text() == "mine\n"
    assert_branch_kept(proj, t1["start"]["symref"], own, new_base)
    assert t2["start"]["status"] == "", "nothing may be left half-merged"


def test_cf_r2_an_ignored_file_alongside_real_work_still_counts_as_own_work(proj):
    """Ignored files do not count — but they do not cancel real work either."""
    _, t1 = start_conversation(proj)
    wt = proj.worktree
    (wt / "__pycache__").mkdir()
    (wt / "__pycache__" / "x.pyc").write_bytes(b"\x00")
    (wt / "notes.txt").write_text("my edit\n")
    proj.advance_base("v2\n")

    proj.consult("turn two")

    t2 = proj.last()
    assert t2["start"]["head"] == t1["start"]["head"]
    assert (wt / "notes.txt").read_text() == "my edit\n"


def test_cf_r2_a_diverged_commit_not_absorbed_blocks_even_if_one_file_was_merged(proj):
    """Absorbed means merging the branch would change NO file. A branch whose
    work is only partly in base still holds own work."""
    _, t1 = start_conversation(proj)
    wt = proj.worktree
    (wt / "a.txt").write_text("a\n")
    (wt / "b.txt").write_text("b\n")
    git(wt, "add", "a.txt", "b.txt")
    git(wt, "commit", "-m", "two files")
    own = git(wt, "rev-parse", "HEAD")
    proj.advance_base("a\n", path="a.txt", message="only a lands")

    proj.consult("turn two")

    t2 = proj.last()
    assert t2["start"]["head"] == own
    assert (wt / "b.txt").read_text() == "b\n"


# --------------------------------------------------------------------------
# CF-R3 — the agent is told when its view moved or is stale
# --------------------------------------------------------------------------

def test_cf_r3_after_a_move_the_prompt_starts_with_one_line_naming_both_shas(proj):
    _, t1 = start_conversation(proj)
    old = t1["start"]["head"]
    new = proj.advance_base("v2\n")
    message = "what does notes.txt say now?"

    proj.consult(message)

    line = first_line_and_rest(proj.last()["prompt"], message)
    tokens = hex_tokens(line)
    olds = [i for i, t in enumerate(tokens) if old.startswith(t)]
    news = [i for i, t in enumerate(tokens) if new.startswith(t)]
    assert olds and news, f"the line must name {old[:7]} and {new[:7]}: {line!r}"
    assert min(olds) < max(news), f"old sha, then new sha: {line!r}"


def test_cf_r3_the_updated_line_tells_the_agent_to_re_read_files(proj):
    """Decision: the line also tells the agent to re-read a file before relying
    on or quoting it."""
    start_conversation(proj)
    proj.advance_base("v2\n")
    message = "and now?"

    proj.consult(message)

    line = first_line_and_rest(proj.last()["prompt"], message).lower()
    assert re.search(r"re-?read", line), f"no instruction to re-read: {line!r}"


def test_cf_r3_when_own_work_blocked_a_move_the_line_says_how_far_behind(proj):
    start_conversation(proj)
    (proj.worktree / "notes.txt").write_text("my edit\n")
    proj.advance_base("v2\n")
    proj.advance_base("x\n", path="x.txt")
    proj.advance_base("v3\n")
    message = "status?"

    proj.consult(message)

    line = first_line_and_rest(proj.last()["prompt"], message)
    assert re.search(r"\b3\b", line) and "behind" in line.lower(), (
        f"the line must say the worktree is 3 commits behind: {line!r}")


def test_cf_r3_a_committed_own_work_line_counts_only_base_commits(proj):
    start_conversation(proj)
    wt = proj.worktree
    for i in range(2):
        (wt / f"own{i}.txt").write_text("x\n")
        git(wt, "add", f"own{i}.txt")
        git(wt, "commit", "-m", f"own {i}")
    proj.advance_base("v2\n")
    message = "status?"

    second = proj.consult(message)

    line = first_line_and_rest(proj.last()["prompt"], message)
    assert re.search(r"\b1\b", line) and "behind" in line.lower(), line
    assert second.get("behind") == 1, second


def test_cf_r3_nothing_moved_and_nothing_stale_means_the_message_is_unchanged(proj):
    start_conversation(proj)
    message = "a second question, exactly as asked"

    proj.consult(message)

    assert proj.last()["prompt"] == message


def test_cf_r3_own_work_with_base_not_advanced_adds_no_line(proj):
    """Neither case applies: nothing was prevented, nothing is behind."""
    start_conversation(proj)
    (proj.worktree / "notes.txt").write_text("my edit\n")
    message = "still with me?"

    second = proj.consult(message)

    assert proj.last()["prompt"] == message
    assert second.get("behind") == 0, second


def test_cf_r3_turn_one_prompt_carries_no_update_line(proj):
    first, t1 = start_conversation(proj)
    assert "turn one" in t1["prompt"]
    first_line = t1["prompt"].lstrip().splitlines()[0].lower()
    assert "behind" not in first_line and not re.search(r"re-?read", first_line)


# --------------------------------------------------------------------------
# CF-R4 — the caller can see what was read
# --------------------------------------------------------------------------

EXISTING_FIELDS = {"agent_id", "agent", "turn", "status", "reply", "usage", "note"}


def test_cf_r4_turn_one_reports_commit_base_commit_and_behind(proj):
    first, t1 = start_conversation(proj)
    base = proj.base_sha()
    assert is_short_of(first.get("commit"), t1["start"]["head"]), first
    assert is_short_of(first.get("base_commit"), base), first
    assert first.get("behind") == 0, first
    assert isinstance(first.get("behind"), int) and not isinstance(first["behind"], bool)


def test_cf_r4_after_a_move_commit_is_the_new_base_and_behind_is_zero(proj):
    start_conversation(proj)
    new = proj.advance_base("v2\n")

    second = proj.consult("turn two")

    assert is_short_of(second.get("commit"), new), second
    assert is_short_of(second.get("base_commit"), new), second
    assert second.get("behind") == 0, second


def test_cf_r4_when_own_work_blocked_the_move_behind_counts_base_commits(proj):
    _, t1 = start_conversation(proj)
    (proj.worktree / "notes.txt").write_text("my edit\n")
    proj.advance_base("v2\n")
    new = proj.advance_base("v3\n")

    second = proj.consult("turn two")

    assert is_short_of(second.get("commit"), t1["start"]["head"]), second
    assert is_short_of(second.get("base_commit"), new), second
    assert second.get("behind") == 2, second


def test_cf_r4_commit_is_the_head_the_turn_ran_on(proj):
    start_conversation(proj)
    wt = proj.worktree
    (wt / "feature.txt").write_text("mine\n")
    git(wt, "add", "feature.txt")
    git(wt, "commit", "-m", "own")
    own = git(wt, "rev-parse", "HEAD")

    second = proj.consult("turn two")

    assert is_short_of(second.get("commit"), own), second
    assert is_short_of(second.get("base_commit"), proj.base_sha()), second
    assert second.get("behind") == 0, second


def test_cf_r4_existing_fields_are_unchanged_on_both_turns(proj):
    first, _ = start_conversation(proj)
    proj.advance_base("v2\n")
    second = proj.consult("turn two")
    for result, turn in ((first, 1), (second, 2)):
        assert EXISTING_FIELDS <= set(result), result
        assert result["agent"] == "advisor"
        assert result["turn"] == turn
        assert result["agent_id"] == first["agent_id"]
        assert "answered" in result["reply"]


# --------------------------------------------------------------------------
# CF-R5 — a refresh that fails does not lose the turn
# --------------------------------------------------------------------------

@pytest.mark.parametrize("fate", ["deleted", "renamed"])
def test_cf_r5_a_missing_base_does_not_raise_and_the_turn_runs_as_it_was(
        tmp_path, monkeypatch, fate):
    proj = Project(tmp_path, monkeypatch, base_branch="cf-trunk")
    first, t1 = start_conversation(proj)
    proj.advance_base("v2\n")
    if fate == "deleted":
        git(proj.root, "branch", "-D", "cf-trunk")
    else:
        git(proj.root, "branch", "-m", "cf-trunk", "cf-trunk-renamed")
    events_before = len(proj.events_for(first["agent_id"]))
    message = "turn two"

    second = proj.consult(message)   # must not raise

    assert second.get("turn") == 2, second
    assert "answered" in second.get("reply", ""), second
    t2 = proj.last()
    assert t2["start"]["head"] == t1["start"]["head"], "the worktree must be as it was"
    assert t2["start"]["notes"] == "v1\n"
    assert t2["start"]["symref"] == t1["start"]["symref"]
    assert t2["resumed"] == SESSION
    # "with the CF-R2 line": the agent is told it was not updated.
    first_line_and_rest(t2["prompt"], message)
    new_events = proj.events_for(first["agent_id"])[events_before:]
    mentions = [e for e in new_events
                if "cf-trunk" in json.dumps(e) or "refresh" in json.dumps(e).lower()]
    assert mentions, (
        f"the failed refresh must be recorded as an event on the node; "
        f"new events were {new_events}")


def test_cf_r5_a_git_error_during_the_move_does_not_raise(proj):
    """A lock left in the worktree's git dir makes any move fail."""
    first, t1 = start_conversation(proj)
    proj.advance_base("v2\n")
    gitdir = Path(git(proj.worktree, "rev-parse", "--absolute-git-dir"))
    (gitdir / "index.lock").write_text("")
    message = "turn two"

    try:
        second = proj.consult(message)
    finally:
        (gitdir / "index.lock").unlink(missing_ok=True)

    assert second.get("turn") == 2 and "answered" in second.get("reply", ""), second
    t2 = proj.last()
    assert t2["start"]["head"] == t1["start"]["head"]
    assert t2["start"]["notes"] == "v1\n"
    first_line_and_rest(t2["prompt"], message)


# --------------------------------------------------------------------------
# CF-R6 — turn 1 and non-conversational runs are unchanged
# --------------------------------------------------------------------------

def test_cf_r6_turn_one_runs_on_base_as_it_is_at_that_moment(proj):
    new = proj.advance_base("v0-latest\n")
    first = proj.consult("turn one")
    t1 = proj.last()
    assert first.get("turn") == 1
    assert t1["start"]["head"] == new
    assert t1["start"]["notes"] == "v0-latest\n"
    assert t1["resumed"] is None, "turn 1 starts a session, it does not resume one"
    assert t1["start"]["symref"] and t1["start"]["symref"].startswith("refs/heads/")


# --------------------------------------------------------------------------
# CF-R7 — one turn at a time per node
# --------------------------------------------------------------------------

def _refused_cleanly(outcome):
    if isinstance(outcome, BaseException):
        return (not isinstance(outcome, subprocess.CalledProcessError)
                and "index.lock" not in str(outcome) and bool(str(outcome)))
    return bool(outcome.get("error")) and "index.lock" not in str(outcome["error"])


def test_cf_r7_two_concurrent_consults_never_overlap_and_never_refresh_mid_turn(proj):
    start_conversation(proj)
    proj.advance_base("v2\n")

    async def both():
        a = asyncio.ensure_future(proj.runner.consult("advisor", "SLOW first", timeout=60))
        # Let A refresh and start its turn, then move base under it.
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 20
        while (proj.probe / "busy").exists() is False and loop.time() < deadline:
            await asyncio.sleep(0.02)
        proj.advance_base("v3\n")
        b = asyncio.ensure_future(proj.runner.consult("advisor", "second", timeout=60))
        return await asyncio.gather(a, b, return_exceptions=True)

    ra, rb = asyncio.run(both())

    assert not isinstance(ra, BaseException) and "answered" in ra.get("reply", ""), ra
    turns = proj.turns()
    later = turns[1:]                              # turn 1 was before this test
    assert not any(t["overlap"] for t in later), (
        "two turns of one node ran at the same time")
    slow = [t for t in later if "SLOW first" in t["prompt"]]
    assert len(slow) == 1
    assert slow[0]["start"] == slow[0]["end"], (
        "the worktree changed under a running turn — a second consult "
        "refreshed it mid-turn")
    b_ran = [t for t in later if t["prompt"].endswith("second")]
    if b_ran:
        assert not isinstance(rb, BaseException) and "answered" in rb.get("reply", ""), rb
        assert b_ran[0]["started"] > slow[0]["started"]
        assert b_ran[0]["start"]["notes"] == "v3\n", "B waited, then refreshed"
    else:
        assert _refused_cleanly(rb), f"B neither ran nor was cleanly refused: {rb!r}"
    wt = Path(slow[0]["workdir"])
    assert git(wt, "status", "--porcelain") == "", "worktree left inconsistent"
    assert git(wt, "symbolic-ref", "-q", "HEAD") == turns[0]["start"]["symref"]


# --------------------------------------------------------------------------
# Decided, from the adversary's attack (ag-2bedc4) and the review (ag-905508)
# --------------------------------------------------------------------------

def _assert_updated_line(prompt, message, old, new):
    line = first_line_and_rest(prompt, message)
    tokens = hex_tokens(line)
    assert any(old.startswith(t) for t in tokens) and any(
        new.startswith(t) for t in tokens), (
        f"the line must say the worktree was updated from {old[:7]} to "
        f"{new[:7]}: {line!r}")
    assert re.search(r"re-?read", line.lower()), f"not the 'updated' line: {line!r}"
    assert "own work" not in line.lower() and "work of your own" not in line.lower(), (
        f"the agent holds no own work, and must not be told it does: {line!r}")


def test_decided_own_work_from_start_point_amended_base_moves_with_updated_line(proj):
    """Own work is measured from the node's recorded start point. A base that
    was amended is never the agent's own work: the worktree moves."""
    _, t1 = start_conversation(proj)
    old = t1["start"]["head"]
    (proj.root / "notes.txt").write_text("v1 amended\n")
    git(proj.root, "commit", "--amend", "-am", "base v1 amended")
    amended = proj.base_sha()
    assert amended != old
    message = "turn two"

    second = proj.consult(message)

    t2 = proj.last()
    assert t2["start"]["head"] == amended, "the worktree must move to the amended base"
    assert t2["start"]["notes"] == "v1 amended\n"
    assert t2["start"]["symref"] == t1["start"]["symref"]
    assert git(proj.root, "rev-parse", t1["start"]["symref"]) == amended
    _assert_updated_line(t2["prompt"], message, old, amended)
    assert is_short_of(second.get("commit"), amended), second
    assert second.get("behind") == 0, second


def test_decided_own_work_from_start_point_backwards_base_moves_back_with_updated_line(proj):
    """A base force-moved backwards is never own work, and a base that differs
    from the worktree HEAD is never silent."""
    proj.advance_base("v2\n")
    first = proj.consult("turn one")
    assert "error" not in first, first
    old = proj.last()["start"]["head"]
    symref = proj.last()["start"]["symref"]
    git(proj.root, "reset", "--hard", "HEAD~1")
    back = proj.base_sha()
    message = "turn two"

    second = proj.consult(message)

    t2 = proj.last()
    assert t2["start"]["head"] == back, "the worktree must move back to base"
    assert t2["start"]["notes"] == "v1\n"
    assert t2["start"]["symref"] == symref
    assert git(proj.root, "rev-parse", symref) == back
    _assert_updated_line(t2["prompt"], message, old, back)
    assert is_short_of(second.get("commit"), back), second
    assert second.get("behind") == 0, second


def test_decided_own_work_from_start_point_after_a_move_a_later_own_commit_still_counts(proj):
    """The start point is re-recorded on every move: a commit made after a
    move is own work and blocks the next one."""
    _, t1 = start_conversation(proj)
    proj.advance_base("v2\n")
    proj.consult("turn two")
    wt = Path(proj.last()["workdir"])
    (wt / "feature.txt").write_text("mine\n")
    git(wt, "add", "feature.txt")
    git(wt, "commit", "-m", "own, after the move")
    own = git(wt, "rev-parse", "HEAD")
    new_base = proj.advance_base("v3\n")

    proj.consult("turn three")

    t3 = proj.last()
    assert t3["start"]["head"] == own
    assert_branch_kept(proj, t1["start"]["symref"], own, new_base)


def test_decided_enolck_from_the_lock_fails_at_once_instead_of_waiting(proj, monkeypatch):
    """Only lock contention means "wait". ENOLCK (a filesystem without locks)
    is a failure the turn reports at once."""
    import errno
    import fcntl
    import time

    start_conversation(proj)
    real_flock = fcntl.flock

    def no_locks(fd, op):
        if op & (fcntl.LOCK_EX | fcntl.LOCK_SH):
            raise OSError(errno.ENOLCK, "No locks available")
        return real_flock(fd, op)

    monkeypatch.setattr(fcntl, "flock", no_locks)
    turns_before = len(proj.turns())

    async def attempt():
        # A contention wait would last timeout + 60 s; 15 s is "not at once".
        return await asyncio.wait_for(
            proj.runner.consult("advisor", "turn two", timeout=60), 15)

    t0 = time.monotonic()
    try:
        outcome = asyncio.run(attempt())
    except asyncio.TimeoutError:
        pytest.fail("ENOLCK was treated as contention: the consult waited")
    except Exception as exc:            # reported by raising: acceptable
        outcome = exc
    elapsed = time.monotonic() - t0

    assert elapsed < 15, f"took {elapsed:.1f}s"
    assert len(proj.turns()) == turns_before, "the turn must not run unlocked"
    if isinstance(outcome, dict):
        error = outcome.get("error")
        assert error, f"the failure must be reported: {outcome}"
        assert "still answering" not in error, (
            f"ENOLCK is not another consult answering: {error!r}")


def test_decided_lock_wait_timeout_result_carries_every_key(proj):
    """Every consult result carries agent, agent_id, turn, commit,
    base_commit and behind, null when unknown — the lock-wait timeout too."""
    first, _ = start_conversation(proj)

    async def both():
        a = asyncio.ensure_future(
            proj.runner.consult("advisor", "HOLD first", timeout=300))
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 20
        while not (proj.probe / "busy").exists() and loop.time() < deadline:
            await asyncio.sleep(0.02)
        try:
            # timeout=1: B waits at most 1 + 60 s for A's turn, then gives up.
            b = await proj.runner.consult("advisor", "second", timeout=1)
        finally:
            (proj.probe / "release").write_text("")
        return b, await a

    rb, ra = asyncio.run(both())

    assert "answered" in ra.get("reply", ""), ra
    assert rb.get("error"), f"B must have given up waiting: {rb}"
    keys = {"agent", "agent_id", "turn", "commit", "base_commit", "behind"}
    missing = keys - set(rb)
    assert not missing, f"lock-wait timeout result lacks {sorted(missing)}: {rb}"
    assert rb["agent"] == "advisor"
    assert rb["agent_id"] in (None, first["agent_id"]), rb
    assert rb["turn"] is None or isinstance(rb["turn"], int), rb
    for key in ("commit", "base_commit"):
        assert rb[key] is None or is_short_of(rb[key], git(proj.root, "rev-parse",
                                                           rb[key])), rb
    assert rb["behind"] is None or (isinstance(rb["behind"], int)
                                    and not isinstance(rb["behind"], bool)), rb
