"""IC-C11/P4: an unreachable delta base must not defeat inflation bounds."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import zlib

SRC = Path(__file__).resolve().parents[1] / "src"
BASE_SIZE = 300_000_000


def git(repo, *args, data=None):
    return subprocess.run(["git", "-C", str(repo), *args], input=data,
                          capture_output=True, check=True).stdout.strip().decode()


def pack_header(kind, size):
    out = bytearray([(kind << 4) | (size & 15)])
    size >>= 4
    while size:
        out[-1] |= 128
        out.append(size & 127)
        size >>= 7
    return bytes(out)


def delta_size(size):
    out = bytearray()
    while size >= 128:
        out.append((size & 127) | 128)
        size >>= 7
    return bytes(out + bytes([size]))


PROBE = r'''
import json, resource, sys
from pathlib import Path
from types import SimpleNamespace
from multiagents.scheduler.import_validation import import_isolated
from multiagents import gitops
host, att, ws, commit, parent = sys.argv[1:]
config = SimpleNamespace(project={"isolation": {"import": {
    "max_bytes": 5_000_000, "max_inflated_bytes": 1_000_000, "timeout_seconds": 60}}})
try:
    import_isolated(Path(att), Path(ws), Path(host), commit, parent, config)
    verdict = "accepted"
except gitops.GitError as exc:
    verdict = str(exc)
print(json.dumps({"verdict": verdict,
    "peak_kib": resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss}))
'''


def test_ic_c11_p4_an_unreachable_packed_delta_base_cannot_exceed_the_inflation_bound(tmp_path, monkeypatch):
    for key, value in {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
                       "GIT_AUTHOR_NAME": "a", "GIT_AUTHOR_EMAIL": "a@b",
                       "GIT_COMMITTER_NAME": "a", "GIT_COMMITTER_EMAIL": "a@b"}.items():
        monkeypatch.setenv(key, value)
    host, att, ws = (tmp_path / name for name in ("host", "attempt", "workspace"))
    host.mkdir()
    git(host, "init", "-q")
    git(host, "commit", "-qm", "base", "--allow-empty")
    parent = git(host, "rev-parse", "HEAD")
    git(tmp_path, "clone", "-q", "--no-local", str(host), str(att))
    # The one-byte reachable blob is a delta against an unreachable 300 MB
    # blob. Both fit in a sub-megabyte pack. No network or external paths.
    raw = b"PACK" + struct.pack(">II", 2, 2) + pack_header(3, BASE_SIZE)
    digest = hashlib.sha1(b"blob %d\0" % BASE_SIZE)
    compressor = zlib.compressobj(9)
    block = b"z" * 1_000_000
    for _ in range(BASE_SIZE // len(block)):
        digest.update(block)
        raw += compressor.compress(block)
    raw += compressor.flush()
    delta = delta_size(BASE_SIZE) + delta_size(1) + b"\x01x"
    raw += pack_header(7, len(delta)) + digest.digest() + zlib.compress(delta)
    raw += hashlib.sha1(raw).digest()
    git(att, "index-pack", "--stdin", data=raw)
    blob = hashlib.sha1(b"blob 1\0x").hexdigest()
    tree = git(att, "hash-object", "-w", "-t", "tree", "--stdin",
               data=b"100644 x\0" + bytes.fromhex(blob))
    body = (f"tree {tree}\nparent {parent}\nauthor a <a@b> 0 +0000\n"
            "committer a <a@b> 0 +0000\n\nresult\n").encode()
    commit = git(att, "hash-object", "-w", "-t", "commit", "--stdin", data=body)
    ws.mkdir()
    env = dict(os.environ, PYTHONPATH=str(SRC))
    probe = subprocess.run([sys.executable, "-c", PROBE, str(host), str(att), str(ws), commit, parent],
                           capture_output=True, text=True, env=env, timeout=90)
    assert probe.returncode == 0, probe.stderr
    report = json.loads(probe.stdout)
    assert report["peak_kib"] < 64 * 1024, report
