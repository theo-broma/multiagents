"""Agent commits are never GPG-signed — context/specs/commit-identity.md (CI-R6).

Setup, as the spec suggests: the project repository has `commit.gpgsign=true`
and a `gpg.program` that cannot sign. It is a script that leaves a marker file
each time git calls it and then exits 1, so "git tried to sign" is observable
directly rather than inferred from an error message.

Under that setup:
- the runner's end-of-run commit on an agent branch succeeds, unsigned;
- a `git commit` the agent runs itself, inside its executor, succeeds, unsigned;
- `gitops.merge` (squash and --no-ff) and `gitops.initial_commit` still try to
  sign, and so fail;
- git config the user passes through the environment (`GIT_CONFIG_COUNT` /
  `GIT_CONFIG_KEY_n` / `GIT_CONFIG_VALUE_n`, `GIT_CONFIG_PARAMETERS`) still
  takes effect on agent commits;
- no config file is written.

Black-box: a real Runner run with a shell "agent", and git's own view of the
result (`git log`, `git cat-file`, `git config`). The mechanism — a commit
option, `GIT_CONFIG_*` in the agent's environment, anything else — is the
implementer's choice and nothing here depends on it.

Only the local executor is exercised. The docker executor is not: `docker` is
deliberately absent where this suite runs (see BRIEF.md).
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from multiagents import gitops


# Everything in the host environment that could feed git config or identity
# into these tests from outside. Each test sets exactly what it means to.
GIT_ENV_VARS = (
    "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_AUTHOR_DATE",
    "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL", "GIT_COMMITTER_DATE",
    "EMAIL", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_CONFIG_PARAMETERS",
    "GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0",
    "GIT_CONFIG_KEY_1", "GIT_CONFIG_VALUE_1", "GIT_CONFIG_KEY_2", "GIT_CONFIG_VALUE_2",
    "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE",
)

REPO_NAME = "Repo Person"
REPO_EMAIL = "repo@example.invalid"


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise AssertionError(f"git {args} failed: {proc.stderr}")
    return proc


def author(repo: Path, ref: str = "HEAD") -> tuple[str, str]:
    out = git(repo, "log", "-1", "--format=%an%x00%ae", ref).stdout.strip()
    name, email = out.split("\0")
    return name, email


def subjects(repo: Path, ref: str) -> list[str]:
    return git(repo, "log", "--format=%s", ref).stdout.splitlines()


def is_signed(repo: Path, ref: str) -> bool:
    """Whether the commit object carries a signature header. Read from the raw
    object, so it needs no working gpg to answer."""
    raw = git(repo, "cat-file", "commit", ref).stdout
    headers = raw.split("\n\n", 1)[0]
    return any(line.startswith(("gpgsig ", "gpgsig-sha256 "))
               for line in headers.splitlines())


def signed_commits(repo: Path, branch: str, base: str) -> list[str]:
    """Every commit on `branch` not on `base` that carries a signature."""
    shas = git(repo, "rev-list", f"{base}..{branch}").stdout.split()
    return [sha for sha in shas if is_signed(repo, sha)]


@pytest.fixture
def isolated_git(tmp_path, monkeypatch):
    """No global or system git config, and no GIT_* from the host."""
    home = tmp_path / "host-home"
    home.mkdir()
    global_cfg = tmp_path / "empty-global-gitconfig"
    global_cfg.write_text("")
    for name in GIT_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "host-xdg"))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_cfg))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    return tmp_path


@pytest.fixture
def signer(isolated_git):
    """A `gpg.program` that records each call and then refuses to sign."""
    marker = isolated_git / "gpg-was-called"
    program = isolated_git / "unusable-gpg"
    program.write_text(f"#!/bin/sh\necho called >> '{marker}'\nexit 1\n")
    program.chmod(0o755)

    class Signer:
        path = program

        @staticmethod
        def called() -> bool:
            return marker.exists()

        @staticmethod
        def reset() -> None:
            marker.unlink(missing_ok=True)

    return Signer


def require_signing(repo: Path, signer) -> None:
    """The user's signing config: in the repository, where a linked agent
    worktree shares it. The identity is configured too, so the only thing
    that can make a commit fail here is signing."""
    git(repo, "config", "user.name", REPO_NAME)
    git(repo, "config", "user.email", REPO_EMAIL)
    git(repo, "config", "commit.gpgsign", "true")
    git(repo, "config", "gpg.program", str(signer.path))


def seeded_repo(path: Path) -> Path:
    """A repository with one unsigned commit, made before signing is required."""
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q")
    git(path, "-c", "user.name=seed", "-c", "user.email=seed@example.invalid",
        "-c", "commit.gpgsign=false", "commit", "-q", "--allow-empty", "-m", "seed")
    return path


# --------------------------------------------------------------------------
# Running an agent: a real Runner, local executor, a shell script as the agent


def _runner(project: Path, agent_script: str, passthrough: list[str] | None = None):
    from multiagents.config import AgentSpec, Config
    from multiagents.paths import ProjectPaths
    from multiagents.runner import Runner
    paths = ProjectPaths(project)
    paths.ensure()
    seeded_repo(project)
    project_cfg = {"security": {"env_passthrough": list(passthrough or [])}}
    config = Config(
        project=project_cfg,
        providers={"p": {"bin": "sh", "spawn": {"args": ["-c", agent_script]}}},
        agents={"worker": AgentSpec("worker", "p", "m")}, models={}, instruction_dirs=[],
    )
    return Runner(paths, config)


def _run_to_end(runner) -> str:
    async def scenario():
        started = await runner.start("worker", "go")
        agent_id = started["agent_id"]
        for _ in range(150):
            node = runner.tree.get(agent_id)
            if node.status not in ("pending", "running"):
                break
            await asyncio.sleep(0.1)
        return agent_id
    return asyncio.run(scenario())


def _result_text(runner, agent_id: str) -> str:
    path = runner.paths.run_dir(agent_id) / "result.json"
    return path.read_text() if path.exists() else "<no result.json>"


@pytest.fixture
def signing_project(isolated_git, signer):
    project = isolated_git / "project"
    project.mkdir()
    return project


def _start(project: Path, signer, agent_script: str, passthrough=None):
    runner = _runner(project, agent_script, passthrough)
    require_signing(project, signer)
    # The project's own state directory is not the user's work: keep it from
    # making the base checkout look dirty to gitops.merge.
    exclude = project / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    with exclude.open("a") as fh:
        fh.write("\n.multiagents/\n")
    base = git(project, "rev-parse", "HEAD").stdout.strip()
    return runner, base


# --------------------------------------------------------------------------
# Control: the setup really makes an ordinary commit try to sign, and fail


def test_control_a_plain_git_commit_tries_to_sign_and_fails(isolated_git, signer):
    repo = seeded_repo(isolated_git / "repo")
    require_signing(repo, signer)
    (repo / "f.txt").write_text("x")
    git(repo, "add", "-A")

    plain = git(repo, "commit", "-m", "plain", check=False)

    assert plain.returncode != 0, (
        "the signing setup does not make git sign, so every CI-R6 test here "
        f"would prove nothing: {plain.stdout}{plain.stderr}")
    assert signer.called(), "git failed, but not because it tried to sign"


# --------------------------------------------------------------------------
# CI-R6 — the runner's end-of-run commit


def test_ci_r6_end_of_run_commit_succeeds_unsigned_under_gpgsign(signing_project, signer):
    runner, base = _start(signing_project, signer, "echo done > work.txt; echo finished")

    agent_id = _run_to_end(runner)

    node = runner.tree.get(agent_id)
    assert node.branch, "the run must have had a branch"
    shown = git(signing_project, "show", f"{node.branch}:work.txt", check=False)
    assert shown.returncode == 0 and shown.stdout == "done\n", (
        "with commit.gpgsign=true and no usable key, the end-of-run commit must "
        f"still reach the branch: {shown.stderr}\nresult: {_result_text(runner, agent_id)}")
    assert signed_commits(signing_project, node.branch, base) == [], \
        "a commit on an agent branch carries a signature"
    assert git(Path(node.worktree), "status", "--porcelain").stdout.strip() == "", \
        "the agent's work was left uncommitted in its worktree"


def test_ci_r6_end_of_run_commit_keeps_the_configured_identity(signing_project, signer):
    """Skipping the signature must not cost anything else of the user's config."""
    runner, _ = _start(signing_project, signer, "echo done > work.txt")

    agent_id = _run_to_end(runner)

    node = runner.tree.get(agent_id)
    assert git(signing_project, "show", f"{node.branch}:work.txt",
               check=False).returncode == 0, _result_text(runner, agent_id)
    assert author(signing_project, node.branch) == (REPO_NAME, REPO_EMAIL)


