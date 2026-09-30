"""Q2: subcommands decide whether a project exists from --path, not the cwd."""
import argparse

import multiagents.cli as cli


def test_resolve_if_project_honours_path_from_outside(tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    (proj / ".multiagents").mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    paths = cli._resolve_if_project(str(proj))
    assert paths is not None and paths.root == proj.resolve()


def test_resolve_if_project_none_without_project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert cli._resolve_if_project(None) is None
    assert cli._resolve_if_project(str(tmp_path)) is None


def test_resolve_if_project_uses_cwd_without_path(tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    (proj / ".multiagents").mkdir(parents=True)
    monkeypatch.chdir(proj)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert cli._resolve_if_project(None).root == proj.resolve()
