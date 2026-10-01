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


# Q2b follow-up: an explicit --path inside a project means that project; a
# --path that does not exist is a typo and is never created.

def test_q2b_explicit_subdirectory_path_resolves_to_ancestor_project(tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    (proj / ".multiagents").mkdir(parents=True)
    (proj / "src").mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    paths = cli._resolve_if_project(str(proj / "src"))
    assert paths is not None and paths.root == proj.resolve()


def test_q2b_subcommand_with_subdirectory_path_writes_nothing_below_it(tmp_path, monkeypatch):
    # `upgrade-config --layer project` is the cheapest offline subcommand that
    # writes into the resolved project; refresh-models would probe real CLIs.
    proj = tmp_path / "proj"
    (proj / ".multiagents").mkdir(parents=True)
    (proj / "src").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    cli.main(["--path", str(proj / "src"), "upgrade-config", "--layer", "project"])
    assert not (proj / "src" / ".multiagents").exists()


def test_q2b_explicit_missing_path_inside_project_exits_and_is_not_created(
        tmp_path, monkeypatch, capsys):
    proj = tmp_path / "proj"
    (proj / ".multiagents").mkdir(parents=True)
    typo = proj / "typo"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    with pytest.raises(SystemExit) as exc:
        cli._resolve_if_project(str(typo))
    assert exc.value.code == 2
    assert str(typo) in capsys.readouterr().err
    assert not typo.exists()


def test_q2b_subcommand_with_missing_path_inside_project_does_not_create_it(
        tmp_path, monkeypatch, capsys):
    proj = tmp_path / "proj"
    (proj / ".multiagents").mkdir(parents=True)
    typo = proj / "typo"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    with pytest.raises(SystemExit) as exc:
        cli.main(["--path", str(typo), "upgrade-config", "--layer", "project"])
    assert exc.value.code == 2
    assert str(typo) in capsys.readouterr().err
    assert not typo.exists()


# Q2b round 2: every command resolves an explicit --path the same way, through
# `_resolve` itself: missing -> exit 2, subdirectory -> ancestor project,
# given-but-empty -> exit 2, not given -> unchanged.

def test_q2b_resolve_subdirectory_path_resolves_to_ancestor_project(tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    (proj / ".multiagents").mkdir(parents=True)
    (proj / "src").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert cli._resolve(str(proj / "src")).root == proj.resolve()


def test_q2b_tree_with_missing_path_exits_and_does_not_create_it(tmp_path, monkeypatch, capsys):
    proj = tmp_path / "proj"
    (proj / ".multiagents").mkdir(parents=True)
    typo = proj / "typo"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    with pytest.raises(SystemExit) as exc:
        cli.main(["--path", str(typo), "tree"])
    assert exc.value.code == 2
    assert str(typo) in capsys.readouterr().err
    assert not typo.exists()


def test_q2b_tree_with_subdirectory_path_reads_the_project_tree(tmp_path, monkeypatch, capsys):
    proj = tmp_path / "proj"
    (proj / ".multiagents").mkdir(parents=True)
    (proj / "src").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert cli.main(["--path", str(proj), "tree"]) == 0
    expected = capsys.readouterr().out
    assert cli.main(["--path", str(proj / "src"), "tree"]) == 0
    assert capsys.readouterr().out == expected
    assert not (proj / "src" / ".multiagents").exists()


def test_q2b_empty_path_exits_outside_any_project(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    with pytest.raises(SystemExit) as exc:
        cli.main(["--path", "", "upgrade-config", "--dry-run", "--layer", "project"])
    assert exc.value.code == 2