# --------------------------------------------------------------------------
# CI-R6 — a commit the agent makes itself, inside its executor


AGENT_OWN_COMMIT = (
    "echo mine > own.txt && git add -A && git commit -q -m 'agent own commit' "
    "&& echo AGENT_COMMIT_OK || echo AGENT_COMMIT_FAILED"
)


def test_ci_r6_an_agent_git_commit_succeeds_unsigned_under_gpgsign(signing_project, signer):
    runner, base = _start(signing_project, signer, AGENT_OWN_COMMIT)

    agent_id = _run_to_end(runner)

    node = runner.tree.get(agent_id)
    assert node.branch, "the run must have had a branch"
    assert "agent own commit" in subjects(signing_project, node.branch), (
        "the agent's own `git commit` inside its executor must succeed under "
        f"commit.gpgsign=true; result: {_result_text(runner, agent_id)}")
    assert git(signing_project, "show", f"{node.branch}:own.txt").stdout == "mine\n"
    assert signed_commits(signing_project, node.branch, base) == [], \
        "a commit on an agent branch carries a signature"
    assert not signer.called(), \
        "signing was attempted on an agent commit; it must be as if commit.gpgsign=false"


def test_ci_r6_an_agent_merge_commit_on_its_branch_is_unsigned(signing_project, signer):
    """`commit.gpgsign` governs merge commits too, and CI-R6 covers every
    commit made on an agent's branch, not only `git commit`."""
    script = (
        "git checkout -q -b side && echo s > side.txt && git add -A "
        "&& git commit -q -m side-work && git checkout -q - "
        "&& git merge -q --no-ff -m 'agent merge' side "
        "&& echo AGENT_MERGE_OK || echo AGENT_MERGE_FAILED"
    )
    runner, base = _start(signing_project, signer, script)

    agent_id = _run_to_end(runner)

    node = runner.tree.get(agent_id)
    assert "agent merge" in subjects(signing_project, node.branch), \
        f"the agent's merge commit must succeed: {_result_text(runner, agent_id)}"
    assert signed_commits(signing_project, node.branch, base) == []


