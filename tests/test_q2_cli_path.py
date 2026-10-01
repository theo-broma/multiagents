"""Q2: subcommands decide whether a project exists from --path, not the cwd."""
import pytest

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


def test_resolve_if_project_uses_cwd_without_path(tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    (proj / ".multiagents").mkdir(parents=True)
    monkeypatch.chdir(proj)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert cli._resolve_if_project(None).root == proj.resolve()


# Q2b: an EXPLICIT --path that is missing or not a project fails loudly, in
# the way cli.py reports every other user error (`_resolve`): a message naming
# the path on stderr, then SystemExit(2). It never falls back to no-project.

def test_q2b_resolve_if_project_explicit_non_project_dir_exits(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(SystemExit) as exc:
        cli._resolve_if_project(str(plain))
    assert exc.value.code == 2
    assert str(plain) in capsys.readouterr().err


def test_q2b_resolve_if_project_explicit_missing_path_exits(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    missing = tmp_path / "does-not-exist"
    with pytest.raises(SystemExit) as exc:
        cli._resolve_if_project(str(missing))
    assert exc.value.code == 2
    assert str(missing) in capsys.readouterr().err


def test_q2b_explicit_bad_path_fails_even_when_cwd_is_a_project(tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    (proj / ".multiagents").mkdir(parents=True)
    plain = tmp_path / "plain"
    plain.mkdir()
    monkeypatch.chdir(proj)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    with pytest.raises(SystemExit) as exc:
        cli._resolve_if_project(str(plain))
    assert exc.value.code == 2


@pytest.mark.parametrize("kind", ["missing", "non_project"])
def test_q2b_skills_subcommand_rejects_bad_path(kind, tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    bad = tmp_path / "bad"
    if kind == "non_project":
        bad.mkdir()
    with pytest.raises(SystemExit) as exc:
        cli.main(["--path", str(bad), "skills"])
    assert exc.value.code not in (0, None)
    assert str(bad) in capsys.readouterr().err


def test_q2b_skills_subcommand_without_path_outside_project_still_works(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert cli.main(["skills"]) == 0
