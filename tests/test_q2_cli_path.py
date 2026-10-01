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


# Q2b round 3: `init` takes its directory from the global --path or its own
# positional (the directory need not be a project yet); the two must agree.
# `docker status --all` validates an explicit --path before listing anything.

def _listing(d):
    return sorted(p.name for p in d.iterdir())


def test_q2b_init_with_empty_path_exits_and_creates_nothing_in_cwd(tmp_path, monkeypatch):
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    with pytest.raises(SystemExit) as exc:
        cli.main(["--path", "", "init"])
    assert exc.value.code == 2
    assert _listing(cwd) == []


def test_q2b_init_global_path_without_positional_initialises_that_path(tmp_path, monkeypatch):
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    target = tmp_path / "target"
    target.mkdir()
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    cli.main(["--path", str(target), "init"])  # the exit code reflects git/auth setup, not the path
    assert (target / ".multiagents").is_dir()
    assert _listing(cwd) == []


def test_q2b_init_global_path_that_does_not_exist_yet_is_created(tmp_path, monkeypatch):
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    target = tmp_path / "fresh"
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    cli.main(["--path", str(target), "init"])  # the exit code reflects git/auth setup, not the path
    assert (target / ".multiagents").is_dir()
    assert _listing(cwd) == []


def test_q2b_init_positional_path_still_initialises_it(tmp_path, monkeypatch):
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    target = tmp_path / "target"
    target.mkdir()
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    cli.main(["init", str(target)])  # the exit code reflects git/auth setup, not the path
    assert (target / ".multiagents").is_dir()
    assert _listing(cwd) == []


def test_q2b_init_same_path_given_both_ways_is_accepted(tmp_path, monkeypatch):
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    target = tmp_path / "target"
    target.mkdir()
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    cli.main(["--path", str(target), "init", str(target)])  # the exit code reflects git/auth setup, not the path
    assert (target / ".multiagents").is_dir()


def test_q2b_init_conflicting_global_and_positional_paths_exit_naming_both(
        tmp_path, monkeypatch, capsys):
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    with pytest.raises(SystemExit) as exc:
        cli.main(["--path", str(a), "init", str(b)])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert str(a) in err and str(b) in err
    assert _listing(a) == [] and _listing(b) == [] and _listing(cwd) == []


@pytest.mark.parametrize("kind", ["empty", "missing"])
def test_q2b_docker_status_all_rejects_invalid_explicit_path_before_listing(
        kind, tmp_path, monkeypatch, capsys):
    # Must exit before docker is touched: a docker probe or listing here is
    # a failure of the test, not something to be answered.
    import multiagents.executor.docker as dk

    def _no_docker(*a, **k):
        raise AssertionError("docker was touched before the --path was validated")

    monkeypatch.setattr(dk, "docker_state", _no_docker)
    monkeypatch.setattr(dk, "list_containers", _no_docker)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    bad = "" if kind == "empty" else str(tmp_path / "missing")
    with pytest.raises(SystemExit) as exc:
        cli.main(["--path", bad, "docker", "status", "--all"])
    assert exc.value.code == 2
    if bad:
        assert bad in capsys.readouterr().err
    assert not (tmp_path / "missing").exists()
