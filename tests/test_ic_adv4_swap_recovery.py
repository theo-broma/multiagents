"""IC-C12/P7/K16: settle every durable swap boundary repeatedly."""
from contextlib import contextmanager
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
    alias = paths.worktree("alias-p7")
    alias.mkdir(parents=True)
    (alias / "old.txt").write_text("old repository")
    staged = alias.with_name(".seed-attempt-p7")
    staged.mkdir()
    (staged / "new.txt").write_text("new repository")
    attempt = {"attempt_id": "attempt-p7", "alias_id": "alias-p7"}
    with Store(root).transaction() as db:
        sessions.save_alias(db, {"id": "alias-p7", "worktree": str(alias), "home_id": "alias-p7"})
    sessions.swap_intent(paths, attempt, alias, staged, complete=True)
    home = paths.home("alias-p7")
    home.mkdir(parents=True)
    (home / "conversation").write_text("preserved")
    return paths, alias, staged, attempt, home


@pytest.mark.parametrize("step", ["old-renamed", "new-renamed", "done", "archived"])
def test_p7_k16_repeated_recovery_after_each_completed_swap_step(tmp_path, monkeypatch, step):
    paths, alias, staged, attempt, home = setup(tmp_path)
    replace, move, save = sessions.os.replace, sessions.shutil.move, sessions.save_alias
    dead = False

    def crash():
        nonlocal dead
        dead = True
        raise Crash()

    def replacing(src, dst, *args, **kwargs):
        if dead:
            raise Crash()
        answer = replace(src, dst, *args, **kwargs)
        if (step == "old-renamed" and Path(dst).name == ".previous-attempt-p7"
                or step == "new-renamed" and Path(src) == staged):
            crash()
        return answer

    def saving(db, binding):
        answer = save(db, binding)
        # Crash after the transaction commits is represented by the next
        # archive operation below, so done is durable rather than rolled back.
        return answer

    def moving(src, dst, *args, **kwargs):
        if step == "done":
            crash()
        answer = move(src, dst, *args, **kwargs)
        if step == "archived":
            crash()
        return answer

    with monkeypatch.context() as patch:
        patch.setattr(sessions.os, "replace", replacing)
        patch.setattr(sessions.shutil, "move", moving)
        patch.setattr(sessions, "save_alias", saving)
        with pytest.raises(Crash):
            sessions.install_checkout(paths, Authority(), alias, staged, attempt)
    for _ in range(3):
        sessions.settle_checkout(paths, Authority(), alias, attempt)
        assert sorted(p.name for p in alias.iterdir()) == ["new.txt"]
        archive = paths.scheduler / "alias-recovery" / attempt["attempt_id"]
        assert (archive / "old.txt").read_text() == "old repository"
        assert (home / "conversation").read_text() == "preserved"
        assert sessions.checkout_swap(paths, attempt)["state"] == "done"


def test_p7_an_existing_archive_cannot_turn_an_unperformed_swap_into_done(tmp_path):
    paths, alias, staged, attempt, home = setup(tmp_path)
    archive = paths.scheduler / "alias-recovery" / attempt["attempt_id"]
    archive.mkdir(parents=True)
    (archive / "prior-evidence").write_text("must not be overwritten")
    try:
        sessions.install_checkout(paths, Authority(), alias, staged, attempt)
    except Exception:
        # Holding on an ambiguous collision is permitted by K6.
        assert (archive / "prior-evidence").read_text() == "must not be overwritten"
        assert (alias / "old.txt").read_text() == "old repository"
        return
    assert (archive / "prior-evidence").read_text() == "must not be overwritten"
    assert (alias / "new.txt").exists(), "the staged clone was discarded and the old checkout marked done"
