"""Adversary 3 — IC-C12 / P7: a crash at every step of the durable alias swap.

`sessions.install_checkout` is interrupted after each filesystem step (the
interruption also defeats its in-process rollback, as SIGKILL would), then
run again as the restart does. Whatever the step, the end state must be the
new checkout complete at the alias path, and the old one archived host-side
whole — never a mix, never lost.
"""
from __future__ import annotations

from contextlib import contextmanager
import os
import shutil
from pathlib import Path

import pytest

from multiagents.paths import ProjectPaths
from multiagents.scheduler import sessions
from multiagents.scheduler.store import Store


class Crash(BaseException):
    pass


class Authority:
    @contextmanager
    def pinned_parent(self, path):
        yield path


def setup(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    paths = ProjectPaths(root)
    paths.ensure()
    Store(root).initialize()
    alias = paths.worktree("alias-adv3")
    alias.mkdir(parents=True)
    (alias / "old.txt").write_text("old checkout\n")
    staged = alias.with_name(".seed-att-2")
    staged.mkdir()
    (staged / "new.txt").write_text("new checkout\n")
    attempt = {"attempt_id": "att-2", "alias_id": "al-1"}
    with Store(root).transaction() as db:
        sessions.save_alias(db, {"id": "al-1", "worktree": str(alias), "home_id": "alias-adv3"})
    sessions.swap_intent(paths, attempt, alias, staged)
    return paths, alias, staged, attempt


@pytest.mark.parametrize("crash_after", [0, 1, "done", "move"])
def test_ic_adv3_the_alias_swap_ends_whole_after_a_crash_at_any_step(tmp_path, monkeypatch, crash_after):
    paths, alias, staged, attempt = setup(tmp_path)
    real_replace, real_move = os.replace, shutil.move
    calls = {"n": 0, "dead": False}

    def replace(src, dst, *a, **k):
        if calls["dead"]:
            raise Crash()
        if calls["n"] == crash_after:
            calls["dead"] = True
            raise Crash()
        calls["n"] += 1
        return real_replace(src, dst, *a, **k)

    def move(src, dst, *a, **k):
        if crash_after == "move":
            calls["dead"] = True
            raise Crash()
        return real_move(src, dst, *a, **k)

    real_save = sessions.save_alias

    def save_alias(db, binding):
        if crash_after == "done" and (binding.get("swap") or {}).get("state") == "done":
            raise Crash()
        return real_save(db, binding)

    monkeypatch.setattr(sessions.os, "replace", replace)
    monkeypatch.setattr(sessions.shutil, "move", move)
    monkeypatch.setattr(sessions, "save_alias", save_alias)
    with pytest.raises(Crash):
        sessions.install_checkout(paths, Authority(), alias, staged, attempt)
    monkeypatch.setattr(sessions.os, "replace", real_replace)
    monkeypatch.setattr(sessions.shutil, "move", real_move)
    monkeypatch.setattr(sessions, "save_alias", real_save)

    # the restart: the attempt is resumed and installs again
    if sessions.checkout_swap(paths, attempt).get("state") not in ("swapping", "done"):
        pytest.fail(f"intent after the crash: {sessions.checkout_swap(paths, attempt)}")
    sessions.install_checkout(paths, Authority(), alias, staged, attempt)

    assert sorted(p.name for p in alias.iterdir()) == ["new.txt"]
    archived = paths.scheduler / "alias-recovery" / "att-2"
    assert (archived / "old.txt").read_text() == "old checkout\n"
    assert sessions.checkout_swap(paths, attempt).get("state") == "done"
    leftovers = sorted(p.name for p in alias.parent.iterdir() if p.name.startswith("."))
    assert leftovers == [], leftovers


@pytest.mark.parametrize("crash_after", [1, "done", "move"])
def test_ic_adv3_a_resumed_nonalias_isolated_install_completes_after_a_crash(tmp_path, monkeypatch, crash_after):
    """P7 resume of a non-alias seeded launch: `_install_checkout` has no
    durable intent for it, so the restart stages the clone again (a fresh
    `.seed-<attempt>`) and installs once more. A crash after the old
    checkout moved aside must still end with the new checkout installed and
    the old one archived, not a launch failure."""
    paths, alias, staged, _ = setup(tmp_path)
    attempt = {"attempt_id": "att-2"}                      # no alias: an attempt-lifetime run
    real_replace, real_move = os.replace, shutil.move
    calls = {"n": 0, "dead": False}

    def replace(src, dst, *a, **k):
        if calls["dead"]:
            raise Crash()
        if calls["n"] == crash_after:
            calls["dead"] = True
            raise Crash()
        calls["n"] += 1
        result = real_replace(src, dst, *a, **k)
        if crash_after == "done" and calls["n"] == 2:
            calls["dead"] = True
            raise Crash()
        return result

    def move(src, dst, *a, **k):
        if crash_after == "move":
            calls["dead"] = True
            raise Crash()
        return real_move(src, dst, *a, **k)

    monkeypatch.setattr(sessions.os, "replace", replace)
    monkeypatch.setattr(sessions.shutil, "move", move)
    with pytest.raises(Crash):
        sessions.install_checkout(paths, Authority(), alias, staged, attempt)
    monkeypatch.setattr(sessions.os, "replace", real_replace)
    monkeypatch.setattr(sessions.shutil, "move", real_move)

    # the restart, as IsolatedExecutor._install_checkout does it without an intent
    if staged.exists():
        shutil.rmtree(staged)
    staged.mkdir()
    (staged / "new.txt").write_text("new checkout\n")
    sessions.install_checkout(paths, Authority(), alias, staged, attempt)

    assert sorted(p.name for p in alias.iterdir()) == ["new.txt"]
    assert ((paths.scheduler / "alias-recovery" / "att-2" / "old.txt").read_text()
            == "old checkout\n")
