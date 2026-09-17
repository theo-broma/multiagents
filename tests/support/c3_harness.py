"""Shared test harness for C3 — agent lifecycle and concurrent tree state.

Builders and seams for `runner.py`, `driver.py`, `tree.py`, `watchdog.py`,
`gitops.py` and `procs.py`, so a characterizer can construct a `Runner`, drive
it through a real (but fake-CLI) spawn/consume/merge cycle, and exercise the
tree, git and watchdog layers directly against throwaway state.

**Read this before writing a test against `runner.start()` or `consult()` —
it is the one thing in this harness that is not optional.**

## The `can_spawn` wall, and why it is not actually a wall

`runner.py:468 Runner.can_spawn()` is:

    def can_spawn(self) -> bool:
        if self.self_id() is None:
            return True                       # the root orchestrator always may
        return os.environ.get("MULTIAGENTS_CAN_SPAWN", "0") == "1"

`self_id()` reads `MULTIAGENTS_AGENT_ID` from the process environment. Every
agent process in this tree — including whichever one is running this test
suite — has that variable set for real, to its own id, by the harness that
spawned it, alongside `MULTIAGENTS_CAN_SPAWN=0`. That is not a test fixture;
it is the actual ambient environment of an actual subagent, and it is why
`tests/test_core.py`'s `_runner`-based spawn tests are 17 of the 4
known-red groups in this repository when run from inside an agent (see
`context/review/MAP.md`, "A measurement I could not complete — now settled")
and green when run by a human or CI on a bare host, where `MULTIAGENTS_AGENT_ID`
is simply unset.

This was CONFIRMED empirically while building this harness, not assumed:
calling `Runner.start()` from inside this very agent, unmodified, raises
`PermissionError: ... can_spawn is false`; the identical call, with only
`monkeypatch.delenv("MULTIAGENTS_AGENT_ID", raising=False)` first, runs a real
`sh -c true` subprocess through `LocalExecutor`, streams it, and returns a
normal `start()` result.

So the wall is an artifact of *who is running pytest*, not of the code under
test, and it has a one-line fix that changes no production behaviour: clear
the ambient identity before construction, the same way `conftest.py` already
redirects `MULTIAGENTS_STATE_DIR` so a test's `ProjectPaths.ensure()` cannot
touch the developer's real `~/.multiagents`. `as_root()` below does exactly
that. **Every builder in this module that constructs a `Runner` and does not
take an explicit `agent_id=` calls `as_root()` for you** — a characterizer
does not need to remember this, only know that it happens.

The corollary: `test_core.py`'s 17 red tests are not being fixed by this
harness (out of scope — "do not investigate or fix" was explicit), but a
characterizer using `c3_harness` instead of `test_core.py`'s bare `_runner`
gets a suite that is green regardless of who runs it. If asked to explain the
17, point here.

## What is real, unmodified production code

Everything. There is no seam over `Runner`, `Tree`, `gitops` or `watchdog`
proper — construction is cheap (no I/O beyond a temp git repo and some
`~/.multiagents`-shaped JSON under `tmp_path`), so nothing here is mocked at
the object level. The one seam is the **subprocess an agent's CLI would be**:
`fake_cli()` below writes a small real, executable Python script and points a
real `Provider` at it, so `Runner._launch` runs a REAL `asyncio` subprocess
through the REAL `LocalExecutor` — it is standing in for `claude`/`opencode`/
`agy`, not for any multiagents code. This is the same principle
`c2_harness.write_script`/`case_script` use for the provider seam; the two are
deliberately similar and a characterizer already familiar with C2 should
recognise the shape.

## What is genuinely unreachable from here, and why

- **Docker.** `config.executor: docker` needs the `docker` binary, unavailable
  in this environment (see `c1_harness`'s module docstring — same fact, same
  reason). `make_runner()` never sets `executor: docker`; a characterizer who
  wants the container path needs `c1_harness.make_docker_executor` and is
  exercising C1, not C3.
- **A real provider CLI.** `claude`, `opencode`, `agy` themselves are never
  invoked — `fake_cli()` always stands in. `Runner._launch`'s command-building
  (`Provider.build_command`, `scripts.build_env`) is real and is exercised
  against a real argv; what runs at the end of that argv is fake, same
  boundary C2's harness draws around the shipped provider scripts.
- **Two-process concurrency.** `tree.py`'s `Tree.transaction()` locks the tree
  file (`fcntl`), which is real and does work across two Python processes —
  but this harness runs everything in one process. A test wanting genuine
  cross-process contention has to launch a second `python -c ...` itself; two
  `Tree` objects over the same `tmp_path` in one process share no lock
  contention worth measuring (the GIL already serialises them) and would
  produce a false sense of coverage.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import time as _time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from multiagents.config import AgentSpec, Config                # noqa: E402
from multiagents.paths import ProjectPaths                       # noqa: E402
from multiagents.providers import Provider                       # noqa: E402
from multiagents.runner import Runner                            # noqa: E402
from multiagents.tree import Node, Tree, now as tree_now          # noqa: E402
from multiagents import gitops, watchdog, procs                  # noqa: E402

__all__ = [
    "as_root", "as_subagent",
    "AgentSpec", "Config",
    "make_paths", "make_config", "make_git_repo", "make_runner",
    "make_provider", "fake_cli",
    "Tree", "Node", "tree_now", "make_tree",
    "gitops", "make_repo_pair", "make_conflicting_branches",
    "watchdog", "make_transcript", "fake_provider_double",
    "procs",
]


# ---------------------------------------------------------------------------
# 1. The ambient-identity seam — see the module docstring
# ---------------------------------------------------------------------------

_IDENTITY_VARS = (
    "MULTIAGENTS_AGENT_ID", "MULTIAGENTS_PARENT_ID", "MULTIAGENTS_DEPTH",
    "MULTIAGENTS_CAN_SPAWN", "MULTIAGENTS_BRANCH",
)


def as_root(monkeypatch) -> None:
    """Make `Runner.self_id()` return `None` regardless of the real process.

    The root orchestrator's env has no `MULTIAGENTS_AGENT_ID` — it was
    launched by a human or by `driver.py`, not spawned as a subagent — so this
    clears every identity variable an agent process (including the one
    possibly running this test) might carry. Idempotent; safe to call from a
    fixture even when the ambient env already looks like root.
    """
    for var in _IDENTITY_VARS:
        monkeypatch.delenv(var, raising=False)


def as_subagent(monkeypatch, *, agent_id: str = "ag-test", parent: str = "",
                depth: int = 1, can_spawn: bool = False) -> None:
    """Make `Runner.self_id()`/`self_depth()`/`can_spawn()` answer as a given
    subagent, deliberately — for testing the gate itself (depth limits,
    ownership, `can_spawn: false` refusals), as opposed to `as_root()`, which
    exists to get *out* of the way of those gates for everything else.
    """
    monkeypatch.setenv("MULTIAGENTS_AGENT_ID", agent_id)
    monkeypatch.setenv("MULTIAGENTS_PARENT_ID", parent)
    monkeypatch.setenv("MULTIAGENTS_DEPTH", str(depth))
    monkeypatch.setenv("MULTIAGENTS_CAN_SPAWN", "1" if can_spawn else "0")


# ---------------------------------------------------------------------------
# 2. Construction — paths, config, a real throwaway git repo, a Runner
# ---------------------------------------------------------------------------

def make_paths(tmp_path: Path) -> ProjectPaths:
    """A `ProjectPaths` over `tmp_path`, directories created.

    Safe under `conftest.py`'s `_machine_state_is_disposable` fixture: the
    worktree/home roots this resolves to (`paths.worktrees`, `paths.homes`)
    live under `MULTIAGENTS_STATE_DIR`, which that fixture has already pointed
    at a directory private to this test.
    """
    paths = ProjectPaths(tmp_path)
    paths.ensure()
    return paths


def make_config(agents: dict[str, AgentSpec] | None = None,
                providers: dict[str, Any] | None = None,
                project: dict[str, Any] | None = None,
                models: dict[str, Any] | None = None) -> Config:
    return Config(project=project or {}, providers=providers or {},
                  agents=agents or {}, models=models or {}, instruction_dirs=[])


def make_git_repo(tmp_path: Path) -> Path:
    """`tmp_path` turned into a real git repo with one empty commit.

    What every spawn needs: `_preflight` refuses a project with no branch to
    cut a worktree from. Uses the real `git` binary, not a fixture — the same
    dependency `gitops.py` itself has.
    """
    tmp_path.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
    for args in (["init"], ["commit", "--allow-empty", "-m", "init"]):
        subprocess.run(["git", "-C", str(tmp_path), *args],
                       capture_output=True, env=env, check=True)
    return tmp_path


def make_runner(tmp_path: Path, monkeypatch, *, agents: dict[str, AgentSpec] | None = None,
                providers: dict[str, Any] | None = None, git: bool = True,
                project: dict[str, Any] | None = None) -> Runner:
    """A `Runner` over a throwaway project, as the root orchestrator.

    `monkeypatch` is required, not optional-with-a-default: it is what wires
    `as_root()` in, and a caller who forgot it would silently get a Runner
    that raises `PermissionError` on every spawn for reasons that have
    nothing to do with what they are testing — exactly the trap this harness
    exists to remove. A characterizer who wants the OPPOSITE — testing
    `can_spawn` itself — calls `as_subagent()` again *after* this, which
    overwrites what `as_root()` set.
    """
    as_root(monkeypatch)
    paths = make_paths(tmp_path)
    if git:
        make_git_repo(tmp_path)
    config = make_config(agents=agents, providers=providers, project=project)
    return Runner(paths, config)


# ---------------------------------------------------------------------------
# 3. The subprocess seam — a fake CLI standing in for claude/opencode/agy
# ---------------------------------------------------------------------------

def make_provider(name: str = "p", **overrides: Any) -> Provider:
    """A `Provider` with sensible defaults, built through the real
    `Provider.from_dict` — see `c2_harness.make_provider`'s docstring for why
    that matters. `overrides` are `providers.yaml` keys.
    """
    overrides.setdefault("bin", name)
    return Provider.from_dict(name, overrides)


def fake_cli(tmp_path: Path, name: str = "fake",
            events: list[dict[str, Any]] | None = None,
            exit_code: int = 0, delay: float = 0.0,
            stderr: str = "") -> dict[str, Any]:
    """A real, executable script that plays the role of an agent CLI, plus
    the `providers.yaml`-shaped dict that points a `Provider` at it.

    Prints each of `events` (plain dicts) as one NDJSON line to stdout, sleeps
    `delay` seconds first if given (to exercise timeout/silence paths without
    a real long-running agent), then exits `exit_code`. `stderr`, if given, is
    printed to stderr before exit — for exercising `Handle.stderr_tail`.

    The returned dict is ready to hand to `make_runner(..., providers={name:
    fake_cli(...)})` or to `Provider.from_dict` directly. `spawn.args` carries
    one placeholder token (`_preflight` refuses any provider with empty
    `spawn.args` outright — `runner.py:606`, "declares no spawn args") that
    this script ignores; the prompt still reaches it as a file
    `runner._launch` writes to `run_dir`, exactly as a real CLI receives it,
    this fake just does not read it.
    """
    script = tmp_path / f"{name}.py"
    payload = json.dumps(events or [])
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, sys, time\n"
        f"time.sleep({delay!r})\n"
        f"for _e in json.loads({payload!r}):\n"
        "    print(json.dumps(_e))\n"
        "    sys.stdout.flush()\n"
        f"sys.stderr.write({stderr!r})\n"
        f"sys.exit({exit_code!r})\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return {"bin": str(script), "spawn": {"args": ["--fake-cli"]},
            "stream": {"format": "ndjson"}}


def fake_provider_double(name: str = "p", **kw: Any) -> Provider:
    """A `Provider` carrying only what `watchdog.py` reads off one — `name`
    and `transcript` — for tests that need a provider-shaped object but are
    nowhere near a real spawn. Distinct from `make_provider()`/`fake_cli()`
    because watchdog code takes `provider: Any` and reads `getattr`, so a
    `SimpleNamespace` is honest about what is actually used rather than
    implying a full `Provider` was constructed.
    """
    return SimpleNamespace(name=name, transcript=kw.pop("transcript", {}), **kw)


# ---------------------------------------------------------------------------
# 4. Tree — no seam needed; re-exported for a single import per characterizer
# ---------------------------------------------------------------------------

def make_tree(tmp_path: Path) -> Tree:
    """A `Tree` over a throwaway project's `tree.json`/`events.jsonl`.

    `tree.py` is 91% covered already and needs nothing stubbed — this exists
    only so a characterizer importing `c3_harness` does not also need
    `multiagents.paths` for the two-line `ProjectPaths(...).ensure()` dance.
    """
    paths = make_paths(tmp_path)
    return Tree(paths.tree_file, paths.events_file)


# ---------------------------------------------------------------------------
# 5. gitops — real temp repositories; no seam, git itself is the dependency
# ---------------------------------------------------------------------------

def make_repo_pair(tmp_path: Path, *, branch: str = "agents/x/1",
                   files: dict[str, str] | None = None) -> tuple[Path, Path, str]:
    """A repo with one commit, plus a worktree on a fresh branch with the
    given files committed — the shape `gitops.merge()` expects to merge FROM.

    Returns `(repo, worktree)`. `files` defaults to one new file, since an
    agent branch that changed nothing merges as `"empty"` rather than
    `"merged"` — pass `files={}` deliberately to test that outcome.
    """
    repo = make_git_repo(tmp_path / "repo")
    worktree = tmp_path / "worktree"
    actual_branch = gitops.create_worktree(repo, worktree, branch)
    changes = {"agent-work.txt": "written by the agent\n"} if files is None else files
    for rel, content in changes.items():
        target = worktree / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    if changes:
        gitops.commit_all(worktree, "agent work")
    return repo, worktree, actual_branch


def make_conflicting_branches(tmp_path: Path) -> tuple[Path, str]:
    """A repo with two branches that touch the same line of the same file, so
    `gitops.merge(repo, second_branch, ...)` after checking out and merging
    the first returns `("conflict", ...)`. Returns `(repo, second_branch)`;
    the caller merges the first branch itself if it wants a clean merge
    landed before triggering the conflict.
    """
    repo = make_git_repo(tmp_path / "repo")
    target = repo / "shared.txt"
    target.write_text("base\n")
    gitops.commit_all(repo, "base file")

    w1 = tmp_path / "w1"
    b1 = gitops.create_worktree(repo, w1, "agents/a/1")
    (w1 / "shared.txt").write_text("branch one\n")
    gitops.commit_all(w1, "branch one edits")

    w2 = tmp_path / "w2"
    b2 = gitops.create_worktree(repo, w2, "agents/a/2")
    (w2 / "shared.txt").write_text("branch two\n")
    gitops.commit_all(w2, "branch two edits")

    status, _ = gitops.merge(repo, b1, "merge branch one")
    assert status == "merged", "fixture is broken: the first merge should be clean"
    return repo, b2


# ---------------------------------------------------------------------------
# 6. watchdog — verdict() is pure; sample()/supervise() need a transcript dir
# ---------------------------------------------------------------------------

def make_transcript(tmp_path: Path, *, lines: list[dict[str, Any]] | None = None,
                    quiet_for: float | None = None,
                    limit_markers: list[dict[str, Any]] | None = None
                    ) -> tuple[Any, Path]:
    """A provider double with a real transcript directory `watchdog.sample()`
    can glob, plus the file itself. Returns `(provider_double, cwd)` — pass
    `cwd` as the `cwd` argument wherever the API under test wants one
    (`watchdog.sample(paths, ...)` reads it as `paths.root`, so build a
    `make_paths(tmp_path)` with this SAME `tmp_path` if exercising `sample`
    directly rather than `newest_transcript`/`limit_reached` alone).

    `quiet_for`, if given, backdates the file's mtime by that many seconds via
    `os.utime` — the deterministic substitute for "wait N seconds and then
    check": `watchdog.verdict()` itself takes `quiet_for` as a plain float
    argument and does no clock reads at all, so driving it directly needs no
    file and no timers; this helper is only for `sample()`/`limit_reached()`,
    which recompute `quiet_for` from a real file's `mtime`.
    """
    directory = tmp_path / "transcripts"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "session.jsonl"
    path.write_text("\n".join(json.dumps(line) for line in (lines or [])) + "\n")
    if quiet_for is not None:
        when = _time.time() - quiet_for
        os.utime(path, (when, when))
    provider = fake_provider_double(
        transcript={"dir": str(directory), "glob": "*.jsonl",
                    "limit_markers": limit_markers or []})
    return provider, tmp_path
