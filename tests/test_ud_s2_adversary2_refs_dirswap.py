"""Adversary, UD slice 2, round 2: ref reads are still not race-free through an
intermediate directory (UD-C9; "Slice 2 clarifications, round 2": opened once
with O_NOFOLLOW, checked through fstat on the open descriptor, inside the root).

decisions._read() opens the path _resolve() already resolved. O_NOFOLLOW guards
only the last component, so a directory on that path that is swapped for a
symlink between _resolve() and os.open() is followed, and the file opened lies
outside the root. The containment check then resolves the directory path by
NAME, not the descriptor; a directory swapped back before that check passes it,
and the outside bytes are hashed and snapshotted.

The race is made deterministic by doing the attacker's two renames around the
os.open() call on the ref.

LOW: needs a concurrent writer in the project root at the instant of the ask
(or of check_refs). The user is then shown, and approves, bytes from outside the
project, or a changed draft is reported as unchanged.
"""
import os

from support.ud2_support import ask, find, sha, world  # noqa: F401
from support.ud_support import D


def _swap_dir_around_open(monkeypatch, sub, outside, name):
    """At the os.open() of `name`, `sub` is a symlink to `outside`; before and
    after, it is the real directory again."""
    real_open, fired = os.open, []
    parked = sub.with_name(sub.name + ".real")

    def opener(path, flags, *args, **kwargs):
        if fired or not os.fspath(path).endswith(name):
            return real_open(path, flags, *args, **kwargs)
        fired.append(True)
        os.rename(sub, parked)
        os.symlink(outside, sub)
        try:
            return real_open(path, flags, *args, **kwargs)
        finally:
            os.unlink(sub)
            os.rename(parked, sub)
    monkeypatch.setattr(os, "open", opener)
    return fired


def test_hole_directory_swapped_for_a_symlink_at_open_snapshots_an_outside_file(
        world, monkeypatch, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "draft.md").write_bytes(b"outside the project root\n")
    world.write("sub/draft.md", "draft v1\n")
    fired = _swap_dir_around_open(monkeypatch, world.root / "sub", outside, "draft.md")
    out = ask(refs=[{"path": "sub/draft.md"}])
    monkeypatch.undo()
    assert fired, "the ref was never opened"
    stored = [p.read_bytes() for p in world.snapshots.glob("*")] if world.snapshots.exists() else []
    assert b"outside the project root\n" not in stored, "an outside file was snapshotted"
    refs = find(out, "refs") or []
    assert sha(b"outside the project root\n") not in [r.get("sha256") for r in refs], out


def test_hole_directory_swap_at_open_makes_check_refs_report_a_changed_draft_unchanged(
        world, monkeypatch, tmp_path):
    world.write("sub/draft.md", "draft v1\n")
    out = ask(refs=[{"path": "sub/draft.md"}])
    assert "error" not in out, out
    world.write("sub/draft.md", "draft v2, not what was asked about\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "draft.md").write_bytes(b"draft v1\n")
    fired = _swap_dir_around_open(monkeypatch, world.root / "sub", outside, "draft.md")
    result = D.check_refs(world.tree, out["id"])
    monkeypatch.undo()
    assert fired, "the ref was never opened"
    assert result["ok"] is False, result
