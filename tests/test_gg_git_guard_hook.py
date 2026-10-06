"""GG-R4: `git-guard install` / `uninstall` and the pre-push hook.

Real `git push` against a local bare remote. A refused push leaves the remote's
refs exactly as they were.
"""
from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))
import gg_world as gw  # noqa: E402
from gg_world import World, git, said  # noqa: E402

EMAIL = gw.email()
TOKENS = gw.tokens()

DIRTY = {
    "email": ("a.txt", "m " + EMAIL + "\n"),
    "tailnet-ip": ("a.txt", "ssh " + gw.tailnet_ip() + "\n"),
    "tailnet-host": ("a.txt", "host " + gw.tailnet_host() + "\n"),
    "private-key": ("a.pem", gw.private_key_block() + "\n"),
    "token": ("a.txt", "k = " + TOKENS["ghp"] + "\n"),
}


@pytest.fixture
def w(tmp_path):
    world = World(tmp_path)
    p = world.guard("install")
    assert p.returncode == 0, said(p)
    return world


def secrets_of(name):
    return {"email": [EMAIL], "tailnet-ip": [gw.tailnet_ip()],
            "tailnet-host": [gw.tailnet_host()], "private-key": ["MIIBplaceholder"],
            "token": [TOKENS["ghp"]]}.get(name, [])


# ------------------------------------------------------------- install -------
def test_gg_r4_install_writes_an_executable_pre_push_hook(tmp_path):
    w = World(tmp_path)
    assert not w.hook_path().exists()
    p = w.guard("install")
    assert p.returncode == 0, said(p)
    assert w.hook_path().is_file()
    assert w.hook_path().stat().st_mode & stat.S_IXUSR


def test_gg_r4_install_is_idempotent(tmp_path):
    w = World(tmp_path)
    assert w.guard("install").returncode == 0
    first = w.hook_path().read_bytes()
    p = w.guard("install")
    assert p.returncode == 0, said(p)
    assert w.hook_path().read_bytes() == first
    assert not (w.hook_path().parent / "pre-push.local").exists()
    # and the hook still guards
    w.commit_file(*DIRTY["email"])
    assert w.push("origin", "main").returncode != 0


def test_gg_r4_install_outside_a_repository_exits_2(tmp_path):
    import subprocess
    w = World(tmp_path)
    assert w.guard("install").returncode == 0           # control
    plain = tmp_path / "plain"
    plain.mkdir()
    p = subprocess.run([sys.executable, "-m", "multiagents.cli", "git-guard", "install"],
                       cwd=str(plain), env=w.env, capture_output=True, text=True, timeout=60)
    assert p.returncode == 2, said(p)
    assert "invalid choice" not in said(p)


# --------------------------------------------------------------- pushes ------
def test_gg_r4_a_clean_push_succeeds(w):
    sha = w.commit_file("ok.txt", "fine\n")
    p = w.push("origin", "main")
    assert p.returncode == 0, said(p)
    assert w.remote_refs()["refs/heads/main"] == sha


@pytest.mark.parametrize("category", sorted(DIRTY))
def test_gg_r4_a_push_carrying_each_category_is_refused_and_the_remote_unchanged(w, category):
    before = w.remote_refs()
    name, body = DIRTY[category]
    w.commit_file(name, body)
    p = w.push("origin", "main")
    assert p.returncode != 0, said(p)
    assert w.remote_refs() == before
    for secret in secrets_of(category):
        assert secret not in said(p), "unmasked match in the refusal"


def test_gg_r4_a_private_pattern_refuses_the_push(tmp_path):
    word = "quuxword"
    w = World(tmp_path, patterns=word + "\n")
    assert w.guard("install").returncode == 0
    before = w.remote_refs()
    w.commit_file("a.txt", "talk of " + word + "\n")
    p = w.push("origin", "main")
    assert p.returncode != 0 and w.remote_refs() == before, said(p)
    assert word not in said(p)


def test_gg_r4_a_disallowed_coauthor_trailer_refuses_the_push(tmp_path):
    w = World(tmp_path, coauthor=False)
    assert w.guard("install").returncode == 0
    before = w.remote_refs()
    w.commit("work\n\nCo-Authored-By: Claude <noreply@anthropic.com>", add=False)
    assert w.push("origin", "main").returncode != 0
    assert w.remote_refs() == before


def test_gg_r4_an_enabled_coauthor_trailer_does_not_refuse(tmp_path):
    w = World(tmp_path, coauthor=True)
    assert w.guard("install").returncode == 0
    sha = w.commit("work\n\nCo-Authored-By: Claude <noreply@anthropic.com>", add=False)
    p = w.push("origin", "main")
    assert p.returncode == 0, said(p)
    assert w.remote_refs()["refs/heads/main"] == sha


