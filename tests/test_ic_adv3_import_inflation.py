"""Adversary 3 — IC-C11 / P4: the decompressed-size bound must hold *before*
the host inflates the result, not only as the eventual verdict.

The result here is one commit object whose message is 300 MB of one byte:
about 1.3 MB on disk, inside `max_bytes`, and 300x `max_inflated_bytes`.
`ImportValidation.validate` refuses it with `import_bounds`, but only after
`git rev-list --objects` has inflated the whole commit in a host process
(trees and commits are read before the size check, which only protects
blobs). With the default bounds (2 GiB compressed) the same shape asks the
host for many gigabytes from a few megabytes of attempt output.

The import runs in a child interpreter so that RUSAGE_CHILDREN measures only
the git processes the validation starts.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
INFLATED_LIMIT = 1_000_000
MESSAGE = 300_000_000

PROBE = r"""
import json, os, resource, subprocess, sys
from pathlib import Path
from types import SimpleNamespace
from multiagents import gitops
from multiagents.scheduler.import_validation import import_isolated

tmp, size, limit = Path(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3])
def g(repo, *a, **k):
    return subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True, **k).stdout.decode().strip()
host = tmp / "host"
g(tmp, "init", "-q", str(host))
(host / "a").write_text("a\n")
g(host, "add", "a")
g(host, "commit", "-qm", "base")
inp = g(host, "rev-parse", "HEAD")
att = tmp / "att"
subprocess.run(["git", "clone", "-q", "--no-local", str(host), str(att)], check=True)
tree = g(att, "rev-parse", "HEAD^{tree}")
body = (f"tree {tree}\nparent {inp}\nauthor a <a@b> 0 +0000\ncommitter a <a@b> 0 +0000\n\n".encode()
        + b"x" * size)
# Written in-process: no setup child may inflate it and pollute RUSAGE_CHILDREN.
import hashlib, zlib
raw = b"commit %d\0" % len(body) + body
commit = hashlib.sha1(raw).hexdigest()
loose = att / ".git" / "objects" / commit[:2]
loose.mkdir(exist_ok=True)
(loose / commit[2:]).write_bytes(zlib.compress(raw, 9))
del body, raw
before = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
config = SimpleNamespace(project={"isolation": {"import": {
    "max_bytes": 50_000_000, "max_inflated_bytes": limit, "timeout_seconds": 120}}})
workspace = tmp / "ws"
workspace.mkdir()
try:
    import_isolated(att, workspace, host, commit, inp, config)
    verdict = "accepted"
except gitops.GitError as exc:
    verdict = str(exc)
print(json.dumps({"verdict": verdict, "peak_kib": resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss,
                  "setup_peak_kib": before}))
"""


def test_ic_adv3_a_compressible_commit_is_refused_without_inflating_it_on_the_host(tmp_path):
    env = {**os.environ, "PYTHONPATH": str(SRC), "GIT_CONFIG_GLOBAL": "/dev/null",
           "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "a", "GIT_AUTHOR_EMAIL": "a@b",
           "GIT_COMMITTER_NAME": "a", "GIT_COMMITTER_EMAIL": "a@b"}
    out = subprocess.run([sys.executable, "-c", PROBE, str(tmp_path), str(MESSAGE), str(INFLATED_LIMIT)],
                         env=env, capture_output=True, text=True, timeout=300)
    assert out.returncode == 0, out.stderr[-2000:]
    report = json.loads(out.stdout.strip().splitlines()[-1])
    assert report["verdict"].startswith("import_bounds"), report
    # The bound is 1 MB of decompressed objects. Allow generous git overhead
    # (64 MiB), far below the 300 MB the commit inflates to.
    assert report["peak_kib"] < 64 * 1024, (
        f"host git peaked at {report['peak_kib'] // 1024} MiB to refuse a result bounded at "
        f"{INFLATED_LIMIT // 1_000_000} MB inflated")
