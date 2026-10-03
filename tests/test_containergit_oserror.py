"""Container git reports a missing docker client as an ordinary git failure."""

from multiagents import gitops
from multiagents.executor.docker import ContainerGit, DockerExecutor
from multiagents.paths import ProjectPaths


def test_missing_docker_returns_failed_git_result(tmp_path, monkeypatch):
    paths = ProjectPaths(tmp_path)
    paths.ensure()
    git = ContainerGit(DockerExecutor({}, paths=paths), "ag-missing-docker")
    empty_path = tmp_path / "empty-bin"
    empty_path.mkdir()
    monkeypatch.setenv("PATH", str(empty_path))

    result = git.run(tmp_path, "status", "--porcelain")

    assert isinstance(result, gitops.GitResult)
    assert not result.ok
    assert result.out == ""
    assert result.code != 0
    assert "could not launch docker client" in result.err
    assert "docker" in result.err
    assert "No such file or directory" in result.err
