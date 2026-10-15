"""K17.2 provider declarations: validate via the public config loader."""
from pathlib import Path
import sys
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))
import c3_harness as h
import sc_harness as sc
from multiagents import config
from multiagents.paths import ProjectPaths


def load(tmp_path, monkeypatch, needs, *, inherited=False):
    h.as_root(monkeypatch)
    p = sc.Project(tmp_path)
    fake = p.add_provider("acme")
    fake.entry["needs"] = needs
    if inherited:
        p.providers["child"] = {"extends": "acme", "needs": {"network": False}}
    p.add_agent("worker", "acme", "acme/m1")
    p.write()
    return config.load(ProjectPaths(p.root), seed=False)


@pytest.mark.parametrize("bad", [None, [], "false", False, {"unknown": False},
                                  {"credentials": None}, {"network": 0}, {"rpc": "false"}])
def test_k17_2_invalid_needs_names_provider_and_field(tmp_path, monkeypatch, bad):
    with pytest.raises(ValueError) as caught:
        load(tmp_path, monkeypatch, bad)
    assert "acme" in str(caught.value) and "needs" in str(caught.value)


@pytest.mark.parametrize("needs", [{}, {"rpc": False},
                                     {"credentials": False, "network": False, "rpc": False}])
def test_k17_2_valid_boolean_declarations_load(tmp_path, monkeypatch, needs):
    assert load(tmp_path, monkeypatch, needs)


def test_k17_2_extends_allows_partial_boolean_override(tmp_path, monkeypatch):
    assert load(tmp_path, monkeypatch, {"credentials": False, "rpc": False}, inherited=True)
