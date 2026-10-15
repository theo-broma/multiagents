"""Adversary 3 — IC-C9 cache entries that verify everywhere except in content.

A cache entry whose header names exactly the pin, whose pack checksum is
valid and which passes `index-pack --strict` / `fsck`, but which carries a
sibling attempt's unmerged objects as well, is not "exactly the objects
reachable from the authorised roots". It must never be handed out. (The
existing corruption tests only damage bytes; the reachable-set comparison
in the verifier survives being replaced by `return True`.)
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import ic_prov as ip  # noqa: E402


@pytest.fixture
def host(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    repo = ip.HostRepo(tmp_path / "host")
    ids = ip.standard_world(repo)
    return repo, ids


def test_ic_adv3_a_well_formed_entry_with_a_siblings_objects_is_never_handed_out(host, tmp_path):
    repo, ids = host
    reg = ip.seed_registry(repo.path)
    ref = reg.build("att-a", ids["input"])
    path = Path(reg.resolve(ref, "att-a"))
    header = path.read_bytes().split(b"\n\n", 1)[0] + b"\n\n"
    # A complete, checksummed pack of the input's closure plus the sibling's.
    revs = f"{ids['input']}\n{ids['sibling']}\n".encode()
    pack = subprocess.run(["git", "-C", str(repo.path), "pack-objects", "--stdout", "--revs"],
                          input=revs, capture_output=True, check=True).stdout
    path.write_bytes(header + pack)
    assert ids["sibling"] in ip.bundle_objects(path, tmp_path / "planted")

    handed = Path(reg.resolve(ref, "att-a"))
    objects = ip.bundle_objects(handed, tmp_path / "handed")
    assert ids["sibling"] not in objects and ids["secret_blob"] not in objects
    assert objects == repo.reachable(ids["input"])
