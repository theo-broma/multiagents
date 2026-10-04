"""Adversary (ag-89bcfd): QH-R8 copy-mode session store escapes."""
from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
import uuid

import pytest

from multiagents import config, quota_handover as qh
from multiagents.providers import load_providers


def _codex_pair(tmp_path):
    raw = config.load(None, seed=False).providers["codex"]
    profiles = {name: tmp_path / name for name in ("codex", "codex-b")}
    providers = load_providers({
        "codex": {**raw, "env": {"MULTIAGENTS_CODEX_PROFILE": str(profiles["codex"])}},
        "codex-b": {"extends": "codex", "env": {"MULTIAGENTS_CODEX_PROFILE": str(profiles["codex-b"])}}})
    executor = SimpleNamespace(kind="local")
    return providers, profiles, executor


def test_qh_r8_target_store_symlinked_directory_does_not_redirect_install(tmp_path):
    """A symlinked directory inside the TARGET store sends the install outside it.

    Only the final path component is checked for a symlink; the target path is
    built from the source's relative path and its parents are followed.
    """
    providers, profiles, executor = _codex_pair(tmp_path)
    sid = str(uuid.uuid4())
    relative = Path("sessions/2026/10/04") / f"rollout-2026-10-04T01-00-00-{sid}.jsonl"
    source = profiles["codex"] / relative
    source.parent.mkdir(parents=True)
    source.write_text(json.dumps({"type": "session_meta", "payload": {"id": sid}}) + "\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    (profiles["codex-b"] / "sessions").mkdir(parents=True)
    os.symlink(outside, profiles["codex-b"] / "sessions" / "2026")
    try:
        a, b = qh.transfer_paths(providers["codex"], providers["codex-b"], tmp_path, sid,
                                 executor, executor)
        qh.transfer_session(a, b)
    except ValueError:
        pass
    written = [p for p in outside.rglob("*") if p.is_file()]
    assert written == [], f"session installed outside the target store: {written}"


def test_qh_r8_hardlinked_credential_is_not_transferred_as_a_rollout(tmp_path):
    """A rollout name hard-linked to the source profile's auth.json is copied.

    The symlink check does not cover hard links, so the account credentials
    land in the sibling account's store.
    """
    providers, profiles, executor = _codex_pair(tmp_path)
    sid = str(uuid.uuid4())
    relative = Path("sessions/2026/10/04") / f"rollout-2026-10-04T01-00-00-{sid}.jsonl"
    source = profiles["codex"] / relative
    source.parent.mkdir(parents=True)
    auth = profiles["codex"] / "auth.json"
    auth.write_text("CREDENTIAL-MARKER-NOT-A-SECRET")
    os.link(auth, source)
    (profiles["codex-b"] / "sessions").mkdir(parents=True)
    try:
        a, b = qh.transfer_paths(providers["codex"], providers["codex-b"], tmp_path, sid,
                                 executor, executor)
        qh.transfer_session(a, b)
    except ValueError:
        return
    leaked = [p for p in profiles["codex-b"].rglob("*")
              if p.is_file() and "CREDENTIAL-MARKER" in p.read_text()]
    assert leaked == [], f"credential file copied into the sibling store: {leaked}"
