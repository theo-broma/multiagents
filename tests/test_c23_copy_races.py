"""QH-R8: descriptor ownership across source and staging-name swaps."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_c23_quota_handover import World
from multiagents import quota_handover as qh


def copy_world(tmp_path, monkeypatch):
    world = World(tmp_path, monkeypatch, mode="copy",
                  models={"beta": "model-a", "other": "model-o"})
    for name in ("alpha", "beta"):
        world.providers[name]["session_store"] = {
            "profile_default": str(tmp_path), "dir": world.stores[name].name,
            "glob": "{session_id}.jsonl"}
    world.reload()
    return world


@pytest.mark.parametrize("seam", ["before_transfer", "source_open"])
def test_qh_r8_source_credential_rename_cannot_be_copied(tmp_path, monkeypatch, seam):
    w = copy_world(tmp_path, monkeypatch)
    source = w.stores["alpha"] / (w.session_id + ".jsonl")
    auth = tmp_path / "auth.json"
    marker = "SOURCE-CREDENTIAL-MARKER-NOT-A-SECRET"
    auth.write_text(marker)
    swaps = []
    source_opens = []
    original_open = qh.os.open

    def swap():
        assert source.exists()
        auth.replace(source)
        assert source.stat().st_nlink == 1
        swaps.append(seam)

    def open_source(path, flags, *args, **kwargs):
        directory = kwargs.get("dir_fd")
        if (path == source.name and directory is not None
                and Path(f"/proc/self/fd/{directory}").resolve() == source.parent):
            assert flags & os.O_NOFOLLOW
            source_opens.append(path)
            if seam == "source_open":
                # Credentials have been pinned, but the rollout has not been
                # opened. Its replacement still has exactly one link.
                swap()
        return original_open(path, flags, *args, **kwargs)

    async def swap_source(attempt):
        if attempt["to"] == "beta" and seam == "before_transfer":
            swap()

    monkeypatch.setattr(qh.os, "open", open_source)
    monkeypatch.setattr(qh, "before_transfer", swap_source)
    asyncio.run(w.start())
    assert swaps
    assert source_opens == [source.name]
    assert [call["instance"] for call in w.calls()] == ["alpha", "other"]
    assert w.events("handover_failed")
    assert all(marker not in path.read_text() for path in w.stores["beta"].rglob("*") if path.is_file())
    assert source.read_text() == marker


def test_qh_r8_staging_credential_swap_fails_verification_and_tries_next_tier(tmp_path, monkeypatch):
    w = copy_world(tmp_path, monkeypatch)
    auth = tmp_path / "auth.json"
    marker = "TARGET-CREDENTIAL-MARKER-NOT-A-SECRET"
    auth.write_text(marker)
    swaps = []

    def swap_staging(directory, staged_name, target_name):
        os.unlink(staged_name, dir_fd=directory)
        os.link(auth, staged_name, dst_dir_fd=directory)
        swaps.append(target_name)

    monkeypatch.setattr(qh, "before_install", swap_staging)
    asyncio.run(w.start())
    assert swaps == [w.session_id + ".jsonl"]
    assert [call["instance"] for call in w.calls()] == ["alpha", "other"]
    assert w.events("handover_failed")
    assert all(marker not in path.read_text() for path in w.stores["beta"].rglob("*") if path.is_file())
    assert not list(w.stores["beta"].glob(".handover-*"))
    assert auth.read_text() == marker
    assert auth.stat().st_nlink == 1
