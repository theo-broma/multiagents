"""What a Git repository looks like from the inside (IC-C10, IC-C12, IC-C13).

Run as a program (`python ic_report.py`) with the repository as the working
directory; prints ONE json line `IC-REPORT {...}`. It is delivered the way any
guest command is: as an argv, run by a backend invocation or by the fixture
provider. It only needs python3 and git.

Keys: cwd, home, tmp, git_is_dir, alternates (text or null), commondir,
head, head_ref, objects (every object id in the repository, alternates
included), local_object_count (loose + packed objects of the repository's OWN
store), refs ({name: oid}), reflog_oids, reflog_files, config (text),
remotes, worktrees, status, tracked, fsck_ok, env, home_files, git_files
(names directly under .git).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys


def git(*args):
    proc = subprocess.run(["git", *args], capture_output=True, text=True)
    return proc.returncode, proc.stdout, proc.stderr


def lines(*args):
    rc, out, _ = git(*args)
    return [line for line in out.splitlines() if line.strip()] if rc == 0 else None


def text(path):
    try:
        with open(path) as handle:
            return handle.read()
    except OSError:
        return None


def report() -> dict:
    cwd = os.getcwd()
    dotgit = os.path.join(cwd, ".git")
    rep = {"cwd": cwd, "home": os.environ.get("HOME"), "tmp": os.environ.get("TMPDIR")}
    rep["git_is_dir"] = os.path.isdir(dotgit) and not os.path.islink(dotgit)
    rep["alternates"] = text(os.path.join(dotgit, "objects", "info", "alternates"))
    rep["commondir"] = os.path.exists(os.path.join(dotgit, "commondir"))
    rep["head"] = (lines("rev-parse", "HEAD") or [None])[0]
    rep["head_ref"] = (lines("symbolic-ref", "-q", "HEAD") or [None])[0]
    objects = lines("cat-file", "--batch-all-objects", "--batch-check=%(objectname)")
    rep["objects"] = sorted(objects or [])
    counts = {}
    for line in lines("count-objects", "-v") or []:
        key, _, value = line.partition(":")
        counts[key.strip()] = value.strip()
    rep["local_object_count"] = int(counts.get("count", 0)) + int(counts.get("in-pack", 0))
    refs = {}
    for line in lines("for-each-ref", "--format=%(refname) %(objectname)") or []:
        name, oid = line.split()
        refs[name] = oid
    rep["refs"] = refs
    oids, files = set(), []
    logs = os.path.join(dotgit, "logs")
    for base, _, names in os.walk(logs):
        for name in names:
            path = os.path.join(base, name)
            files.append(os.path.relpath(path, logs))
            for entry in (text(path) or "").splitlines():
                parts = entry.split()
                oids.update(p for p in parts[:2] if len(p) == 40 and set(p) != {"0"})
    rep["reflog_oids"] = sorted(oids)
    rep["reflog_files"] = sorted(files)
    rep["config"] = text(os.path.join(dotgit, "config")) or ""
    rep["remotes"] = lines("remote", "-v") or []
    rep["worktrees"] = lines("worktree", "list", "--porcelain") or []
    rep["status"] = lines("status", "--porcelain", "--untracked-files=all") or []
    rep["tracked"] = lines("ls-files") or []
    rep["fsck_ok"] = git("fsck", "--full", "--no-dangling")[0] == 0
    rep["env"] = dict(os.environ)
    home = os.environ.get("HOME") or ""
    rep["home_files"] = sorted(os.listdir(home)) if os.path.isdir(home) else []
    try:
        rep["git_files"] = sorted(os.listdir(dotgit))
    except OSError:
        rep["git_files"] = []
    return rep


if __name__ == "__main__":
    print("IC-REPORT " + json.dumps(report(), default=str))
    sys.stdout.flush()