# --------------------------------------------------------------------------
# CI-R6 — the user's config is never written, and nothing leaks out of the run


def test_ci_r6_no_git_config_is_written(signing_project, signer, isolated_git):
    runner, _ = _start(signing_project, signer, AGENT_OWN_COMMIT + "; echo more > w2.txt")
    repo_config = (signing_project / ".git" / "config").read_bytes()
    global_cfg = isolated_git / "empty-global-gitconfig"

    agent_id = _run_to_end(runner)

    node = runner.tree.get(agent_id)
    # Guard against a vacuous pass: the run must actually have committed.
    assert "agent own commit" in subjects(signing_project, node.branch), \
        _result_text(runner, agent_id)
    assert (signing_project / ".git" / "config").read_bytes() == repo_config, \
        "the repository's config was changed"
    assert global_cfg.read_bytes() == b""
    worktree_cfg = signing_project / ".git" / "worktrees"
    for cfg in worktree_cfg.glob("*/config.worktree"):
        assert "gpgsign" not in cfg.read_text(), f"signing config written to {cfg}"
    assert git(signing_project, "config", "--get", "commit.gpgsign").stdout.strip() == "true"
    assert git(Path(node.worktree), "config", "--get",
               "commit.gpgsign").stdout.strip() == "true", \
        "git on its own in the agent worktree must still see the user's setting"


