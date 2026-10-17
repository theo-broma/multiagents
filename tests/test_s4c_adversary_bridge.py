"""Adversary, slice 4C (IC-C33): the host auth bridge's socket directory.

`claude_credentials.bridge_directory(root)` is a predictable name directly
under the world-writable `/var/tmp` (`ma-auth-<uid>-<sha256(root)[:24]>`).
`ensure_bridge` creates it with `mkdir(exist_ok=True)` and `chmod`, both of
which follow a symlink. Whoever plants that name first decides where the
socket the Claude gateway is mounted with lives, and so who answers the
gateway's requests (prompts, code) and its `/health` probe.
"""
import os
import sys
from pathlib import Path

import pytest

from multiagents.scheduler import claude_credentials as cc


@pytest.fixture
def planted(tmp_path):
    root = tmp_path / "gateway-root"
    root.mkdir()
    link = cc.bridge_directory(root)
    elsewhere = tmp_path / "planted-by-someone-else"
    elsewhere.mkdir()
    assert not os.path.lexists(link)
    link.symlink_to(elsewhere)
    try:
        yield root, link, elsewhere
    finally:
        link.unlink(missing_ok=True)


def test_adv_s4c_the_bridge_never_uses_a_planted_symlink_as_its_socket_directory(planted, monkeypatch):
    root, link, elsewhere = planted
    started = []
    monkeypatch.setattr(cc.subprocess, "Popen", lambda *a, **k: started.append(a))
    monkeypatch.setattr(cc, "_health", lambda path, digest: False)
    monkeypatch.setattr(cc.time, "sleep", lambda s: None)
    with pytest.raises(Exception):
        cc.ensure_bridge(root, {}, "digest")
    assert not (elsewhere / "socket").exists(), \
        "the bridge built its socket directory through a symlink it did not create"
    assert not (elsewhere / "launch.lock").exists(), "the bridge took its launch lock through a planted symlink"
    assert started == [], "the bridge service was started on a socket path through a planted symlink"