def test_gg_r4_a_dirty_message_or_author_refuses_the_push(w):
    before = w.remote_refs()
    w.commit("by " + EMAIL, add=False)
    assert w.push("origin", "main").returncode != 0
    assert w.remote_refs() == before


def test_gg_r4_only_the_commits_the_push_would_add_are_scanned(w):
    """A dirty commit already on the remote does not block later clean ones."""
    git(w.root, "push", "-q", "--no-verify", "origin", "main", env=w.env)
    w.commit_file(*DIRTY["email"])
    git(w.root, "push", "-q", "--no-verify", "origin", "main", env=w.env)
    sha = w.commit_file("later.txt", "fine\n")
    p = w.push("origin", "main")
    assert p.returncode == 0, said(p)
    assert w.remote_refs()["refs/heads/main"] == sha


def test_gg_r4_a_dirty_commit_below_a_clean_tip_still_refuses(w):
    before = w.remote_refs()
    w.commit_file(*DIRTY["email"])
    w.commit_file("later.txt", "fine\n")
    assert w.push("origin", "main").returncode != 0
    assert w.remote_refs() == before


def test_gg_r4_a_new_remote_branch_scans_what_no_remote_ref_reaches(w):
    w.branch("feature")
    w.commit_file("ok.txt", "fine\n")
    sha = w.commit_file("ok2.txt", "fine\n")
    p = w.push("-u", "origin", "feature")
    assert p.returncode == 0, said(p)
    assert w.remote_refs()["refs/heads/feature"] == sha


def test_gg_r4_a_new_remote_branch_carrying_a_dirty_commit_is_refused(w):
    before = w.remote_refs()
    w.branch("feature")
    w.commit_file(*DIRTY["token"])
    w.commit_file("ok.txt", "fine\n")
    p = w.push("-u", "origin", "feature")
    assert p.returncode != 0, said(p)
    assert w.remote_refs() == before
    assert "refs/heads/feature" not in w.remote_refs()


def test_gg_r4_a_new_branch_off_dirty_history_the_remote_already_holds_is_allowed(w):
    git(w.root, "push", "-q", "--no-verify", "origin", "main", env=w.env)
    w.branch("other")
    w.commit_file(*DIRTY["email"])
    git(w.root, "push", "-q", "--no-verify", "origin", "other", env=w.env)
    git(w.root, "fetch", "-q", "origin", env=w.env)          # origin/other now tracks it
    w.branch("child", "other")
    sha = w.commit_file("child.txt", "fine\n")
    p = w.push("origin", "child")
    assert p.returncode == 0, said(p)
    assert w.remote_refs()["refs/heads/child"] == sha


def test_gg_r4_one_dirty_ref_in_a_multi_ref_push_refuses_the_whole_push(w):
    before = w.remote_refs()
    w.commit_file("ok.txt", "fine\n")
    w.branch("feature")
    w.commit_file(*DIRTY["email"])
    p = w.push("origin", "main", "feature")
    assert p.returncode != 0, said(p)
    assert w.remote_refs() == before


def test_gg_r4_deleting_a_remote_branch_is_not_blocked(w):
    w.branch("feature")
    w.commit_file("ok.txt", "fine\n")
    assert w.push("origin", "feature").returncode == 0
    assert "refs/heads/feature" in w.remote_refs()
    p = w.push("origin", ":feature")
    assert p.returncode == 0, said(p)
    assert "refs/heads/feature" not in w.remote_refs()


def test_gg_r4_a_tag_on_a_dirty_commit_is_refused(w):
    before = w.remote_refs()
    w.commit_file(*DIRTY["email"])
    git(w.root, "tag", "v1", env=w.env)
    p = w.push("origin", "v1")
    assert p.returncode != 0, said(p)
    assert w.remote_refs() == before


def test_gg_r4_a_failing_scan_refuses_the_push(w):
    """Fail closed: a patterns file readable by others makes the scan fail."""
    sha = w.commit_file("ok.txt", "fine\n")
    w.patterns.parent.mkdir(exist_ok=True)
    w.patterns.write_text("whatever\n")
    os.chmod(w.patterns, 0o644)
    w.write_config({"git": {"remote": "origin", "base_branch": "main",
                            "guard": {"patterns_file": str(w.patterns)}}})
    before = w.remote_refs()
    p = w.push("origin", "main")
    assert p.returncode != 0, said(p)
    assert w.remote_refs() == before
    os.chmod(w.patterns, 0o600)                       # control: fixed, the push goes
    assert w.push("origin", "main").returncode == 0
    assert w.remote_refs()["refs/heads/main"] == sha


