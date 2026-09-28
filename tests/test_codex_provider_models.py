"""Codex provider contract: `models` (CX-C12) and the shipped egress (CX-C14)."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import codex_harness as h                              # noqa: E402

CACHE = {"models": [
    {"slug": "gpt-6-sol", "display_name": "GPT-6 Sol", "visibility": "list"},
    {"slug": "gpt-6-hidden", "display_name": "Hidden", "visibility": "hide"},
    {"slug": "gpt-5.5", "display_name": "GPT\t5.5\nlegacy", "visibility": "list"},
    {"slug": "gpt-internal", "display_name": "Internal", "visibility": "none"},
]}


@pytest.fixture
def fake(tmp_path):
    return h.FakeCodex(tmp_path)


def write_cache(home: Path, content) -> None:
    home.mkdir(parents=True, exist_ok=True)
    data = content if isinstance(content, bytes) else json.dumps(content).encode()
    (home / "models_cache.json").write_bytes(data)


# ----------------------------------------------------------------- CX-C12 --

def test_cx_c12_lists_visible_models_as_tsv(tmp_path, fake):
    write_cache(tmp_path / "profile", CACHE)
    result = h.invoke(["models"], h.base_env(tmp_path, fake))
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert all(line.count("\t") == 1 for line in lines), lines
    assert [line.split("\t")[0] for line in lines] == ["gpt-6-sol", "gpt-5.5"]
    assert lines[0] == "gpt-6-sol\tGPT-6 Sol"
    ids = [m["id"] for m in h.provider().parse_models(result.stdout)]
    assert ids == ["gpt-6-sol", "gpt-5.5"]


def test_cx_c12_reads_the_profile_not_an_ambient_codex_home(tmp_path, fake):
    write_cache(tmp_path / "profile", {"models": [
        {"slug": "right", "display_name": "Right", "visibility": "list"}]})
    write_cache(tmp_path / "ambient", {"models": [
        {"slug": "wrong", "display_name": "Wrong", "visibility": "list"}]})
    write_cache(tmp_path / "home" / ".codex", {"models": [
        {"slug": "users-own", "display_name": "Own", "visibility": "list"}]})
    env = h.base_env(tmp_path, fake, CODEX_HOME=str(tmp_path / "ambient"))
    result = h.invoke(["models"], env)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "right\tRight\n"


def test_cx_c12_default_profile_when_no_override(tmp_path, fake):
    write_cache(tmp_path / "home" / ".multiagents" / "profiles" / "codex", {"models": [
        {"slug": "m1", "display_name": "M1", "visibility": "list"}]})
    env = h.base_env(tmp_path, fake, MULTIAGENTS_CODEX_PROFILE=None)
    result = h.invoke(["models"], env)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "m1\tM1\n"


@pytest.mark.parametrize("content", [
    None, b"{not json", b"\xff\xfe\x00", b"[1, 2]", b'{"models": "many"}', "directory"],
    ids=["missing", "invalid-json", "not-utf8", "not-an-object", "models-not-a-list",
         "a-directory"])
def test_cx_c12_unreadable_cache_is_one_line_and_no_traceback(tmp_path, fake, content):
    profile = tmp_path / "profile"
    profile.mkdir()
    if content == "directory":
        (profile / "models_cache.json").mkdir()
    elif content is not None:
        (profile / "models_cache.json").write_bytes(content)
    result = h.invoke(["models"], h.base_env(tmp_path, fake))
    assert result.returncode != 0
    assert "Traceback" not in result.stderr
    assert len(result.stderr.strip().splitlines()) == 1, result.stderr
    assert result.stdout.strip() == ""


def test_cx_c12_models_never_calls_the_native_cli(tmp_path, fake):
    write_cache(tmp_path / "profile", CACHE)
    h.invoke(["models"], h.base_env(tmp_path, fake))
    assert fake.calls() == []


# ----------------------------------------------------------------- CX-C14 --

def _allowlist() -> list[str]:
    data = yaml.safe_load(h.PROJECT_YAML.read_text())
    return list(data["executor"]["docker"]["egress_allowlist"])


def _group_one() -> list[str]:
    """Entries between the "# 1." and "# 2." comments of egress_allowlist."""
    text = h.PROJECT_YAML.read_text()
    start = re.search(r"^\s*# 1\. ", text, re.M)
    end = re.search(r"^\s*# 2\. ", text, re.M)
    assert start and end and start.start() < end.start()
    return re.findall(r"^\s*-\s*([^\s#]+)", text[start.start():end.start()], re.M)


def test_cx_c14_openai_and_chatgpt_are_model_endpoints():
    group = _group_one()
    assert "openai.com" in group
    assert "chatgpt.com" in group


def test_cx_c14_each_new_host_carries_a_comment():
    text = h.PROJECT_YAML.read_text()
    for host in ("openai.com", "chatgpt.com"):
        lines = text.splitlines()
        found = [i for i, line in enumerate(lines)
                 if re.match(rf"^\s*-\s*{re.escape(host)}\b", line)]
        assert found, f"{host} is not in {h.PROJECT_YAML.name}"
        index = found[0]
        inline = "#" in lines[index]
        above = lines[index - 1].strip().startswith("#")
        assert inline or above, f"{host} has no comment"


def test_cx_c14_nothing_else_openai_and_no_wildcard_or_bare_suffix():
    entries = _allowlist()
    assert entries.count("openai.com") == 1
    assert entries.count("chatgpt.com") == 1
    related = [e for e in entries if re.search(r"openai|chatgpt|oaistatic|oaiusercontent", e)]
    assert sorted(related) == ["chatgpt.com", "openai.com"]
    for entry in entries:
        assert "*" not in entry, entry
        assert not entry.startswith("."), entry
        assert "." in entry, f"bare suffix {entry!r}"
