"""IC-C11/P4/P5/P9: object types, graph bounds and ambient Git authority."""
from pathlib import Path
from types import SimpleNamespace
import subprocess
import random

import pytest

from multiagents import gitops
from multiagents.scheduler.import_validation import ImportValidation, import_isolated


def git(repo, *args, data=None):
    return subprocess.run(["git", "-C", str(repo), *args], input=data,
                          capture_output=True, check=True).stdout.strip().decode()


@pytest.fixture
def repos(tmp_path, monkeypatch):
    for name, value in {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
                        "GIT_AUTHOR_NAME": "a", "GIT_AUTHOR_EMAIL": "a@b",
                        "GIT_COMMITTER_NAME": "a", "GIT_COMMITTER_EMAIL": "a@b"}.items():
        monkeypatch.setenv(name, value)
    host, attempt, workspace = (tmp_path / name for name in ("host", "attempt", "workspace"))
    host.mkdir()
    git(host, "init", "-q")
    git(host, "commit", "-qm", "input", "--allow-empty")
    parent = git(host, "rev-parse", "HEAD")
    git(tmp_path, "clone", "-q", "--no-local", str(host), str(attempt))
    workspace.mkdir()
    return host, attempt, workspace, parent


def config(inflated=1_000_000, compressed=5_000_000):
    return SimpleNamespace(project={"isolation": {"import": {
        "max_bytes": compressed, "max_inflated_bytes": inflated, "timeout_seconds": 30}}})


def snapshot(host):
    return {str(p.relative_to(host)): p.read_bytes() for p in (host / ".git" / "objects").rglob("*")
            if p.is_file()}


def commit(attempt, tree, parent, message="result"):
    return git(attempt, "hash-object", "-w", "-t", "commit", "--stdin", data=(
        f"tree {tree}\nparent {parent}\nauthor a <a@b> 0 +0000\n"
        f"committer a <a@b> 0 +0000\n\n{message}\n").encode())


@pytest.mark.parametrize("kind", ["tag", "nested-tag", "tree", "blob"])
@pytest.mark.parametrize("packed", [False, True])
def test_ic_c11_p5_noncommit_tips_are_refused_before_host_writes(repos, kind, packed):
    host, attempt, workspace, parent = repos
    git(attempt, "tag", "-a", "-m", "tag", "first", parent)
    git(attempt, "tag", "-a", "-m", "nested", "second", "first")
    tips = {"tag": git(attempt, "rev-parse", "first"),
            "nested-tag": git(attempt, "rev-parse", "second"),
            "tree": git(attempt, "rev-parse", "HEAD^{tree}"),
            "blob": git(attempt, "hash-object", "-w", "--stdin", data=b"unpublished blob")}
    tip = tips[kind]
    if packed:
        git(attempt, "update-ref", "refs/result-probe", tip)
        git(attempt, "repack", "-ad")
        git(attempt, "prune-packed")
        assert not (attempt / ".git" / "objects" / tip[:2] / tip[2:]).exists()
    before = snapshot(host)
    with pytest.raises(gitops.GitError, match="import_malformed"):
        import_isolated(attempt, workspace, host, tip, parent, config())
    assert snapshot(host) == before


@pytest.mark.parametrize("shape", ["tree", "history", "small-objects"])
def test_ic_c11_p4_inflation_is_aggregated_over_the_whole_graph(repos, shape):
    host, attempt, workspace, parent = repos
    tree = git(attempt, "rev-parse", "HEAD^{tree}")
    if shape == "history":
        tip = parent
        for i in range(180):
            tip = commit(attempt, tree, tip, str(i))
    else:
        entries = bytearray()
        for i in range(1400):
            blob = git(attempt, "hash-object", "-w", "--stdin", data=f"{i}:".encode() + b"x" * 30)
            entries.extend(b"100644 " + f"f{i:05}".encode() + b"\0" + bytes.fromhex(blob))
        tree = git(attempt, "hash-object", "-w", "-t", "tree", "--stdin", data=bytes(entries))
        tip = commit(attempt, tree, parent)
    before = snapshot(host)
    with pytest.raises(gitops.GitError, match="import_bounds"):
        # The many-objects case admits the tree itself, then crosses the
        # cumulative bound while visiting the separately queued blobs.
        limit = 70_000 if shape == "small-objects" else 20_000
        import_isolated(attempt, workspace, host, tip, parent, config(inflated=limit))
    assert snapshot(host) == before


def test_ic_c11_p9_git_config_environment_cannot_redirect_the_object_store(repos, monkeypatch, tmp_path):
    host, attempt, workspace, parent = repos
    tree = git(attempt, "rev-parse", "HEAD^{tree}")
    tip = commit(attempt, tree, parent)
    hostile = tmp_path / "ambient.config"
    hostile.write_text("[extensions]\nobjectFormat=sha256\n[core]\nrepositoryFormatVersion=1\n")
    with monkeypatch.context() as ambient:
        ambient.setenv("GIT_CONFIG", str(hostile))
        ambient.setenv("GIT_CONFIG_COUNT", "1")
        ambient.setenv("GIT_CONFIG_KEY_0", "core.abbrev")
        ambient.setenv("GIT_CONFIG_VALUE_0", "invalid-number")
        import_isolated(attempt, workspace, host, tip, parent, config())
    assert git(host, "cat-file", "-t", tip) == "commit"


def test_ic_c11_p4_an_expired_deadline_never_yields_a_positive_budget(monkeypatch):
    validation = ImportValidation(config())
    monkeypatch.setattr(validation, "deadline", 0)
    with pytest.raises(gitops.GitError, match="import_bounds"):
        validation.remaining()


def test_ic_c11_p4_the_final_pack_must_fit_even_when_all_source_files_fit(repos):
    host, attempt, workspace, parent = repos
    git(attempt, "config", "core.looseCompression", "9")
    rng = random.Random(1453)
    words = [f"word-{i:04d}".encode() for i in range(1000)]
    body = b" ".join(rng.choice(words) for _ in range(100_000))
    blob = git(attempt, "hash-object", "-w", "--stdin", data=body)
    tree = git(attempt, "hash-object", "-w", "-t", "tree", "--stdin",
               data=b"100644 data\0" + bytes.fromhex(blob))
    tip = commit(attempt, tree, parent)
    objects = attempt / ".git" / "objects"
    bound = sum(p.stat().st_size for p in objects.rglob("*") if p.is_file())
    pack = subprocess.run(["git", "-C", str(attempt), "pack-objects", "--stdout", "--revs"],
                          input=(tip + "\n").encode(), capture_output=True, check=True).stdout
    assert len(pack) > bound, (len(pack), bound)
    before = snapshot(host)
    with pytest.raises(gitops.GitError, match="import_bounds"):
        import_isolated(attempt, workspace, host, tip, parent, config(inflated=2_000_000, compressed=bound))
    assert snapshot(host) == before