def test_ci_r6_orchestrator_commits_after_an_agent_run_still_sign(signing_project, signer):
    """The override is scoped to agent commits: after a run in this process,
    an ordinary commit on the base branch still tries to sign."""
    runner, _ = _start(signing_project, signer, "echo done > work.txt")
    agent_id = _run_to_end(runner)
    node = runner.tree.get(agent_id)
    assert git(signing_project, "show", f"{node.branch}:work.txt",
               check=False).returncode == 0, _result_text(runner, agent_id)
    signer.reset()

    status, detail = gitops.merge(signing_project, node.branch, "worker: merged")

    assert status != "merged", f"the merge commit was made without signing: {detail}"
    assert signer.called(), "gitops.merge did not try to sign after an agent run"


# --------------------------------------------------------------------------
# CI-R6 — orchestrator-side commits keep the user's signing config.
# These describe behaviour that already holds before CI-R6 and must survive it.


@pytest.fixture
def merge_repo(isolated_git, signer):
    repo = seeded_repo(isolated_git / "merge-repo")
    git(repo, "checkout", "-q", "-b", "agents/worker/abc")
    (repo / "work.txt").write_text("agent output\n")
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=seed", "-c", "user.email=seed@example.invalid",
        "-c", "commit.gpgsign=false", "commit", "-q", "-m", "agent work")
    git(repo, "checkout", "-q", "-")
    require_signing(repo, signer)
    return repo


@pytest.mark.parametrize("style", ["squash", "no-ff"])
def test_ci_r6_merge_still_tries_to_sign(merge_repo, signer, style):
    before = git(merge_repo, "rev-parse", "HEAD").stdout.strip()

    status, detail = gitops.merge(merge_repo, "agents/worker/abc",
                                  "worker: did the thing", style=style)

    assert status != "merged", \
        f"a {style} merge committed although signing is required and impossible: {detail}"
    assert signer.called(), f"the {style} merge never tried to sign"
    assert git(merge_repo, "rev-parse", "HEAD").stdout.strip() == before, \
        "the base branch moved"


def test_ci_r6_initial_commit_still_tries_to_sign(isolated_git, signer):
    repo = isolated_git / "fresh"
    repo.mkdir()
    git(repo, "init", "-q")
    require_signing(repo, signer)
    (repo / "README").write_text("hello\n")

    result = gitops.initial_commit(repo)

    assert not result.ok, f"initial_commit committed without signing: {result}"
    assert signer.called(), "initial_commit never tried to sign"


# --------------------------------------------------------------------------
# CI-R6 — git config the user passes through the environment is preserved


ENV_NAME = "Env Config Person"
PARAMS_NAME = "Params Person"


def _env_config(monkeypatch, *pairs: tuple[str, str]) -> list[str]:
    monkeypatch.setenv("GIT_CONFIG_COUNT", str(len(pairs)))
    names = ["GIT_CONFIG_COUNT"]
    for i, (key, value) in enumerate(pairs):
        monkeypatch.setenv(f"GIT_CONFIG_KEY_{i}", key)
        monkeypatch.setenv(f"GIT_CONFIG_VALUE_{i}", value)
        names += [f"GIT_CONFIG_KEY_{i}", f"GIT_CONFIG_VALUE_{i}"]
    return names


def test_ci_r6_end_of_run_commit_keeps_user_git_config_count(
        signing_project, signer, monkeypatch):
    _env_config(monkeypatch, ("user.name", ENV_NAME))
    runner, base = _start(signing_project, signer, "echo done > work.txt")

    agent_id = _run_to_end(runner)

    node = runner.tree.get(agent_id)
    assert git(signing_project, "show", f"{node.branch}:work.txt",
               check=False).returncode == 0, _result_text(runner, agent_id)
    assert author(signing_project, node.branch)[0] == ENV_NAME, \
        "the user's GIT_CONFIG_COUNT/KEY/VALUE setting was dropped"
    assert signed_commits(signing_project, node.branch, base) == []


