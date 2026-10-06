"""GG-R1 (config) and GG-R6 (the shipped `git` library agent).

Defaults of the guard settings are asserted by behaviour in the scan, hook and
brief tests (a default is what the system does when the key is absent); this
file covers what load refuses and accepts, and the library agent.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))
import gg_world as gw  # noqa: E402

from multiagents import config as config_mod  # noqa: E402
from multiagents.paths import ProjectPaths, shipped_defaults_dir  # noqa: E402


def _load(tmp_path, monkeypatch, git_section):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("MULTIAGENTS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("MULTIAGENTS_STATE_DIR", str(tmp_path / "state"))
    root = tmp_path / "proj"
    (root / ".multiagents" / "config").mkdir(parents=True)
    (root / ".multiagents" / "config" / "project.yaml").write_text(
        yaml.safe_dump({"git": git_section}))
    return config_mod.load(ProjectPaths(root), seed=False)


# ------------------------------------------------------------------ GG-R1 ---
BAD = [
    ("coauthor_orchestrator", "yes"),
    ("coauthor_orchestrator", "true"),
    ("coauthor_orchestrator", 1),
    ("coauthor_orchestrator", 0),
    ("coauthor_orchestrator", ["true"]),
]


@pytest.mark.parametrize("key,value", BAD)
def test_gg_r1_coauthor_orchestrator_must_be_a_boolean(tmp_path, monkeypatch, key, value):
    with pytest.raises(ValueError) as exc:
        _load(tmp_path, monkeypatch, {key: value})
    assert key in str(exc.value)


@pytest.mark.parametrize("value", [5, True, ["a"], {"a": 1}])
def test_gg_r1_patterns_file_must_be_a_string(tmp_path, monkeypatch, value):
    with pytest.raises(ValueError) as exc:
        _load(tmp_path, monkeypatch, {"guard": {"patterns_file": value}})
    assert "patterns_file" in str(exc.value)


@pytest.mark.parametrize("key", ["allowed_emails", "allow"])
@pytest.mark.parametrize("value", ["a-string", 5, {"a": "b"}, [1], [None], ["ok", 2], [["nested"]]])
def test_gg_r1_guard_lists_must_be_lists_of_strings(tmp_path, monkeypatch, key, value):
    with pytest.raises(ValueError) as exc:
        _load(tmp_path, monkeypatch, {"guard": {key: value}})
    assert key in str(exc.value)


@pytest.mark.parametrize("value", ["on", 3, ["x"]])
def test_gg_r1_guard_must_be_a_mapping(tmp_path, monkeypatch, value):
    with pytest.raises(ValueError) as exc:
        _load(tmp_path, monkeypatch, {"guard": value})
    assert "guard" in str(exc.value)


@pytest.mark.parametrize("section", [
    {},
    {"coauthor_orchestrator": True},
    {"coauthor_orchestrator": False},
    {"guard": {}},
    {"guard": {"patterns_file": "~/somewhere/patterns", "allowed_emails": [],
               "allow": []}},
    {"guard": {"allowed_emails": ["a-placeholder@example.com"], "allow": ["x", "y"]}},
])
def test_gg_r1_valid_settings_load(tmp_path, monkeypatch, section):
    cfg = _load(tmp_path, monkeypatch, section)
    for key, value in section.items():
        assert cfg.project["git"][key] == value


# ------------------------------------------------------------------ GG-R6 ---
LIB = shipped_defaults_dir() / "agents" / "library"
BRIEF = LIB / "git.md"


def _blocks():
    text = (LIB / "README.md").read_text()
    merged = {}
    for block in re.findall(r"```yaml\n(.*?)```", text, re.S):
        import textwrap
        merged.update(yaml.safe_load(textwrap.dedent(block)))
    return merged


def test_gg_r6_the_library_lists_git_with_a_pasteable_block():
    blocks = _blocks()
    assert "git" in blocks, sorted(blocks)
    spec = blocks["git"]
    assert spec["instructions"] == "library/git.md"
    assert spec["can_spawn"] is False
    assert spec.get("models"), "no cross-provider fallback"
    assert spec["provider"] not in spec["models"]


def test_gg_r6_git_is_in_the_library_not_in_the_default_team():
    roster = yaml.safe_load((shipped_defaults_dir() / "agents.yaml").read_text())["agents"]
    assert "git" not in roster
    assert BRIEF.is_file()


def test_gg_r6_the_brief_names_no_address_host_key_or_account():
    text = BRIEF.read_text()
    allowed_domains = {"example.com", "example.org", "example.net", "example.invalid",
                       "multiagents.local", "multiagents.invalid"}
    for found in re.findall(r"[\w.+-]+@([\w.-]+\.\w+)", text):
        domain = found.lower()
        assert (domain in allowed_domains or "noreply" in domain), found
    for ip in re.findall(r"\b(\d{1,3})\.(\d{1,3})\.\d{1,3}\.\d{1,3}\b", text):
        a, b = int(ip[0]), int(ip[1])
        assert not (a == 100 and 64 <= b <= 127), ip
    for host in re.findall(r"[\w<>.-]*\.ts\.net", text):
        assert "<" in host, host
    assert "PRIVATE KEY-----" not in text
    for kind, token in gw.tokens().items():
        assert token not in text, kind
    assert "/home/" not in text and "/Users/" not in text


def test_gg_r6_the_brief_states_the_four_duties():
    flat = " ".join(BRIEF.read_text().split())
    low = flat.lower()
    assert "git-guard" in low or "git guard" in low          # audit procedure
    assert "audit" in low
    assert "Co-Authored-By" in flat                            # the co-author rule check
    assert "coauthor_orchestrator" in flat
    assert re.search(r"never[^.]*\bpush", low)                 # never pushing
    assert re.search(r"never[^.]*\b(move|moving|mov|update|reset|rewrite)[^.]*\b(ref|branch)", low)
    assert re.search(r"\bmask", low)                           # masked reporting
