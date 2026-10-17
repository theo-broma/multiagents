"""Adversary, UD slice 2: TOCTOU between the containment check and the read
of a path ref (UD-C9).

decisions._resolve() resolves the path, checks it is inside the root and is a
regular file, and returns the resolved Path; _bind_refs() then reopens that
path by name with a plain open(), which follows symlinks. A file swapped for a
symlink in that window is read through it: the snapshot holds bytes from
OUTSIDE the project root and the ref records their digest, which UD-C9 ("must
resolve, after symlinks, inside the project root") forbids.

The window is made deterministic by swapping the file at the moment the
containment check calls Path.is_file() on the resolved target.

LOW: needs a concurrent writer in the project root at the instant of the ask.
The user would then be shown, and approve, bytes that are not the draft.
"""
import os
import pathlib

from support.ud2_support import ask, find, sha, world  # noqa: F401


def _swap_on_check(monkeypatch, victim: pathlib.Path, outside: pathlib.Path,
                   replacement=None):
    real = pathlib.Path.is_file
    fired = []

    def is_file(self):
        result = real(self)
        if not fired and os.path.abspath(self) == os.path.abspath(victim):
            fired.append(True)
            victim.unlink()
            if replacement is None:
                victim.symlink_to(outside)
            else:
                replacement(victim)
        return result
    monkeypatch.setattr(pathlib.Path, "is_file", is_file)
    return fired


def test_hole_symlink_swapped_in_after_the_check_snapshots_an_outside_file(world, monkeypatch,
                                                                            tmp_path):
    secret = tmp_path / "outside-secret"
    secret.write_bytes(b"outside the project root\n")
    draft = world.write("draft.md", "draft v1\n")
    fired = _swap_on_check(monkeypatch, draft.resolve(), secret)
    out = ask(refs=[{"path": "draft.md"}])
    monkeypatch.undo()
    assert fired, "the containment check was not reached"
    stored = [p.read_bytes() for p in world.snapshots.glob("*")] if world.snapshots.exists() else []
    assert b"outside the project root\n" not in stored, "an outside file was snapshotted"
    refs = find(out, "refs") or []
    assert sha(b"outside the project root\n") not in [r.get("sha256") for r in refs], out