def test_gg_r4_a_missing_patterns_file_does_not_block_a_clean_push(w):
    assert not w.patterns.exists()
    sha = w.commit_file("ok.txt", "fine\n")
    assert w.push("origin", "main").returncode == 0
    assert w.remote_refs()["refs/heads/main"] == sha


# ----------------------------------------------------- foreign hooks ---------
def foreign(w: World, body: str = "exit 0\n", marker: str = "foreign-hook") -> bytes:
    hooks = w.hook_path().parent
    hooks.mkdir(exist_ok=True)
    seen = w.base / (marker + ".seen")
    text = (f"#!/bin/sh\n# {marker}\necho \"$@\" > '{seen}'\ncat >> '{seen}'\n" + body).encode()
    w.hook_path().write_bytes(text)
    os.chmod(w.hook_path(), 0o755)
    return text


def test_gg_r4_install_refuses_to_replace_a_hook_it_did_not_write(tmp_path):
    w = World(tmp_path)
    original = foreign(w)
    p = w.guard("install")
    assert p.returncode != 0, said(p)
    assert "invalid choice" not in said(p)
    assert w.hook_path().read_bytes() == original
    assert not (w.hook_path().parent / "pre-push.local").exists()


def test_gg_r4_force_keeps_the_old_hook_as_pre_push_local_and_chains_to_it(tmp_path):
    w = World(tmp_path)
    original = foreign(w)
    p = w.guard("install", "--force")
    assert p.returncode == 0, said(p)
    local = w.hook_path().parent / "pre-push.local"
    assert local.read_bytes() == original
    assert local.stat().st_mode & stat.S_IXUSR
    assert w.hook_path().read_bytes() != original
    sha = w.commit_file("ok.txt", "fine\n")
    p = w.push("origin", "main")
    assert p.returncode == 0, said(p)
    assert w.remote_refs()["refs/heads/main"] == sha
    seen = (w.base / "foreign-hook.seen").read_text()
    assert "origin" in seen                                # git's arguments reach it
    assert "refs/heads/main" in seen                       # and its stdin


def test_gg_r4_a_chained_hook_that_fails_refuses_the_push(tmp_path):
    w = World(tmp_path)
    foreign(w, "exit 1\n")
    assert w.guard("install", "--force").returncode == 0
    before = w.remote_refs()
    w.commit_file("ok.txt", "fine\n")
    assert w.push("origin", "main").returncode != 0
    assert w.remote_refs() == before


def test_gg_r4_the_guard_still_refuses_when_chained_to_a_passing_hook(tmp_path):
    w = World(tmp_path)
    foreign(w)
    assert w.guard("install", "--force").returncode == 0
    before = w.remote_refs()
    w.commit_file(*DIRTY["email"])
    assert w.push("origin", "main").returncode != 0
    assert w.remote_refs() == before


def test_gg_r4_force_twice_does_not_overwrite_pre_push_local_with_its_own_hook(tmp_path):
    w = World(tmp_path)
    original = foreign(w)
    assert w.guard("install", "--force").returncode == 0
    assert w.guard("install", "--force").returncode == 0
    assert (w.hook_path().parent / "pre-push.local").read_bytes() == original


def test_gg_r4_force_with_no_existing_hook_just_installs(tmp_path):
    w = World(tmp_path)
    p = w.guard("install", "--force")
    assert p.returncode == 0, said(p)
    w.commit_file(*DIRTY["email"])
    assert w.push("origin", "main").returncode != 0


# ------------------------------------------------------------ uninstall ------
def test_gg_r4_uninstall_removes_the_hook_it_wrote(w):
    p = w.guard("uninstall")
    assert p.returncode == 0, said(p)
    assert not w.hook_path().exists()
    w.commit_file(*DIRTY["email"])
    assert w.push("origin", "main").returncode == 0      # no longer guarded


def test_gg_r4_uninstall_leaves_a_hook_it_did_not_write(tmp_path):
    w = World(tmp_path)
    original = foreign(w)
    w.guard("uninstall")
    assert w.hook_path().read_bytes() == original
    assert w.guard("scan").returncode == 0               # the guard itself exists


def test_gg_r4_uninstall_then_install_again_works(w):
    assert w.guard("uninstall").returncode == 0
    assert w.guard("install").returncode == 0
    w.commit_file(*DIRTY["email"])
    assert w.push("origin", "main").returncode != 0
