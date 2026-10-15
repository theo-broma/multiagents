"""IC-C11/P4: a long packed delta chain preserves reachability and bounds."""
import hashlib
import struct
import subprocess
import zlib

from multiagents.scheduler.import_validation import import_isolated

from test_ic_adv4_import_edges import repos, config, git, commit  # noqa: F401
from test_ic_adv4_pack_bounds import pack_header, delta_size


def test_ic_c11_p4_a_120_link_delta_chain_imports_only_the_reachable_object(repos):
    host, attempt, workspace, parent = repos
    base = b"initial"
    oid = hashlib.sha1(b"blob 7\0" + base).digest()
    ancestors = [oid.hex()]
    raw = b"PACK" + struct.pack(">II", 2, 121) + pack_header(3, len(base)) + zlib.compress(base)
    previous_size = len(base)
    for i in range(120):
        value = i.to_bytes(2, "big") + b"data"
        delta = delta_size(previous_size) + delta_size(len(value)) + bytes([len(value)]) + value
        raw += pack_header(7, len(delta)) + oid + zlib.compress(delta)
        oid = hashlib.sha1(b"blob 6\0" + value).digest()
        ancestors.append(oid.hex())
        previous_size = len(value)
    raw += hashlib.sha1(raw).digest()
    git(attempt, "index-pack", "--stdin", data=raw)
    tree = git(attempt, "hash-object", "-w", "-t", "tree", "--stdin",
               data=b"100644 data\0" + oid)
    tip = commit(attempt, tree, parent)
    import_isolated(attempt, workspace, host, tip, parent, config(inflated=20_000))
    assert git(host, "cat-file", "blob", oid.hex()) == value.decode()
    for unreachable in (ancestors[0], ancestors[50], ancestors[-2]):
        assert subprocess.run(["git", "-C", str(host), "cat-file", "-e", unreachable],
                              capture_output=True).returncode != 0