def test_ci_r6_end_of_run_commit_keeps_user_git_config_parameters(
        signing_project, signer, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_PARAMETERS", f"'user.name'='{PARAMS_NAME}'")
    runner, base = _start(signing_project, signer, "echo done > work.txt")

    agent_id = _run_to_end(runner)

    node = runner.tree.get(agent_id)
    assert git(signing_project, "show", f"{node.branch}:work.txt",
               check=False).returncode == 0, _result_text(runner, agent_id)
    assert author(signing_project, node.branch)[0] == PARAMS_NAME, \
        "the user's GIT_CONFIG_PARAMETERS setting was dropped"
    assert signed_commits(signing_project, node.branch, base) == []


def test_ci_r6_agent_commit_keeps_user_git_config_count(signing_project, signer, monkeypatch):
    """The agent is handed the user's GIT_CONFIG_* by passthrough; a signing
    override added for it must extend that, not overwrite it."""
    names = _env_config(monkeypatch, ("user.name", ENV_NAME))
    runner, base = _start(signing_project, signer, AGENT_OWN_COMMIT, passthrough=names)

    agent_id = _run_to_end(runner)

    node = runner.tree.get(agent_id)
    assert "agent own commit" in subjects(signing_project, node.branch), \
        _result_text(runner, agent_id)
    sha = git(signing_project, "log", "-1", "--format=%H", "--grep=agent own commit",
              node.branch).stdout.strip()
    assert author(signing_project, sha)[0] == ENV_NAME, \
        "the user's GIT_CONFIG_COUNT/KEY/VALUE did not reach the agent's own commit"
    assert signed_commits(signing_project, node.branch, base) == []


def test_ci_r6_agent_commit_keeps_user_git_config_count_with_several_entries(
        signing_project, signer, monkeypatch):
    """Every one of the user's entries survives, not only the first or last."""
    names = _env_config(monkeypatch,
                        ("user.name", ENV_NAME),
                        ("user.email", "env-config@example.invalid"),
                        ("core.abbrev", "12"))
    runner, base = _start(signing_project, signer, AGENT_OWN_COMMIT, passthrough=names)

    agent_id = _run_to_end(runner)

    node = runner.tree.get(agent_id)
    assert "agent own commit" in subjects(signing_project, node.branch), \
        _result_text(runner, agent_id)
    sha = git(signing_project, "log", "-1", "--format=%H", "--grep=agent own commit",
              node.branch).stdout.strip()
    assert author(signing_project, sha) == (ENV_NAME, "env-config@example.invalid")
    assert signed_commits(signing_project, node.branch, base) == []


def test_ci_r6_agent_commit_keeps_user_git_config_parameters(
        signing_project, signer, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_PARAMETERS", f"'user.name'='{PARAMS_NAME}'")
    runner, base = _start(signing_project, signer, AGENT_OWN_COMMIT,
                          passthrough=["GIT_CONFIG_PARAMETERS"])

    agent_id = _run_to_end(runner)

    node = runner.tree.get(agent_id)
    assert "agent own commit" in subjects(signing_project, node.branch), \
        _result_text(runner, agent_id)
    sha = git(signing_project, "log", "-1", "--format=%H", "--grep=agent own commit",
              node.branch).stdout.strip()
    assert author(signing_project, sha)[0] == PARAMS_NAME, \
        "the user's GIT_CONFIG_PARAMETERS did not reach the agent's own commit"
    assert signed_commits(signing_project, node.branch, base) == []


def test_ci_r6_user_env_asking_for_signing_does_not_sign_agent_commits(
        signing_project, signer, monkeypatch):
    """"Never GPG-signed" is absolute: even when the user's own GIT_CONFIG_*
    says commit.gpgsign=true, the override added to it wins for agent commits,
    and the user's other entries still apply."""
    names = _env_config(monkeypatch,
                        ("user.name", ENV_NAME),
                        ("commit.gpgsign", "true"))
    runner, base = _start(signing_project, signer, AGENT_OWN_COMMIT, passthrough=names)

    agent_id = _run_to_end(runner)

    node = runner.tree.get(agent_id)
    assert "agent own commit" in subjects(signing_project, node.branch), \
        _result_text(runner, agent_id)
    assert signed_commits(signing_project, node.branch, base) == []
    sha = git(signing_project, "log", "-1", "--format=%H", "--grep=agent own commit",
              node.branch).stdout.strip()
    assert author(signing_project, sha)[0] == ENV_NAME
