"""Provider CLIs remain discoverable under a minimal SSH PATH."""

import json
from types import SimpleNamespace

from multiagents import scripts
from multiagents.providers import Provider


MINIMAL_PATH = (
    "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:"
    "/usr/games:/usr/local/games:/snap/bin"
)


def executable(path, body="exit 0"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n" + body + "\n")
    path.chmod(0o755)
    return path


def local_provider(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PATH", MINIMAL_PATH)
    return Provider.from_dict("agy", {"bin": "agy", "script": "agy.sh"})


def test_resolve_bin_falls_back_to_local_bin(tmp_path, monkeypatch):
    provider = local_provider(tmp_path, monkeypatch)
    binary = executable(tmp_path / ".local/bin/agy")
    resolved = provider.resolve_bin()
    assert resolved.launcher == binary
    assert resolved.path == binary
    assert provider.available() == str(binary)


def test_agy_budget_uses_resolved_local_binary(tmp_path, monkeypatch):
    provider = local_provider(tmp_path, monkeypatch)
    payload = {"command": {"data": {"groups": [{
        "name": "Gemini", "description": "Models: Gemini Flash",
        "buckets": [{"id": "gemini-weekly", "remaining_fraction": 0.8}],
    }]}}}
    executable(tmp_path / ".local/bin/agy", "printf '%s\\n' '" + json.dumps(payload) + "'")
    code, out, err = scripts.run_action(
        "agy", provider, SimpleNamespace(kind="local"), "budget", tmp_path,
    )
    assert code == 0, err
    assert "binary 'agy' not found" not in err
    data = json.loads(out)
    assert data["known"] is True
    assert data["headroom"] == 0.8


def test_path_precedes_local_bin(tmp_path, monkeypatch):
    provider = local_provider(tmp_path, monkeypatch)
    executable(tmp_path / ".local/bin/agy")
    binary = executable(tmp_path / "path/agy")
    monkeypatch.setenv("PATH", str(binary.parent) + ":" + MINIMAL_PATH)
    resolved = provider.resolve_bin()
    assert resolved.launcher == binary
    assert resolved.via == "PATH"


def test_provider_search_precedes_local_bin(tmp_path, monkeypatch):
    provider = local_provider(tmp_path, monkeypatch)
    executable(tmp_path / ".local/bin/agy")
    binary = executable(tmp_path / "custom/agy")
    provider.bin_search = [str(binary.parent)]
    assert provider.resolve_bin().launcher == binary


def test_missing_binary_lists_local_bin(tmp_path, monkeypatch):
    provider = local_provider(tmp_path, monkeypatch)
    provider.bin = "missing-provider-cli"
    resolved = provider.resolve_bin()
    assert resolved.path is None
    error = provider.bin_error(resolved)
    assert "binary 'missing-provider-cli' not found; searched:" in error
    assert str(tmp_path / ".local/bin/missing-provider-cli") in error
